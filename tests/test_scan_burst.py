"""
Pruebas de ráfaga de escaneo — Stock Cellular Center V8.0 (Fase A)

Objetivo fundamental:
    "Si introduzco N códigos, el sistema registra exactamente N códigos
     y conserva exactamente su orden."

Cubre:
- Ráfaga de 50 códigos (registro exacto y orden).
- Códigos repetidos.
- Códigos inexistentes en el CSV.
- Worker real con colas: 50 eventos -> 50 resultados, orden preservado.
- Un error en el worker no lo mata ni detiene la cola.
- compute_scan_alerts es puro (sin Tkinter).
"""

import os
import sys
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
from src.core.scanpipeline import compute_scan_alerts, ScanWorker


class TestScanBurst(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {"excluded_skus": []}
        self.inv = InventoryManager(self.config)
        self.inv.scan_dir = self.temp_dir
        self.inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        self.inv.main_stock = self.inv.load_main_stock()

        # 60 SKUs de catálogo
        self.inv.stock_data = [(f"SKU{i:03d}", f"Desc {i}", 1) for i in range(60)]
        self.inv.original_quantities = {f"SKU{i:03d}": 1 for i in range(60)}
        self.inv.full_family_map = {f"SKU{i:03d}": "AM" for i in range(60)}

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_burst_50_registers_exactly_and_in_order(self):
        """50 códigos consecutivos: 50 registros, exactamente en orden."""
        codes = [f"SKU{i:03d}" for i in range(50)]
        results = [self.inv.add_item(c) for c in codes]

        self.assertEqual(len(self.inv.scan_sequence), 50)
        self.assertEqual(self.inv.scan_sequence, codes)          # orden exacto
        self.assertEqual(len(self.inv.scanned_items), 50)        # 50 claves únicas
        for c in codes:
            self.assertEqual(len(self.inv.scanned_items.get(c, [])), 1)  # 1 vez c/u
        positions = [r["pos"] for r in results]
        self.assertEqual(positions, list(range(1, 51)))          # posiciones 1..50

    def test_burst_30_repeated_codes(self):
        """30 repeticiones del mismo código: se registran todas, en orden."""
        codes = ["SKU000"] * 30
        for c in codes:
            self.inv.add_item(c)
        self.assertEqual(self.inv.scan_sequence, codes)
        self.assertEqual(len(self.inv.scanned_items["SKU000"]), 30)

    def test_burst_mixed_repeated(self):
        """Ráfaga mixta con repeticiones: orden exacto y conteos exactos."""
        codes = (["SKU000"] * 25) + (["SKU001"] * 15) + (["SKU002"] * 10)
        for c in codes:
            self.inv.add_item(c)
        self.assertEqual(self.inv.scan_sequence, codes)
        self.assertEqual(len(self.inv.scanned_items["SKU000"]), 25)
        self.assertEqual(len(self.inv.scanned_items["SKU001"]), 15)
        self.assertEqual(len(self.inv.scanned_items["SKU002"]), 10)

    def test_burst_unknown_codes(self):
        """Códigos inexistentes en el CSV: se registran igual (aviso, no bloqueo)."""
        codes = [f"UNKNOWN{i:03d}" for i in range(50)]
        for c in codes:
            self.inv.add_item(c)
        self.assertEqual(self.inv.scan_sequence, codes)
        self.assertEqual(len(self.inv.scan_sequence), 50)

    def test_alerts_pure_no_tk(self):
        """compute_scan_alerts devuelve dicts serializables, sin Tkinter."""
        self.inv.add_item("@CAJA_1")
        self.inv.add_item("SKU005")
        alerts = compute_scan_alerts(self.inv, "SKU005", pos=2)
        self.assertIsInstance(alerts, list)
        for a in alerts:
            self.assertEqual(a["type"], "toast")
            self.assertIn("msg", a)
            self.assertIn("mtype", a)

    def test_unknown_code_alert(self):
        """SKU inexistente -> alerta 'no pertenece al listado'."""
        self.inv.add_item("UNKNOWN999")
        alerts = compute_scan_alerts(self.inv, "UNKNOWN999", pos=1)
        self.assertTrue(any("no pertenece" in a["msg"] for a in alerts))

    def test_worker_50_events_order_and_refresh_count(self):
        """Worker real: 50 eventos -> 50 refreshes en el MISMO orden."""
        event_q = queue.Queue()
        result_q = queue.Queue()
        stop = threading.Event()
        worker = ScanWorker(self.inv, event_q, result_q, stop)
        t = threading.Thread(target=worker.run, daemon=True)
        t.start()

        codes = [f"SKU{i:03d}" for i in range(50)]
        for i, c in enumerate(codes, start=1):
            event_q.put({"sku": c, "pos": i, "fam": "AM", "ts": time.time(), "replaced": False, "is_qr": False})

        # Esperar a que el worker consuma todo y publique resultados
        deadline = time.time() + 10
        while time.time() < deadline:
            if event_q.empty() and result_q.qsize() >= 50:
                break
            time.sleep(0.02)

        stop.set()
        t.join(timeout=3)

        self.assertFalse(t.is_alive(), "el worker debería terminar con stop_event")
        self.assertGreaterEqual(result_q.qsize(), 50, "deberían llegar al menos 50 resultados")

        refreshes = []
        while not result_q.empty():
            r = result_q.get()
            if r.get("type") == "refresh":
                refreshes.append(r.get("sku"))

        self.assertEqual(len(refreshes), 50)
        self.assertEqual(refreshes, codes, "el orden de los refreshes debe ser FIFO estricto")

    def test_worker_error_does_not_kill_queue(self):
        """Un error procesando un evento no detiene el worker ni pierde el siguiente."""
        event_q = queue.Queue()
        result_q = queue.Queue()
        stop = threading.Event()
        worker = ScanWorker(self.inv, event_q, result_q, stop)

        # Sabotear check_proximity para que falle SOLO la primera vez
        original = self.inv.check_proximity
        state = {"calls": 0}
        def broken_proximity(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("boom simulado")
            return original(*args, **kwargs)
        self.inv.check_proximity = broken_proximity

        t = threading.Thread(target=worker.run, daemon=True)
        t.start()

        event_q.put({"sku": "SKU000", "pos": 1, "fam": "AM", "ts": time.time(), "replaced": False, "is_qr": False})
        event_q.put({"sku": "SKU001", "pos": 2, "fam": "AM", "ts": time.time(), "replaced": False, "is_qr": False})

        deadline = time.time() + 10
        while time.time() < deadline:
            if event_q.empty() and result_q.qsize() >= 2:
                break
            time.sleep(0.02)

        stop.set()
        t.join(timeout=3)

        self.inv.check_proximity = original  # restaurar

        refreshes = []
        errors = 0
        while not result_q.empty():
            r = result_q.get()
            if r.get("type") == "refresh":
                refreshes.append(r.get("sku"))
            elif r.get("type") == "toast" and "Error procesando" in r.get("msg", ""):
                errors += 1

        # El primer evento falló pero no mató al worker; el segundo se procesó
        self.assertEqual(errors, 1, "debe registrarse exactamente 1 error")
        self.assertEqual(refreshes, ["SKU001"], "el segundo evento debe procesarse normalmente")


if __name__ == "__main__":
    unittest.main()
