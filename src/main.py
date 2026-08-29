import customtkinter as ctk
import tkinter as tk
from src.config import current_config, save_config
from src.logger import logger
from src.core.auth import AuthManager
from src.core.inventory import InventoryManager
from src.core.automation import AutomationManager
from src.core.images import ImageManager
from src.gui.components.tables import InventoryTable
from src.gui.components.selector import SelectorWindow
from src.gui.utils import center_window
from tkinter import messagebox, Toplevel, filedialog
from tkinter.colorchooser import askcolor
from PIL import Image, ImageTk
import os
import sys
import traceback
import threading
import time
import json
import queue

from src.core.scanpipeline import ScanWorker
from src.gui.updates import (
    ViewOptions,
    build_full_view,
    apply_event,
    TABLE_SCANNED,
    TABLE_MASTER,
    TABLE_DIFF,
)

class StockApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        # 1. Configuración de Ventana
        self.title("Stock Cellular Center V8.0")
        center_window(self, 1280, 680)
        ctk.set_appearance_mode(current_config.get("theme", "dark"))
        ctk.set_default_color_theme("blue")
        
        self.withdraw()

        # 2. Inicializar Módulos Core
        try:
            self.inventory = InventoryManager(current_config)
            self.auth = AuthManager(current_config)
            # Pasamos un callback que salta al MainThread para actualizar la UI con CustomTkinter
            self.images = ImageManager(current_config, progress_callback=self._on_image_progress)
            self.automation = AutomationManager(current_config, self.inventory)
            logger.info("Módulos core inicializados correctamente")
        except Exception as e:
            logger.exception("Error inicializando módulos core")
            print(f"Error inicializando módulos: {e}")
            self.quit()
            return

        self.authenticated = False
        self.toast_history = []
        self.current_toasts = []
        self.collapsed_containers = set() # Contenedores QR plegados/colapsados (V8)
        self.active_grouped_alerts = {}   # Registro de alertas agrupadas por código
        self._excluded_from_export = set()

        # --- FASE B: estado de vista proyectado (updates.py) ---
        self._view = None  # ScanView actual: fuente visual única
        self._row_ids = {TABLE_SCANNED: {}, TABLE_MASTER: {}, TABLE_DIFF: {}}  # key -> item_id
        
        # Inyectar referencia de la app en config para el AutomationManager
        current_config["parent_app"] = self
        self.command_delete_available = True
        self.windows = {} # Seguimiento de ventanas

        # --- FASE A: Pipeline de escaneo desacoplado (recepción -> cola -> worker -> UI) ---
        self.scan_queue = queue.Queue()          # FIFO, sin límite artificial
        self.result_queue = queue.Queue()        # Resultados del worker hacia la UI
        self._worker_stop = threading.Event()    # Señal de cierre ordenado
        self._scan_worker = ScanWorker(self.inventory, self.scan_queue, self.result_queue, self._worker_stop)
        self._worker_thread = threading.Thread(target=self._scan_worker.run, daemon=True)
        self._worker_thread.start()
        self.after(60, self._poll_results)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._load_icons()
        self.bind("<F3>", lambda e: self._toggle_diff_window())
        self.after(200, self._perform_initial_auth)

    def _load_icons(self):
        """Carga los iconos del programa."""
        if getattr(sys, 'frozen', False):
            res_dir = os.path.join(sys._MEIPASS, "res")
        else:
            res_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "res")
        def get_icon(name):
            p = os.path.join(res_dir, f"{name}.png")
            if os.path.exists(p):
                img = Image.open(p)
                return ctk.CTkImage(light_image=img, dark_image=img, size=(20, 20))
            return None

        self.icon_search = get_icon("search")
        self.icon_delete = get_icon("delete")
        self.icon_bell = get_icon("bell")
        self.icon_settings = get_icon("settings")

    def _on_image_progress(self, downloaded, total):
        """Callback llamado desde el worker-thread del ImageManager para actualizar UI."""
        self.after(0, self._update_img_progress_ui, downloaded, total)        
    def _update_img_progress_ui(self, downloaded, total):
        """Actualiza el label del Header en el hilo principal."""
        if hasattr(self, 'lbl_img_progress') and self.lbl_img_progress.winfo_exists():
            if total > 0:
                percent = int((downloaded / total) * 100)
                color = "#ffc107" if ctk.get_appearance_mode() == "Dark" else "#e67e22" 
                if downloaded < total:
                    self.lbl_img_progress.configure(text=f"Imágenes: {downloaded}/{total} - {percent}%", text_color=color)
                else:
                    self.lbl_img_progress.configure(text=f"Imágenes: {downloaded}/{total} - 100%", text_color="#28a745")
            else:
                self.lbl_img_progress.configure(text="", text_color="white")

    def _on_close(self):
        """Cierre ordenado: detiene el worker antes de destruir la ventana.
        Evita que un thread secundario siga trabajando sobre una UI destruida.
        """
        logger.info("Cierre de aplicación solicitado (WM_DELETE_WINDOW)")
        self._worker_stop.set()
        try:
            self.destroy()
        except Exception:
            pass

    def _poll_results(self):
        """Único puente worker -> UI: consume result_queue en el hilo principal.
        - Muestra toasts (show_toast solo desde aquí para el pipeline).
        - FASE B: los refrescos se aplican de forma INCREMENTAL (updates.py);
          una ráfaga produce inserciones puntuales, no una reconstrucción.
        - Mantiene autoscroll hacia el último escaneo ("Último arriba"/"Último abajo").
        """
        try:
            if not self.winfo_exists():
                return
            refresh_events = []
            last_sku = None
            while True:
                try:
                    res = self.result_queue.get_nowait()
                except queue.Empty:
                    break
                rtype = res.get("type")
                if rtype == "toast":
                    self.show_toast(
                        res.get("msg", ""),
                        mtype=res.get("mtype", "info"),
                        duration=res.get("duration", 3000),
                        use_history=res.get("use_history", True),
                        metadata=res.get("metadata"),
                    )
                elif rtype == "refresh":
                    refresh_events.append(res)
                    last_sku = res.get("sku")

            if refresh_events:
                self._apply_incremental(refresh_events)
                # Autoscroll al último escaneado (comportamiento V8 actual)
                children = self.scanned_table.tree.get_children()
                if children:
                    target_row = children[0] if self.sort_var.get() == "Último arriba" else children[-1]
                    self.scanned_table.tree.see(target_row)
                # Sincronización a tabla maestra si está activa (comportamiento V8 actual)
                if self.sync_active.get() and last_sku and not self.inventory.is_qr_code(last_sku):
                    self._sync_to_master(last_sku)

            self.after(60, self._poll_results)
        except Exception:
            # Ventana destruida o Tk cerrado: no reintentar
            pass

    def _perform_initial_auth(self):
        if self.auth.check_license():
            if self.auth.verify_password():
                self.authenticated = True
                self.selector = SelectorWindow(self, self.inventory, self._on_start_selection)
            else:
                self.quit()
        else:
            self.quit()

    def _on_start_selection(self, data):
        """Manejador del resultado del SelectorWindow."""
        mode = data.get("mode")
        
        if mode == "new":
            self.family_type = data.get("family")
            self._setup_ui()
            self._finalize_start()
            
        elif mode == "history":
            self._load_saved_session(data.get("path"))

    def _finalize_start(self):
        self.automation.start_global_listener(on_f2_callback=self._focus_search)
        
        def on_save():
            self.after(0, self._on_auto_save_success)
            
        threading.Thread(target=self.inventory.auto_save, args=("ultima_sesion.json", on_save), daemon=True).start()
        
        self.deiconify()
        self.focus_force()
        self.status_label.configure(text=f"Sesión activa: {self.family_type}")

    def _load_saved_session(self, path):
        """Carga una sesión JSON desde la carpeta /Escaneos o ruta absoluta."""
        try:
            if not os.path.exists(path):
                full_path = os.path.join(self.inventory.scan_dir, os.path.basename(path))
            else:
                full_path = path

            with open(full_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self.family_type = data.get("family_type", "COMBINED")
            self.inventory.load_json(path)
            
            self._setup_ui()
            self._finalize_start()
            self._update_all_ui()
            
            if self.inventory.original_quantities:
                self.images.start_background_download(list(self.inventory.original_quantities.keys()))
            logger.info("Sesión cargada desde: %s (familia=%s)", full_path, self.family_type)
            
        except Exception as e:
            logger.exception("Error al cargar sesión desde %s", path)
            messagebox.showerror("Error de Carga", f"No se pudo cargar la sesión: {e}")
            self.quit()

    def _on_auto_save_success(self):
        """Callback para feedback visual de guardado."""
        if hasattr(self, 'btn_save'):
            self.btn_save.configure(fg_color="#28a745")
            self.status_label.configure(text="Progreso auto-guardado correctamente.")

    def _setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # --- HEADER ---
        self.header = ctk.CTkFrame(self)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(5, 3))
        
        self.title_label = ctk.CTkLabel(self.header, text="STOCK CELLULAR CENTER V8.0", font=("Roboto", 20, "bold"))
        self.title_label.pack(side="left", padx=20, pady=5)

        self.btn_import = ctk.CTkButton(self.header, text="Importar CSV", width=120, command=self._on_import_click)
        self.btn_import.pack(side="left", padx=5)

        self.btn_save = ctk.CTkButton(self.header, text="Guardar", width=100, fg_color="#28a745", command=self._on_save_manual)
        self.btn_save.pack(side="left", padx=5)

        self.btn_export = ctk.CTkButton(self.header, text="Exportar", width=100, fg_color="#3a7ebf", command=self._on_export_click)
        self.btn_export.pack(side="left", padx=5)

        self.btn_diff = ctk.CTkButton(self.header, text="Diferencias (F3)", width=120, fg_color="#ffc107", text_color="black", command=self._on_diff_click)
        self.btn_diff.pack(side="left", padx=5)

        self.btn_open_ext = ctk.CTkButton(self.header, text="Ver CSV", width=80, fg_color="#6c757d", command=self._on_open_ext_click)
        self.btn_open_ext.pack(side="left", padx=5)

        self.btn_options = ctk.CTkButton(self.header, text="", image=self.icon_settings, width=40, command=self._open_options)
        self.btn_options.pack(side="right", padx=10)
        
        self.lbl_img_progress = ctk.CTkLabel(self.header, text="", font=("Roboto", 13, "bold"))
        self.lbl_img_progress.pack(side="right", padx=10)

        self.btn_toast_hist = ctk.CTkButton(self.header, text="", image=self.icon_bell, width=35, command=self._open_toast_history, fg_color="transparent")
        self.btn_toast_hist.pack(side="right", padx=5)

        # --- SUMMARY PANEL ---
        self.summary_panel = ctk.CTkFrame(self, height=35)
        self.summary_panel.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=2)
        
        self.lbl_expected = ctk.CTkLabel(self.summary_panel, text="Esperado: 0", font=("Roboto", 13))
        self.lbl_expected.pack(side="left", padx=15)
        
        self.lbl_scanned = ctk.CTkLabel(self.summary_panel, text="Escaneado: 0", font=("Roboto", 13, "bold"), text_color="#3a7ebf")
        self.lbl_scanned.pack(side="left", padx=10)
        
        self.progress_bar = ctk.CTkProgressBar(self.summary_panel)
        self.progress_bar.pack(side="left", padx=10, fill="x", expand=True)
        self.progress_bar.set(0)
        
        self.lbl_percent = ctk.CTkLabel(self.summary_panel, text="(0%)", font=("Roboto", 13))
        self.lbl_percent.pack(side="left", padx=5)

        # Contador visual de diferencias relevantes (V8 - Requisito 18)
        self.lbl_relevant_diffs = ctk.CTkLabel(self.summary_panel, text="Incidencias: 0", font=("Roboto", 13, "bold"), text_color="#28a745")
        self.lbl_relevant_diffs.pack(side="left", padx=15)

        self.lbl_net_diff = ctk.CTkLabel(self.summary_panel, text="Dif. Neta: 0", font=("Roboto", 13))
        self.lbl_net_diff.pack(side="right", padx=15)

        # --- SCAN PANEL ---
        self.scan_panel = ctk.CTkFrame(self, height=80)
        self.scan_panel.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=3)
        
        self.input_subframe = ctk.CTkFrame(self.scan_panel, fg_color="transparent")
        self.input_subframe.pack(side="left", padx=20, fill="y")
        
        ctk.CTkLabel(self.input_subframe, text="ESCANEAR:", font=("Roboto", 12, "bold")).pack(anchor="sw", pady=(5, 0))
        self.scan_var = ctk.StringVar()
        self.scan_var.trace_add("write", lambda *args: self.scan_var.set(self.scan_var.get().upper()))
        self.scan_entry = ctk.CTkEntry(self.input_subframe, width=350, height=32, font=("Roboto", 14), textvariable=self.scan_var)
        self.scan_entry.pack(pady=3)
        self.scan_entry.bind("<Return>", self._on_scan_event)
        
        btns_scan_frame = ctk.CTkFrame(self.input_subframe, fg_color="transparent")
        btns_scan_frame.pack(fill="x", pady=2)
        
        self.btn_search_manual = ctk.CTkButton(btns_scan_frame, text=" BUSCAR (F2)", image=self.icon_search, width=170, height=32, fg_color="#3a7ebf", command=self._focus_search)
        self.btn_search_manual.pack(side="left", padx=(0, 5))
        
        self.btn_delete_manual = ctk.CTkButton(btns_scan_frame, text=" ELIMINAR", image=self.icon_delete, width=170, height=32, fg_color="#dc3545", command=self._on_delete_last_manual)
        self.btn_delete_manual.pack(side="left")

        self.last_code_frame = ctk.CTkFrame(self.scan_panel, corner_radius=10, height=70)
        self.last_code_frame.pack(side="right", padx=10, pady=5, fill="both", expand=True)
        
        self.last_code_label = ctk.CTkLabel(self.last_code_frame, text="---", font=("Roboto", 38, "bold"), text_color="#3a7ebf")
        self.last_code_label.pack(expand=True)
        ctk.CTkLabel(self.last_code_frame, text="Último Escaneado", font=("Roboto", 11, "bold")).place(relx=0.01, rely=0.01)

        # --- TABLES ---
        self.container_tables = ctk.CTkFrame(self, fg_color="transparent")
        self.container_tables.grid(row=3, column=0, columnspan=2, sticky="nsew", padx=10, pady=5)
        self.container_tables.grid_columnconfigure(0, weight=1)
        self.container_tables.grid_columnconfigure(1, weight=1)
        self.container_tables.grid_rowconfigure(1, weight=1)

        # Título Dinámico y Selector de Orden para Escaneados (V8 - Requisito 9)
        self.scanned_header = ctk.CTkFrame(self.container_tables, height=40, fg_color="transparent")
        self.scanned_header.grid(row=0, column=0, sticky="ew", padx=(0, 5), pady=(0, 5))
        
        initial_sort = "Último abajo" if self.inventory.config.get("list_order", "bottom") == "bottom" else "Último arriba"
        self.sort_var = ctk.StringVar(value=initial_sort)
        self.sort_combo = ctk.CTkComboBox(
            self.scanned_header,
            values=["Último abajo", "Último arriba", "Alfabético", "Cantidad"], 
            variable=self.sort_var,
            command=self._on_sort_changed,
            width=135, height=28, font=("Roboto", 11)
        )
        self.sort_combo.pack(side="left", padx=5)
        
        self.title_scanned = ctk.CTkLabel(self.scanned_header, text="PRODUCTOS ESCANEADOS", font=("Roboto", 16, "bold"))
        self.title_scanned.pack(side="left", expand=True)
        
        self.sync_active = tk.BooleanVar(value=False)
        self.btn_sync = ctk.CTkButton(self.scanned_header, text="↔", width=35, height=28, 
                                     fg_color=("gray75", "gray25"), text_color=("black", "white"),
                                     command=self._toggle_sync, font=("Roboto", 16, "bold"))
        self.btn_sync.pack(side="right", padx=5)

        self.btn_down = ctk.CTkButton(self.scanned_header, text="▼", width=30, height=28,
                                      command=self._move_selected_down, font=("Roboto", 12, "bold"))
        self.btn_down.pack(side="right", padx=2)

        self.btn_up = ctk.CTkButton(self.scanned_header, text="▲", width=30, height=28,
                                    command=self._move_selected_up, font=("Roboto", 12, "bold"))
        self.btn_up.pack(side="right", padx=2)
        
        self.scanned_table = InventoryTable(self.container_tables, title="", columns=("Familia", "Código", "Cant."), 
                                           config_manager=self.inventory.config, show_title=False)
        self.scanned_table.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        self.scanned_table.tree.bind("<Double-1>", lambda e: self._on_row_double_click(self.scanned_table))
        self.scanned_table.tree.bind("<<TreeviewSelect>>", self._on_scanned_select)
        self.scanned_table.tree.bind("<Delete>", self._on_delete_key_event)
        self.scanned_table.tree.bind("<Control-Up>", lambda e: self._move_selected_up())
        self.scanned_table.tree.bind("<Control-Down>", lambda e: self._move_selected_down())
        
        self.master_table = InventoryTable(self.container_tables, title="INVENTARIO MAESTRO", columns=("Código", "Descripción", "Stock(Scan)"), 
                                           config_manager=self.inventory.config)
        self.master_table.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(5, 0))
        self.master_table.tree.bind("<Double-1>", lambda e: self._on_row_double_click(self.master_table))

        # --- FOOTER ---
        self.status_bar = ctk.CTkFrame(self, height=25)
        self.status_bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        self.status_label = ctk.CTkLabel(self.status_bar, text="Stock Cellular Center V8.0 | F2: Buscar | F3: Diferencias | F8: Pausar vaciado", font=("Roboto", 11))
        self.status_label.pack(side="left", padx=10)
        
        self._setup_drag_and_drop()
        self.scan_entry.focus_set()

        # FASE B: la vista proyectada arranca consistente con las tablas vacías.
        # (En carga de sesión, _load_saved_session llama _update_all_ui() después.)
        self._view = build_full_view(self.inventory, self._view_options())
        self._row_ids = {TABLE_SCANNED: {}, TABLE_MASTER: {}, TABLE_DIFF: {}}

    def _on_sort_changed(self, new_val=None):
        mode = self.sort_var.get()
        if mode == "Último arriba":
            self.inventory.config["list_order"] = "top"
        elif mode == "Último abajo":
            self.inventory.config["list_order"] = "bottom"
        self._update_all_ui()

    def _on_scan_event(self, event):
        """FASE A: camino crítico mínimo.
        Recibe -> registra -> encola -> limpia -> foco.
        TODO lo demás (proximidad, sobrantes, avisos, refresco UI) corre en el worker/poll.
        """
        sku = self.scan_entry.get().strip().upper()
        self.scan_entry.delete(0, "end")
        if not sku:
            return
        
        # Comando textual de borrado (comportamiento V8 intacto)
        if sku == "ELIMINAR":
            if not self.command_delete_available:
                self.show_toast("No se puede borrar más de un ítem consecutivamente.", mtype="warning", duration=3000)
            else:
                if self.inventory.delete_last():
                    self.command_delete_available = False
                    self.last_code_label.configure(text="BORRADO", text_color="#dc3545")
                    self._update_all_ui()
                    self.show_toast("Último escaneo eliminado", mtype="info", duration=1500, use_history=False)
            return

        if self.automation.check_qr_command(sku):
            self.last_code_label.configure(text="BORRADO", text_color="#dc3545")
            self._update_all_ui()
            return

        is_qr = self.inventory.is_qr_code(sku)

        # Validación de familia: es DECISIÓN DE REGISTRO (rápida, O(1)) -> queda aquí.
        # Los toasts asociados se encolan (no bloquean).
        if not is_qr and self.family_type != "COMBINED":
            res_fam = self.inventory.full_family_map.get(sku, "")
            is_correct = False
            if self.family_type == "AM_AO" and (res_fam.startswith("AM") or res_fam.startswith("AO")):
                is_correct = True
            elif self.family_type == "AG" and res_fam.startswith("AG"):
                is_correct = True
            if not is_correct:
                self.result_queue.put({
                    "type": "toast", "mtype": "warning",
                    "msg": f"¡AVISO! {sku} es de familia: {res_fam}",
                    "duration": 10000, "use_history": False,
                })
                self.scan_entry.focus_set()
                return

        result = self.inventory.add_item(sku)
        if result:
            self.btn_save.configure(fg_color="#dc3545")

            if result.get("excluded"):
                self.last_code_label.configure(text="EXCLUIDO", text_color="orange")
            else:
                self.last_code_label.configure(text=sku, text_color="#3a7ebf")

                if result.get("replaced"):
                    self.result_queue.put({
                        "type": "toast", "mtype": "success",
                        "msg": f"QR Reemplazado: {result['old_sku']} -> {sku}",
                        "duration": 3000, "use_history": False,
                    })
                    self.status_label.configure(text=f"QR Reemplazado: {sku}")
                else:
                    self.status_label.configure(text=f"Escaneado: {sku} ({result['fam']})")

                # Encolar para procesamiento secundario (worker FIFO)
                self.scan_queue.put({
                    "sku": sku,
                    "pos": result["pos"],
                    "fam": result.get("fam"),
                    "ts": time.time(),
                    "replaced": result.get("replaced", False),
                    "is_qr": is_qr,
                    "old_sku": result.get("old_sku"),
                })
                self.command_delete_available = True

        self.scan_entry.focus_set()

    def _update_all_ui(self):
        """Rebuild COMPLETO del estado visual (acciones discretas del usuario).

        Pasa por la misma proyección de updates.py que el camino incremental:
        la vista (self._view) y los índices (self._row_ids) quedan consistentes
        con las tablas pintadas, de modo que el siguiente incremental parte
        de un estado correcto.
        """
        self._rebuild_view_state()
        self._update_metrics_labels(self._view.metrics)

    # ------------------------------------------------------------------
    # FASE B: proyección de vista (updates.py) — aplicador Tk
    # ------------------------------------------------------------------

    def _view_options(self):
        """Opciones de vista desde el estado de la app (orden, colapso, exclusión)."""
        return ViewOptions(
            sort_mode=self.sort_var.get(),
            collapsed_containers=frozenset(self.collapsed_containers),
            excluded_from_export=frozenset(self._excluded_from_export),
            location_resolver=self.get_possible_location,
        )

    def _rebuild_view_state(self):
        """Pinta las tres tablas completas desde build_full_view y registra
        los índices key -> item_id que usa el incremental."""
        opts = self._view_options()
        new_view = build_full_view(self.inventory, opts)
        self._view = new_view
        self._row_ids = {TABLE_SCANNED: {}, TABLE_MASTER: {}, TABLE_DIFF: {}}

        # Tabla escaneada
        self.scanned_table.clear()
        for row in new_view.scanned:
            item = self.scanned_table.tree.insert("", "end", values=row.values, text=row.text or "")
            self._row_ids[TABLE_SCANNED][row.key] = item
            self.scanned_table.set_row_style(item, row.colors, bold=row.bold)

        # Tabla maestra
        self.master_table.clear()
        for row in new_view.master:
            item = self.master_table.tree.insert("", "end", values=row.values)
            self._row_ids[TABLE_MASTER][row.key] = item
            self.master_table.set_row_color(item, row.colors)

        # Ventana de diferencias (solo si está abierta)
        if self._diff_window_open():
            self._sync_diff_table_from(new_view)

    def _update_metrics_labels(self, m):
        """Pinta los labels del panel resumen desde un objeto Metrics."""
        self.lbl_scanned.configure(text=f"Escaneado: {m.scanned_count}")
        self.lbl_expected.configure(text=f"Esperado: {m.expected_count}")

        color = "#28a745" if m.diff_net > 0 else ("#dc3545" if m.diff_net < 0 else "white")
        self.lbl_net_diff.configure(text=f"Dif. Neta: {m.diff_net}", text_color=color)

        rel_color = "#28a745" if m.relevant_diffs == 0 else ("#ffa500" if m.relevant_diffs < 5 else "#dc3545")
        self.lbl_relevant_diffs.configure(text=f"Incidencias: {m.relevant_diffs}", text_color=rel_color)

        self.progress_bar.set(min(1.0, m.percent / 100))
        self.lbl_percent.configure(text=f"({m.percent:.1f}%)")

    def _apply_incremental(self, events):
        """Aplica una tanda de refrescos de forma incremental.

        Cada evento se resuelve contra el modelo (fuente de verdad) vía
        updates.apply_event y las acciones resultantes se ejecutan sobre
        los Treeviews. Ante CUALQUIER inconsistencia o excepción:
        fallback a rebuild completo (nunca una vista potencialmente incorrecta).
        """
        opts = self._view_options()
        view = self._view
        try:
            for ev in events:
                view, actions = apply_event(view, ev, self.inventory, opts)
                self._execute_actions(actions)
            self._view = view
            self._update_metrics_labels(view.metrics)
        except Exception:
            traceback.print_exc()
            self._update_all_ui()

    def _execute_actions(self, actions):
        """Traduce acciones (insert/update/delete/move) a operaciones Tk.

        Misma semántica que updates.apply_actions (deletes -> updates ->
        moves -> inserts). Si la ventana de diferencias está cerrada, sus
        acciones se descartan: la vista de datos se actualiza igual y la
        ventana se sincroniza al abrir (_sync_diff_table).
        """
        diff_open = self._diff_window_open()

        for a in actions:
            if a.op == "delete" and (a.table != TABLE_DIFF or diff_open):
                item = self._row_ids[a.table].pop(a.key, None)
                if item is not None:
                    try:
                        self._tree_for(a.table).delete(item)
                    except Exception:
                        pass

        for a in actions:
            if a.op == "update" and (a.table != TABLE_DIFF or diff_open):
                item = self._row_ids[a.table].get(a.key)
                if item is None:
                    raise KeyError(f"update sin fila registrada: {a.table}:{a.key}")
                self._apply_row(a.table, item, a.row)

        for a in actions:
            if a.op == "move" and (a.table != TABLE_DIFF or diff_open):
                item = self._row_ids[a.table].get(a.key)
                if item is None:
                    raise KeyError(f"move sin fila registrada: {a.table}:{a.key}")
                self._tree_for(a.table).move(item, "", a.index)

        for a in sorted((x for x in actions if x.op == "insert"), key=lambda x: x.index):
            if a.table == TABLE_DIFF and not diff_open:
                continue
            tree = self._tree_for(a.table)
            item = tree.insert("", a.index, values=a.row.values, text=a.row.text or "")
            self._row_ids[a.table][a.key] = item
            self._apply_row_style(a.table, item, a.row)

    def _apply_row(self, table, item, row):
        tree = self._tree_for(table)
        tree.item(item, values=row.values, text=row.text or "")
        self._apply_row_style(table, item, row)

    def _apply_row_style(self, table, item, row):
        if table == TABLE_SCANNED:
            self.scanned_table.set_row_style(item, row.colors, bold=row.bold)
        else:
            self._table_widget(table).set_row_color(item, row.colors)

    def _table_widget(self, table):
        if table == TABLE_SCANNED:
            return self.scanned_table
        if table == TABLE_MASTER:
            return self.master_table
        return self.windows["diff_table"]

    def _tree_for(self, table):
        return self._table_widget(table).tree

    def _diff_window_open(self):
        return (
            "diff" in self.windows
            and self.windows["diff"].winfo_exists()
            and "diff_table" in self.windows
            and self.windows["diff_table"].winfo_exists()
        )

    def _sync_diff_table(self):
        """Llena la ventana de diferencias recién abierta desde la vista actual."""
        self._sync_diff_table_from(self._view)

    def _sync_diff_table_from(self, view):
        table = self.windows["diff_table"]
        table.clear()
        self._row_ids[TABLE_DIFF] = {}
        for row in view.diff:
            item = table.tree.insert("", "end", values=row.values)
            self._row_ids[TABLE_DIFF][row.key] = item
            table.set_row_color(item, row.colors)

    def _refresh_tables(self):
        """Actualización de tablas con soporte de orden, jerarquía y plegado de QRs (V8)."""
        self.scanned_table.clear()
        sort_mode = self.sort_var.get()
        
        if sort_mode in ("Último arriba", "Último abajo", "Escaneo"):
            indices = range(len(self.inventory.scan_sequence) - 1, -1, -1) if sort_mode in ("Último arriba", "Escaneo") else range(0, len(self.inventory.scan_sequence))
            
            for idx in indices:
                code = self.inventory.scan_sequence[idx]
                pos = idx + 1
                
                # Reemplazo de QR temporal
                if idx == self.inventory.qr_replace_index:
                    code_display = "[ESPERANDO REEMPLAZO]"
                else:
                    code_display = code
                
                # Identificar tipo de fila
                is_qr = self.inventory.is_qr_code(code)
                if is_qr:
                    c_type = self.inventory.get_code_type(code)
                    if c_type == 'caja':
                        fam = "CAJA"
                        tag_colors = ("#333333", "#ffffff")
                    elif c_type == 'mueble':
                        fam = "MUEBLE"
                        tag_colors = ("#1f4e78", "#ffffff")
                    else:
                        fam = "VIDRIERA"
                        tag_colors = ("#7030a0", "#ffffff")
                    
                    # Jerarquía y Plegado (V8 - Requisitos 11 y 12)
                    is_collapsed = code in self.collapsed_containers
                    fold_icon = "▶ " if is_collapsed else "▼ "
                    
                    # Columna Cantidad para QRs muestra faltantes o ✓ (V8)
                    c_status = self.inventory.get_container_status(code, self.inventory.scan_sequence)
                    qty_str = c_status["display_str"]
                    
                    row_id = self.scanned_table.insert_data((fam, f"{fold_icon}{code_display}", qty_str))
                    self.scanned_table.tree.item(row_id, text=str(pos))
                    self.scanned_table.set_row_style(row_id, tag_colors, bold=True)
                else:
                    # Verificar si el producto pertenece a un contenedor colapsado
                    box, sec = self.inventory.get_containers_for_index(self.inventory.scan_sequence, idx)
                    if (box and box in self.collapsed_containers) or (sec and sec in self.collapsed_containers):
                        continue # Ocultar ítem por estar dentro de carpeta plegada
                    
                    fam = self.inventory.family_map.get(code, "??")
                    total_qty = len(self.inventory.scanned_items.get(code, []))
                    positions = self.inventory.scanned_items.get(code, [])
                    is_last = (pos == max(positions) if positions else False)
                    
                    if is_last:
                        expected = self.inventory.original_quantities.get(code, 0)
                        tag_colors = self.inventory.get_row_color(expected, total_qty)
                        qty_str = str(total_qty)
                    else:
                        bg = self.inventory.config.get("table_bg", "#242424")
                        fg = self.inventory.config.get("table_fg", "#ffffff")
                        tag_colors = (bg, fg)
                        qty_str = ""
                
                    row_id = self.scanned_table.insert_data((fam, code_display, qty_str))
                    self.scanned_table.tree.item(row_id, text=str(pos))
                    self.scanned_table.set_row_style(row_id, tag_colors, bold=False)
                
        else:
            # Agrupar por SKU (Alfabético / Cantidad)
            scanned_groups = {}
            for code, positions in self.inventory.scanned_items.items():
                if self.inventory.is_qr_code(code):
                    continue
                fam = self.inventory.family_map.get(code, "??")
                scanned_groups[code] = {
                    "fam": fam,
                    "qty": len(positions),
                }

            items = list(scanned_groups.items())
            if sort_mode == "Alfabético":
                items.sort(key=lambda x: x[0])
            elif sort_mode == "Cantidad":
                items.sort(key=lambda x: x[1]["qty"], reverse=True)

            for code, info in items:
                expected = self.inventory.original_quantities.get(code, 0)
                tag_colors = self.inventory.get_row_color(expected, info["qty"])
                row_id = self.scanned_table.insert_data((info["fam"], code, info["qty"]))
                self.scanned_table.set_row_color(row_id, tag_colors)

        self._load_master_table()

    def _toggle_sync(self):
        """Activa/Desactiva sincronización bidireccional."""
        active = not self.sync_active.get()
        self.sync_active.set(active)
        if active:
            self.btn_sync.configure(fg_color="#3a7ebf", text_color="white")
            self.show_toast("Sincronización de Tablas: ACTIVA", mtype="info", duration=2000, use_history=False)
        else:
            self.btn_sync.configure(fg_color=("gray75", "gray25"), text_color=("black", "white"))
            self.show_toast("Sincronización de Tablas: INACTIVA", mtype="info", duration=2000, use_history=False)

    def _on_scanned_select(self, event):
        """Maneja el evento de selección para sincronización."""
        if not self.sync_active.get(): return
        
        selected = self.scanned_table.tree.selection()
        if not selected: return
        
        values = self.scanned_table.tree.item(selected[0], "values")
        if values:
            sku = values[1].replace("▶ ", "").replace("▼ ", "").strip()
            self._sync_to_master(sku)

    def _sync_to_master(self, sku):
        """Busca y enfoca un SKU en el listado maestro."""
        for item in self.master_table.tree.get_children():
            if self.master_table.tree.item(item, "values")[0] == sku:
                self.master_table.tree.selection_set(item)
                self.master_table.tree.see(item)
                break

    def _on_delete_key_event(self, event):
        """Elimina una unidad del código seleccionado."""
        self._on_delete_last_manual()

    def _on_delete_last_manual(self):
        """Lógica de borrado refinada."""
        selected = self.scanned_table.tree.selection()
        
        if not selected:
            messagebox.showwarning("Aviso", "No hay códigos seleccionados para eliminar.")
            return

        values = self.scanned_table.tree.item(selected[0], 'values')
        sku = values[1].replace("▶ ", "").replace("▼ ", "").strip() if values else ""
        is_qr = self.inventory.is_qr_code(sku)
        
        if is_qr:
            msg = f"¿Desea marcar el código QR '{sku}' para su reemplazo? No se puede eliminar directamente para evitar productos huérfanos."
            confirm = messagebox.askyesno("Confirmar Reemplazo de QR", msg)
        else:
            msg = "¿Eliminar una unidad de todos los seleccionados?" if len(selected) > 1 else f"¿Eliminar una unidad de {sku}?"
            confirm = messagebox.askyesno("Confirmar Borrado", msg)
            
        if confirm:
            deleted_count = 0
            waiting_repl = False
            for item_id in selected:
                val = self.scanned_table.tree.item(item_id, "values")
                if val:
                    item_sku = val[1].replace("▶ ", "").replace("▼ ", "").strip()
                    res = self.inventory.delete_last(item_sku)
                    if res == "waiting_replacement":
                        waiting_repl = True
                    elif res:
                        deleted_count += 1
            
            if waiting_repl:
                self.show_toast("Modo reemplazo activo. Escanee el nuevo código QR.", mtype="warning", duration=3000, use_history=False)
                self._update_all_ui()
            elif deleted_count > 0:
                self.show_toast(f"Eliminado: {deleted_count} unidad(es)", mtype="info", duration=1500, use_history=False)
                self._update_all_ui()

    def _load_master_table(self):
        self.master_table.clear()
        for code, desc, qty in self.inventory.stock_data:
            scanned = len(self.inventory.scanned_items.get(code, []))
            item_id = self.master_table.insert_data((code, desc, f"{qty} ({scanned})"))
            self.master_table.set_row_color(item_id, self.inventory.get_row_color(qty, scanned))

    def _on_import_click(self):
        file_path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if file_path:
            if self.inventory.import_csv(file_path, family_filter=self.family_type):
                logger.info("CSV importado: %s", os.path.basename(file_path))
                self._update_all_ui()
                self.images.start_background_download(list(self.inventory.original_quantities.keys()))
                self.status_label.configure(text=f"CSV: {os.path.basename(file_path)} cargado.")
            else:
                logger.error("Falló la importación del CSV: %s", file_path)

    def _on_row_double_click(self, table):
        selected = table.tree.selection()
        if not selected: return
        values = table.tree.item(selected[0], "values")
        if not values: return
        
        if table == self.scanned_table:
            raw_val = values[1]
            code = raw_val.replace("▶ ", "").replace("▼ ", "").strip()
            
            # Si es código QR, alternar plegado / desplegado (V8)
            if self.inventory.is_qr_code(code):
                if code in self.collapsed_containers:
                    self.collapsed_containers.remove(code)
                else:
                    self.collapsed_containers.add(code)
                self._update_all_ui()
                return
            
            self._show_product_image(code)
        elif table == self.master_table:
            code = values[0]
            self._show_product_image(code)
        else:
            # Tabla de auditoría de diferencias (columna 1 es Código/SKU, no columna 0)
            code = values[1] if len(values) > 1 else values[0]
            self._show_product_image(code)


    def _show_product_image(self, sku):
        win_key = f"galeria_{sku}"
        if win_key in self.windows and self.windows[win_key].winfo_exists():
            self.windows[win_key].deiconify()
            self.windows[win_key].focus_force()
            return

        win = ctk.CTkToplevel(self)
        self.windows[win_key] = win
        win.title(f"Galería - {sku}")
        center_window(win, 500, 600)
        win.focus_force()
        win.transient(self)
        
        lbl_info = ctk.CTkLabel(win, text=f"Producto: {sku}", font=("Roboto", 16, "bold"))
        lbl_info.pack(pady=10)
        
        img_container = ctk.CTkFrame(win)
        img_container.pack(expand=True, fill="both", padx=20, pady=10)
        
        placeholder = ctk.CTkLabel(img_container, text="Buscando imágenes...")
        placeholder.pack(expand=True)
        
        lbl_count = ctk.CTkLabel(win, text="", font=("Roboto", 12))
        lbl_count.pack(pady=5)

        navigation_frame = ctk.CTkFrame(win, fg_color="transparent")
        navigation_frame.pack(pady=10)

        # Estado del visor
        state = {"index": 0, "paths": []}

        def update_gallery():
            if not state["paths"]:
                placeholder.configure(text="Imagen no encontrada en la web")
                lbl_count.configure(text="")
                return

            path = state["paths"][state["index"]]
            try:
                img = Image.open(path)
                size = self.inventory.config.get("viewer_size", [400, 400])
                img.thumbnail(size, Image.LANCZOS if hasattr(Image, 'LANCZOS') else Image.ANTIALIAS)
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=size)
                placeholder.configure(image=ctk_img, text="")
                lbl_count.configure(text=f"Imagen {state['index'] + 1} de {len(state['paths'])}")
            except Exception as e:
                placeholder.configure(text=f"Error al cargar imagen:\n{e}")

        def next_img(e=None):
            if state["paths"]:
                state["index"] = (state["index"] + 1) % len(state["paths"])
                update_gallery()

        def prev_img(e=None):
            if state["paths"]:
                state["index"] = (state["index"] - 1) % len(state["paths"])
                update_gallery()

        btn_prev = ctk.CTkButton(navigation_frame, text="◀ Anterior", width=100, command=prev_img)
        btn_prev.pack(side="left", padx=10)

        btn_next = ctk.CTkButton(navigation_frame, text="Siguiente ▶", width=100, command=next_img)
        btn_next.pack(side="left", padx=10)

        win.bind("<Left>", prev_img)
        win.bind("<Right>", next_img)
        win.bind("<Escape>", lambda e: win.destroy())
        placeholder.bind("<Button-1>", next_img)

        def load():
            # Intentar descargar si no existen
            self.images.download_image(sku)
            state["paths"] = self.images.get_local_paths(sku)
            self.after(0, update_gallery)

        threading.Thread(target=load, daemon=True).start()
        ctk.CTkButton(win, text="Cerrar (Esc)", command=win.destroy, width=150, fg_color="#dc3545").pack(pady=15)

    def _on_save_manual(self):
        if self.inventory.save_json("auto_save_v8.json"):
            logger.info("Guardado manual exitoso")
            self.show_toast("Sesión guardada correctamente.", mtype="success", duration=2000, use_history=False)
            self.btn_save.configure(fg_color="#28a745") # Reset a verde
        else:
            logger.error("Falló el guardado manual de la sesión")

    def _on_export_click(self):
        """Paso 1: Selector de familia para exportar (Feedback paridad)."""
        win = ctk.CTkToplevel(self)
        win.title("Exportar - Selección de Familia")
        center_window(win, 400, 350)
        win.transient(self)
        win.grab_set()

        ctk.CTkLabel(win, text="¿Qué familia desea exportar?", font=("Roboto", 16, "bold")).pack(pady=20)
        
        # Familias principales según manual/feedback
        all_options = ["AM", "AO", "AG"]
        
        if self.family_type == "COMBINED":
            options = all_options
        elif self.family_type == "AM_AO":
            options = ["AM", "AO"]
        else:
            options = ["AG"]
        
        for opt in options:
            ctk.CTkButton(win, text=f"Exportar Familia {opt}", 
                         height=40, font=("Roboto", 13, "bold"),
                         command=lambda o=opt: [win.destroy(), self._start_export_flow(o)]).pack(pady=10)

        ctk.CTkButton(win, text="Cancelar", fg_color="gray", command=win.destroy).pack(pady=20)

    def _start_export_flow(self, family):
        """Paso 2: Ventana de progreso y ejecución (Feedback paridad)."""
        # Filtrar códigos por familia
        scans_with_pos = []
        for code, positions in self.inventory.scanned_items.items():
            if code in self._excluded_from_export:
                continue
            # Feedback Matías: AG export bug fix.
            # Convertimos a uppercase y buscamos por prefijo para captar "AG", "AG-xxx", etc.
            item_fam = self.inventory.family_map.get(code, "").upper()
            if item_fam.startswith(family.upper()):
                for pos in positions:
                    scans_with_pos.append((pos, code))
        scans_with_pos.sort() # Orden cronológico de escaneo
        codes_to_send = [s[1] for s in scans_with_pos]

        if not codes_to_send:
            self.show_toast(f"No hay códigos de la familia {family}", mtype="error")
            return

        # Limpiar el evento de parada para una nueva exportación
        self.automation.stop_event.clear()

        # Ventana de Progreso
        prog_win = ctk.CTkToplevel(self)
        prog_win.title(f"Exportando Familia {family}")
        center_window(prog_win, 500, 400)
        prog_win.attributes("-topmost", True)
        
        ctk.CTkLabel(prog_win, text=f"PROCESANDO FAMILIA: {family}", font=("Roboto", 16, "bold")).pack(pady=10)
        
        txt_log = ctk.CTkTextbox(prog_win, height=200)
        txt_log.pack(fill="both", expand=True, padx=20, pady=10)
        
        lbl_status = ctk.CTkLabel(prog_win, text="Preparando...", font=("Roboto", 14))
        lbl_status.pack(pady=5)
        
        btn_stop = ctk.CTkButton(prog_win, text="DETENER (Esc)", fg_color="#dc3545", command=lambda: self.automation.stop_automation())
        btn_stop.pack(pady=10)

        def progress_cb(code, current, total):
            # Efecto de "corte" o procesamiento detallado
            self.after(0, lambda: [
                txt_log.insert("end", f"✂ CORTE [{current}/{total}] FAM: {family} | SKU: {code} ... ENVIADO ✓\n"),
                txt_log.see("end"),
                lbl_status.configure(text=f"Exportando: {current} de {total} (Familia {family})")
            ])

        def run():
            # Cuenta regresiva
            delay = self.inventory.config.get("export_delay_seconds", 10)
            for i in range(delay, 0, -1):
                if self.automation.stop_event.is_set(): break
                self.after(0, lambda v=i: lbl_status.configure(text=f"Iniciando en {v}s..."))
                time.sleep(1)
            
            if not self.automation.stop_event.is_set():
                mode = self.inventory.config.get("paste_mode", "typing")
                try:
                    logger.info("Exportación iniciada: familia=%s, %d códigos, modo=%s", family, len(codes_to_send), mode)
                    self.automation.process_export(codes_to_send, mode=mode, progress_callback=progress_cb)
                    self.after(0, lambda: [lbl_status.configure(text="¡Exportación Finalizada!"), btn_stop.configure(text="Cerrar", fg_color="#28a745", command=prog_win.destroy)])
                    self.show_toast(f"Exportación {family} completa", mtype="success")
                    logger.info("Exportación finalizada: familia=%s (%d códigos)", family, len(codes_to_send))
                except Exception as e:
                    import traceback
                    err_str = traceback.format_exc()
                    logger.exception("Error fatal de exportación (familia=%s)", family)
                    self.after(0, lambda: [
                        txt_log.insert("end", f"\n\n¡ERROR FATAL DE EXPORTACIÓN!\n{err_str}"),
                        txt_log.see("end"),
                        lbl_status.configure(text="¡Exportación Estancada!"),
                        btn_stop.configure(text="Cerrar", fg_color="#dc3545", command=prog_win.destroy)
                    ])

        threading.Thread(target=run, daemon=True).start()

    def _on_open_ext_click(self):
        """Apertura externa del CSV (Feedback paridad)."""
        if hasattr(self.inventory, 'current_csv_path') and os.path.exists(self.inventory.current_csv_path):
            os.startfile(self.inventory.current_csv_path)
        else:
            messagebox.showinfo("Información", "No hay un CSV cargado para abrir.")

    def _toggle_diff_window(self):
        """Alterna abrir/cerrar la ventana de auditoría de diferencias con F3."""
        if "diff" in self.windows and self.windows["diff"].winfo_exists():
            try:
                self.windows["diff"].destroy()
            except:
                pass
        else:
            self._on_diff_click()

    def _refresh_diff_window(self):
        if "diff" in self.windows and self.windows["diff"].winfo_exists() and "diff_table" in self.windows:
            table = self.windows["diff_table"]
            table.clear()
            # 1. Recolectar Faltantes
            for code, desc, expected in self.inventory.stock_data:
                scanned = len(self.inventory.scanned_items.get(code, []))
                if scanned < expected:
                    diff = expected - scanned
                    colors = self.inventory.get_row_color(expected, scanned)
                    no_exp = "☑" if code in self._excluded_from_export else "☐"
                    pos_loc = self.get_possible_location(code)
                    item_id = table.insert_data((no_exp, code, desc, expected, scanned, f"-{diff}", "FALTANTE", pos_loc))
                    table.set_row_color(item_id, colors)

            # 2. Recolectar Sobrantes
            for code, positions in self.inventory.scanned_items.items():
                if self.inventory.is_qr_code(code):
                    continue
                scanned = len(positions)
                expected = self.inventory.original_quantities.get(code, 0)
                if scanned > expected:
                    diff = scanned - expected
                    colors = self.inventory.get_row_color(expected, scanned)
                    no_exp = "☑" if code in self._excluded_from_export else "☐"
                    
                    desc = ""
                    for c, d, e in self.inventory.stock_data:
                        if c == code:
                            desc = d
                            break
                            
                    pos_loc = self.get_possible_location(code)
                    item_id = table.insert_data((no_exp, code, desc, expected, scanned, f"+{diff}", "SOBRANTE", pos_loc))
                    table.set_row_color(item_id, colors)

    def _on_diff_click(self):
        if "diff" in self.windows and self.windows["diff"].winfo_exists():
            self.windows["diff"].deiconify()
            self.windows["diff"].focus_force()
            return

        win = ctk.CTkToplevel(self)
        self.windows["diff"] = win
        win.title("Auditoría de Diferencias (V8.0) - F3: Cerrar | F4: +1 | Supr: -1")
        center_window(win, 1200, 680)
        win.transient(self)
        win.bind("<Escape>", lambda e: win.destroy())
        win.bind("<F3>", lambda e: win.destroy())

        ctk.CTkLabel(win, text="AUDITORÍA GENERAL DE STOCK (V8.0)", font=("Roboto", 18, "bold")).pack(pady=10)
        
        # Tabla Única
        dif_columns = ("No exportar", "Código", "Descripción", "CSV", "Scan", "Dif", "Estado", "Posible Ubicación")
        table = InventoryTable(win, title="FALTANTES Y SOBRANTES", columns=dif_columns, config_manager=self.inventory.config)
        self.windows["diff_table"] = table
        table.pack(expand=True, fill="both", padx=15, pady=5)
        
        # Ajustar anchos de columnas
        table.tree.column("No exportar", width=90, anchor="center")
        table.tree.column("Código", width=120, anchor="center")
        table.tree.column("Descripción", width=250, anchor="w")
        table.tree.column("CSV", width=60, anchor="center")
        table.tree.column("Scan", width=60, anchor="center")
        table.tree.column("Dif", width=60, anchor="center")
        table.tree.column("Estado", width=90, anchor="center")
        table.tree.column("Posible Ubicación", width=180, anchor="center")
        
        table.tree.bind("<Double-1>", lambda e: self._on_row_double_click(table))
        
        def toggle_no_export(event):
            item = table.tree.identify_row(event.y)
            column = table.tree.identify_column(event.x)
            if item and column == "#1":
                values = table.tree.item(item, "values")
                if values:
                    code = values[1]
                    if code in self._excluded_from_export:
                        self._excluded_from_export.remove(code)
                    else:
                        self._excluded_from_export.add(code)
                    self._update_all_ui()
                    
        table.tree.bind("<Button-1>", toggle_no_export)

        def on_diff_f4(event=None):
            # IMPORTANTE: el handler SIEMPRE devuelve "break" para consumir la tecla
            # y cortar la cadena de bindtags (widget -> clase -> toplevel -> all).
            # Sin esto, un binding duplicado en `win` (o de clase) dispararía el
            # handler DOS veces por presión -> +2 unidades en vez de +1.
            selected = table.tree.selection()
            if not selected: return "break"
            values = table.tree.item(selected[0], "values")
            if not values or len(values) < 2: return "break"
            sku = values[1].strip()
            if sku and not self.inventory.is_qr_code(sku):
                self.inventory.add_item(sku)
                self._update_all_ui()
                self.show_toast(f"+1 unidad agregada a escaneo: {sku}", mtype="success", duration=1500, use_history=False)
                for it in table.tree.get_children():
                    v = table.tree.item(it, "values")
                    if v and v[1] == sku:
                        table.tree.selection_set(it)
                        table.tree.see(it)
                        break
            return "break"

        def on_diff_delete(event=None):
            # Misma política que on_diff_f4: consumir la tecla con "break".
            selected = table.tree.selection()
            if not selected: return "break"
            values = table.tree.item(selected[0], "values")
            if not values or len(values) < 2: return "break"
            sku = values[1].strip()
            if sku and not self.inventory.is_qr_code(sku):
                res = self.inventory.delete_last(sku)
                if res:
                    self._update_all_ui()
                    self.show_toast(f"-1 unidad restada de escaneo: {sku}", mtype="info", duration=1500, use_history=False)
                    for it in table.tree.get_children():
                        v = table.tree.item(it, "values")
                        if v and v[1] == sku:
                            table.tree.selection_set(it)
                            table.tree.see(it)
                            break
            return "break"

        # FIX (2026-08-29): se eliminan los bindings duplicados en `win`.
        # Tk propaga el evento por bindtags (widget -> clase -> toplevel -> all):
        # con el foco en el tree, F4/Supr disparaba el handler DOS veces (+2/-2).
        # Los handlers devuelven "break" para consumir la tecla.
        table.tree.bind("<F4>", on_diff_f4)
        table.tree.bind("<Delete>", on_diff_delete)

        self._sync_diff_table()

        footer_diff = ctk.CTkFrame(win, fg_color="transparent")
        footer_diff.pack(pady=10)
        ctk.CTkLabel(footer_diff, text="Atajos: [F4] Sumar +1 a escaneo  |  [Supr] Restar -1 de escaneo  |  [Doble Clic] Ver foto", font=("Roboto", 11)).pack(side="left", padx=15)
        ctk.CTkButton(footer_diff, text="Cerrar (Esc / F3)", command=win.destroy, width=150, fg_color="#dc3545").pack(side="right", padx=15)


    def _open_log_viewer(self):
        """Abre la ventana de consulta del log persistente (solo lectura).

        Maneja: log inexistente, log vacío, archivo grande (solo últimas N
        líneas para no colgar la UI) y errores de lectura.
        """
        from src.logger import read_log_lines, default_log_file

        log_path = default_log_file()
        lines = read_log_lines(log_path)

        win = ctk.CTkToplevel(self)
        self.windows["log_viewer"] = win
        win.title("Ver Log - Stock Cellular Center V8.0")
        center_window(win, 900, 600)
        win.transient(self)
        win.focus_force()

        ctk.CTkLabel(win, text="REGISTRO DE ACTIVIDAD (LOG)", font=("Roboto", 16, "bold")).pack(pady=(10, 2))
        ctk.CTkLabel(win, text=log_path, font=("Roboto", 10), text_color="gray").pack(pady=(0, 5))

        txt_log = ctk.CTkTextbox(win, font=("Consolas", 11))
        txt_log.pack(fill="both", expand=True, padx=15, pady=5)
        txt_log.insert("1.0", "\n".join(lines))
        txt_log.configure(state="disabled")

        footer = ctk.CTkFrame(win, fg_color="transparent")
        footer.pack(pady=10)

        def refresh():
            txt_log.configure(state="normal")
            txt_log.delete("1.0", "end")
            txt_log.insert("1.0", "\n".join(read_log_lines(log_path)))
            txt_log.configure(state="disabled")

        ctk.CTkButton(footer, text="Refrescar", command=refresh, width=120).pack(side="left", padx=10)
        ctk.CTkButton(footer, text="Cerrar", command=win.destroy, width=120, fg_color="#dc3545").pack(side="left", padx=10)

    def _open_options(self):
        if not self.auth.ask_master_password(title="Seguridad V8.0", text="Ingrese Contraseña de Acceso para Opciones:"):
            return

        if "options" in self.windows and self.windows["options"].winfo_exists():
            self.windows["options"].deiconify()
            self.windows["options"].focus_force()
            return

        win = ctk.CTkToplevel(self)
        self.windows["options"] = win
        win.title("Opciones Avanzadas - Stock Cellular Center V8.0")
        center_window(win, 650, 680) # Changed from 650x950 to fit 1366x768
        win.transient(self)
        win.focus_force()
        
        container = ctk.CTkScrollableFrame(win, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=10, pady=10)
        
        # --- APARIENCIA Y TEMAS ---
        ctk.CTkLabel(container, text="APARIENCIA Y TEMAS", font=("Roboto", 16, "bold"), text_color="#3498db").pack(pady=(10, 5))
        
        theme_frame = ctk.CTkFrame(container)
        theme_frame.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(theme_frame, text="Tema Global:").grid(row=0, column=0, padx=10, pady=10)
        theme_var = ctk.StringVar(value=self.inventory.config.get("theme", "dark"))
        theme_opt = ctk.CTkOptionMenu(theme_frame, values=["dark", "light", "system"], variable=theme_var)
        theme_opt.grid(row=0, column=1, padx=10, pady=10)

        # --- COLORES DE TABLA ---
        ctk.CTkLabel(container, text="COLORES DE TABLAS (Haz click para elegir)", font=("Roboto", 14, "bold")).pack(pady=(15, 5))
        
        colors_frame = ctk.CTkFrame(container)
        colors_frame.pack(fill="x", padx=10, pady=5)
        
        color_entries = {}
        row = 0
        color_params = [
            ("table_bg", "Fondo Base Tabla"),
            ("table_fg", "Fuente Base Tabla"),
            ("row_finished_bg", "Fondo Completado"),
            ("row_finished_fg", "Fuente Completado"),
            ("row_pending_bg", "Fondo Pendiente"),
            ("row_pending_fg", "Fuente Pendiente"),
            ("row_excess_bg", "Fondo Sobrante"),
            ("row_excess_fg", "Fuente Sobrante"),
            ("row_unknown_bg", "Fondo Desconocido"),
            ("row_unknown_fg", "Fuente Desconocido")
        ]
        
        def pick_color(var_name, var_ref):
            current = var_ref.get()
            color = askcolor(color=current, title=f"Elegir Color: {var_name}")
            if color[1]:
                var_ref.set(color[1])

        def update_preview_color(var, label):
            color = var.get().strip()
            if color.startswith("#") and (len(color) == 7 or len(color) == 4):
                try:
                    label.configure(fg_color=color)
                except:
                    pass

        for key, label_text in color_params:
            ctk.CTkLabel(colors_frame, text=label_text).grid(row=row, column=0, padx=10, pady=5, sticky="w")
            
            var = ctk.StringVar(value=self.inventory.config.get(key, ""))
            entry = ctk.CTkEntry(colors_frame, width=120, textvariable=var)
            entry.grid(row=row, column=1, padx=10, pady=5)
            
            # Recuadro de Previsualización (Feedback Matías)
            preview = ctk.CTkLabel(colors_frame, text="", width=40, height=25, 
                                 fg_color=var.get() if var.get() else "transparent", 
                                 corner_radius=4)
            preview.grid(row=row, column=2, padx=10, pady=5)
            
            # Vincular actualización de color
            var.trace_add("write", lambda *args, v=var, p=preview: update_preview_color(v, p))
            
            # Vincular click al selector visual
            entry.bind("<Button-1>", lambda e, k=key, v=var: pick_color(k, v))
            color_entries[key] = var
            row += 1

        # MODO DE EXPORTACIÓN
        ctk.CTkLabel(container, text="MODO DE EXPORTACIÓN", font=("Roboto", 14, "bold")).pack(pady=(20, 5))
        paste_var = ctk.StringVar(value=self.inventory.config.get("paste_mode", "typing"))
        ctk.CTkRadioButton(container, text="Tecleo Carácter por Carácter", variable=paste_var, value="typing").pack(pady=5)
        ctk.CTkRadioButton(container, text="Pegado Rápido (Control+V)", variable=paste_var, value="clipboard").pack(pady=5)
        
        # VELOCIDAD
        ctk.CTkLabel(container, text="VELOCIDAD DE EXPORTACIÓN", font=("Roboto", 14, "bold")).pack(pady=(20, 5))
        speed_var = ctk.DoubleVar(value=self.inventory.config.get("speed_multiplier", 1.0))
        lbl_speed = ctk.CTkLabel(container, text=f"Velocidad actual: {speed_var.get():.1f}x", font=("Roboto", 12))
        lbl_speed.pack(pady=2)
        slider_speed = ctk.CTkSlider(container, from_=0.1, to=10.0, number_of_steps=99, variable=speed_var, command=lambda v: lbl_speed.configure(text=f"Velocidad actual: {v:.1f}x"))
        slider_speed.pack(pady=5)
        
        # EXCLUSIÓN Y LICENCIA
        ctk.CTkLabel(container, text="LISTA DE EXCLUSIÓN Y LICENCIA", font=("Roboto", 14, "bold")).pack(pady=(20, 5))
        
        excl_frame = ctk.CTkFrame(container)
        excl_frame.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(excl_frame, text="SKUs a Ignorar (uno por línea):").pack(pady=5)
        text_excl = ctk.CTkTextbox(excl_frame, height=100)
        text_excl.pack(fill="x", padx=10, pady=5)
        # Cargar lista actual
        current_excl = self.inventory.config.get("excluded_skus", [])
        text_excl.insert("1.0", "\n".join(current_excl))

        # DETECTOR DE PROXIMIDAD
        ctk.CTkLabel(container, text="DETECTOR DE PROXIMIDAD INTELIGENTE", font=("Roboto", 14, "bold"), text_color="#28a745").pack(pady=(20, 5))
        
        prox_frame = ctk.CTkFrame(container)
        prox_frame.pack(fill="x", padx=10, pady=5)
        
        # Ventanas y Umbrales vinculados lógicamente
        def update_win(v):
            val_win = int(v)
            lbl_win.configure(text=f"Actual: {val_win}")
            if thresh_var.get() > val_win:
                thresh_var.set(val_win)
                lbl_thresh.configure(text=f"Actual: {val_win}")

        def update_thresh(v):
            val_thresh = int(v)
            if val_thresh > win_var.get():
                # Forzar a retroceder
                slider_thresh.set(win_var.get())
                lbl_thresh.configure(text=f"Actual: {win_var.get()}")
            else:
                lbl_thresh.configure(text=f"Actual: {val_thresh}")

        ctk.CTkLabel(prox_frame, text="Cantidad de códigos a verificar (antes y después):").pack(pady=(5,0))
        win_var = ctk.IntVar(value=self.inventory.config.get("proximity_window", 50))
        lbl_win = ctk.CTkLabel(prox_frame, text=f"Actual: {win_var.get()}", font=("Roboto", 12))
        lbl_win.pack(pady=2)
        slider_win = ctk.CTkSlider(prox_frame, from_=10, to=100, number_of_steps=90, variable=win_var, command=update_win)
        slider_win.pack(pady=5)

        ctk.CTkLabel(prox_frame, text="Cantidad de coincidencias:").pack(pady=(10,0))
        thresh_var = ctk.IntVar(value=self.inventory.config.get("proximity_threshold", 30))
        lbl_thresh = ctk.CTkLabel(prox_frame, text=f"Actual: {thresh_var.get()}", font=("Roboto", 12))
        lbl_thresh.pack(pady=2)
        slider_thresh = ctk.CTkSlider(prox_frame, from_=5, to=100, number_of_steps=95, variable=thresh_var, command=update_thresh)
        slider_thresh.pack(pady=5)

        # --- GESTOR DE IMÁGENES --- (Feedback Matías: Restaurado acceso a carpeta)
        ctk.CTkLabel(container, text="GESTOR DE IMÁGENES", font=("Roboto", 14, "bold"), text_color="#3498db").pack(pady=(20, 5))
        
        img_actions_frame = ctk.CTkFrame(container)
        img_actions_frame.pack(fill="x", padx=10, pady=5)
        
        def open_img_folder():
            folder = self.images.img_folder
            if os.path.exists(folder):
                os.startfile(os.path.abspath(folder))
            else:
                self.show_toast("La carpeta de imágenes aún no existe.", mtype="warning")

        ctk.CTkButton(img_actions_frame, text="Abrir Carpeta de Imágenes", command=open_img_folder, fg_color="#6c757d").pack(pady=10, padx=20, fill="x")

        ctk.CTkLabel(container, text="DESCARGAS DE IMAGEN FALLIDAS:", font=("Roboto", 14, "bold"), text_color="#e67e22").pack(pady=(20, 5))
        txt_failed = ctk.CTkTextbox(container, width=450, height=100)
        txt_failed.pack(pady=5)
        txt_failed.insert("1.0", "\n".join(self.images.failed_skus) if self.images.failed_skus else "No hay fallos registrados.")
        txt_failed.configure(state="disabled")

        ctk.CTkButton(container, text="RENOVAR LICENCIA (30 días)", command=self.auth.show_renewal_window, fg_color="#6f42c1").pack(pady=10)

        # --- VER LOG --- (Logging persistente V8)
        ctk.CTkLabel(container, text="REGISTRO DE ACTIVIDAD (LOG)", font=("Roboto", 14, "bold"), text_color="#17a2b8").pack(pady=(20, 5))
        log_actions_frame = ctk.CTkFrame(container)
        log_actions_frame.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(log_actions_frame, text="El log acumula eventos y errores entre sesiones (solo lectura).", font=("Roboto", 11), text_color="gray").pack(pady=5)
        ctk.CTkButton(log_actions_frame, text="Ver Log", command=self._open_log_viewer, fg_color="#17a2b8").pack(pady=10, padx=20, fill="x")

        def save_all():
            # Actualizar config de manera integral
            self.inventory.config["theme"] = theme_var.get()
            self.inventory.config["paste_mode"] = paste_var.get()
            self.inventory.config["speed_multiplier"] = round(speed_var.get(), 1)
            self.inventory.config["proximity_window"] = win_var.get()
            self.inventory.config["proximity_threshold"] = thresh_var.get()
            
            # Actualizar exclusiones
            new_excl = [s.strip().upper() for s in text_excl.get("1.0", "end").split("\n") if s.strip()]
            self.inventory.config["excluded_skus"] = new_excl
            
            # Actualizar colores
            for key, var in color_entries.items():
                val = var.get().strip()
                if val.startswith("#") and len(val) == 7:
                    self.inventory.config[key] = val
            
            # Guardar en disco
            save_config(self.inventory.config)
            
            # Aplicar cambios en vivo (Feedback Matías)
            ctk.set_appearance_mode(theme_var.get())
            self.inventory.refresh_data() # Recalcula el total esperado sin ignorados
            self.scanned_table.refresh_styles()
            self.master_table.refresh_styles()
            self._update_all_ui()
            
            self.show_toast("Configuración guardada y aplicada.", mtype="success", use_history=False)
            win.destroy()
        
        # Un solo botón centrado al final de la ventana
        ctk.CTkButton(win, text="GUARDAR CAMBIOS Y APLICAR", command=save_all, 
                     fg_color="#28a745", hover_color="#218838", height=45, font=("Roboto", 14, "bold")).pack(pady=20)

    def show_toast(self, message, mtype="info", duration=3000, use_history=True, pos=None, master=None, metadata=None):
        """Muestra una notificación flotante. Intento definitivo de bordes limpios."""
        if use_history:
            self.toast_history.append({
                "time": time.strftime("%H:%M:%S"),
                "msg": message,
                "type": mtype,
                "metadata": metadata,
                "resolved": False
            })
            if hasattr(self, 'btn_toast_hist'):
                self.btn_toast_hist.configure(fg_color="#dc3545")
        
        colors = {"info": "#3a7ebf", "warning": "#e67e22", "error": "#dc3545", "success": "#28a745"}
        color = colors.get(mtype, "#333")
        
        w, h = (400, 80) if duration > 5000 else (240, 50)
        target = master if master else self
        parent_bg = getattr(target, 'cget', lambda x: self.cget("bg"))("bg") if hasattr(target, 'cget') else self.cget("bg")
        
        # ELIMINACIÓN TOTAL DE BORDES: Usamos corner_radius=0 para evitar aliasing blanco en las esquinas
        toast = ctk.CTkFrame(target, fg_color=color, bg_color=parent_bg, corner_radius=0, border_width=0)
        
        # Etiqueta de texto (Standard Tk para control total)
        lbl = tk.Label(toast, text=message, fg="white", bg=color,
                       padx=20, pady=10,
                       font=("Roboto", 14 if duration > 5000 else 11, "bold"),
                       wraplength=w-20, justify="center")
        
        if duration > 5000:
            lbl.pack(side="left", expand=True)
            def remove():
                if toast in self.current_toasts:
                    self.current_toasts.remove(toast)
                    try: toast.destroy()
                    except: pass
            btn_close = ctk.CTkButton(toast, text="✕", width=30, height=30, 
                                     fg_color="transparent", bg_color=color, hover_color="#888888",
                                     command=remove)
            btn_close.pack(side="right", padx=10)
        else:
            lbl.pack(expand=True, fill="both")

        # Posicionamiento exacto
        if pos:
            px, py = pos
        else:
            px = self.winfo_width() - w - 20
            py = self.winfo_height() - h - 20 - (len([t for t in self.current_toasts if t.winfo_exists()]) * (h + 10))
        
        toast.configure(width=w, height=h)
        toast.place(x=px, y=py)
        self.current_toasts.append(toast)

        def remove_auto():
            if toast in self.current_toasts:
                self.current_toasts.remove(toast)
                try: toast.destroy()
                except: pass
        self.after(duration, remove_auto)

    def _open_toast_history(self):
        # Resetear color de campana
        if hasattr(self, 'btn_toast_hist'):
            self.btn_toast_hist.configure(fg_color="transparent")

        win = ctk.CTkToplevel(self)
        win.title("Historial de Notificaciones")
        center_window(win, 450, 500)
        win.transient(self)
        
        ctk.CTkLabel(win, text="HISTORIAL DE MÁS GUARDADOS", font=("Roboto", 16, "bold")).pack(pady=10)
        
        scroll = ctk.CTkScrollableFrame(win)
        scroll.pack(expand=True, fill="both", padx=10, pady=5)
        
        # Filtrar solo alertas no resueltas
        active_alerts = [item for item in self.toast_history if not item.get("resolved")]
        
        if not active_alerts:
            ctk.CTkLabel(scroll, text="No hay alertas de mal guardado pendientes.").pack(pady=20)
        else:
            header_frame = ctk.CTkFrame(win, fg_color="transparent")
            header_frame.pack(fill="x", padx=10, pady=5)
            ctk.CTkButton(header_frame, text="🗑 Limpiar Todo", width=120, fg_color="#dc3545", command=lambda: [self.toast_history.clear(), win.destroy(), self._open_toast_history()]).pack(side="right")
            
            for item in reversed(active_alerts):
                f = ctk.CTkFrame(scroll, fg_color=("#f0f0f0", "#2b2b2b"), cursor="hand2")
                f.pack(fill="x", pady=2, padx=5)
                
                t_color = "#dc3545" # Alertas de mal guardado son rojas
                ctk.CTkLabel(f, text=f"[{item['time']}]", font=("Roboto", 10, "bold"), text_color="gray").pack(side="left", padx=5)
                
                lbl_msg = ctk.CTkLabel(f, text=item["msg"], font=("Roboto", 11, "bold"), text_color=t_color, wraplength=330, justify="left")
                lbl_msg.pack(side="left", padx=5, fill="x", expand=True)
                
                # Hacer la fila interactiva para resolver el mal guardado
                lbl_msg.bind("<Button-1>", lambda e, it=item, w=win: self._resolve_wrong_placement(it, w))
                f.bind("<Button-1>", lambda e, it=item, w=win: self._resolve_wrong_placement(it, w))

    def _focus_search(self):
        """Búsqueda interactiva con iteración (Enter)."""
        win = ctk.CTkToplevel(self)
        win.title("Buscar Código (SKU)")
        center_window(win, 450, 160)
        win.transient(self)
        win.focus_force()
        win.bind("<Escape>", lambda e: win.destroy())

        ctk.CTkLabel(win, text="Ingrese el código a buscar:", font=("Roboto", 12)).pack(pady=5)
        entry = ctk.CTkEntry(win, width=300)
        entry.pack(pady=5)
        entry.focus_set()
        win.after(100, entry.focus_set)
        
        lbl_info = ctk.CTkLabel(win, text="", font=("Roboto", 12))
        lbl_info.pack(pady=(2, 5))

        state = {"matches": [], "index": 0, "last_query": ""}

        def do_search(e=None):
            query = entry.get().strip().upper()
            if not query: return

            if query != state["last_query"]:
                # Recolectar posiciones reales de escaneo (ordenadas de mayor a menor)
                scan_positions = sorted(self.inventory.scanned_items.get(query, []), reverse=True)
                
                # Buscar en la tabla maestra (CSV)
                m_item = None
                m_idx = -1
                for idx, (code, desc, qty) in enumerate(self.inventory.stock_data):
                    if code == query:
                        m_idx = idx + 1 # Fila 1-indexed
                        # Buscar su item_id en el árbol gráfico
                        for child in self.master_table.tree.get_children():
                            if self.master_table.tree.item(child, 'values')[0] == query:
                                m_item = child
                                break
                        break

                # Buscar item_id en la tabla agrupada de escaneos
                s_items = []
                for child in self.scanned_table.tree.get_children():
                    if self.scanned_table.tree.item(child, 'values')[1] == query:
                        s_items.append(child)
                
                # Construir timeline de resultados
                matches = []
                for i, p in enumerate(scan_positions):
                    current_s_item = s_items[i] if i < len(s_items) else (s_items[0] if s_items else None)
                    matches.append({"type": "scanned", "pos": p, "s_item": current_s_item, "m_item": m_item})
                
                if m_item:
                    matches.append({"type": "master", "pos": m_idx, "s_item": s_items[0] if s_items else None, "m_item": m_item})

                state["matches"] = matches
                state["index"] = 0
                state["last_query"] = query
            
            if state["matches"]:
                match = state["matches"][state["index"]]
                
                # Resaltar en ambas tablas si el item existe (y limpiar previas selecciones)
                if match["s_item"]:
                    self.scanned_table.tree.selection_set(match["s_item"])
                    self.scanned_table.tree.see(match["s_item"])
                
                if match["m_item"]:
                    self.master_table.tree.selection_set(match["m_item"])
                    self.master_table.tree.see(match["m_item"])

                if match["type"] == "scanned":
                    origin = "Orden de Escaneo"
                    msg = f"Escaneado en posición {match['pos']}"
                else:
                    origin = "Inventario CSV"
                    msg = f"Fila en Maestro: {match['pos']}"

                self.status_label.configure(text=f"Buscando '{query}': {state['index']+1} de {len(state['matches'])} (en {origin})")
                lbl_info.configure(text=f"[{state['index']+1}/{len(state['matches'])}] {msg}", text_color="#28a745")
                
                state["index"] = (state["index"] + 1) % len(state["matches"])
                if len(state["matches"]) > 1:
                    btn_find.configure(text=f"Siguiente ({state['index']+1}/{len(state['matches'])})")
            else:
                # Fallback genérico para búsquedas parciales
                m_matches = []
                for item in self.master_table.tree.get_children():
                    if query in str(self.master_table.tree.item(item, 'values')[0]):
                        m_matches.append(item)
                
                if m_matches:
                    target = m_matches[0]
                    self.master_table.tree.selection_set(target)
                    self.master_table.tree.see(target)
                    self.status_label.configure(text=f"Encontrado resultado parcial para '{query}'")
                    lbl_info.configure(text=f"Resultado parcial encontrado", text_color="#3a7ebf")
                else:
                    self.status_label.configure(text=f"No se encontraron coincidencias para '{query}'")
                    lbl_info.configure(text=f"Código '{query}' no encontrado", text_color="#dc3545")
                btn_find.configure(text="Buscar")

        entry.bind("<Return>", do_search)
        btn_find = ctk.CTkButton(win, text="Buscar / Siguiente", command=do_search)
        btn_find.pack(pady=10)

    def _move_selected_up(self):
        if self.sort_var.get() not in ("Último arriba", "Último abajo"):
            self.show_toast("Reordenamiento solo disponible en modos secuenciales.", mtype="warning", duration=2000, use_history=False)
            return
        selected = self.scanned_table.tree.selection()
        if not selected: return
        
        item_id = selected[0]
        pos_str = self.scanned_table.tree.item(item_id, "text")
        if not pos_str: return
        pos = int(pos_str)
        idx = pos - 1
        
        reverse = self.sort_var.get() == "Último arriba"
        target = idx + 1 if reverse else idx - 1
        
        if 0 <= target < len(self.inventory.scan_sequence):
            if self.inventory.move_item_in_sequence(idx, target):
                self._update_all_ui()
                new_pos_str = str(target + 1)
                for item in self.scanned_table.tree.get_children():
                    if self.scanned_table.tree.item(item, "text") == new_pos_str:
                        self.scanned_table.tree.selection_set(item)
                        self.scanned_table.tree.see(item)
                        break
                moved_sku = self.inventory.scan_sequence[target]
                self._schedule_deferred_validation(moved_sku, target + 1)

    def _move_selected_down(self):
        if self.sort_var.get() not in ("Último arriba", "Último abajo"):
            self.show_toast("Reordenamiento solo disponible en modos secuenciales.", mtype="warning", duration=2000, use_history=False)
            return
        selected = self.scanned_table.tree.selection()
        if not selected: return
        
        item_id = selected[0]
        pos_str = self.scanned_table.tree.item(item_id, "text")
        if not pos_str: return
        pos = int(pos_str)
        idx = pos - 1
        
        reverse = self.sort_var.get() == "Último arriba"
        target = idx - 1 if reverse else idx + 1
        
        if 0 <= target < len(self.inventory.scan_sequence):
            if self.inventory.move_item_in_sequence(idx, target):
                self._update_all_ui()
                new_pos_str = str(target + 1)
                for item in self.scanned_table.tree.get_children():
                    if self.scanned_table.tree.item(item, "text") == new_pos_str:
                        self.scanned_table.tree.selection_set(item)
                        self.scanned_table.tree.see(item)
                        break
                moved_sku = self.inventory.scan_sequence[target]
                self._schedule_deferred_validation(moved_sku, target + 1)

    def _setup_drag_and_drop(self):
        tree = self.scanned_table.tree
        self._drag_item = None
        
        # Cargar configuración de color para el target
        tree.tag_configure("drag_target", background="#0078d7", foreground="white")
        
        def on_press(event):
            if self.sort_var.get() not in ("Último arriba", "Último abajo"):
                return
            item = tree.identify_row(event.y)
            if item:
                self._drag_item = item
        
        def on_motion(event):
            if not self._drag_item:
                return
            
            # Autoscroll al arrastrar cerca de los bordes
            h = tree.winfo_height()
            if event.y < 20:
                tree.yview_scroll(-1, "units")
            elif event.y > h - 20:
                tree.yview_scroll(1, "units")
                
            # Identificar target actual
            target = tree.identify_row(event.y)
            
            # Limpiar etiquetas anteriores de todos los elementos
            for item in tree.get_children():
                tags = list(tree.item(item, "tags") or [])
                if "drag_target" in tags:
                    tags.remove("drag_target")
                    tree.item(item, tags=tags)
            
            # Añadir etiqueta drag_target al elemento bajo el cursor
            if target and target != self._drag_item:
                tags = list(tree.item(target, "tags") or [])
                if "drag_target" not in tags:
                    tags.append("drag_target")
                    tree.item(target, tags=tags)
                
        def on_release(event):
            if not self._drag_item:
                return
            
            # Limpiar etiquetas de arrastre
            for item in tree.get_children():
                tags = list(tree.item(item, "tags") or [])
                if "drag_target" in tags:
                    tags.remove("drag_target")
                    tree.item(item, tags=tags)
            
            target = tree.identify_row(event.y)
            if target and target != self._drag_item:
                src_pos_str = tree.item(self._drag_item, "text")
                dst_pos_str = tree.item(target, "text")
                if src_pos_str and dst_pos_str:
                    src_idx = int(src_pos_str) - 1
                    dst_idx = int(dst_pos_str) - 1
                    
                    if self.inventory.move_item_in_sequence(src_idx, dst_idx):
                        self._update_all_ui()
                        # Re-seleccionar el ítem movido
                        new_pos_str = str(dst_idx + 1)
                        for item in tree.get_children():
                            if tree.item(item, "text") == new_pos_str:
                                tree.selection_set(item)
                                tree.see(item)
                                break
                        moved_sku = self.inventory.scan_sequence[dst_idx]
                        self._schedule_deferred_validation(moved_sku, dst_idx + 1)
            self._drag_item = None


        tree.bind("<ButtonPress-1>", on_press, add="+")
        tree.bind("<B1-Motion>", on_motion, add="+")
        tree.bind("<ButtonRelease-1>", on_release, add="+")

    def _schedule_deferred_validation(self, sku, pos):
        """Programa la validación de ubicación diferida (5s debounced) al mover o soltar un producto."""
        if hasattr(self, '_deferred_val_timer') and self._deferred_val_timer:
            self.after_cancel(self._deferred_val_timer)
        
        delay_sec = self.inventory.config.get("location_validation_delay", 5)
        delay_ms = int(delay_sec * 1000)
        self._deferred_val_timer = self.after(delay_ms, self._run_deferred_validation, sku, pos)

    def _run_deferred_validation(self, sku, pos):
        self._deferred_val_timer = None
        if not sku or self.inventory.is_qr_code(sku):
            return
        prox_result = self.inventory.check_proximity(sku, pos)
        if prox_result:
            self.show_toast(
                f"¡Cuidado! {sku} fuera de orden inmediato. Ubicado en {prox_result['current_container']} pero pertenece a {prox_result['expected_container']}",
                mtype="error", duration=10000, use_history=True, metadata=prox_result
            )

    def _resolve_wrong_placement(self, item, history_window):
        meta = item["metadata"]
        sku = meta["sku"]
        pos = meta["pos"]
        curr_c = meta["current_container"]
        exp_c = meta["expected_container"]
        
        # 1. Seleccionar el producto en el listado de escaneados en modo "Último arriba"
        self.sort_var.set("Último arriba")
        self._update_all_ui()
        
        pos_str = str(pos)
        for row in self.scanned_table.tree.get_children():
            if self.scanned_table.tree.item(row, "text") == pos_str:
                self.scanned_table.tree.selection_set(row)
                self.scanned_table.tree.see(row)
                break
                
        # 2. Abrir diálogo de confirmación emergente
        dialog = ctk.CTkToplevel(self)
        dialog.title("Resolución de Ubicación")
        center_window(dialog, 500, 200)
        dialog.transient(self)
        dialog.focus_force()
        dialog.grab_set()
        
        msg = f"El producto {sku} fue escaneado en\n{curr_c} pero pertenece a {exp_c}.\n\n¿Desea moverlo a su contenedor o confirmar nueva ubicación?"
        ctk.CTkLabel(dialog, text=msg, font=("Roboto", 12, "bold"), justify="center").pack(pady=20)
        
        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=10)
        
        def on_move():
            if self.inventory.move_product_to_container(sku, pos, exp_c):
                item["resolved"] = True
                if item in self.toast_history:
                    self.toast_history.remove(item)
                self.show_toast(f"Producto movido a {exp_c}", mtype="success", duration=2000, use_history=False)
            dialog.destroy()
            if history_window.winfo_exists():
                history_window.destroy()
                self._open_toast_history()
            self._update_all_ui()
            
        def on_keep():
            # Actualizar explícitamente en main_stock.json la nueva ubicación confirmada
            self.inventory.update_product_location(sku, curr_c)
            item["resolved"] = True
            if item in self.toast_history:
                self.toast_history.remove(item)
            self.show_toast(f"Ubicación confirmada en {curr_c} y actualizada en main_stock.json", mtype="info", duration=2500, use_history=False)
            dialog.destroy()
            if history_window.winfo_exists():
                history_window.destroy()
                self._open_toast_history()
            self._update_all_ui()

        ctk.CTkButton(btn_frame, text="Mover a su lugar", fg_color="#28a745", command=on_move, width=150).pack(side="left", padx=10)
        ctk.CTkButton(btn_frame, text="Dejar aquí (Guardar)", fg_color="#3a7ebf", command=on_keep, width=150).pack(side="left", padx=10)

    def get_possible_location(self, sku):
        """Busca la última ubicación conocida del SKU en el historial."""
        for hist_file in sorted(self.inventory.historical_sequences.keys(), key=os.path.getmtime, reverse=True):
            hist_seq = self.inventory.historical_sequences[hist_file]
            for i, code in enumerate(hist_seq):
                if code == sku:
                    h_box, h_sec = self.inventory.get_containers_for_index(hist_seq, i)
                    h_container = h_box if h_box else (h_sec if h_sec else None)
                    if h_container:
                        return h_container
        return "Desconocida"

if __name__ == "__main__":
    app = StockApp()
    app.mainloop()
