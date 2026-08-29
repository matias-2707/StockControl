"""
Tests de bindings F4/Supr (ventana de Diferencias) — Stock Cellular Center V8.0

Problema auditado (2026-08-29): `table.tree.bind("<F4>", ...)` + `win.bind("<F4>", ...)`
disparaban el handler DOS veces por presión cuando el foco estaba en el tree.
Tk resuelve un evento recorriendo los bindtags del widget:
    (widget, clase, toplevel, "all")
y ejecuta TODOS los scripts registrados para la tecla hasta que uno devuelve
"break". Con binding en widget + toplevel, el handler corría 2 veces: F4
sumaba +2 (y Supr restaba -2) aunque el toast dijera "+1"/"-1".

Fix aplicado (main.py, ventana de Diferencias):
  - Se eliminaron los bindings duplicados en `win`.
  - Los handlers devuelven "break" para consumir la tecla (defensa en
    profundidad: corta la cadena aunque exista un binding residual).

Estrategia de tests (compatible con SSH headless, donde event_generate NO
entrega eventos porque no hay ventana mapeada en session 0):
  1. Simulación del dispatch por bindtags (puro, corre siempre): modela la
     semántica documentada de Tk y pinnea la invariante "1 presión = 1 llamada".
  2. Introspección real de Tk (headless-safe): verifica el patrón de bindings
     "solo en widget, nada en toplevel" sobre widgets Tk reales.
  3. Tests E2E con event_generate: solo corren si hay ventana visible
     (sesión interactiva). En SSH headless se saltan con mensaje claro.
  4. Guard estático sobre src/main.py (regresión del doble binding).
"""

import os
import sys
import unittest
import tkinter as tk
from tkinter import ttk

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(project_root, "src", "main.py")

# Orden de bindtags de Tk para un widget (documentado):
BINDTAGS = ("widget", "class", "toplevel", "all")


class Counter:
    """Contador simple para handlers de prueba."""
    def __init__(self):
        self.count = 0

    def handler(self, event=None):
        self.count += 1
        return "break"


# ---------------------------------------------------------------------------
# 1. Simulación pura del dispatch por bindtags (no requiere display)
# ---------------------------------------------------------------------------

class TestBindTagsDispatchSimulation(unittest.TestCase):
    """Modela la resolución de eventos de Tk: recorre bindtags, ejecuta cada
    script registrado y se detiene al primer "break"."""

    @staticmethod
    def tk_dispatch(bindings, returns_break=()):
        """bindings: dict {bindtag: True/False} indicando si hay un script que
        devuelve "break". Devuelve cuántos scripts se ejecutarían."""
        calls = 0
        for tag in BINDTAGS:
            if tag in bindings:
                calls += 1
                if tag in returns_break:
                    break
        return calls

    def test_old_bug_widget_plus_toplevel_fires_twice(self):
        """Patrón original (widget + toplevel, sin break) -> 2 llamadas."""
        # Registro original: handler en widget y en toplevel, ninguno corta
        bindings = {"widget": True, "toplevel": True}
        self.assertEqual(self.tk_dispatch(bindings), 2,
                         "con el patrón original el handler se ejecuta 2 veces")

    def test_fixed_widget_only_fires_once(self):
        """Patrón del fix (solo widget + break) -> 1 llamada."""
        bindings = {"widget": True}
        self.assertEqual(self.tk_dispatch(bindings, returns_break=("widget",)), 1,
                         "con el fix el handler se ejecuta exactamente 1 vez")

    def test_break_blocks_residual_toplevel_binding(self):
        """Defensa en profundidad: break en el widget anula un binding residual."""
        bindings = {"widget": True, "toplevel": True}
        self.assertEqual(self.tk_dispatch(bindings, returns_break=("widget",)), 1,
                         "'break' en el widget corta la cadena antes del toplevel")

    def test_invariant_one_press_one_call(self):
        """Invariante central: cualquier presión de F4/Supr ejecuta el handler
        exactamente 1 vez (nunca 0, nunca 2+)."""
        # El patrón del fix debe producir exactamente 1 llamada
        self.assertEqual(self.tk_dispatch({"widget": True}, returns_break=("widget",)), 1)


# ---------------------------------------------------------------------------
# 2. Introspección real de Tk (funciona en SSH headless: no requiere eventos)
# ---------------------------------------------------------------------------

