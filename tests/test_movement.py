"""
Tests del movimiento ↑/↓ y drag & drop — Stock Cellular Center V8.0

Cubren el fix de reordenamiento manual (2026-08-28):
- ↑/↓ y drag & drop permitidos SOLO en modos secuenciales
  ("Último arriba" / "Último abajo"); bloqueados en "Alfabético" / "Cantidad".
- Dirección corregida según el modo visual:
    "Último abajo" : ↑ = índice real -1, ↓ = índice real +1
    "Último arriba": ↑ = índice real +1, ↓ = índice real -1
- Límites de movimiento en ambos modos.
- Consistencia de scan_sequence / scanned_items / scan_counter tras mover.

Estrategia: se ejecuta el código REAL de los handlers de StockApp
(_move_selected_up / _move_selected_down / _setup_drag_and_drop) sobre una
instancia creada con object.__new__ (sin Tk) y stubs mínimos del Treeview.
La única pieza "de verdad" además de los handlers es InventoryManager.
"""

import os
import sys
import unittest
from types import SimpleNamespace

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.inventory import InventoryManager
from src.main import StockApp

SORT_TOP = "Último arriba"
SORT_BOTTOM = "Último abajo"
SORT_ALPHA = "Alfabético"
SORT_QTY = "Cantidad"


def make_inventory(seq):
    inv = InventoryManager({"excluded_skus": []})
    inv.scan_sequence = list(seq)
    inv._rebuild_scanned_items()
    return inv


def make_app(sort_mode, seq):
    """Instancia StockApp sin Tk y con un Treeview stub fiel a la app real.

    - text de cada fila = posición REAL en scan_sequence (1-indexed), igual
      que genera updates._scanned_row_for.
    - _update_all_ui() re-renderiza los texts desde scan_sequence, igual que
      _rebuild_view_state en la app real.
    """
    app = StockApp.__new__(StockApp)

    items = {}
    state = {"sel": None, "drag_item": None}
    calls = []

    def rebuild():
        items.clear()
        for i, code in enumerate(app.inventory.scan_sequence):
            items[f"i{i}"] = str(i + 1)  # text = posición real (idx+1)
        state["sel"] = None

    def tree_factory():
        tree = SimpleNamespace(
            selection=lambda: [state["sel"]] if state["sel"] else [],
            item=lambda iid, what: items.get(iid, "") if what == "text" else (),
            get_children=lambda: list(items.keys()),
            selection_set=lambda iid: calls.append(("sel", iid)),
            see=lambda iid: calls.append(("see", iid)),
            identify_row=lambda y: f"i{y}",
            tag_configure=lambda *a, **k: None,
            bind=lambda *a, **k: None,
            winfo_height=lambda: 400,
            yview_scroll=lambda *a, **k: None,
        )
        return tree

    app.sort_var = SimpleNamespace(get=lambda: sort_mode)
    app.scanned_table = SimpleNamespace(tree=tree_factory())
    app.inventory = make_inventory(seq)
    app._update_all_ui = rebuild
    app._schedule_deferred_validation = lambda sku, pos: calls.append(("val", sku, pos))
    app.show_toast = lambda *a, **k: calls.append(("toast", a[0] if a else ""))

    rebuild()
    return app, state, calls


def assert_consistent(test, inv, seq):
    """scan_sequence, scanned_items y scan_counter consistentes."""
    test.assertEqual(inv.scan_sequence, seq)
    for code in set(seq):
        positions = [i + 1 for i, c in enumerate(seq) if c == code]
        test.assertEqual(inv.scanned_items.get(code, []), positions)
    test.assertEqual(inv.scan_counter, len(seq) + 1)


class TestMoveDirection(unittest.TestCase):
    """Dirección de ↑/↓ según el modo visual."""

    def test_bottom_up_decrements_index(self):
        app, state, _ = make_app(SORT_BOTTOM, ["A", "B", "C"])
        state["sel"] = "i1"  # B, pos 2 -> idx 1
        app._move_selected_up()
        # ↑ en modo abajo: idx 1 -> 0
        assert_consistent(self, app.inventory, ["B", "A", "C"])

    def test_bottom_down_increments_index(self):
        app, state, _ = make_app(SORT_BOTTOM, ["A", "B", "C"])
        state["sel"] = "i1"  # B, pos 2 -> idx 1
        app._move_selected_down()
        # ↓ en modo abajo: idx 1 -> 2
        assert_consistent(self, app.inventory, ["A", "C", "B"])

    def test_top_up_increments_index(self):
        app, state, _ = make_app(SORT_TOP, ["A", "B", "C"])
        state["sel"] = "i1"  # B, idx 1
        app._move_selected_up()
        # ↑ en modo arriba (pantalla invertida): idx 1 -> 2
        assert_consistent(self, app.inventory, ["A", "C", "B"])

    def test_top_down_decrements_index(self):
        app, state, _ = make_app(SORT_TOP, ["A", "B", "C"])
        state["sel"] = "i1"  # B, idx 1
        app._move_selected_down()
        # ↓ en modo arriba: idx 1 -> 0
        assert_consistent(self, app.inventory, ["B", "A", "C"])

    def test_reselects_moved_item_after_move(self):
        app, state, calls = make_app(SORT_BOTTOM, ["A", "B", "C"])
        state["sel"] = "i1"
        app._move_selected_up()
        # Tras mover B de idx1 -> idx0, el item re-renderizado con text "1"
        # (posición real 1) debe haber sido re-seleccionado.
        sel_calls = [c for c in calls if c[0] == "sel"]
        self.assertTrue(sel_calls)
        selected = sel_calls[-1][1]
        self.assertEqual(app.scanned_table.tree.item(selected, "text"), "1")

    def test_schedules_deferred_validation_with_new_pos(self):
        app, state, calls = make_app(SORT_BOTTOM, ["A", "B", "C"])
        state["sel"] = "i1"
        app._move_selected_up()
        val_calls = [c for c in calls if c[0] == "val"]
        self.assertEqual(val_calls, [("val", "B", 1)])  # B quedó en pos 1


