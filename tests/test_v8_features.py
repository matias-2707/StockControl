import os
import sys
import unittest
import tempfile
import json
import shutil

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.inventory import InventoryManager
from src.core.images import ImageManager

class TestV8Features(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config = {
            "excluded_skus": ["EXCLUDE_ME"],
            "image_folder": os.path.join(self.temp_dir, "img"),
            "list_order": "bottom"
        }
        self.inv = InventoryManager(self.config)
        self.inv.scan_dir = self.temp_dir
        self.inv.main_stock_file = os.path.join(self.temp_dir, "main_stock.json")
        self.inv.main_stock = self.inv.load_main_stock()
        
        # Cargar datos simulados de stock
        self.inv.stock_data = [
            ("IPHONE13_128", "iPhone 13 128GB", 10),
            ("SAMSUNGS23", "Samsung S23 256GB", 5),
            ("AIRPODSPRO", "AirPods Pro 2", 3)
        ]
        self.inv.original_quantities = {
            "IPHONE13_128": 10,
            "SAMSUNGS23": 5,
            "AIRPODSPRO": 3
        }
        self.inv.full_family_map = {
            "IPHONE13_128": "AM",
            "SAMSUNGS23": "AM",
            "AIRPODSPRO": "AG"
        }

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_qr_codes_excluded_from_stock_count(self):
        """Requisito 13: Los códigos @, %, # no deben ser contados como stock."""
        self.inv.add_item("%MUEBLE_1")
        self.inv.add_item("@CAJA_A")
        self.inv.add_item("IPHONE13_128")
        self.inv.add_item("IPHONE13_128")
        self.inv.add_item("#VIDRIERA_CENTRAL")
        self.inv.add_item("SAMSUNGS23")

        # Conteo físico total excluyendo QRs
        physical_scans = sum(len(p) for code, p in self.inv.scanned_items.items() if not self.inv.is_qr_code(code))
        self.assertEqual(physical_scans, 3) # 2 iPhones + 1 Samsung

        # Verificar que get_code_type identifica correctamente los contenedores
        self.assertEqual(self.inv.get_code_type("%MUEBLE_1"), "mueble")
        self.assertEqual(self.inv.get_code_type("@CAJA_A"), "caja")
        self.assertEqual(self.inv.get_code_type("#VIDRIERA_CENTRAL"), "vidriera")
        self.assertEqual(self.inv.get_code_type("IPHONE13_128"), "product")

    def test_images_manager_ignores_qrs(self):
        """Requisito 11: Los códigos QR no deben buscar ni descargar imágenes."""
        img_mgr = ImageManager(self.config)
        self.assertEqual(img_mgr.find_image_urls("@CAJA_1"), [])
        self.assertEqual(img_mgr.find_image_urls("%MUEBLE_1"), [])
        self.assertEqual(img_mgr.find_image_urls("#VIDRIERA_1"), [])
        self.assertFalse(img_mgr.download_image("@CAJA_1"))
        self.assertIsNone(img_mgr.get_tk_image("@CAJA_1"))

    def test_container_hierarchy_and_status(self):
        """Requisito 11 y 12: Estado de faltantes / completado en contenedores."""
        self.inv.update_product_location("IPHONE13_128", "@CAJA_1") # 10 esperados
        
        # 1. Sin escanear
        status0 = self.inv.get_container_status("@CAJA_1", seq=[])
        self.assertEqual(status0["expected_total"], 10)
        self.assertEqual(status0["scanned_count"], 0)
        self.assertEqual(status0["missing_count"], 10)
        self.assertEqual(status0["display_str"], "10")
        self.assertFalse(status0["is_complete"])

        # 2. Escaneando 10 unidades
        seq = ["@CAJA_1"] + ["IPHONE13_128"] * 10
        status10 = self.inv.get_container_status("@CAJA_1", seq=seq)
        self.assertEqual(status10["scanned_count"], 10)
        self.assertEqual(status10["missing_count"], 0)
        self.assertEqual(status10["display_str"], "✓")
        self.assertTrue(status10["is_complete"])

    def test_relevant_differences_calculation(self):
        """Requisito 18: Contador visual de incidencias / diferencias relevantes."""
        # 1. Producto desconocido (no en CSV)
        self.inv.add_item("UNKNOWN_PRODUCT_999")
        # 2. Producto con sobrante (esperado 3, escaneamos 4)
        for _ in range(4):
            self.inv.add_item("AIRPODSPRO")

        unlisted = sum(
            len(p) for code, p in self.inv.scanned_items.items()
            if not self.inv.is_qr_code(code) and code not in self.inv.full_family_map
        )
        excess = sum(
            max(0, len(p) - self.inv.original_quantities.get(code, 0))
            for code, p in self.inv.scanned_items.items()
            if not self.inv.is_qr_code(code) and self.inv.original_quantities.get(code, 0) > 0
        )
        relevant_diffs = unlisted + excess

        self.assertEqual(unlisted, 1) # 1 desconocido
        self.assertEqual(excess, 1)   # 4 - 3 = 1 sobrante
        self.assertEqual(relevant_diffs, 2)

    def test_f4_and_delete_operations(self):
        """Requisitos 16 y 17: Atajo F4 suma +1 y Delete resta -1."""
        self.inv.add_item("IPHONE13_128")
        self.assertEqual(len(self.inv.scanned_items.get("IPHONE13_128", [])), 1)

        # F4: +1 unidad
        self.inv.add_item("IPHONE13_128")
        self.assertEqual(len(self.inv.scanned_items.get("IPHONE13_128", [])), 2)

        # Delete: -1 unidad
        res = self.inv.delete_last("IPHONE13_128")
        self.assertTrue(res)
        self.assertEqual(len(self.inv.scanned_items.get("IPHONE13_128", [])), 1)

if __name__ == "__main__":
    unittest.main()
