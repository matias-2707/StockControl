import os
import sys
import unittest
import tempfile
import json
import shutil
from datetime import datetime, timedelta
from cryptography.hazmat.primitives import serialization

# Asegurar root en sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from unittest.mock import patch
from src.core.auth import AuthManager, PUBLIC_KEY_HEX
from tools.license_generator import generate_license, ensure_keypair

class TestAuthAndLicense(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_config = {}
        
        # Patch messagebox para tests headless
        self.patcher_err = patch('tkinter.messagebox.showerror')
        self.patcher_warn = patch('tkinter.messagebox.showwarning')
        self.patcher_info = patch('tkinter.messagebox.showinfo')
        self.mock_err = self.patcher_err.start()
        self.mock_warn = self.patcher_warn.start()
        self.mock_info = self.patcher_info.start()

        self.auth = AuthManager(self.mock_config)
        self.auth.app_dir = self.temp_dir
        self.auth.auth_file = os.path.join(self.temp_dir, "auth.json")
        self.auth.runtime_file = os.path.join(self.temp_dir, "runtime_state.json")
        self.auth.license_file = os.path.join(self.temp_dir, "license.dat")
        self.keys_dir = os.path.join(self.temp_dir, "license-authority")

    def tearDown(self):
        self.patcher_err.stop()
        self.patcher_warn.stop()
        self.patcher_info.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_first_run_detection(self):
        self.assertTrue(self.auth.is_first_run())

    def test_setup_and_verify_password(self):
        self.assertTrue(self.auth.setup_initial_password("SecretPass123", iterations=50000))
        self.assertFalse(self.auth.is_first_run())
        self.assertTrue(self.auth.verify_password_hash("SecretPass123"))
        self.assertFalse(self.auth.verify_password_hash("WrongPass"))
        self.assertFalse(self.auth.verify_password_hash(""))

    def test_dynamic_iterations_support(self):
        # Probar con 20.000 iteraciones
        self.auth.setup_initial_password("CustomPass", iterations=20000)
        with open(self.auth.auth_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["iterations"], 20000)
        self.assertTrue(self.auth.verify_password_hash("CustomPass"))

        # Cambiar artificialmente en auth.json las iteraciones a 80.000 (re-hasheando)
        self.auth.setup_initial_password("CustomPass", iterations=80000)
        self.assertTrue(self.auth.verify_password_hash("CustomPass"))

    def _create_signed_license(self, days=30, grace_days=2):
        _, public_key = ensure_keypair(self.keys_dir, generate=True)
        self.auth.public_key_hex = public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ).hex()
        path = os.path.join(self.temp_dir, "source_license.dat")
        generate_license(days=days, licensee="Test Store", output_file=path, grace_days=grace_days, keys_dir=self.keys_dir)
        return path

    def test_license_ed25519_valid(self):
        lic_path = self._create_signed_license()

        with open(lic_path, "r", encoding="utf-8") as f:
            content = f.read()

        valid, payload = self.auth.verify_license_data(content)
        self.assertTrue(valid)
        self.assertEqual(payload["licensee"], "Test Store")
        self.assertEqual(payload["version"], "8.0")

    def test_license_ed25519_tampered_payload(self):
        lic_path = self._create_signed_license()

        with open(lic_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Alterar payload (ej. cambiar fecha de expiración)
        data["payload"]["expiry_date"] = "2099-12-31"
        tampered_content = json.dumps(data)

        valid, error_msg = self.auth.verify_license_data(tampered_content)
        self.assertFalse(valid)
        self.assertIn("Firma digital no válida", error_msg)

    def test_license_ed25519_tampered_signature(self):
        lic_path = self._create_signed_license()

        with open(lic_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Alterar firma
        sig = list(data["signature"])
        sig[0] = 'a' if sig[0] != 'a' else 'b'
        data["signature"] = "".join(sig)

        valid, error_msg = self.auth.verify_license_data(json.dumps(data))
        self.assertFalse(valid)
        self.assertIn("Firma digital no válida", error_msg)

    def test_license_remaining_days(self):
        lic_path = self._create_signed_license(days=45)
        self.auth.license_file = lic_path

        days = self.auth.get_remaining_days()
        self.assertIn(days, [44, 45])

    def test_anti_rollback_detection(self):
        lic_path = self._create_signed_license()
        self.auth.license_file = lic_path

        # Simular que en runtime_state se usó el sistema en el futuro (ej. mañana o el año próximo)
        future_date = (datetime.now().date() + timedelta(days=5)).strftime("%Y-%m-%d")
        self.auth._write_runtime_state({"last_used_date": future_date})

        # check_license debe detectar el retroceso de reloj
        self.assertFalse(self.auth.check_license())

    def test_grace_period_is_temporal_and_survives_restart(self):
        lic_path = self._create_signed_license(days=0, grace_days=2)
        self.auth.license_file = lic_path
        expiry = datetime.now().date()
        self.auth._today = lambda: expiry + timedelta(days=1)

        self.assertTrue(self.auth.check_license())
        # Un reinicio durante el mismo día de gracia no consume otro día.
        self.assertTrue(self.auth.check_license())

    def test_grace_period_last_day_and_after_expiry(self):
        lic_path = self._create_signed_license(days=0, grace_days=2)
        self.auth.license_file = lic_path
        expiry = datetime.now().date()
        self.auth._today = lambda: expiry + timedelta(days=2)
        self.assertTrue(self.auth.check_license())

        self.auth._today = lambda: expiry + timedelta(days=3)
        self.assertFalse(self.auth.check_license())

    def test_renewal_copies_to_runtime_location(self):
        source = self._create_signed_license(days=30)
        activated, message = self.auth.activate_license_file(source)
        self.assertTrue(activated, message)
        self.assertEqual(self.auth.license_file, os.path.join(self.temp_dir, "license.dat"))
        self.assertTrue(os.path.exists(self.auth.license_file))
        self.assertTrue(self.auth.check_license())

if __name__ == "__main__":
    unittest.main()
