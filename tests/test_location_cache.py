"""
Tests de caché de ubicaciones y resolver condicional — Stock Cellular Center V8.0

Cubre las propuestas 1+2 de la auditoría de rendimiento (2026-08-29):
  1. Caché/memoización de ubicación (get_expected_container_for_sku y
     get_possible_location) para no re-recorrer el historial en búsquedas
     repetidas del mismo SKU.
  2. _diff_view NO ejecuta el resolver de ubicaciones cuando la ventana de
     Diferencias está cerrada (resolver=None), manteniendo el comportamiento
     previo cuando está abierta.

Verifica de forma DETERMINISTA (contadores/mocks, sin umbrales de tiempo):
  - búsqueda repetida del mismo SKU no vuelve a recorrer el historial;
  - invalidación de caché al cambiar main_stock y al reindexar historial;
  - build_full_view con resolver=None no llama al resolver;
  - con Diferencias abierta las ubicaciones se siguen calculando;
  - la vista incremental sigue siendo equivalente al rebuild completo;
  - 100 escaneos no generan cantidades incorrectas ni eventos duplicados;
  - el patrón "filas x archivos x posiciones" ya no ocurre (caché caliente).
"""

import os
import sys
import json
import glob
import time
import queue
import shutil
import tempfile
import threading
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.inventory import InventoryManager
from src.core.scanpipeline import ScanWorker, compute_scan_alerts
from src.gui.updates import build_full_view, apply_event, ViewOptions, diff_views, SORT_LAST_TOP


def make_inv(scan_dir=None, catalog_n=600):
    cfg = {"excluded_skus": [], "list_order": "bottom"}
    inv = InventoryManager(cfg)
    if scan_dir:
        inv.scan_dir = scan_dir
        inv.main_stock_file = os.path.join(scan_dir, "main_stock.json")
    inv.main_stock = inv.load_main_stock()
    inv.stock_data = [(f"SKU{i:04d}", f"Desc {i}", 1) for i in range(catalog_n)]
    inv.original_quantities = {f"SKU{i:04d}": 1 for i in range(catalog_n)}
    inv.full_family_map = {f"SKU{i:04d}": "AM" for i in range(catalog_n)}
    inv.family_map = dict(inv.full_family_map)
    return inv


def build_history(hist_dir, n_files, codes_per_file, sku_occ=1, sku=None):
    """Crea archivos JSON de historial en un dir temporal (fuera del repo).

    Cada archivo empieza con el contenedor '@CAJA_H' para que la búsqueda de
    ubicaciones encuentre un contenedor real (get_containers_for_index busca
    QRs @/%/# hacia atrás).
    """
    os.makedirs(hist_dir, exist_ok=True)
    for f in glob.glob(os.path.join(hist_dir, "*.json")):
        os.remove(f)
    for fi in range(n_files):
        seq = ["@CAJA_H"] + [f"SKU{i:04d}" for i in range(codes_per_file)]
        if sku and sku_occ > 0:
            seq += [sku] * sku_occ
        with open(os.path.join(hist_dir, f"hist_{fi}.json"), "w", encoding="utf-8") as fh:
            json.dump({"scan_sequence": seq}, fh)


class LocationCacheTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hist_dir = os.path.join(self.tmp, "hist")
        self.inv = make_inv(scan_dir=self.hist_dir)

    def _index_history_sync(self):
        # indexación síncrona y determinista (el thread de fondo del init
        # no interfiere: _index_history pisa historical_sequences)
        self.inv._index_history()


