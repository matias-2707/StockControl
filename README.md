# Stock Cellular Center V8.0

Aplicación de escritorio de alto rendimiento para la gestión, auditoría y control de inventario de Cellular Center.

Permite escanear y registrar productos (con lector de código de barras o ingreso manual), comparar en tiempo real contra el catálogo maestro importado desde CSV, auditar faltantes, sobrantes y discrepancias de ubicación física mediante `main_stock.json`, y exportar los resultados a sistemas de gestión mediante automatización de teclado o portapapeles.

---

## Novedades y Arquitectura de V8.0

- **Escaneo y actualización visual**: registra los códigos y mantiene el foco; la separación completa del procesamiento secundario está planificada.
- **Selector de Orden**: Soporte nativo para orden `"Último abajo"` (recomendado) y `"Último arriba"`, con foco visual constante.
- **Gestión de Ubicaciones con `main_stock.json`**:
  - Modelo jerárquico de contenedores: Muebles (`%`), Cajas (`@`) y Vidrieras (`#`).
  - Soporte para escaneos no lineales (muebles intercalados, productos escaneados antes o después del QR de caja).
  - Detección inteligente de discrepancias con resolución interactiva (*Mover* vs *Dejar aquí*).
  - Validación diferida de 5 segundos con *debounce* tras movimientos manuales o drag & drop.
- **Jerarquía y Plegado de QRs**:
  - Los códigos QR de estructura no se contabilizan como productos físicos ni inflan el porcentaje de progreso.
  - Plegado/desplegado tipo carpetas con doble clic en la tabla de escaneados.
  - Visualización del conteo de faltantes o tilde de completado (`✓`) por contenedor.
- **Auditoría de Diferencias**:
  - Atajo global `F3` para abrir/cerrar la ventana de auditoría.
  - Atajo `F4` para agregar +1 unidad al escaneo de un producto seleccionado.
  - Atajo `Supr` / `Delete` para descontar -1 unidad de un producto seleccionado.
  - Vista previa de imagen corregida enlazada directamente al SKU.
  - Contador visual de incidencias relevantes en el panel de resumen.
- **Seguridad Criptográfica**:
  - Autenticación con PBKDF2-HMAC-SHA256 y costo dinámico configurable.
  - Sistema de licencias asimétricas **Ed25519** (RFC 8032) con verificación pública en cliente y protección contra manipulación de reloj.
  - Eliminación total de credenciales y secretos hardcodeados.

---

## Tecnologías Principales

- **CustomTkinter** — Interfaz gráfica moderna con soporte para temas claro/oscuro.
- **cryptography** — Criptografía asimétrica Ed25519 para licencias y seguridad.
- **Pillow** — Carga, pre-renderizado y galería de imágenes de productos.
- **pynput** — Captura de atajos globales y emulación de teclado para exportación.
- **pyperclip** — Manejo seguro del portapapeles.
- **requests** — Descarga optimizada en segundo plano de imágenes desde el CDN.

---

## Instalación y Entorno

```bash
# 1. Crear entorno virtual
python -m venv .venv
.venv\Scripts\activate        # Windows

# 2. Instalar dependencias
pip install -r requirements.txt
```

---

## Ejecución

```bash
python run_app.py
```

---

## Estructura del Proyecto

```
Stock-Cellular-Center/
├── src/
│   ├── main.py               # Ventana y controlador principal de la aplicación
│   ├── config.py             # Configuración persistente en %LOCALAPPDATA%
│   ├── core/
│   │   ├── auth.py           # Autenticación PBKDF2 y verificación de licencias Ed25519
│   │   ├── automation.py     # Emulación de teclado y atajos globales
│   │   ├── images.py         # Descarga y renderizado de imágenes de productos
│   │   └── inventory.py      # Lógica de stock, CSV, historial y main_stock.json
│   └── gui/
│       ├── utils.py          # Utilidades de centrado y diálogos
│       └── components/       # Tablas (Treeview), selector inicial, explorador histórico
├── tools/
│   ├── license_generator.py  # Generador de licencias firmado con Ed25519 (solo para el propietario)
│   └── convert_icon.py       # Conversión opcional de PNG a ICO mediante argumentos
├── tests/
│   ├── test_auth.py          # Pruebas unitarias de autenticación y licencias
│   ├── test_main_stock.py    # Pruebas de contenedores, jerarquía y main_stock.json
│   └── test_v8_features.py   # Pruebas de progreso, incidencias y atajos V8.0
├── res/                      # Recursos gráficos e íconos
├── requirements.txt          # Dependencias del proyecto
└── run_app.py                # Punto de entrada con auto-elevación en Windows
```

---

## Generación de Licencias (Uso del Propietario)

Para generar una licencia válida para un cliente (por ejemplo, por 30 días):

```bash
python tools/license_generator.py --licensee "Nombre del Comercio" --days 30 --grace 2 --out C:\Licencias\license.dat
```

La clave privada se guarda fuera del repositorio, por defecto en `%LOCALAPPDATA%\StockCellularCenter\license-authority`. Para crear una autoridad nueva de forma explícita:

```bash
python tools/license_generator.py --generate-keypair
```

La aplicación importa la licencia a `%LOCALAPPDATA%\StockCellularCenter\license.dat`; no requiere escritura en el directorio del EXE.

---

## Compilación para Windows (.EXE)

Para compilar la aplicación en un ejecutable independiente de Windows:

```bash
python build_windows.py
```

El ejecutable resultante se ubicará en la carpeta `dist/`.
