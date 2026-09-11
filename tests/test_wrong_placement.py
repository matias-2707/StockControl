"""
Tests de regresión: _resolve_wrong_placement sin traceback ante metadata None.

Caso real (evidencia): click en una notificación del historial que no tiene
metadata de ubicación (o cuyo metadata no es un dict completo) lanzaba:
    TypeError: 'NoneType' object is not subscriptable
    File "src/main.py", line 1871, in _resolve_wrong_placement
        sku = meta["sku"]

Causa raíz: la ventana de historial bindea el resolver a TODAS las
notificaciones no resueltas, pero solo las alertas de "mal guardado" llevan
metadata. Click en cualquier otra -> crash. El bug es HEREDADO de V7.1
(misma estructura sin defensa); no es nuevo de V8.

Solución: get_placement_meta() (pura) valida la metadata; la UI solo bindea
el resolver a alertas resolvibles; el callback defiende el caso None con
logging del contexto y aviso informativo (no oculta el estado: lo registra).
"""

import os
import sys
import unittest
from unittest import mock

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.main import StockApp, get_placement_meta  # noqa: E402


VALID_META = {
    "sku": "IPHONE13",
    "pos": 3,
    "current_container": "@CAJA_1",
    "expected_container": "@CAJA_2",
}


class TestGetPlacementMeta(unittest.TestCase):
    def test_metadata_none_rejected(self):
        item = {"time": "12:00", "msg": "x", "metadata": None, "resolved": False}
        meta, reason = get_placement_meta(item)
        self.assertIsNone(meta)
        self.assertIn("metadata ausente", reason)

    def test_item_without_metadata_key(self):
        item = {"time": "12:00", "msg": "x", "resolved": False}
        meta, reason = get_placement_meta(item)
        self.assertIsNone(meta)
        self.assertIn("metadata ausente", reason)

    def test_item_not_dict(self):
        meta, reason = get_placement_meta("soy un string")
        self.assertIsNone(meta)
        self.assertIn("no es dict", reason)

    def test_metadata_incomplete(self):
        item = {"metadata": {"sku": "IPHONE13"}}
        meta, reason = get_placement_meta(item)
        self.assertIsNone(meta)
        self.assertIn("incompleta", reason)

    def test_valid_metadata_accepted(self):
        item = {"metadata": dict(VALID_META)}
        meta, reason = get_placement_meta(item)
        self.assertEqual(meta, VALID_META)
        self.assertIsNone(reason)


class TestResolveWrongPlacementGuard(unittest.TestCase):
    """El callback NO debe lanzar traceback ante una interacción normal."""

    def _bound_method(self, app):
        # bindea el método real a una instancia mock (sin Tk)
        return StockApp._resolve_wrong_placement.__get__(app, type(app))

    def test_no_traceback_when_metadata_none(self):
        app = mock.MagicMock()
        app.toast_history = [{"time": "12:00", "msg": "aviso sin metadata",
                              "metadata": None, "resolved": False}]
        item = app.toast_history[0]
        bound = self._bound_method(app)
        # no debe lanzar
        bound(item, mock.MagicMock())
        app.show_toast.assert_called_once()
        # y no debe abrir el diálogo de resolución
        self.assertFalse(any(
            c[0] == "ctk.CTkToplevel" for c in app.method_calls
        ))

    def test_no_traceback_when_item_is_not_dict(self):
        app = mock.MagicMock()
        bound = self._bound_method(app)
        bound("nota suelta sin estructura", mock.MagicMock())  # no debe lanzar
        app.show_toast.assert_called_once()

    def test_metadata_incomplete_logs_and_returns(self):
        app = mock.MagicMock()
        item = {"msg": "alerta", "metadata": {"sku": "X"}}
        bound = self._bound_method(app)
        bound(item, mock.MagicMock())  # no debe lanzar
        app.show_toast.assert_called_once()


if __name__ == "__main__":
    unittest.main()