class TestHistoryCache(LocationCacheTestBase):
    def test_repeated_sku_does_not_rescan_history(self):
        """Requisito 1: búsqueda repetida del mismo SKU no vuelve a recorrer el historial."""
        build_history(self.hist_dir, 5, 500, sku_occ=3, sku="SKU0042")
        self._index_history_sync()

        orig = self.inv._history_location
        calls = {"n": 0}
        def counting(sku):
            calls["n"] += 1
            return orig(sku)
        self.inv._history_location = counting

        # Primera llamada: recorre el historial (1 vez)
        r1 = self.inv.get_expected_container_for_sku("SKU0042")
        self.assertIsNotNone(r1)
        self.assertEqual(calls["n"], 1, "la primera búsqueda debe recorrer el historial 1 vez")

        # Llamadas repetidas: caché caliente, cero recorridos nuevos
        for _ in range(10):
            self.assertEqual(self.inv.get_expected_container_for_sku("SKU0042"), r1)
        self.assertEqual(calls["n"], 1, "las búsquedas repetidas NO deben re-recorrer el historial")

    def test_miss_is_cached_too(self):
        """Un miss (SKU inexistente) también se cachea: no re-escanea el historial."""
        build_history(self.hist_dir, 5, 500)
        self._index_history_sync()
        orig = self.inv._history_location
        calls = {"n": 0}
        def counting(sku):
            calls["n"] += 1
            return orig(sku)
        self.inv._history_location = counting

        self.assertIsNone(self.inv.get_expected_container_for_sku("SKU9999"))
        self.assertIsNone(self.inv.get_expected_container_for_sku("SKU9999"))
        self.assertEqual(calls["n"], 1, "el miss repetido no debe re-escancar el historial")

    def test_cache_invalidated_on_main_stock_change(self):
        """Requisito 2: la caché se invalida cuando cambia main_stock."""
        build_history(self.hist_dir, 2, 100, sku_occ=1, sku="SKU0001")
        self._index_history_sync()
        # SKU0001 aparece en historial -> ubicación del historial (@CAJA_H)
        self.assertEqual(self.inv.get_expected_container_for_sku("SKU0001"), "@CAJA_H")

        # Cambiar la fuente de verdad: main_stock ahora dice otra cosa
        self.inv.update_product_location("SKU0001", "@CAJA_NUEVA", update_file=False)
        self.assertEqual(
            self.inv.get_expected_container_for_sku("SKU0001"),
            "@CAJA_NUEVA",
            "tras invalidar, la caché debe devolver el valor nuevo de main_stock",
        )

    def test_cache_invalidated_on_history_reindex(self):
        """La caché se invalida al reindexar el historial (miss -> hit)."""
        build_history(self.hist_dir, 1, 100)  # SKU9999 NO está
        self._index_history_sync()
        self.assertIsNone(self.inv.get_expected_container_for_sku("SKU9999"))

        # Nuevo historial que SÍ contiene SKU9999 con contenedor
        seq = [f"SKU{i:04d}" for i in range(100)] + ["@CAJA_H", "SKU9999"]
        with open(os.path.join(self.hist_dir, "hist_new.json"), "w", encoding="utf-8") as fh:
            json.dump({"scan_sequence": seq}, fh)
        self._index_history_sync()

        loc = self.inv.get_expected_container_for_sku("SKU9999")
        self.assertEqual(loc, "@CAJA_H", "tras reindexar, el miss cacheado debe re-resolverse")

    def test_possible_location_cached_and_equivalent(self):
        """get_possible_location (usado por la diff abierta) usa la misma caché."""
        build_history(self.hist_dir, 3, 200, sku_occ=2, sku="SKU0042")
        self._index_history_sync()
        orig = self.inv._history_location
        calls = {"n": 0}
        def counting(sku):
            calls["n"] += 1
            return orig(sku)
        self.inv._history_location = counting

        # Replica del contrato de StockApp.get_possible_location
        def possible(sku):
            loc = self.inv._history_location_cached(sku)
            return loc if loc else "Desconocida"

        self.assertEqual(possible("SKU0042"), "@CAJA_H")
        self.assertEqual(calls["n"], 1)
        for _ in range(5):
            possible("SKU0042")
        self.assertEqual(calls["n"], 1, "get_possible_location repetido no re-recorre historial")
        self.assertEqual(possible("SKU9999"), "Desconocida")


