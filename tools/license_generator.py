"""
Generador de Licencias Offline — Stock Cellular Center V8.0
Este script es de uso exclusivo del propietario (Matías).
NO debe incluirse en el ejecutable final del cliente ni distribuirse públicamente.
"""

import os
import json
import argparse
from datetime import datetime, timedelta
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

# La autoridad de firma vive fuera del repositorio y del instalador del cliente.
DEFAULT_KEYS_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "StockCellularCenter", "license-authority"
)

def ensure_keypair(keys_dir=DEFAULT_KEYS_DIR, generate=False):
    """Carga un par existente; solo lo genera con autorización explícita."""
    priv_path = os.path.join(keys_dir, "license_ed25519_private.pem")
    pub_path = os.path.join(keys_dir, "license_ed25519_public.pem")

    if os.path.exists(priv_path) and os.path.exists(pub_path):
        with open(priv_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(pub_path, "rb") as f:
            public_key = serialization.load_pem_public_key(f.read())
        return private_key, public_key

    if not generate:
        raise FileNotFoundError(
            "No se encontró un par de claves de licencia. Indique --keys-dir "
            "con una autoridad existente o use --generate-keypair explícitamente."
        )

    os.makedirs(keys_dir, exist_ok=True)
    # Generar nuevo par de claves Ed25519 solo por solicitud explícita.
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )

    with open(priv_path, "wb") as f:
        f.write(priv_bytes)
    with open(pub_path, "wb") as f:
        f.write(pub_bytes)

    # Imprimir clave pública en hex para embeber en auth.py
    raw_pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )
    print(f"[+] Nuevo par de claves generado en: {keys_dir}")
    print(f"[+] Clave pública RAW (HEX): {raw_pub_bytes.hex()}")

    return private_key, public_key

def canonical_payload_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')

def generate_license(days: int = 30, licensee: str = "Stock Cellular Center", license_id: str = None, grace_days: int = 2, output_file: str = "license.dat", keys_dir: str = DEFAULT_KEYS_DIR):
    private_key, public_key = ensure_keypair(keys_dir)
    
    today = datetime.now().date()
    exp_date = today + timedelta(days=days)
    
    if not license_id:
        license_id = f"SCC-V8-{today.strftime('%Y%m%d')}-{days}D"

    payload = {
        "licensee": licensee,
        "issue_date": today.strftime('%Y-%m-%d'),
        "expiry_date": exp_date.strftime('%Y-%m-%d'),
        "license_id": license_id,
        "grace_days": grace_days,
        "version": "8.0"
    }

    payload_bytes = canonical_payload_bytes(payload)
    signature = private_key.sign(payload_bytes)

    license_structure = {
        "payload": payload,
        "signature": signature.hex()
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(license_structure, f, indent=4)

    raw_pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw
    )

    print(f"[OK] Licencia generada exitosamente: {output_file}")
    print(f"    - Titular: {licensee}")
    print(f"    - Emisión: {payload['issue_date']}")
    print(f"    - Expiración: {payload['expiry_date']} ({days} días)")
    print(f"    - Gracia: {grace_days} días")
    print(f"    - Clave pública requerida en cliente (HEX): {raw_pub_bytes.hex()}")

    return output_file, raw_pub_bytes.hex()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generador de licencias firmado para Stock Cellular Center V8")
    parser.add_argument("--days", type=int, default=30, help="Días de duración de la licencia (30, 60, 90, etc.)")
    parser.add_argument("--licensee", type=str, default="Stock Cellular Center", help="Nombre del titular")
    parser.add_argument("--out", type=str, default="license.dat", help="Ruta de salida del archivo license.dat")
    parser.add_argument("--grace", type=int, default=2, help="Días de gracia tras vencimiento")
    parser.add_argument("--keys-dir", type=str, default=DEFAULT_KEYS_DIR, help="Directorio externo de la autoridad de firma")
    parser.add_argument("--generate-keypair", action="store_true", help="Crear un par nuevo en --keys-dir y salir")
    args = parser.parse_args()

    if args.generate_keypair:
        ensure_keypair(args.keys_dir, generate=True)
    else:
        generate_license(days=args.days, licensee=args.licensee, grace_days=args.grace, output_file=args.out, keys_dir=args.keys_dir)
