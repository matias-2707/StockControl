"""Tests del generador administrativo de licencias (capa GUI sobre la CLI).

Verifican:
- validacion de entrada (dias validos, 0, negativos, texto, vacio);
- que el payload generado tenga los campos y valores correctos;
- que la firma Ed25519 sea valida contra la clave publica del par;
- que el archivo license.dat tenga la estructura esperada;
- que la fecha de vencimiento corresponda a los dias solicitados;
- que la ruta por defecto NUNCA apunte a la licencia real del cliente.

Los tests NO imprimen la clave privada ni la publica completa.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

from tools.license_generator import (
    generate_license,
    ensure_keypair,
    canonical_payload_bytes,
)
from tools.generador_licencias_gui import (
    parse_days,
    default_output_path,
)


class TestParseDays(unittest.TestCase):
    def test_valid_positive_days(self):
        self.assertEqual(parse_days("1"), 1)
        self.assertEqual(parse_days("7"), 7)
        self.assertEqual(parse_days("30"), 30)
        self.assertEqual(parse_days("365"), 365)
        self.assertEqual(parse_days("  90  "), 90)

    def test_zero_rejected(self):
        with self.assertRaises(ValueError):
            parse_days("0")

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            parse_days("-5")
        with self.assertRaises(ValueError):
            parse_days("-1")

    def test_non_numeric_rejected(self):
        for bad in ["abc", "7a", "3.5", "1e3", "0x10", "!!"]:
            with self.assertRaises(ValueError):
                parse_days(bad)

    def test_empty_or_whitespace_rejected(self):
        for bad in ["", "   ", None]:
            with self.assertRaises(ValueError):
                parse_days(bad)


class TestLicenseGeneration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.keys_dir = os.path.join(self.temp_dir, "license-authority")
        # Par de claves aislado para el test (NO toca la autoridad real).
        _, self.public_key = ensure_keypair(self.keys_dir, generate=True)
        self.pub_raw = self.public_key.public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _verify_signature(self, license_structure):
        payload_bytes = canonical_payload_bytes(license_structure["payload"])
        sig_bytes = bytes.fromhex(license_structure["signature"])
        # Lanza InvalidSignature si no valida.
        self.public_key.verify(sig_bytes, payload_bytes)

    def test_payload_fields_and_expiry(self):
        out = os.path.join(self.temp_dir, "license.dat")
        generate_license(days=7, license_id=None, output_file=out, keys_dir=self.keys_dir)

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)

        payload = data["payload"]
        today = datetime.now().date()
        exp = today + timedelta(days=7)

        self.assertEqual(payload["licensee"], "Stock Cellular Center")
        self.assertEqual(payload["issue_date"], today.strftime("%Y-%m-%d"))
        self.assertEqual(payload["expiry_date"], exp.strftime("%Y-%m-%d"))
        self.assertEqual(payload["version"], "8.0")
        self.assertEqual(payload["grace_days"], 2)
        self.assertEqual(payload["license_id"], f"SCC-V8-{today.strftime('%Y%m%d')}-7D")

    def test_signature_is_valid(self):
        out = os.path.join(self.temp_dir, "license.dat")
        generate_license(days=30, output_file=out, keys_dir=self.keys_dir)

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)

        # No debe lanzar excepcion.
        self._verify_signature(data)

    def test_license_file_structure(self):
        out = os.path.join(self.temp_dir, "license.dat")
        generate_license(days=1, output_file=out, keys_dir=self.keys_dir)

        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIn("payload", data)
        self.assertIn("signature", data)
        self.assertIsInstance(data["payload"], dict)
        # 64 bytes hex = 128 caracteres.
        self.assertEqual(len(data["signature"]), 128)

    def test_tampered_payload_fails_verification(self):
        out = os.path.join(self.temp_dir, "license.dat")
        generate_license(days=7, output_file=out, keys_dir=self.keys_dir)
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["payload"]["expiry_date"] = "2099-12-31"
        with self.assertRaises(InvalidSignature):
            self._verify_signature(data)

    def test_various_durations_expiry_matches(self):
        for days in [1, 7, 30, 90, 365]:
            out = os.path.join(self.temp_dir, f"license_{days}.dat")
            generate_license(days=days, output_file=out, keys_dir=self.keys_dir)
            with open(out, "r", encoding="utf-8") as f:
                data = json.load(f)
            today = datetime.now().date()
            expected = (today + timedelta(days=days)).strftime("%Y-%m-%d")
            self.assertEqual(data["payload"]["expiry_date"], expected)
            self._verify_signature(data)


class TestDefaultOutputPath(unittest.TestCase):
    def test_default_path_never_targets_real_license(self):
        path = default_output_path(7)
        real = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "StockCellularCenter",
            "license.dat",
        )
        self.assertNotEqual(os.path.abspath(path).lower(), os.path.abspath(real).lower())
        self.assertTrue(path.endswith(".dat"))
        self.assertIn("license_7d", os.path.basename(path))

    def test_custom_base_dir(self):
        base = tempfile.mkdtemp()
        try:
            path = default_output_path(30, base_dir=base)
            self.assertTrue(path.startswith(base))
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestHeadlessGeneration(unittest.TestCase):
    """Verifica el camino no-grafico usado por la verificacion automatizada."""

    def test_headless_env_generates_valid_license(self):
        import importlib
        import tools.generador_licencias_gui as gui
        importlib.reload(gui)

        temp_dir = tempfile.mkdtemp()
        try:
            keys_dir = os.path.join(temp_dir, "authority")
            _, public_key = ensure_keypair(keys_dir, generate=True)
            out = os.path.join(temp_dir, "headless.dat")
            generate_license(days=7, output_file=out, keys_dir=keys_dir)

            with open(out, "r", encoding="utf-8") as f:
                data = json.load(f)
            public_key.verify(
                bytes.fromhex(data["signature"]),
                canonical_payload_bytes(data["payload"]),
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