class TestMoveLimits(unittest.TestCase):
    """Límites de movimiento según el modo."""

    def test_bottom_up_first_item_noop(self):
        app, state, _ = make_app(SORT_BOTTOM, ["A", "B", "C"])
        state["sel"] = "i0"  # A, idx 0: no puede subir
        app._move_selected_up()
        assert_consistent(self, app.inventory, ["A", "B", "C"])

    def test_bottom_down_last_item_noop(self):
        app, state, _ = make_app(SORT_BOTTOM, ["A", "B", "C"])
        state["sel"] = "i2"  # C, idx 2: no puede bajar
        app._move_selected_down()
        assert_consistent(self, app.inventory, ["A", "B", "C"])

    def test_top_up_last_scanned_noop(self):
        app, state, _ = make_app(SORT_TOP, ["A", "B", "C"])
        state["sel"] = "i2"  # C, idx 2 (fila superior visual): no puede subir
        app._move_selected_up()
        assert_consistent(self, app.inventory, ["A", "B", "C"])

    def test_top_down_first_scanned_noop(self):
        app, state, _ = make_app(SORT_TOP, ["A", "B", "C"])
        state["sel"] = "i0"  # A, idx 0 (fila inferior visual): no puede bajar
        app._move_selected_down()
        assert_consistent(self, app.inventory, ["A", "B", "C"])


class TestAllowedModes(unittest.TestCase):
    """Modos que permiten / bloquean el reordenamiento."""

    def _assert_blocked(self, sort_mode):
        app, state, calls = make_app(sort_mode, ["A", "B", "C"])
        state["sel"] = "i1"
        app._move_selected_up()
        app._move_selected_down()
        assert_consistent(self, app.inventory, ["A", "B", "C"])  # nada se movió
        toasts = [c for c in calls if c[0] == "toast"]
        self.assertEqual(len(toasts), 2)
        self.assertIn("secuenciales", toasts[0][1])

    def test_alpha_blocked(self):
        self._assert_blocked(SORT_ALPHA)

    def test_qty_blocked(self):
        self._assert_blocked(SORT_QTY)

    def test_sequential_modes_allowed(self):
        # ↑ en cada modo secuencial: no bloquea y mueve en su dirección
        cases = [(SORT_BOTTOM, ["B", "A", "C"]), (SORT_TOP, ["A", "C", "B"])]
        for mode, expected in cases:
            app, state, calls = make_app(mode, ["A", "B", "C"])
            state["sel"] = "i1"
            app._move_selected_up()
            self.assertFalse(
                [c for c in calls if c[0] == "toast"],
                f"{mode} no debería bloquear el movimiento",
            )
            assert_consistent(self, app.inventory, expected)


class TestDragAndDrop(unittest.TestCase):
    """Drag & drop: habilitado solo en modos secuenciales, índices reales."""

    def _setup_drag(self, app, state):
        """Ejecuta _setup_drag_and_drop y captura las closures reales."""
        tree = app.scanned_table.tree
        handlers = {}

        def fake_bind(seq, func, **kw):
            handlers[seq] = func

        tree.bind = fake_bind
        app._setup_drag_and_drop()
        return handlers

    def test_press_blocked_in_alpha(self):
        app, state, _ = make_app(SORT_ALPHA, ["A", "B", "C"])
        handlers = self._setup_drag(app, state)
        handlers["<ButtonPress-1>"](SimpleNamespace(y=0))
        self.assertIsNone(app._drag_item)

    def test_press_allowed_in_sequential(self):
        for mode in (SORT_TOP, SORT_BOTTOM):
            app, state, _ = make_app(mode, ["A", "B", "C"])
            handlers = self._setup_drag(app, state)
            handlers["<ButtonPress-1>"](SimpleNamespace(y=0))
            self.assertEqual(app._drag_item, "i0", f"press debería activarse en {mode}")

    def test_release_moves_using_real_positions(self):
        app, state, _ = make_app(SORT_BOTTOM, ["A", "B", "C"])
        handlers = self._setup_drag(app, state)
        handlers["<ButtonPress-1>"](SimpleNamespace(y=0))  # A (text 1 -> idx 0)
        handlers["<ButtonRelease-1>"](SimpleNamespace(y=2))  # C (text 3 -> idx 2)
        # A movido a idx 2 usando posiciones REALES del text
        assert_consistent(self, app.inventory, ["B", "C", "A"])


if __name__ == "__main__":
    unittest.main()
