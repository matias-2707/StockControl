"""
Tests de las optimizaciones F4/Delete incremental + caché de contenedores
(Stock Cellular Center V8.0 — auditoría de rendimiento 2026-08-31)

Cubren:
1. F4 (add) incremental == rebuild completo para la misma secuencia.
2. Delete/Supr incremental == rebuild completo.
3. F4 agrega exactamente +1 unidad.
4. Delete quita exactamente -1 unidad.
5. get_container_status cacheado == método original (equivalencia exacta).
6. Caché correcta tras agregar/eliminar/mover productos y agregar QRs.
7. Regresión: 1600 elementos NO regeneran el patrón O(n²) (llamadas a
   get_containers_for_index acotadas).
8. Conteo de llamadas a get_containers_for_index antes/después.
"""

import os
import sys
import tempfile
import shutil
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.inventory import InventoryManager
from src.gui.updates import (
    ViewOptions,
    build_full_view,
    apply_event,
    SORT_LAST_TOP,
    SORT_LAST_BOTTOM,
    SORT_ALPHA,
    SORT_QTY,
    SORT_SCAN,
)


def _raw_container_status(model, container_code, seq=None):
    """Implementación ORIGINAL de get_container_status (pre-caché) para comparar."""
    if seq is None:
        seq = model.scan_sequence
    expected_items = model.get_container_expected_items(container_code)
    expected_total = sum(expected_items.values())
    scanned_count = 0
    for i, code in enumerate(seq):
        if not model.is_qr_code(code):
            box, sec = model.get_containers_for_index(seq, i)
            active_c = box if box else (sec if sec else None)
            if active_c == container_code or sec == container_code:
                scanned_count += 1
    missing = max(0, expected_total - scanned_count)
    is_complete = (expected_total > 0 and scanned_count >= expected_total)
    if expected_total > 0:
        display_str = "✓" if is_complete else str(missing)
    else:
        display_str = ""
    return {
        "container": container_code,
        "expected_total": expected_total,
        "scanned_count": scanned_count,
        "missing_count": missing,
        "is_complete": is_complete,
        "display_str": display_str,
    }


