# SECURITY NOTES — Stock Cellular Center V8.0

> ✅ **ESTADO: AUDITADO Y SEGURO PARA PUBLICACIÓN**
> Todas las vulnerabilidades de versiones anteriores (V7.x y anteriores) han sido resueltas en la arquitectura V8.0.

---

## 1. Arquitectura Criptográfica y Autenticación

### A. Autenticación Local
- **Eliminación de secretos hardcodeados**: No existen contraseñas, hashes fijos ni credenciales maestras en el código fuente ni en el repositorio.
- **Configuración de Primer Inicio**: La aplicación solicita la creación de una contraseña de acceso en el primer uso.
- **Algoritmo de Derivación**: PBKDF2 con HMAC-SHA256 (`hashlib.pbkdf2_hmac`), salt criptográfico aleatorio de 32 bytes (`secrets.token_hex(32)`) y costo dinámico configurable (`iterations: 200000`).
- **Comparación Segura**: Comparación de hashes en tiempo constante (`hmac.compare_digest`) para prevenir ataques de temporización (*timing attacks*).
- **Almacenamiento Local**: Credenciales almacenadas fuera del repositorio en `%LOCALAPPDATA%\StockCellularCenter\auth.json`.

### B. Sistema de Licencias Asimétricas
- **Algoritmo**: Firma digital asimétrica **Ed25519** (RFC 8032) mediante la biblioteca estándar auditada `cryptography`.
- **Aislamiento de Claves**:
  - La **clave privada** (`ed25519_private.pem`) permanece exclusivamente en el entorno del propietario (herramienta `tools/license_generator.py`). Nunca se compila en el binario ni se sube al repositorio (`.gitignore`).
  - La **clave pública** (32 bytes / 64 caracteres hex) está integrada en el cliente únicamente para verificar la firma de `license.dat`.
- **Protección contra Manipulación de Reloj**: Detección de retroceso de reloj (*anti-rollback clock tampering*) mediante persistencia de marca de tiempo incremental en `%LOCALAPPDATA%\StockCellularCenter\runtime_state.json`.

---

## 2. Exclusiones de Git y Seguridad de Datos

Las siguientes rutas y patrones están estrictamente excluidos en `.gitignore`:
- `.keys/` (Claves privadas Ed25519)
- `*.pem`, `*.key`
- `license.dat`
- `*.csv` (Datos comerciales reales)
- `Escaneos/`
- `dist/`, `build/`
- `%LOCALAPPDATA%\StockCellularCenter\`
