import csv
import json
import os
import glob
import time
import threading
import sys
from collections import defaultdict, Counter
from datetime import datetime

class InventoryManager:
    def __init__(self, config_manager):
        self.config = config_manager
        self.stock_data = []         # List of (code, desc, qty) - FILTRADO
        self.all_stock_data = []     # List of (code, desc, qty, fam) - COMPLETO (para refresco)
        self.scanned_items = defaultdict(list) # code -> [positions]
        self.scan_sequence = []      # List of strings in scan order (includes products and QRs @, %, #)
        self.qr_replace_index = None # Index of QR code awaiting replacement
        self.family_map = {}         # code -> family (AM, AO, AG)
        self.full_family_map = {}    # All codes in CSV
        self.original_quantities = {} # code -> qty
        self.scan_counter = 1
        self.data_changed = False
        self.current_csv_path = None
        self.family_type = None      # AM_AO, AG, or COMBINED
        self.current_base_name = None # Nombre base para el par (CSV + JSON)
        self.last_saved_path = None
        self.is_historical = False
        
        # 1. Rutas locales estrictas para V8
        app_root = os.path.dirname(os.path.abspath(sys.argv[0]))
        self.scan_dir = os.path.join(app_root, "Escaneos")
        if not os.path.exists(self.scan_dir):
            os.makedirs(self.scan_dir)

        self.main_stock_file = os.path.join(self.scan_dir, "main_stock.json")
        self.main_stock = self.load_main_stock()

        self.last_action_time = time.time()
        self.historical_sequences = {} # Dict of lists: list of scanned items in order for each JSON
        self.pending_verifications = {} # code -> { 'pos': N, 'history': [] }
        
        self.lock = threading.Lock()

        # --- Caché de ubicaciones (auditoría rendimiento 2026-08-29) ---
        # Evita re-recorrer el historial completo (O(archivos x posiciones)) por
        # cada búsqueda repetida del mismo SKU. Dos niveles:
        #   - _location_cache: resultado FINAL de get_expected_container_for_sku
        #     (main_stock primero, historial como fallback).
        #   - _history_location_cache: resultado SOLO-historail (usado por
        #     StockApp.get_possible_location y como fallback del anterior).
        # Invalidación: se limpian al mutar main_stock (update_product_location)
        # y al reindexar el historial (_index_history). Lock dedicado para no
        # anidar con inventory.lock (check_proximity ya toma ese lock).
        self._location_cache = {}
        self._history_location_cache = {}
        self._location_cache_lock = threading.Lock()
        self._location_version = 0  # se incrementa en cada invalidación

        # --- Caché de estado de contenedores (auditoría 2026-08-31) ---
        # get_container_status() recorría TODA la secuencia por cada QR
        # (patrón O(QRs × n) ≈ O(n²) detectado en _scanned_view con 1600 ítems).
        # Ahora se precomputa container -> scanned_count UNA vez por versión
        # de scan_sequence; las consultas posteriores son O(1).
        # Invalidación: _invalidate_container_status_cache() en cada mutación
        # de scan_sequence (add/delete/move/QR replace/load).
        self._container_counts = {}          # container -> scanned_count
        self._container_counts_version = 0   # versión de scan_sequence computada
        self._container_counts_computed_version = -1  # última versión precomputada
        self._container_status_lock = threading.Lock()
        
        # Iniciar indexación en segundo plano
        threading.Thread(target=self._index_history, daemon=True).start()

    def is_qr_code(self, code: str) -> bool:
        """Determina si un código corresponde a un contenedor QR (@, %, #)."""
        if not code or not isinstance(code, str):
            return False
        return code.startswith(('@', '%', '#'))

    def get_code_type(self, code: str) -> str:
        """Devuelve el tipo semántico del código."""
        if not code: return 'unknown'
        if code.startswith('@'):
            return 'caja'
        elif code.startswith('%'):
            return 'mueble'
        elif code.startswith('#'):
            return 'vidriera'
        return 'product'

    def load_main_stock(self) -> dict:
        """Carga la referencia maestra de ubicaciones desde main_stock.json."""
        default_stock = {
            "version": "8.0",
            "updated_at": datetime.now().isoformat(),
            "containers": {},        # container_code -> {"type": str, "parent": str, "expected_skus": {sku: qty}}
            "product_locations": {}   # sku -> container_code
        }
        if os.path.exists(self.main_stock_file):
            try:
                with open(self.main_stock_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return {**default_stock, **data}
            except Exception as e:
                logger.warning("Error al leer main_stock.json: %s", e)
                print(f"Error al leer main_stock.json: {e}")
                return default_stock
        return default_stock

    def save_main_stock(self) -> bool:
        """Guarda la referencia maestra de ubicaciones en main_stock.json."""
        try:
            self.main_stock["updated_at"] = datetime.now().isoformat()
            with open(self.main_stock_file, "w", encoding="utf-8") as f:
                json.dump(self.main_stock, f, indent=4)
            return True
        except Exception as e:
            logger.error("Error al guardar main_stock.json: %s", e)
            print(f"Error al guardar main_stock.json: {e}")
            return False

    def update_product_location(self, sku: str, new_container: str, update_file: bool = True) -> bool:
        """
        Actualiza explícitamente la ubicación esperada de un SKU en main_stock.json
        tras la confirmación del usuario ("Mover" o "Dejar ahí").
        """
        sku = sku.strip().upper()
        new_container = new_container.strip() if new_container else ""
        
        with self.lock:
            if not sku:
                return False
            
            old_container = self.main_stock.get("product_locations", {}).get(sku)
            
            # Actualizar mapeo producto -> contenedor
            self.main_stock.setdefault("product_locations", {})[sku] = new_container
            
            # Actualizar contenedores
            containers = self.main_stock.setdefault("containers", {})
            if old_container and old_container in containers:
                containers[old_container].setdefault("expected_skus", {}).pop(sku, None)
            
            if new_container:
                c_type = self.get_code_type(new_container)
                if new_container not in containers:
                    containers[new_container] = {
                        "type": c_type,
                        "parent": None,
                        "expected_skus": {}
                    }
                expected_qty = self.original_quantities.get(sku, 1)
                containers[new_container].setdefault("expected_skus", {})[sku] = expected_qty
        
        # Fuente de verdad cambiada: invalidar caché de ubicaciones
        self._invalidate_location_cache()
        
        with self.lock:
            if update_file:
                return self.save_main_stock()
            return True

    def _history_location(self, sku: str):
        """Busca la última ubicación conocida de un SKU SOLO en el historial.

        Lógica extraída (idéntica a la usada antes por get_expected_container_for_sku
        y por StockApp.get_possible_location): recorre los archivos históricos del
        más reciente al más antiguo y devuelve el primer contenedor donde aparece
        el SKU, o None si no aparece en ningún historial.

        NO toma locks: el llamador gestiona la sincronización (la caché usa su
        propio lock).
        """
        sku = sku.strip().upper()
        sorted_hist_files = sorted(self.historical_sequences.keys(), key=os.path.getmtime, reverse=True)
        for hist_file in sorted_hist_files:
            hist_seq = self.historical_sequences[hist_file]
            for i, code in enumerate(hist_seq):
                if code == sku:
                    h_box, h_sec = self.get_containers_for_index(hist_seq, i)
                    h_container = h_box if h_box else (h_sec if h_sec else None)
                    if h_container:
                        return h_container
        return None

    def _history_location_cached(self, sku: str):
        """Versión cacheada de _history_location (thread-safe).

        La caché se invalida con _invalidate_location_cache() cuando cambia la
        fuente de verdad (main_stock o historial). Cachea también el miss (None)
        para no re-escancar el historial completo por SKUs que no existen.

        Seguridad de hilos: si la fuente de verdad cambia (incremento de
        _location_version) mientras se computa la búsqueda, el resultado NO se
        cachea (evita escribir un valor stale después de una invalidación).
        """
        sku = sku.strip().upper()
        with self._location_cache_lock:
            if sku in self._history_location_cache:
                return self._history_location_cache[sku]
            version = self._location_version
        loc = self._history_location(sku)
        with self._location_cache_lock:
            if version != self._location_version:
                return loc  # fuente de verdad cambió durante el cálculo: no cachear
            self._history_location_cache[sku] = loc
        return loc

    def _invalidate_location_cache(self):
        """Invalida ambas cachés de ubicación (thread-safe).

        Se llama cuando cambia la fuente de verdad:
        - update_product_location() (mutación de main_stock)
        - _index_history() (incorporación de historial nuevo)
        """
        with self._location_cache_lock:
            self._location_cache.clear()
            self._history_location_cache.clear()
            self._location_version += 1

    def get_expected_container_for_sku(self, sku: str) -> str:
        """Busca el contenedor esperado para un SKU en main_stock.json o en el historial.

        La lectura de main_stock y la escritura en caché ocurren bajo el mismo
        lock de caché: así, una invalidación concurrente (update_product_location)
        queda serializada y nunca deja un valor stale cacheado.
        """
        sku = sku.strip().upper()
        with self._location_cache_lock:
            # 1. Caché: resultado previo (main_stock o historial)
            if sku in self._location_cache:
                return self._location_cache[sku]
            # 2. Prioridad absoluta: main_stock.json
            expected = self.main_stock.get("product_locations", {}).get(sku)
            if expected:
                self._location_cache[sku] = expected
                return expected

        # 3. Fallback a historial (cacheado y versionado)
        loc = self._history_location_cached(sku)
        with self._location_cache_lock:
            if sku not in self._location_cache:
                self._location_cache[sku] = loc
        return loc

    def get_container_expected_items(self, container_code: str) -> dict:
        """Devuelve un diccionario {sku: expected_qty} para el contenedor según main_stock.json."""
        container_code = container_code.strip()
        containers = self.main_stock.get("containers", {})
        if container_code in containers:
            return dict(containers[container_code].get("expected_skus", {}))
        
        # Si no está en containers, computar a partir de product_locations
        items = {}
        for sku, c_code in self.main_stock.get("product_locations", {}).items():
            if c_code == container_code:
                items[sku] = self.original_quantities.get(sku, 1)
        return items

    def get_container_status(self, container_code: str, seq: list = None):
        """
        Calcula el estado del contenedor:
        - expected_total: total de unidades esperadas
        - scanned_total: total de unidades escaneadas actualmente dentro de este contenedor
        - missing_count: faltantes
        - is_complete: True si completó todo lo esperado
        - display_str: "✓" si está completo, str(missing) si faltan, o ""

        Optimización (auditoría 2026-08-31): el conteo de productos por
        contenedor se precomputa UNA vez por versión de scan_sequence
        (_compute_container_counts) en lugar de recorrer la secuencia completa
        por cada llamada. La caché se invalida en cada mutación de
        scan_sequence; si seq es una copia externa (no self.scan_sequence) se
        computa directo para no devolver estado stale.
        """
        if seq is None:
            seq = self.scan_sequence

        expected_items = self.get_container_expected_items(container_code)
        expected_total = sum(expected_items.values())

        if seq is self.scan_sequence:
            with self._container_status_lock:
                if self._container_counts_version != self._container_counts_computed_version:
                    self._container_counts = self._compute_container_counts(seq)
                    self._container_counts_computed_version = self._container_counts_version
                scanned_count = self._container_counts.get(container_code, 0)
        else:
            # Secuencia externa (tests/manipulación directa): conteo directo,
            # misma semántica que el método original.
            scanned_count = 0
            for i, code in enumerate(seq):
                if not self.is_qr_code(code):
                    box, sec = self.get_containers_for_index(seq, i)
                    active_c = box if box else (sec if sec else None)
                    if active_c == container_code or sec == container_code:
                        scanned_count += 1

        missing = max(0, expected_total - scanned_count)
        is_complete = (expected_total > 0 and scanned_count >= expected_total)

        if expected_total > 0:
            if is_complete:
                display_str = "✓"
            else:
                display_str = str(missing)
        else:
            display_str = ""

        return {
            "container": container_code,
            "expected_total": expected_total,
            "scanned_count": scanned_count,
            "missing_count": missing,
            "is_complete": is_complete,
            "display_str": display_str
        }

    def _index_history(self):
        """Lee los últimos 10 JSONs históricos en Escaneos/ y guarda sus secuencias ordenadas."""
        history = {}
        if os.path.exists(self.scan_dir):
            all_files = glob.glob(os.path.join(self.scan_dir, "*.json"))
            valid_files = [f for f in all_files if "ultima_sesion" not in f and "main_stock" not in f]
            
            valid_files.sort(key=os.path.getmtime, reverse=True)
            recent_files = valid_files[:10]
            
            for file in recent_files:
                try:
                    with open(file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    if "scan_sequence" in data:
                        history[file] = list(data["scan_sequence"])
                    else:
                        scanned = data.get("scanned") or data.get("scanned_items") or {}
                        seq = []
                        for code, positions in scanned.items():
                            for p in positions:
                                seq.append((p, code))
                        seq.sort()
                        if seq:
                            history[file] = [code for p, code in seq]
                except:
                    pass
                    
        with self.lock:
            self.historical_sequences = history

        # El historial es fuente de verdad de la caché de ubicaciones:
        # invalidar para que SKUs cacheados (incluso misses) se re-resuelvan.
        self._invalidate_location_cache()

    def parse_sku_intelligently(self, sku):
        """
        Analiza el SKU para agruparlo por modelo usando prefijos reales detectados en escaneos.
        """
        prefixes = ["IMPIPH", "IMPIP", "IMP", "TPUIP", "TPIP", "TPU", "TPI", "CASE", "CA", "ENGOP", "ENGO", "ENGIP", "ENGI", "ENG", "EN", "HARDI", "HAR", "HA", "MAGI", "MAG", "RE", "TI", "FILM", "TP"]
        found_prefix = ""
        for p in prefixes:
            if sku.startswith(p):
                if len(p) > len(found_prefix):
                    found_prefix = p
        
        remaining = sku[len(found_prefix):]
        model = ""
        for char in remaining:
            if char.isdigit() or char in "PXL":
                model += char
            elif model:
                break
        
        if not model: model = remaining[:3]
        return {"prefix": found_prefix, "model": model, "sku": sku}

    def get_containers_for_index(self, seq, idx):
        """
        Devuelve (caja_activa, mueble_activo) en la posición idx de la secuencia.
        Soporta escaneo hacia adelante (cuando el usuario escanea productos primero y el QR después).
        """
        active_box = None
        active_sec = None

        if idx < 0 or idx >= len(seq):
            return None, None

        # 1. Búsqueda hacia atrás (flujo estándar Mueble -> Caja -> Productos)
        for i in range(idx - 1, -1, -1):
            if i >= len(seq):
                continue
            code = seq[i]
            if code.startswith('@') and not active_box:
                active_box = code
            elif code.startswith(('%', '#')) and not active_sec:
                active_sec = code
                break

        # 2. Fallback hacia adelante si no hay contenedor previo (Productos escaneados antes de su QR)
        if not active_box and not active_sec:
            for i in range(idx + 1, len(seq)):
                code = seq[i]
                if code.startswith('@') and not active_box:
                    active_box = code
                    break
                elif code.startswith(('%', '#')) and not active_sec:
                    active_sec = code
                    break

        if active_sec and active_sec.startswith('#'):
            active_box = None # No hay cajas dentro de vidrieras (#)

        return active_box, active_sec

    def check_proximity(self, sku, pos):
        """
        Valida si el SKU está en una caja/mueble/vidriera incorrecto según main_stock.json o el historial.
        Retorna un dict con la información del error si está mal guardado, o None si está OK.
        """
        idx = pos - 1
        with self.lock:
            if idx < 0 or idx >= len(self.scan_sequence):
                return None
            
            # Obtener contenedor actual activo
            curr_box, curr_sec = self.get_containers_for_index(self.scan_sequence, idx)
            curr_container = curr_box if curr_box else (curr_sec if curr_sec else None)
            
            # Excepción: Vidriera (#) permite un máximo de 1 unidad exhibida sin alertas
            if curr_sec and curr_sec.startswith('#'):
                unit_count = 0
                for i, code in enumerate(self.scan_sequence):
                    if code == sku:
                        _, s = self.get_containers_for_index(self.scan_sequence, i)
                        if s == curr_sec:
                            unit_count += 1
                if unit_count <= 1:
                    return None # Exento de alerta
            
            # Buscar contenedor esperado (main_stock.json -> historial)
            expected_container = self.get_expected_container_for_sku(sku)
            
            # Si no hay ubicación registrada, se considera OK (producto nuevo)
            if not expected_container:
                return None
                
            # Si el contenedor actual coincide con el esperado, está OK
            if curr_container == expected_container or (curr_sec and curr_sec == expected_container):
                return None
                
            # Discrepancia detectada
            return {
                "sku": sku,
                "pos": pos,
                "current_container": curr_container if curr_container else "Ninguno",
                "expected_container": expected_container
            }


    def resolve_pending_verifications(self, current_pos, alert_callback):
        pass # Terminado (sea OK o Alerta)

    def detect_proximity_missing(self, current_sku):
        """
        Si terminamos con un modelo y en el CSV hay otro del mismo 'grupo' que falta.
        (Punto 7.3 del manual)
        """
        info = self.parse_sku_intelligently(current_sku)
        group_model = info["model"]
        
        missing_in_group = []
        for code, desc, expected in self.stock_data:
            scanned = len(self.scanned_items.get(code, []))
            if scanned < expected:
                item_info = self.parse_sku_intelligently(code)
                if item_info["model"] == group_model:
                    missing_in_group.append(code)
        
        # Si hay faltantes en el mismo grupo de modelo
        if missing_in_group:
            # Solo alertar si escaneamos al menos uno de este grupo satisfactoriamente
            return missing_in_group
        return []

    def refresh_data(self):
        """
        Vuelve a filtrar la lista maestra basándose en las exclusiones actuales de la configuración.
        Esto permite que el total 'Esperado' se actualice dinámicamente si se agregan SKUs a ignorar.
        """
        excluded = set(self.config.get("excluded_skus", []))
        new_stock = []
        new_orig_qty = {}
        new_fam_map = {}

        with self.lock:
            for code, desc, qty, fam in self.all_stock_data:
                if code in excluded:
                    continue
                
                # Re-aplicar el filtro de familia original con prefijos ESTRICTOS (Feedback Matías)
                # Solo tomamos si empieza por AG, AM o AO.
                is_valid_prefix = fam.startswith("AM") or fam.startswith("AO") or fam.startswith("AG")
                
                if not is_valid_prefix:
                    continue

                if self.family_type == "AM_AO" and (fam.startswith("AM") or fam.startswith("AO")):
                    new_stock.append((code, desc, qty))
                    new_orig_qty[code] = qty
                    new_fam_map[code] = fam
                elif self.family_type == "AG" and fam.startswith("AG"):
                    new_stock.append((code, desc, qty))
                    new_orig_qty[code] = qty
                    new_fam_map[code] = fam
                elif self.family_type == "COMBINED":
                    # En modo combinado también somos estrictos con los 3 prefijos permitidos
                    new_stock.append((code, desc, qty))
                    new_orig_qty[code] = qty
                    new_fam_map[code] = fam

            self.stock_data = sorted(new_stock, key=lambda x: x[0])
            self.original_quantities = new_orig_qty
            self.family_map = new_fam_map
            self.data_changed = True

    def import_csv(self, file_path, family_filter, is_loading=False):
        """Carga el CSV con encoding latin1 y delimitador ; con detección dinámica de cabecera."""
        try:
            temp_all_data = [] # Guardaremos todo para el refresco posterior
            new_full_map = {}
            
            data_started = False
            with open(file_path, 'r', encoding='latin1', errors='ignore') as f:
                reader = csv.reader(f, delimiter=';')
                for row in reader:
                    if not row: continue
                    if not data_started:
                        row_str = " ".join(row).upper()
                        if "PRODUCTO" in row_str or "FAMILIA" in row_str:
                            data_started = True
                        continue
                    if row[0].startswith(';') or len(row) < 5:
                        continue
                        
                    fam = row[0].strip().upper()
                    code = row[1].strip().upper()
                    desc = row[2].strip()
                    
                    try:
                        qty_str = row[4].strip().replace('.', '').replace(',', '')
                        qty = int(qty_str) if qty_str else 0
                    except:
                        qty = 0
                    
                    new_full_map[code] = fam
                    temp_all_data.append((code, desc, qty, fam))
            
            with self.lock:
                self.all_stock_data = temp_all_data
                self.full_family_map = new_full_map
                self.family_type = family_filter
                self.current_csv_path = file_path
                
                if not is_loading:
                    # Determinar nombre base para el par (CSV + JSON)
                    date_str = datetime.now().strftime("%Y-%m-%d")
                    self.current_base_name = f"{date_str}_{family_filter}"
                    
                    # Copiar el CSV a la carpeta Escaneos
                    import shutil
                    dest_path = os.path.join(self.scan_dir, f"{self.current_base_name}.csv")
                    try:
                        shutil.copy2(file_path, dest_path)
                        self.current_csv_path = dest_path # Actualizar a la ruta local
                    except Exception as e:
                        logger.warning("Error al copiar CSV a Escaneos: %s", e)
                        print(f"Error al copiar CSV: {e}")

            # Aplicar filtros iniciales (incluyendo exclusiones actuales)
            self.refresh_data()
            return True
        except Exception as e:
            logger.error("Error importando CSV (%s): %s", file_path, e)
            print(f"Error importando CSV: {e}")
            return False
        except Exception as e:
            logger.error("Error importando CSV (%s): %s", file_path, e)
            print(f"Error importando CSV: {e}")
            return False

    def _rebuild_scanned_items(self):
        self.scanned_items.clear()
        for idx, code in enumerate(self.scan_sequence):
            pos = idx + 1
            self.scanned_items[code].append(pos)
        self.scan_counter = len(self.scan_sequence) + 1
        self._invalidate_container_status_cache()

    def _invalidate_container_status_cache(self):
        """Marca la caché de conteo de contenedores como obsoleta.

        Se llama desde _rebuild_scanned_items (add/delete/move/QR replace) y
        desde load_json (asignación directa de scan_sequence). El próximo
        get_container_status() re-precomputa el mapa container -> scanned_count
        en una sola pasada O(n).
        """
        with self._container_status_lock:
            self._container_counts_version += 1

    def _compute_container_counts(self, seq):
        """Precomputa container_code -> cantidad de productos escaneados bajo él.

        Una sola pasada O(n) sobre la secuencia; reemplaza el recorrido completo
        que get_container_status hacía por cada QR (patrón O(QRs × n)).

        Semántica idéntica al conteo original: un producto cuenta para su
        contenedor ACTIVO (caja si hay, si no mueble/vidriera) y además para su
        MUEBLE cuando está dentro de una caja (el original hacía
        `active_c == container or sec == container`, que cuenta en ambos casos).
        Si active_c == sec (producto sin caja), cuenta una sola vez.
        """
        counts = {}
        for i, code in enumerate(seq):
            if self.is_qr_code(code):
                continue
            box, sec = self.get_containers_for_index(seq, i)
            active_c = box if box else (sec if sec else None)
            seen = set()
            if active_c:
                seen.add(active_c)
            if sec:
                seen.add(sec)
            for c in seen:
                counts[c] = counts.get(c, 0) + 1
        return counts

    def add_item(self, sku):
        """Añade un ítem al escaneo o reemplaza un QR si está en espera."""
        sku = sku.strip().upper()
        if not sku: return None
        
        excluded = self.config.get("excluded_skus", [])
        if sku in excluded:
            return {"sku": sku, "fam": "EXC", "pos": -1, "excluded": True}
        
        # Determinar tipo
        is_qr = sku.startswith(('@', '%', '#'))
        if is_qr:
            fam = "QR"
        else:
            fam = self.full_family_map.get(sku, "??")
            
        with self.lock:
            # Si estamos esperando reemplazo de QR y el escaneado es un QR
            if self.qr_replace_index is not None and is_qr:
                idx = self.qr_replace_index
                old_sku = self.scan_sequence[idx]
                self.scan_sequence[idx] = sku
                self.qr_replace_index = None
                self._rebuild_scanned_items()
                pos = idx + 1
                self.data_changed = True
                self.last_action_time = time.time()
                return {"sku": sku, "fam": fam, "pos": pos, "replaced": True, "old_sku": old_sku}
            else:
                # Si se intentó agregar un producto pero estábamos esperando un QR,
                # igual se agrega al final normalmente (o el usuario canceló de alguna forma)
                self.scan_sequence.append(sku)
                self._rebuild_scanned_items()
                pos = len(self.scan_sequence)
                self.data_changed = True
                self.last_action_time = time.time()
                return {"sku": sku, "fam": fam, "pos": pos}

    def delete_last(self, sku=None):
        """Elimina la última posición de un SKU o el último escaneado en general."""
        with self.lock:
            target_sku = sku
            if not target_sku:
                if self.scan_sequence:
                    target_sku = self.scan_sequence[-1]
                else:
                    return False
            
            is_qr = target_sku.startswith(('@', '%', '#'))
            if is_qr:
                # Buscar la última aparición del QR en la secuencia
                indices = [i for i, x in enumerate(self.scan_sequence) if x == target_sku]
                if indices:
                    last_idx = indices[-1]
                    self.qr_replace_index = last_idx
                    self.data_changed = True
                    return "waiting_replacement"
                return False
            else:
                # Eliminar la última aparición del SKU normal
                indices = [i for i, x in enumerate(self.scan_sequence) if x == target_sku]
                if indices:
                    last_idx = indices[-1]
                    self.scan_sequence.pop(last_idx)
                    self._rebuild_scanned_items()
                    self.data_changed = True
                    self.last_action_time = time.time()
                    return True
        return False

    def get_row_color(self, expected, scanned):
        """Determina los colores de fila (Fondo, Fuente) basado en la configuración."""
        if expected < 0: expected = 0
        if scanned < 0: scanned = 0
        
        cfg = self.config
        
        if scanned == expected:
            if expected == 0:
                return (cfg.get("table_bg", "#242424"), cfg.get("table_fg", "#ffffff"))
            return (cfg.get("row_finished_bg", "#90ee90"), cfg.get("row_finished_fg", "#000000"))
        elif expected == 0 and scanned > 0:
            return (cfg.get("row_unknown_bg", "#ffa500"), cfg.get("row_unknown_fg", "#000000"))
        elif scanned > expected:
            return (cfg.get("row_excess_bg", "#fa8072"), cfg.get("row_excess_fg", "#000000"))
        else:
            return (cfg.get("row_pending_bg", "#f0e68c"), cfg.get("row_pending_fg", "#000000"))

    def auto_save(self, base_path, on_save_callback=None):
        """Hilo de auto-guardado."""
        while True:
            time.sleep(1) # Revisar cada segundo
            
            # Solo guardar si hay cambios Y han pasado 15s de inactividad (Feedback Matías)
            elapsed = time.time() - self.last_action_time
            if self.data_changed and elapsed >= 15:
                if self.save_json(base_path):
                    if on_save_callback:
                        on_save_callback()

    def save_json(self, filename):
        """Guarda el estado actual en JSON y actualiza el puntero de 'Última Sesión'."""
        # Feedback Matías: Usar el nombre base del par si existe
        if self.current_base_name:
            filename = f"{self.current_base_name}.json"
        else:
            filename = os.path.basename(filename)
            
        path = os.path.join(self.scan_dir, filename)
        last_session_path = os.path.join(self.scan_dir, "ultima_sesion.json")
        
        with self.lock:
            data = {
                "scanned": dict(self.scanned_items),
                "scan_sequence": list(self.scan_sequence),
                "counter": self.scan_counter,
                "family_type": self.family_type,
                "csv_path": self.current_csv_path,
                "base_name": self.current_base_name,
                "timestamp": datetime.now().isoformat()
            }
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=4)
                
                if filename != "ultima_sesion.json" and not self.is_historical:
                    with open(last_session_path, 'w', encoding='utf-8') as f:
                        json.dump(data, f, indent=4)

                self.data_changed = False
                self.last_saved_path = path
                return True
            except Exception as e:
                logger.error("Error al guardar JSON (%s): %s", path, e)
                return False

    def load_json(self, path):
        """Carga el estado desde JSON y busca su par CSV correspondiente."""
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            is_last_session = "ultima_sesion.json" in os.path.basename(path)
            
            with self.lock:
                self.is_historical = not is_last_session
                scanned_data = data.get("scanned") or data.get("scanned_items") or {}
                self.scanned_items = defaultdict(list, scanned_data)
                
                # Cargar/reconstruir scan_sequence
                if "scan_sequence" in data:
                    self.scan_sequence = list(data["scan_sequence"])
                else:
                    max_pos = data.get("counter", 1) - 1
                    seq = [None] * max_pos
                    for code, positions in scanned_data.items():
                        for p in positions:
                            idx = p - 1
                            if 0 <= idx < len(seq):
                                seq[idx] = code
                    self.scan_sequence = [x for x in seq if x is not None]

                self._invalidate_container_status_cache()
                
                self.scan_counter = data.get("counter") or data.get("scan_counter") or 1
                self.family_type = data.get("family_type")
                self.current_base_name = data.get("base_name")
                
                # Intentar cargar el CSV del par (mismo nombre base en la misma carpeta)
                folder = os.path.dirname(path)
                base = os.path.splitext(os.path.basename(path))[0]
                
                if not self.current_base_name and base != "ultima_sesion":
                    self.current_base_name = base
                    
                csv_pair = os.path.join(folder, f"{base}.csv")
                
                if os.path.exists(csv_pair):
                    csv_path = csv_pair
                else:
                    csv_path = data.get("csv_path")

                # Búsqueda de emergencia si el par no existe
                if not csv_path or not os.path.exists(csv_path):
                    all_csvs = glob.glob(os.path.join(folder, "*.csv"))
                    fam_id = "AG" if self.family_type == "AG" else ("AM" if self.family_type == "AM_AO" else "COMBINED")
                    for c_path in all_csvs:
                        if fam_id in os.path.basename(c_path).upper():
                            csv_path = c_path
                            break
            
            if csv_path and os.path.exists(csv_path):
                self.import_csv(csv_path, self.family_type, is_loading=True)
            
            return True
        except Exception as e:
            logger.error("Error cargando JSON (%s): %s", path, e)
            print(f"Error cargando JSON: {e}")
            return False

    def move_item_in_sequence(self, from_idx, to_idx):
        """Mueve un elemento en la secuencia física desde una posición a otra."""
        with self.lock:
            if from_idx < 0 or from_idx >= len(self.scan_sequence):
                return False
            if to_idx < 0 or to_idx >= len(self.scan_sequence):
                return False
            
            # Mover el elemento en la lista
            item = self.scan_sequence.pop(from_idx)
            self.scan_sequence.insert(to_idx, item)
            self._rebuild_scanned_items()
            self.data_changed = True
            return True

    def move_product_to_container(self, sku, pos, target_container):
        """
        Busca el producto en la posición dada y lo mueve justo después
        de la primera aparición del contenedor de destino en la secuencia.
        """
        with self.lock:
            idx = pos - 1
            if idx < 0 or idx >= len(self.scan_sequence) or self.scan_sequence[idx] != sku:
                return False
            
            # Buscar el contenedor de destino en la secuencia
            target_idx = -1
            for i, code in enumerate(self.scan_sequence):
                if code == target_container:
                    target_idx = i
                    # Mantener el último encontrado para agruparlo allí
            
            if target_idx != -1:
                # Mover el producto justo después del contenedor
                item = self.scan_sequence.pop(idx)
                if idx < target_idx:
                    target_idx -= 1
                self.scan_sequence.insert(target_idx + 1, item)
            else:
                # Si el contenedor no existe en toda la lista de escaneados actual,
                # se crea al final de la lista y se mueve el producto allí
                item = self.scan_sequence.pop(idx)
                self.scan_sequence.append(target_container)
                self.scan_sequence.append(item)
                
            self._rebuild_scanned_items()
            self.data_changed = True
            return True