class PerfOptBase(unittest.TestCase):
    """Base común: inventario real con 60 SKUs de catálogo (AG) + helpers."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {"excluded_skus": []}
        self.inv = InventoryManager(self.config)
        self.inv.scan_dir = self.temp_dir
        self.inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        self.inv.main_stock = self.inv.load_main_stock()
        self._seed_catalog(60)

    def _seed_catalog(self, n):
        self.inv.stock_data = [(f"AG{i:04d}", f"Desc {i}", 1) for i in range(n)]
        self.inv.original_quantities = {f"AG{i:04d}": 1 for i in range(n)}
        self.inv.full_family_map = {f"AG{i:04d}": "AG" for i in range(n)}
        self.inv.family_map = {f"AG{i:04d}": "AG" for i in range(n)}
        self.inv.family_type = "AG"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def scan_on(self, inv, sku):
        res = inv.add_item(sku)
        self.assertIsNotNone(res)
        return {
            "sku": sku,
            "pos": res["pos"],
            "fam": res.get("fam"),
            "is_qr": inv.is_qr_code(sku),
            "replaced": res.get("replaced", False),
            "old_sku": res.get("old_sku"),
        }

    def assert_invariant(self, view, options=None):
        opts = options if options is not None else ViewOptions()
        self.assertEqual(view, build_full_view(self.inv, opts))

    def _seed_sequence_with_qrs(self, inv=None, n_products=40, qr_every=10):
        """Construye secuencia realista: %MUEBLE cada qr_every, @CAJA a la mitad."""
        inv = inv or self.inv
        seq = []
        for i in range(n_products):
            if i % qr_every == 0:
                seq.append(f"%MUEBLE{i // qr_every:03d}")
            if i % (qr_every // 2) == 0:
                seq.append(f"@CAJA{i // (qr_every // 2):03d}")
            seq.append(f"AG{i:04d}")
        inv.scan_sequence = list(seq)
        inv._rebuild_scanned_items()


class TestF4Incremental(PerfOptBase):
    """F4 (+1) incremental == rebuild; agrega exactamente 1 unidad."""

    def _apply_f4(self, inv, sku):
        """Replica on_diff_f4: add_item + apply_event con el evento del worker."""
        res = inv.add_item(sku)
        self.assertIsNotNone(res)
        ev = {
            "sku": sku,
            "pos": res["pos"],
            "fam": res.get("fam"),
            "ts": 0.0,
            "replaced": res.get("replaced", False),
            "is_qr": False,
            "old_sku": res.get("old_sku"),
        }
        return ev

    def test_f4_incremental_equals_rebuild(self):
        for sort_mode in (SORT_LAST_TOP, SORT_LAST_BOTTOM, SORT_SCAN, SORT_ALPHA, SORT_QTY):
            inv = self._fresh_inv()
            opts = ViewOptions(sort_mode=sort_mode)
            # Estado inicial: algunos escaneados
            for sku in ["AG0000", "AG0001", "AG0002", "AG0003"]:
                inv.add_item(sku)
            view = build_full_view(inv, opts)
            # F4 sobre un faltante
            ev = self._apply_f4(inv, "AG0010")
            new_view, actions = apply_event(view, ev, inv, opts)
            # 1) Equivalencia: vista incremental == rebuild
            self.assertEqual(new_view, build_full_view(inv, opts))
            # 2) +1 exacto en scanned_items
            self.assertEqual(len(inv.scanned_items["AG0010"]), 1)
            # 3) Acciones acotadas (O(1), no reconstrucción masiva):
            #    el delete legítimo es solo la fila d:AG0010 que desaparece.
            scanned_ops = [a for a in actions if a.table == "scanned"]
            self.assertLessEqual(len(scanned_ops), 2,
                                 f"demasiadas acciones scanned: {scanned_ops}")

    def test_f4_adds_exactly_one(self):
        inv = self._fresh_inv()
        inv.add_item("AG0000")
        before = dict(inv.scanned_items)
        res = inv.add_item("AG0000")
        self.assertEqual(len(inv.scanned_items["AG0000"]), 2)
        self.assertEqual(res["pos"], 2)
        self.assertEqual(before["AG0000"], [1])

    def test_f4_sequence_burst_incremental_equals_rebuild(self):
        """Ráfaga de 50 F4: incremental paso a paso == rebuild final."""
        inv = self._fresh_inv()
        opts = ViewOptions()
        view = build_full_view(inv, opts)
        skus = [f"AG{i:04d}" for i in range(50)]
        for sku in skus:
            ev = self._apply_f4(inv, sku)
            view, actions = apply_event(view, ev, inv, opts)
            self.assertEqual(view, build_full_view(inv, opts))

    def _fresh_inv(self):
        inv = InventoryManager({"excluded_skus": []})
        inv.scan_dir = self.temp_dir
        inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        inv.main_stock = inv.load_main_stock()
        inv.stock_data = [(f"AG{i:04d}", f"Desc {i}", 1) for i in range(60)]
        inv.original_quantities = {f"AG{i:04d}": 1 for i in range(60)}
        inv.full_family_map = {f"AG{i:04d}": "AG" for i in range(60)}
        inv.family_map = {f"AG{i:04d}": "AG" for i in range(60)}
        inv.family_type = "AG"
        return inv


class TestDeleteIncremental(PerfOptBase):
    """Delete/Supr (-1) incremental == rebuild; quita exactamente 1 unidad."""

    def _apply_delete(self, inv, sku, opts):
        """Replica on_diff_delete: captura posición, delete_last, apply_event op=delete."""
        indices = [i for i, x in enumerate(inv.scan_sequence) if x == sku]
        res = inv.delete_last(sku)
        self.assertTrue(res)
        ev = {"op": "delete", "sku": sku, "pos": indices[-1] + 1, "is_qr": False}
        return apply_event(self.view, ev, inv, opts)

    def test_delete_incremental_equals_rebuild(self):
        for sort_mode in (SORT_LAST_TOP, SORT_LAST_BOTTOM, SORT_SCAN, SORT_ALPHA, SORT_QTY):
            inv = self._fresh_inv()
            opts = ViewOptions(sort_mode=sort_mode)
            for sku in ["AG0000", "AG0000", "AG0001", "AG0002", "AG0003", "AG0010"]:
                inv.add_item(sku)
            self.view = build_full_view(inv, opts)
            # Delete de AG0000 (tenía 2 unidades -> queda 1)
            new_view, actions = self._apply_delete(inv, "AG0000", opts)
            self.assertEqual(new_view, build_full_view(inv, opts))
            self.assertEqual(len(inv.scanned_items["AG0000"]), 1)

    def test_delete_removes_exactly_one(self):
        inv = self._fresh_inv()
        inv.add_item("AG0000")
        inv.add_item("AG0000")
        inv.add_item("AG0001")
        self.assertEqual(len(inv.scanned_items["AG0000"]), 2)
        indices = [i for i, x in enumerate(inv.scan_sequence) if x == "AG0000"]
        inv.delete_last("AG0000")
        self.assertEqual(len(inv.scanned_items["AG0000"]), 1)
        self.assertEqual(inv.scan_sequence.count("AG0000"), 1)
        self.assertGreaterEqual(indices[-1] + 1, 1)

    def test_delete_last_unit_removes_row(self):
        """Borrar la única unidad: la fila del SKU desaparece de scanned y diff."""
        inv = self._fresh_inv()
        opts = ViewOptions()
        inv.add_item("AG0000")
        self.view = build_full_view(inv, opts)
        new_view, actions = self._apply_delete(inv, "AG0000", opts)
        self.assertEqual(new_view, build_full_view(inv, opts))
        keys = [r.key for r in new_view.scanned]
        self.assertNotIn("p:1", keys)

    def test_delete_with_qrs_equals_rebuild(self):
        """Delete con QRs de contenedores: corrimiento + reproyección de QRs."""
        inv = self._fresh_inv()
        opts = ViewOptions()
        self._seed_sequence_with_qrs(inv, n_products=30, qr_every=10)
        self.view = build_full_view(inv, opts)
        # Borrar el último producto (AG0029) -> corrimiento de su fila
        indices = [i for i, x in enumerate(inv.scan_sequence) if x == "AG0029"]
        res = inv.delete_last("AG0029")
        self.assertTrue(res)
        ev = {"op": "delete", "sku": "AG0029", "pos": indices[-1] + 1, "is_qr": False}
        new_view, actions = apply_event(self.view, ev, inv, opts)
        self.assertEqual(new_view, build_full_view(inv, opts))

    def _fresh_inv(self):
        inv = InventoryManager({"excluded_skus": []})
        inv.scan_dir = self.temp_dir
        inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        inv.main_stock = inv.load_main_stock()
        inv.stock_data = [(f"AG{i:04d}", f"Desc {i}", 1) for i in range(60)]
        inv.original_quantities = {f"AG{i:04d}": 1 for i in range(60)}
        inv.full_family_map = {f"AG{i:04d}": "AG" for i in range(60)}
        inv.family_map = {f"AG{i:04d}": "AG" for i in range(60)}
        inv.family_type = "AG"
        return inv


class TestContainerStatusCache(PerfOptBase):
    """Equivalencia caché vs original + invalidación correcta."""

    def test_cache_equals_original(self):
        inv = self._fresh_inv()
        self._seed_sequence_with_qrs(inv, n_products=40, qr_every=10)
        containers = ["%MUEBLE000", "%MUEBLE001", "%MUEBLE002", "%MUEBLE003",
                      "@CAJA000", "@CAJA001", "@CAJA002", "@CAJA003",
                      "@CAJA004", "@CAJA005", "@CAJA006", "@CAJA007", "NOEXISTE"]
        for c in containers:
            cached = inv.get_container_status(c)
            raw = _raw_container_status(inv, c)
            self.assertEqual(cached, raw, f"container {c}")

    def _assert_all_containers_match_raw(self, inv):
        """Valida que la caché coincida con la implementación original para TODOS
        los contenedores presentes en la secuencia (detección de stale completa)."""
        containers = set()
        for i, code in enumerate(inv.scan_sequence):
            if inv.is_qr_code(code):
                containers.add(code)
        for c in containers:
            self.assertEqual(inv.get_container_status(c),
                             _raw_container_status(inv, c),
                             f"container {c} stale")

    def test_cache_invalidates_on_add(self):
        inv = self._fresh_inv()
        self._seed_sequence_with_qrs(inv, n_products=20, qr_every=10)
        self._assert_all_containers_match_raw(inv)
        # Agregar un producto (cae bajo el último contenedor activo)
        inv.add_item("AG0005")
        self._assert_all_containers_match_raw(inv)

    def test_cache_invalidates_on_delete(self):
        inv = self._fresh_inv()
        self._seed_sequence_with_qrs(inv, n_products=20, qr_every=10)
        self._assert_all_containers_match_raw(inv)
        inv.delete_last("AG0000")
        self._assert_all_containers_match_raw(inv)

    def test_cache_invalidates_on_move(self):
        inv = self._fresh_inv()
        self._seed_sequence_with_qrs(inv, n_products=20, qr_every=10)
        self._assert_all_containers_match_raw(inv)
        # Mover AG0015 hacia el inicio (cambia de contenedor activo)
        idx = inv.scan_sequence.index("AG0015")
        inv.move_item_in_sequence(idx, 0)
        self._assert_all_containers_match_raw(inv)

    def test_cache_invalidates_on_qr_add(self):
        inv = self._fresh_inv()
        inv.add_item("AG0000")
        inv.add_item("AG0001")
        inv.add_item("AG0002")
        # Sin contenedores: todos los status son 0/esperado
        self.assertEqual(inv.get_container_status("@CAJA000")["scanned_count"], 0)
        # Agregar QR de caja al inicio cambia la asociación de los 3 productos
        inv.scan_sequence.insert(0, "@CAJA000")
        inv._rebuild_scanned_items()
        self.assertEqual(inv.get_container_status("@CAJA000")["scanned_count"], 3)
        self._assert_all_containers_match_raw(inv)

    def test_cache_with_external_seq_falls_back(self):
        """seq externo (no self.scan_sequence) se computa directo, sin stale."""
        inv = self._fresh_inv()
        self._seed_sequence_with_qrs(inv, n_products=10, qr_every=5)
        external = list(inv.scan_sequence)
        cached = inv.get_container_status("@CAJA000", external)
        raw = _raw_container_status(inv, "@CAJA000", external)
        self.assertEqual(cached, raw)

    def _fresh_inv(self):
        inv = InventoryManager({"excluded_skus": []})
        inv.scan_dir = self.temp_dir
        inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        inv.main_stock = inv.load_main_stock()
        inv.stock_data = [(f"AG{i:04d}", f"Desc {i}", 1) for i in range(60)]
        inv.original_quantities = {f"AG{i:04d}": 1 for i in range(60)}
        inv.full_family_map = {f"AG{i:04d}": "AG" for i in range(60)}
        inv.family_map = {f"AG{i:04d}": "AG" for i in range(60)}
        inv.family_type = "AG"
        return inv


class TestScalingRegression(PerfOptBase):
    """1600 elementos: sin O(n²); llamadas a get_containers_for_index acotadas."""

    def _big_inv(self, n=1600, qr_every=20):
        inv = InventoryManager({"excluded_skus": []})
        inv.scan_dir = self.temp_dir
        inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        inv.main_stock = inv.load_main_stock()
        inv.stock_data = [(f"AG{i:04d}", f"Desc {i}", 1) for i in range(n)]
        inv.original_quantities = {f"AG{i:04d}": 1 for i in range(n)}
        inv.full_family_map = {f"AG{i:04d}": "AG" for i in range(n)}
        inv.family_map = {f"AG{i:04d}": "AG" for i in range(n)}
        inv.family_type = "AG"
        seq = []
        for i in range(n):
            if i % qr_every == 0:
                seq.append(f"%MUEBLE{i // qr_every:03d}")
            if i % (qr_every // 2) == 0:
                seq.append(f"@CAJA{i // (qr_every // 2):03d}")
            seq.append(f"AG{i:04d}")
        inv.scan_sequence = list(seq)
        inv._rebuild_scanned_items()
        return inv

    def test_scanned_view_1600_linear_calls(self):
        inv = self._big_inv()
        opts = ViewOptions()

        from src.gui import updates as U

        # Contador de llamadas a get_containers_for_index
        original = InventoryManager.get_containers_for_index
        calls = {"n": 0}

        def counting(self_, seq, idx):
            calls["n"] += 1
            return original(self_, seq, idx)

        InventoryManager.get_containers_for_index = counting
        try:
            U._scanned_view(inv, opts)
            n_products = len(inv.scan_sequence)
            # O(n) con constante chica: una pasada del precompute de contenedores
            # (por producto) + get_containers_for_index por fila en _scanned_row_for.
            # El patrón O(QRs × n) hubiera sido ~QRs * n (ej. 80 QRs * 2400 filas).
            self.assertLess(calls["n"], n_products * 5,
                            f"llamadas={calls['n']} para {n_products} filas")
        finally:
            InventoryManager.get_containers_for_index = original

    def test_build_full_view_1600_under_threshold(self):
        """Regresión temporal laxa: 1600 ítems debe proyectar en < 1s (antes ~3s)."""
        import time
        inv = self._big_inv()
        opts = ViewOptions()
        from src.gui import updates as U

        t0 = time.perf_counter()
        U.build_full_view(inv, opts)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0, f"build_full_view tardó {elapsed:.2f}s")


if __name__ == "__main__":
    unittest.main()