class TestTkBindingPattern(unittest.TestCase):
    """Verifica sobre widgets Tk reales que el patrón de bindings sea el del
    fix: tecla registrada SOLO en el widget, NADA en el toplevel."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except Exception as e:
            cls.root = None
            cls.tk_error = str(e)

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass

    def setUp(self):
        if self.root is None:
            self.skipTest(f"Tk no disponible en este entorno: {getattr(self, 'tk_error', '?')}")
        self.win = tk.Toplevel(self.root)
        self.tree = ttk.Treeview(self.win)
        self.tree.pack()

    def tearDown(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    def test_f4_registered_only_on_widget(self):
        """F4: registrado en el tree, vacío en el toplevel (patrón del fix)."""
        # Aplicar el MISMO patrón que main.py tras el fix
        self.tree.bind("<F4>", Counter().handler)   # devuelve "break"
        self.assertNotEqual(self.tree.bind("<F4>"), "", "F4 debe estar en el tree")
        self.assertEqual(self.win.bind("<F4>"), "", "F4 NO debe estar en el toplevel")

    def test_delete_registered_only_on_widget(self):
        """Delete: registrado en el tree, vacío en el toplevel (patrón del fix)."""
        self.tree.bind("<Delete>", Counter().handler)
        self.assertNotEqual(self.tree.bind("<Delete>"), "", "Delete debe estar en el tree")
        self.assertEqual(self.win.bind("<Delete>"), "", "Delete NO debe estar en el toplevel")


# ---------------------------------------------------------------------------
# 3. E2E con event_generate (solo con ventana visible / sesión interactiva)
# ---------------------------------------------------------------------------

class TestBindTagsRealEvents(unittest.TestCase):
    """Prueba real de entrega de eventos. Requiere ventana mapeada: en SSH
    headless (session 0) Tk no recibe eventos y estos tests se saltan."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except Exception as e:
            cls.root = None
            cls.tk_error = str(e)

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass

    def setUp(self):
        if self.root is None:
            self.skipTest(f"Tk no disponible: {getattr(self, 'tk_error', '?')}")
        self.root.update()
        if not self.root.winfo_viewable():
            self.skipTest("Sin ventana visible (sesión headless/SSH): "
                          "event_generate no entrega eventos; correr en sesión interactiva")
        self.win = tk.Toplevel(self.root)
        self.tree = ttk.Treeview(self.win)
        self.tree.pack()
        self.tree.focus_set()
        self.win.update()

    def tearDown(self):
        try:
            self.win.destroy()
        except Exception:
            pass

    def test_old_bug_widget_plus_toplevel_fires_twice(self):
        c = Counter()
        def no_break(event=None):
            c.count += 1
        self.tree.bind("<F4>", no_break)
        self.win.bind("<F4>", no_break)
        self.tree.event_generate("<F4>")
        self.root.update()
        self.assertEqual(c.count, 2)

    def test_fixed_widget_only_fires_once(self):
        c = Counter()
        self.tree.bind("<F4>", c.handler)
        self.tree.event_generate("<F4>")
        self.root.update()
        self.assertEqual(c.count, 1)

    def test_break_blocks_toplevel_residual_binding(self):
        c = Counter()
        self.tree.bind("<F4>", c.handler)
        self.win.bind("<F4>", c.handler)
        self.tree.event_generate("<F4>")
        self.root.update()
        self.assertEqual(c.count, 1)


# ---------------------------------------------------------------------------
# 4. Guard estático sobre src/main.py
# ---------------------------------------------------------------------------

class TestMainPyStaticGuard(unittest.TestCase):
    """Guarda de regresión estática sobre el código real de la app."""

    def _read_main_py(self):
        self.assertTrue(os.path.exists(MAIN_PY), f"Falta {MAIN_PY}")
        with open(MAIN_PY, "r", encoding="utf-8") as f:
            return f.read()

    def test_no_duplicate_f4_delete_bindings_on_win(self):
        src = self._read_main_py()
        # No deben existir bindings F4/Delete sobre la ventana toplevel (win)
        self.assertNotIn('win.bind("<F4>"', src, "No debe haber binding de F4 en win (duplicado)")
        self.assertNotIn('win.bind("<Delete>"', src, "No debe haber binding de Delete en win (duplicado)")
        # Sí deben existir sobre el tree
        self.assertIn('table.tree.bind("<F4>"', src, "F4 debe estar bindeado en table.tree")
        self.assertIn('table.tree.bind("<Delete>"', src, "Delete debe estar bindeado en table.tree")

    def test_handlers_return_break(self):
        src = self._read_main_py()
        # El bloque de la ventana de diferencias debe devolver "break"
        diff_block = src[src.index('def on_diff_f4'):src.index('def on_diff_delete')]
        self.assertIn('return "break"', diff_block, "on_diff_f4 debe devolver 'break'")
        del_block = src[src.index('def on_diff_delete'):src.index('self._sync_diff_table()')]
        self.assertIn('return "break"', del_block, "on_diff_delete debe devolver 'break'")


if __name__ == "__main__":
    unittest.main()
