import os
import sys
import unittest
import tempfile
import json
import shutil

# Asegurar root en sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.inventory import InventoryManager

class TestMainStockAndLocations(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_config = {"excluded_skus": []}
        self.inv = InventoryManager(self.mock_config)
        self.inv.scan_dir = self.temp_dir
        self.inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        self.inv.main_stock = self.inv.load_main_stock()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_main_stock_init_and_save(self):
        self.assertEqual(self.inv.main_stock["version"], "8.0")
        self.inv.main_stock["product_locations"]["IPHONE13"] = "@CAJA_1"
        self.assertTrue(self.inv.save_main_stock())

        # Reload
        reloaded = self.inv.load_main_stock()
        self.assertEqual(reloaded["product_locations"]["IPHONE13"], "@CAJA_1")

    def test_update_product_location(self):
        self.inv.original_quantities["IPHONE13"] = 5
        self.assertTrue(self.inv.update_product_location("IPHONE13", "@CAJA_1"))
        
        self.assertEqual(self.inv.get_expected_container_for_sku("IPHONE13"), "@CAJA_1")
        expected_items = self.inv.get_container_expected_items("@CAJA_1")
        self.assertEqual(expected_items.get("IPHONE13"), 5)

        # Mover a otra caja
        self.assertTrue(self.inv.update_product_location("IPHONE13", "@CAJA_2"))
        self.assertEqual(self.inv.get_expected_container_for_sku("IPHONE13"), "@CAJA_2")
        self.assertNotIn("IPHONE13", self.inv.get_container_expected_items("@CAJA_1"))
        self.assertEqual(self.inv.get_container_expected_items("@CAJA_2").get("IPHONE13"), 5)

    def test_non_linear_scans_and_containers(self):
        # Secuencia no lineal:
        # %MUEBLE_A -> @CAJA_1 -> PROD_A -> %MUEBLE_B -> @CAJA_2 -> PROD_B -> %MUEBLE_A -> @CAJA_3 -> PROD_C
        seq = [
            "%MUEBLE_A", "@CAJA_1", "PROD_A",
            "%MUEBLE_B", "@CAJA_2", "PROD_B",
            "%MUEBLE_A", "@CAJA_3", "PROD_C"
        ]
        self.inv.scan_sequence = seq
        self.inv._rebuild_scanned_items()

        # PROD_A está en index 2
        box, sec = self.inv.get_containers_for_index(seq, 2)
        self.assertEqual(box, "@CAJA_1")
        self.assertEqual(sec, "%MUEBLE_A")

        # PROD_B está en index 5
        box, sec = self.inv.get_containers_for_index(seq, 5)
        self.assertEqual(box, "@CAJA_2")
        self.assertEqual(sec, "%MUEBLE_B")

        # PROD_C está en index 8
        box, sec = self.inv.get_containers_for_index(seq, 8)
        self.assertEqual(box, "@CAJA_3")
        self.assertEqual(sec, "%MUEBLE_A")

    def test_backward_scan_association(self):
        # Usuario escanea productos primero y el QR de la caja después:
        # PROD_1 -> PROD_2 -> @CAJA_A
        seq = ["PROD_1", "PROD_2", "@CAJA_A"]
        self.inv.scan_sequence = seq

        box1, _ = self.inv.get_containers_for_index(seq, 0)
        self.assertEqual(box1, "@CAJA_A")

        box2, _ = self.inv.get_containers_for_index(seq, 1)
        self.assertEqual(box2, "@CAJA_A")

    def test_container_status_and_checkmark(self):
        # Configurar 2 items esperados en @CAJA_X
        self.inv.original_quantities["CASE_IP12"] = 2
        self.inv.update_product_location("CASE_IP12", "@CAJA_X")

        seq = ["@CAJA_X", "CASE_IP12"]
        status1 = self.inv.get_container_status("@CAJA_X", seq)
        self.assertEqual(status1["expected_total"], 2)
        self.assertEqual(status1["scanned_count"], 1)
        self.assertEqual(status1["missing_count"], 1)
        self.assertEqual(status1["display_str"], "1")
        self.assertFalse(status1["is_complete"])

        # Escanear el segundo
        seq.append("CASE_IP12")
        status2 = self.inv.get_container_status("@CAJA_X", seq)
        self.assertEqual(status2["scanned_count"], 2)
        self.assertEqual(status2["missing_count"], 0)
        self.assertEqual(status2["display_str"], "✓")
        self.assertTrue(status2["is_complete"])

    def test_location_discrepancy_check(self):
        self.inv.update_product_location("IPHONE14", "@CAJA_CORRECTA")
        
        # Escaneado en @CAJA_INCORRECTA
        self.inv.scan_sequence = ["@CAJA_INCORRECTA", "IPHONE14"]
        self.inv._rebuild_scanned_items()

        discrepancy = self.inv.check_proximity("IPHONE14", pos=2)
        self.assertIsNotNone(discrepancy)
        self.assertEqual(discrepancy["sku"], "IPHONE14")
        self.assertEqual(discrepancy["current_container"], "@CAJA_INCORRECTA")
        self.assertEqual(discrepancy["expected_container"], "@CAJA_CORRECTA")

    def test_move_product_to_container(self):
        # Secuencia inicial: @CAJA_A -> PROD_1 -> @CAJA_B -> PROD_2
        self.inv.scan_sequence = ["@CAJA_A", "PROD_1", "@CAJA_B", "PROD_2"]
        self.inv._rebuild_scanned_items()

        # Mover PROD_1 (pos 2) a @CAJA_B
        self.assertTrue(self.inv.move_product_to_container("PROD_1", pos=2, target_container="@CAJA_B"))
        
        # Ahora la secuencia debe ser: @CAJA_A -> @CAJA_B -> PROD_1 -> PROD_2
        self.assertEqual(self.inv.scan_sequence, ["@CAJA_A", "@CAJA_B", "PROD_1", "PROD_2"])
        box, _ = self.inv.get_containers_for_index(self.inv.scan_sequence, 2)
        self.assertEqual(box, "@CAJA_B")

if __name__ == "__main__":
    unittest.main()