class TestConditionalResolver(LocationCacheTestBase):
    def test_resolver_none_does_not_call_resolver(self):
        """Requisito 3: build_full_view con resolver=None no llama al resolver."""
        build_history(self.hist_dir, 5, 500, sku_occ=2, sku="SKU0042")
        self._index_history_sync()
        for i in range(50):
            self.inv.add_item(f"SKU{i:04d}")  # 50 escaneados, el resto faltantes

        calls = {"n": 0}
        def resolver(sku):
            calls["n"] += 1
            return "UBICACION"

        opts = ViewOptions(sort_mode=SORT_LAST_TOP, location_resolver=None)
        view = build_full_view(self.inv, opts)
        self.assertEqual(calls["n"], 0, "resolver=None: el resolver NO debe ejecutarse")
        self.assertGreater(len(view.diff), 0, "la vista diff se construye igual")
        # Las filas diff con resolver=None deben decir "Desconocida"
        self.assertTrue(all(r.values[-1] == "Desconocida" for r in view.diff))

        # Con resolver presente, se usa (ventana abierta)
        opts2 = ViewOptions(sort_mode=SORT_LAST_TOP, location_resolver=resolver)
        build_full_view(self.inv, opts2)
        self.assertGreater(calls["n"], 0, "con resolver, debe ejecutarse por las filas de diff")

    def test_diff_open_resolves_locations(self):
        """Requisito 4: con Diferencias abierta, las ubicaciones se calculan de verdad."""
        build_history(self.hist_dir, 3, 300, sku_occ=2, sku="SKU0042")
        self._index_history_sync()
        for i in range(100):
            self.inv.add_item(f"SKU{i:04d}")
        # Segunda unidad de SKU0042: queda como SOBRANTE -> genera fila en diff
        self.inv.add_item("SKU0042")

        # Resolver real (equivalente a StockApp.get_possible_location cacheado)
        def real_resolver(sku):
            loc = self.inv._history_location_cached(sku)
            return loc if loc else "Desconocida"

        opts = ViewOptions(sort_mode=SORT_LAST_TOP, location_resolver=real_resolver)
        view = build_full_view(self.inv, opts)
        diff_rows = {r.values[1]: r for r in view.diff}
        self.assertIn("SKU0042", diff_rows)  # está en el historial con contenedor
        self.assertEqual(
            diff_rows["SKU0042"].values[-1], "@CAJA_H",
            "la fila del SKU con ubicación histórica debe resolverla",
        )

    def test_incremental_equivalent_to_rebuild_with_resolver(self):
        """Requisito 5: apply_event == build_full_view final (con resolver)."""
        build_history(self.hist_dir, 3, 300, sku_occ=2, sku="SKU0042")
        self._index_history_sync()

        def real_resolver(sku):
            loc = self.inv._history_location_cached(sku)
            return loc if loc else "Desconocida"

        opts = ViewOptions(sort_mode=SORT_LAST_TOP, location_resolver=real_resolver)
        inv = self.inv
        view = build_full_view(inv, opts)
        codes = [f"SKU{i:04d}" for i in range(100)]
        for i, c in enumerate(codes, start=1):
            inv.add_item(c)
            ev = {"sku": c, "pos": i, "fam": "AM", "is_qr": False, "replaced": False}
            view, _ = apply_event(view, ev, inv, opts)

        final = build_full_view(inv, opts)
        self.assertEqual(view.scanned, final.scanned, "tabla escaneada debe ser idéntica")
        self.assertEqual(view.master, final.master, "tabla maestra debe ser idéntica")
        self.assertEqual(view.diff, final.diff, "tabla de diferencias debe ser idéntica")
        self.assertEqual(view.metrics, final.metrics, "métricas deben ser idénticas")

    def test_incremental_equivalent_to_rebuild_without_resolver(self):
        """Requisito 5 (variante): mismo invariante con resolver=None (diff cerrada)."""
        opts = ViewOptions(sort_mode=SORT_LAST_TOP, location_resolver=None)
        inv = self.inv
        view = build_full_view(inv, opts)
        codes = [f"SKU{i:04d}" for i in range(50)]
        for i, c in enumerate(codes, start=1):
            inv.add_item(c)
            ev = {"sku": c, "pos": i, "fam": "AM", "is_qr": False, "replaced": False}
            view, _ = apply_event(view, ev, inv, opts)
        final = build_full_view(inv, opts)
        self.assertEqual(view.scanned, final.scanned)
        self.assertEqual(view.master, final.master)
        self.assertEqual(view.diff, final.diff)
        self.assertEqual(view.metrics, final.metrics)


