"""
Tests del invariante de proyección incremental — Stock Cellular Center V8.0
(Fase B, paso 1)

Demuestran que, para secuencias normales y casos límite, la proyección
incremental (apply_event) produce EXACTAMENTE el mismo estado visual que
una reconstrucción completa (build_full_view) desde el mismo modelo.

Además verifican que las acciones generadas (diff_views + apply_actions)
reproducen la vista incremental paso a paso — la misma semántica que tendrá
el aplicador Tk del paso 3.

Casos cubiertos:
- Secuencias normales en "Último arriba" y "Último abajo" (y alias "Escaneo").
- Ráfaga con backlog (poll diferido): N eventos aplicados sobre vista vieja.
- Repetición de SKU: solo la última aparición muestra cantidad y color.
- QRs @ (caja), % (mueble), # (vidriera): etiqueta, colores, bold, fold, faltantes y ✓.
- Productos escaneados ANTES y DESPUÉS de su QR (asociación adelante/atrás).
- Contenedores colapsados: productos ocultos, QR actualiza su faltante.
- Reemplazo de QR pendiente ("[ESPERANDO REEMPLAZO]" → QR nuevo).
- Evento inconsistente con el modelo → fallback a rebuild (nunca vista corrupta).
- Evento duplicado/retransmitido → idempotencia.
- Métricas fieles a _update_all_ui (escaneado, esperado, dif. neta, incidencias, %).
- Ventana de diferencias: faltantes, sobrantes, desconocidos, ☑/☐, ubicación.
- Modos agrupados "Alfabético" y "Cantidad" (incluye reordenamiento → moves).
"""

import os
import sys
import time
import queue
import tempfile
import shutil
import threading
import unittest

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.inventory import InventoryManager
from src.core.scanpipeline import ScanWorker
from src.gui.updates import (
    ViewOptions,
    build_full_view,
    apply_event,
    diff_views,
    apply_actions,
    SORT_LAST_TOP,
    SORT_LAST_BOTTOM,
    SORT_ALPHA,
    SORT_QTY,
    SORT_SCAN,
)


class InvariantBase(unittest.TestCase):
    """Base común: inventario real con 60 SKUs de catálogo (AM) y helpers."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {"excluded_skus": []}
        self.inv = InventoryManager(self.config)
        self.inv.scan_dir = self.temp_dir
        self.inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        self.inv.main_stock = self.inv.load_main_stock()
        self._seed_catalog()

    def _seed_catalog(self):
        self.inv.stock_data = [(f"SKU{i:03d}", f"Desc {i}", 1) for i in range(60)]
        self.inv.original_quantities = {f"SKU{i:03d}": 1 for i in range(60)}
        self.inv.full_family_map = {f"SKU{i:03d}": "AM" for i in range(60)}
        self.inv.family_map = {f"SKU{i:03d}": "AM" for i in range(60)}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _fresh_inventory(self):
        """Inventario aislado con el mismo catálogo (para comparar modos sin
        contaminar el modelo entre corridas)."""
        inv = InventoryManager({"excluded_skus": []})
        inv.scan_dir = self.temp_dir
        inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        inv.main_stock = inv.load_main_stock()
        inv.stock_data = [(f"SKU{i:03d}", f"Desc {i}", 1) for i in range(60)]
        inv.original_quantities = {f"SKU{i:03d}": 1 for i in range(60)}
        inv.full_family_map = {f"SKU{i:03d}": "AM" for i in range(60)}
        inv.family_map = {f"SKU{i:03d}": "AM" for i in range(60)}
        return inv

    def scan_on(self, inv, sku):
        """Aplica add_item al modelo y devuelve el evento equivalente al del worker."""
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

    def scan(self, sku):
        return self.scan_on(self.inv, sku)

    def assert_invariant(self, view, options=None):
        """Invariante de oro: la vista incremental == rebuild completo."""
        opts = options if options is not None else ViewOptions()
        self.assertEqual(view, build_full_view(self.inv, opts))

    def run_sequence_on(self, inv, skus, options=None):
        """Escanea cada SKU aplicando el incremental paso a paso sobre `inv`.

        Tras cada evento verifica el invariante y que las acciones generadas
        reproducen la vista nueva (simulador puro, sin Tk).
        """
        opts = options if options is not None else ViewOptions()
        view = build_full_view(inv, opts)
        for sku in skus:
            ev = self.scan_on(inv, sku)
            prev = view
            view, actions = apply_event(view, ev, inv, opts)
            self.assertEqual(view, build_full_view(inv, opts))
            rebuilt = apply_actions(prev, actions)
            self.assertEqual(rebuilt.scanned, view.scanned)
            self.assertEqual(rebuilt.master, view.master)
            self.assertEqual(rebuilt.diff, view.diff)
        return view

    def run_sequence(self, skus, options=None):
        return self.run_sequence_on(self.inv, skus, options)


class TestVistaBasica(InvariantBase):
    """Sanidad de la proyección completa sobre estados simples."""

    def test_vista_vacia(self):
        view = build_full_view(self.inv, ViewOptions())
        self.assertEqual(view.scanned, ())
        # la maestra SIEMPRE muestra el CSV completo (60 filas, sin escanear)
        self.assertEqual(len(view.master), 60)
        self.assertEqual(view.master[0].values, ("SKU000", "Desc 0", "1 (0)"))
        # los 60 SKUs sin escanear aparecen como faltantes en la ventana de diff
        self.assertEqual(len(view.diff), 60)
        self.assertEqual(view.diff[0].values[6], "FALTANTE")
        self.assertEqual(view.metrics.scanned_count, 0)
        self.assertEqual(view.metrics.expected_count, 60)
        self.assertEqual(view.metrics.diff_net, -60)
        self.assertEqual(view.metrics.relevant_diffs, 0)
        self.assertEqual(view.metrics.percent, 0.0)

    def test_maestra_refleja_escaneo(self):
        self.scan("SKU005")
        view = build_full_view(self.inv, ViewOptions())
        master = {r.values[0]: r for r in view.master}
        self.assertIn("SKU005", master)
        self.assertEqual(master["SKU005"].values[2], "1 (1)")
        self.assertEqual(master["SKU005"].colors, self.inv.get_row_color(1, 1))

    def test_diff_unknown_y_ubicacion(self):
        opts = ViewOptions(location_resolver=lambda c: "CASA-1")
        self.scan("UNKNOWN999")
        view = build_full_view(self.inv, opts)
        diff = {r.values[1]: r for r in view.diff}
        self.assertIn("UNKNOWN999", diff)
        row = diff["UNKNOWN999"]
        self.assertEqual(row.values[3], 0)   # expected
        self.assertEqual(row.values[4], 1)   # scanned
        self.assertEqual(row.values[5], "+1")
        self.assertEqual(row.values[6], "SOBRANTE")
        self.assertEqual(row.values[7], "CASA-1")

    def test_diff_faltante(self):
        view = build_full_view(self.inv, ViewOptions())
        diff = {r.values[1]: r for r in view.diff}
        self.assertIn("SKU001", diff)
        self.assertEqual(diff["SKU001"].values[5], "-1")
        self.assertEqual(diff["SKU001"].values[6], "FALTANTE")

    def test_diff_checkbox_exclusion(self):
        opts = ViewOptions(excluded_from_export=frozenset({"SKU001"}))
        self.scan("SKU001")
        self.scan("SKU001")  # sobrante +1
        self.scan("SKU002")
        self.scan("SKU002")  # sobrante +1
        view = build_full_view(self.inv, opts)
        diff = {r.values[1]: r for r in view.diff}
        self.assertEqual(diff["SKU001"].values[0], "☑")
        self.assertEqual(diff["SKU001"].values[6], "SOBRANTE")
        self.assertEqual(diff["SKU002"].values[0], "☐")


class TestInvarianteSecuencial(InvariantBase):
    """Invariante incremental == rebuild en modos secuenciales."""

    def test_secuencia_50_abajo(self):
        skus = [f"SKU{i:03d}" for i in range(50)]
        view = self.run_sequence(skus, ViewOptions(sort_mode=SORT_LAST_BOTTOM))
        self.assertEqual([r.values[1] for r in view.scanned], skus)
        self.assertEqual([r.text for r in view.scanned], [str(i) for i in range(1, 51)])

    def test_secuencia_50_arriba(self):
        skus = [f"SKU{i:03d}" for i in range(50)]
        view = self.run_sequence(skus, ViewOptions(sort_mode=SORT_LAST_TOP))
        self.assertEqual([r.values[1] for r in view.scanned], list(reversed(skus)))
        self.assertEqual(view.scanned[0].values[1], skus[-1])
        self.assertEqual([r.text for r in view.scanned], [str(i) for i in range(50, 0, -1)])

    def test_modo_escaneo_equivale_arriba(self):
        skus = ["SKU001", "SKU002", "SKU003", "SKU004"]
        v1 = self.run_sequence_on(self._fresh_inventory(), skus, ViewOptions(sort_mode=SORT_SCAN))
        v2 = self.run_sequence_on(self._fresh_inventory(), skus, ViewOptions(sort_mode=SORT_LAST_TOP))
        self.assertEqual(v1.scanned, v2.scanned)

    def test_repeticion_sku(self):
        view = self.run_sequence(["SKU000"] * 30, ViewOptions(sort_mode=SORT_LAST_BOTTOM))
        rows = view.scanned
        self.assertEqual(len(rows), 30)
        # solo la última aparición muestra cantidad; las anteriores van vacías
        self.assertEqual([r.values[2] for r in rows], [""] * 29 + ["30"])
        # solo la última lleva el color de estado (sobrante: 3 escaneados vs 1 esperado)
        self.assertEqual(rows[-1].colors, self.inv.get_row_color(1, 30))
        self.assertEqual(rows[0].colors, ("#242424", "#ffffff"))

    def test_mezcla_conocidos_desconocidos(self):
        skus = ["SKU001", "UNKNOWN_A", "SKU001", "UNKNOWN_B", "SKU002"]
        view = self.run_sequence(skus, ViewOptions(sort_mode=SORT_LAST_BOTTOM))
        self.assertEqual(len(view.scanned), 5)

    def test_rafaga_backlog_poll_diferido(self):
        """Flujo real: el modelo se llena (ráfaga) y el poll procesa el backlog
        con una vista desactualizada. El incremental debe alcanzar el rebuild."""
        skus = [f"SKU{i:03d}" for i in range(30)]
        view = build_full_view(self.inv, ViewOptions(sort_mode=SORT_LAST_BOTTOM))  # vista vacía
        eventos = [self.scan(s) for s in skus]  # muta el modelo 30 veces
        for ev in eventos:
            view, _ = apply_event(view, ev, self.inv, ViewOptions(sort_mode=SORT_LAST_BOTTOM))
        self.assert_invariant(view, ViewOptions(sort_mode=SORT_LAST_BOTTOM))
        self.assertEqual([r.values[1] for r in view.scanned], skus)

    def test_evento_duplicado_idempotente(self):
        ev = self.scan("SKU001")
        view = build_full_view(self.inv, ViewOptions())
        view2, actions = apply_event(view, ev, self.inv, ViewOptions())
        self.assertEqual(view2, view)
        self.assertEqual(actions, ())
        # re-aplicar el mismo evento (retransmisión) no cambia nada
        view3, actions3 = apply_event(view2, ev, self.inv, ViewOptions())
        self.assertEqual(view3, view2)
        self.assertEqual(actions3, ())


class TestInvarianteQRs(InvariantBase):
    """QRs estructurales: estilos, plegado, faltantes y ✓."""

    def _seed_containers(self):
        self.inv.main_stock = {
            "version": "8.0",
            "containers": {
                "@CAJA_1": {
                    "type": "caja", "parent": None,
                    "expected_skus": {"SKU001": 2, "SKU002": 1},
                },
                "%MUEBLE_1": {"type": "mueble", "parent": None, "expected_skus": {}},
                "#VIDRIERA_1": {"type": "vidriera", "parent": None, "expected_skus": {}},
            },
            "product_locations": {"SKU001": "@CAJA_1", "SKU002": "@CAJA_1"},
        }

    def test_qr_estilos_y_fold(self):
        self._seed_containers()
        self.run_sequence(["%MUEBLE_1", "@CAJA_1", "#VIDRIERA_1"], ViewOptions())
        view = build_full_view(self.inv, ViewOptions())
        rows = {r.values[1].replace("▼ ", "").replace("▶ ", ""): r for r in view.scanned}
        self.assertEqual(rows["%MUEBLE_1"].values[0], "MUEBLE")
        self.assertEqual(rows["%MUEBLE_1"].colors, ("#1f4e78", "#ffffff"))
        self.assertTrue(rows["%MUEBLE_1"].bold)
        self.assertEqual(rows["@CAJA_1"].values[0], "CAJA")
        self.assertEqual(rows["@CAJA_1"].colors, ("#333333", "#ffffff"))
        self.assertTrue(rows["@CAJA_1"].bold)
        self.assertEqual(rows["#VIDRIERA_1"].values[0], "VIDRIERA")
        self.assertEqual(rows["#VIDRIERA_1"].colors, ("#7030a0", "#ffffff"))
        self.assertTrue(rows["#VIDRIERA_1"].bold)
        # sin colapso → flecha abierta
        self.assertTrue(rows["@CAJA_1"].values[1].startswith("▼ "))

    def test_qr_faltantes_y_checkmark(self):
        self._seed_containers()
        view = self.run_sequence(["@CAJA_1", "SKU001"], ViewOptions())
        qr = next(r for r in view.scanned if r.values[1].endswith("@CAJA_1"))
        self.assertEqual(qr.values[2], "2")   # faltan 2 de 3

        self.scan("SKU001")
        view = build_full_view(self.inv, ViewOptions())
        qr = next(r for r in view.scanned if r.values[1].endswith("@CAJA_1"))
        self.assertEqual(qr.values[2], "1")

        self.scan("SKU002")
        view = build_full_view(self.inv, ViewOptions())
        qr = next(r for r in view.scanned if r.values[1].endswith("@CAJA_1"))
        self.assertEqual(qr.values[2], "✓")

    def test_productos_antes_del_qr(self):
        """Asociación hacia adelante: productos escaneados antes que su QR."""
        self._seed_containers()
        view = self.run_sequence(["SKU001", "SKU002", "@CAJA_1"], ViewOptions())
        qr = next(r for r in view.scanned if r.values[1].endswith("@CAJA_1"))
        self.assertEqual(qr.values[2], "1")   # scanned 2 de 3

    def test_qr_no_afecta_metricas_ni_diff(self):
        self._seed_containers()
        view = build_full_view(self.inv, ViewOptions())
        m0 = view.metrics
        self.run_sequence(["@CAJA_1", "%MUEBLE_1", "#VIDRIERA_1"], ViewOptions())
        view = build_full_view(self.inv, ViewOptions())
        self.assertEqual(view.metrics, m0)
        self.assertEqual(len(view.diff), 60)  # solo faltantes de SKUs

    def test_contenedores_colapsados(self):
        self._seed_containers()
        opts = ViewOptions(collapsed_containers=frozenset({"@CAJA_1"}))
        view = self.run_sequence(["@CAJA_1", "SKU001", "SKU002"], opts)
        # solo el QR visible, con flecha cerrada; los productos ocultos
        self.assertEqual(len(view.scanned), 1)
        self.assertTrue(view.scanned[0].values[1].startswith("▶ "))
        # escanear otro producto dentro de la caja colapsada: no aparece fila,
        # pero el QR actualiza su faltante (2 → ✓)
        ev = self.scan("SKU001")
        view2, actions = apply_event(view, ev, self.inv, opts)
        self.assert_invariant(view2, opts)
        self.assertEqual(len(view2.scanned), 1)
        self.assertEqual(view2.scanned[0].values[2], "✓")
        # descolapsar (doble clic → rebuild): aparecen los productos
        opts2 = ViewOptions()
        view3 = build_full_view(self.inv, opts2)
        self.assertEqual(len(view3.scanned), 4)


class TestReemplazoYFallback(InvariantBase):
    """Reemplazo de QR pendiente y eventos inconsistentes."""

    def test_reemplazo_qr(self):
        self.scan("@CAJA_1")
        self.scan("SKU001")
        # marcar QR para reemplazo (delete manual → waiting_replacement)
        self.assertEqual(self.inv.delete_last("@CAJA_1"), "waiting_replacement")
        view = build_full_view(self.inv, ViewOptions())
        fila = view.scanned[0]  # modo abajo: primera fila = posición 1
        self.assertTrue(fila.values[1].endswith("[ESPERANDO REEMPLAZO]"))

        # escanear el QR nuevo → add_item reemplaza en la misma posición
        ev = self.scan("@CAJA_9")
        self.assertTrue(ev["replaced"])
        self.assertEqual(ev["old_sku"], "@CAJA_1")
        new_view, actions = apply_event(view, ev, self.inv, ViewOptions())
        self.assert_invariant(new_view, ViewOptions())
        self.assertTrue(new_view.scanned[0].values[1].endswith("@CAJA_9"))
        self.assertEqual(self.inv.scan_sequence, ["@CAJA_9", "SKU001"])

    def test_evento_inconsistente_fallback(self):
        self.scan("SKU001")
        view = build_full_view(self.inv, ViewOptions())
        # posición fuera de rango
        bad1 = {"sku": "SKU999", "pos": 999, "is_qr": False}
        v1, a1 = apply_event(view, bad1, self.inv, ViewOptions())
        self.assertEqual(v1, build_full_view(self.inv, ViewOptions()))
        # posición que apunta a otro código
        bad2 = {"sku": "SKU002", "pos": 1, "is_qr": False}
        v2, a2 = apply_event(view, bad2, self.inv, ViewOptions())
        self.assertEqual(v2, build_full_view(self.inv, ViewOptions()))
        # el fallback devuelve acciones aplicables que reproducen la vista
        self.assertEqual(apply_actions(view, a1), v1)
        self.assertEqual(apply_actions(view, a2), v2)


class TestMetricasYDiff(InvariantBase):
    """Métricas del resumen y transiciones de la ventana de diferencias."""

    def test_incidencias_desconocidos_y_sobrantes(self):
        view = build_full_view(self.inv, ViewOptions())
        self.assertEqual(view.metrics.scanned_count, 0)
        self.assertEqual(view.metrics.relevant_diffs, 0)
        # 2 desconocidos → unlisted 2
        self.scan("UNKNOWN_A")
        self.scan("UNKNOWN_B")
        view = build_full_view(self.inv, ViewOptions())
        self.assertEqual(view.metrics.scanned_count, 2)
        self.assertEqual(view.metrics.relevant_diffs, 2)
        # SKU000 esperado 1, escaneo 3 → excess 2
        self.scan("SKU000")
        self.scan("SKU000")
        self.scan("SKU000")
        view = build_full_view(self.inv, ViewOptions())
        self.assertEqual(view.metrics.scanned_count, 5)
        self.assertEqual(view.metrics.relevant_diffs, 4)   # 2 unknown + 2 excess
        self.assertEqual(view.metrics.diff_net, 5 - 60)
        self.assertEqual(view.metrics.expected_count, 60)

    def test_metricas_incrementales(self):
        skus = ["UNKNOWN_A", "SKU000", "UNKNOWN_B", "SKU000", "SKU000"]
        view = self.run_sequence(skus, ViewOptions())
        self.assertEqual(view.metrics.scanned_count, 5)
        self.assertEqual(view.metrics.relevant_diffs, 4)
        self.assertEqual(view.metrics.diff_net, 5 - 60)

    def test_diff_transiciones_incrementales(self):
        """faltante → completo → sobrante, vía incremental."""
        opts = ViewOptions()
        view = build_full_view(self.inv, opts)
        self.assertIn("SKU001", {r.values[1] for r in view.diff})

        ev1 = self.scan("SKU001")
        view, _ = apply_event(view, ev1, self.inv, opts)
        self.assert_invariant(view, opts)
        self.assertNotIn("SKU001", {r.values[1] for r in view.diff})

        ev2 = self.scan("SKU001")
        view, _ = apply_event(view, ev2, self.inv, opts)
        self.assert_invariant(view, opts)
        diff = {r.values[1]: r for r in view.diff}
        self.assertEqual(diff["SKU001"].values[5], "+1")
        self.assertEqual(diff["SKU001"].values[6], "SOBRANTE")

    def test_diff_desconocido_incremental(self):
        opts = ViewOptions()
        view = build_full_view(self.inv, opts)
        self.assertNotIn("UNKNOWN_X", {r.values[1] for r in view.diff})
        ev = self.scan("UNKNOWN_X")
        view, _ = apply_event(view, ev, self.inv, opts)
        self.assert_invariant(view, opts)
        diff = {r.values[1]: r for r in view.diff}
        self.assertEqual(diff["UNKNOWN_X"].values[5], "+1")
        self.assertEqual(diff["UNKNOWN_X"].values[6], "SOBRANTE")


class TestModosAgrupados(InvariantBase):
    """Modos Alfabético y Cantidad (una fila por SKU, sin QRs)."""

    def test_alfabetico(self):
        opts = ViewOptions(sort_mode=SORT_ALPHA)
        view = self.run_sequence(
            ["SKU001", "SKU001", "SKU001", "SKU002", "UNKNOWN_X", "UNKNOWN_X"], opts
        )
        codes = [r.values[1] for r in view.scanned]
        self.assertEqual(codes, sorted(codes))
        by_code = {r.values[1]: r for r in view.scanned}
        self.assertEqual(by_code["SKU001"].values[2], "3")
        self.assertEqual(by_code["UNKNOWN_X"].values[2], "2")
        self.assertFalse(any(self.inv.is_qr_code(r.values[1]) for r in view.scanned))

    def test_cantidad(self):
        opts = ViewOptions(sort_mode=SORT_QTY)
        view = self.run_sequence(
            ["SKU001", "SKU001", "SKU001", "SKU002", "UNKNOWN_X", "UNKNOWN_X"], opts
        )
        qtys = [int(r.values[2]) for r in view.scanned]
        self.assertEqual(qtys, sorted(qtys, reverse=True))

    def test_cantidad_reorden_con_moves(self):
        opts = ViewOptions(sort_mode=SORT_QTY)
        for s in ["SKU001", "SKU001", "SKU001", "SKU002"]:
            self.scan(s)
        prev = build_full_view(self.inv, opts)
        self.assertEqual([r.values[1] for r in prev.scanned], ["SKU001", "SKU002"])

        # SKU002 pasa de 1 a 4 unidades → debe quedar primero
        evs = [self.scan("SKU002"), self.scan("SKU002"), self.scan("SKU002")]
        view = prev
        for ev in evs:
            view, _ = apply_event(view, ev, self.inv, opts)
            self.assert_invariant(view, opts)
        self.assertEqual([r.values[1] for r in view.scanned], ["SKU002", "SKU001"])

        # las acciones del último evento reproducen la vista nueva (con moves)
        view2, actions = apply_event(prev, evs[-1], self.inv, opts)
        self.assert_invariant(view2, opts)
        self.assertTrue(any(a.op == "move" for a in actions))
        rebuilt = apply_actions(prev, actions)
        self.assertEqual(rebuilt.scanned, view2.scanned)

    def test_qrs_invisibles_en_agrupado(self):
        opts = ViewOptions(sort_mode=SORT_ALPHA)
        view = self.run_sequence(["@CAJA_1", "SKU001", "%MUEBLE_1"], opts)
        codes = [r.values[1] for r in view.scanned]
        self.assertEqual(codes, ["SKU001"])
        # los QRs no alteran las métricas en modo agrupado
        self.assertEqual(view.metrics.scanned_count, 1)


class TestWorkerPropagaRefreshCompleto(InvariantBase):
    """El refresh del worker debe llevar sku/pos/is_qr/replaced/old_sku para
    que el poll pueda resolver el evento incremental sin re-derivar nada.
    (Conexión pipeline -> poll -> updates, sin Tk.)"""

    def _drain(self, worker, event_q, result_q, stop, n):
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            if event_q.empty() and result_q.qsize() >= n:
                break
            time.sleep(0.02)
        stop.set()
        t.join(timeout=3)
        return t

    def test_refresh_lleva_campos_del_evento(self):
        event_q = queue.Queue()
        result_q = queue.Queue()
        stop = threading.Event()
        worker = ScanWorker(self.inv, event_q, result_q, stop)

        event_q.put({"sku": "SKU001", "pos": 1, "fam": "AM", "ts": time.time(), "replaced": False, "is_qr": False, "old_sku": None})
        event_q.put({"sku": "@CAJA_1", "pos": 2, "fam": "QR", "ts": time.time(), "replaced": True, "is_qr": True, "old_sku": "@CAJA_0"})

        t = self._drain(worker, event_q, result_q, stop, 2)
        self.assertFalse(t.is_alive(), "el worker debería terminar con stop_event")

        refreshes = [r for r in list(result_q.queue) if r.get("type") == "refresh"]
        self.assertEqual(len(refreshes), 2)
        by_sku = {r["sku"]: r for r in refreshes}

        r1 = by_sku["SKU001"]
        self.assertEqual(r1["pos"], 1)
        self.assertFalse(r1["is_qr"])
        self.assertFalse(r1["replaced"])

        r2 = by_sku["@CAJA_1"]
        self.assertEqual(r2["pos"], 2)
        self.assertTrue(r2["is_qr"])
        self.assertTrue(r2["replaced"])
        self.assertEqual(r2["old_sku"], "@CAJA_0")

    def test_refresh_resuelve_evento_incremental(self):
        """Integración: un refresh del worker real alimenta apply_event y el
        resultado es idéntico al rebuild (flujo completo sin Tk)."""
        event_q = queue.Queue()
        result_q = queue.Queue()
        stop = threading.Event()
        worker = ScanWorker(self.inv, event_q, result_q, stop)

        opts = ViewOptions()
        # Vista inicial con el modelo AÚN vacío (el poll corre con backlog)
        view = build_full_view(self.inv, opts)

        # Modelo ya mutado (el add ocurre en el <Return>, antes del poll)
        res1 = self.inv.add_item("SKU001")
        res2 = self.inv.add_item("@CAJA_1")

        event_q.put({"sku": "SKU001", "pos": res1["pos"], "fam": "AM", "ts": time.time(), "replaced": False, "is_qr": False})
        event_q.put({"sku": "@CAJA_1", "pos": res2["pos"], "fam": "QR", "ts": time.time(), "replaced": False, "is_qr": True})

        self._drain(worker, event_q, result_q, stop, 2)

        for r in list(result_q.queue):
            if r.get("type") == "refresh":
                view, _ = apply_event(view, r, self.inv, opts)
        self.assertEqual(view, build_full_view(self.inv, opts))
        # El QR se muestra con su icono de plegado (▼ ) como en la app real
        self.assertEqual([row.values[1] for row in view.scanned], ["SKU001", "▼ @CAJA_1"])
        self.assertEqual(self.inv.scan_sequence, ["SKU001", "@CAJA_1"])


if __name__ == "__main__":
    unittest.main()