class TestScanIntegrity(LocationCacheTestBase):
    def test_100_scans_no_duplicates_no_duplicate_events(self):
        """Requisito 6: 100 escaneos -> 100 unidades exactas, sin eventos duplicados."""
        inv = self.inv
        for c in [f"SKU{i:04d}" for i in range(100)]:
            inv.add_item(c)

        self.assertEqual(len(inv.scan_sequence), 100)
        total = sum(len(p) for p in inv.scanned_items.values())
        self.assertEqual(total, 100, "100 escaneos deben producir exactamente 100 unidades")
        # Ningún SKU con más de 1 unidad (cada escaneo agrega exactamente 1)
        for code, positions in inv.scanned_items.items():
            self.assertEqual(len(positions), 1, f"{code} debe tener exactamente 1 unidad")

        # Worker real: 100 eventos -> 100 refreshes, sin duplicados
        eq = queue.Queue(); rq = queue.Queue(); stop = threading.Event()
        w = ScanWorker(inv, eq, rq, stop)
        th = threading.Thread(target=w.run, daemon=True); th.start()
        for i, c in enumerate([f"SKU{i:04d}" for i in range(100)], start=101):
            eq.put({"sku": c, "pos": i, "fam": "AM", "ts": time.time(), "replaced": False, "is_qr": False})
        refreshes = []
        deadline = time.time() + 15
        while len(refreshes) < 100 and time.time() < deadline:
            try:
                r = rq.get(timeout=0.05)
                if r.get("type") == "refresh":
                    refreshes.append(r.get("sku"))
            except queue.Empty:
                pass
        stop.set()
        self.assertEqual(len(refreshes), 100, "deben llegar exactamente 100 refreshes")
        self.assertEqual(refreshes, [f"SKU{i:04d}" for i in range(100)],
                         "un refresh por evento, en orden FIFO, sin duplicados")

    def test_no_rows_x_files_x_positions_pattern(self):
        """Extra: el patrón filas x archivos x posiciones ya no ocurre.

        Con la caché caliente, rebuilds repetidos de la vista (como un F4 con
        Diferencias abierta) NO vuelven a recorrer el historial: la segunda
        build_full_view resuelve cada fila desde la caché sin llamar al
        recorrido de historial.
        """
        build_history(self.hist_dir, 5, 500, sku_occ=2, sku="SKU0042")
        self._index_history_sync()
        for i in range(50):
            self.inv.add_item(f"SKU{i:04d}")

        orig = self.inv._history_location
        calls = {"n": 0}
        def counting(sku):
            calls["n"] += 1
            return orig(sku)
        self.inv._history_location = counting

        def real_resolver(sku):
            loc = self.inv._history_location_cached(sku)
            return loc if loc else "Desconocida"

        opts = ViewOptions(sort_mode=SORT_LAST_TOP, location_resolver=real_resolver)
        view1 = build_full_view(self.inv, opts)
        n_files = len(self.inv.historical_sequences)
        n_positions = max((len(s) for s in self.inv.historical_sequences.values()), default=0)
        expected_upper_bound = len(view1.diff)  # a lo sumo UNA búsqueda por fila

        # Segundo rebuild completo (simula un F4): caché caliente -> 0 recorridos nuevos
        view2 = build_full_view(self.inv, opts)
        self.assertEqual(view1.diff, view2.diff)
        self.assertLessEqual(
            calls["n"], expected_upper_bound,
            f"recorridos de historial ({calls['n']}) deben ser <= filas de diff "
            f"({expected_upper_bound}); el patrón filas x {n_files} archivos x "
            f"{n_positions} posiciones ya no debe ocurrir",
        )


if __name__ == "__main__":
    unittest.main()
