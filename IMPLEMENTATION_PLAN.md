# Plan de Implementación — Stock Cellular Center V8.0

## 1. Resumen Ejecutivo
El objetivo de este proyecto es evolucionar **Stock Cellular Center** desde la versión base V7.1 a la **Versión V8.0**, transformándola en una herramienta de alto rendimiento, arquitectura modular, segura, mantenible y preparada para portafolio y publicación en GitHub.

---

## 2. Principios Arquitectónicos Fundamentales
1. **El escaneo tiene prioridad absoluta**: La captura de códigos de barras / QR no debe bloquearse bajo ninguna circunstancia por procesos secundarios (cálculos de diferencias, auditorías, descargas de imágenes, validaciones de proximidad ni diálogos).
2. **Concurrencia segura en UI**: Ningún hilo secundario interactúa directamente con widgets de Tkinter/CustomTkinter. Todas las mutaciones de UI se enrutan al hilo principal vía `root.after()`.
3. **Persistencia y Modelo de Datos Limpio**: Separación clara entre inventario maestro (`CSV`), inventario escaneado en sesión (`JSON`), estructura de ubicación maestra (`main_stock.json`), configuración (`config.json`), credenciales locales (`auth.json`) y archivo de licencia firmado (`license.dat`).
4. **Seguridad y Cero Secretos**: Eliminación absoluta de contraseñas, hashes fijos, salts estáticos y claves privadas del código fuente. Criptografía robusta (PBKDF2-HMAC-SHA256, firmas asimétricas con clave pública en cliente y generador offline con clave privada).

---

## 3. Mapeo de Requisitos y Módulos a Modificar

| Módulo / Archivo | Estado | Responsabilidades en V8.0 |
| :--- | :--- | :--- |
| `src/config.py` | Modificar | Manejo de configuración en `%LOCALAPPDATA%\StockCellularCenter\config.json`. Eliminación de hash hardcodeado. Configuración de orden de lista (arriba/abajo), tiempos de validación y paths dinámicos. |
| `src/core/auth.py` | Reestructurar | Nuevo sistema de autenticación local con salt aleatorio por instalación + PBKDF2 (100k iteraciones) + `hmac.compare_digest`. Sistema de licencias asimétrico: validación de `license.dat` con clave pública embebida. Eliminación de secretos hardcodeados. |
| `src/core/inventory.py` | Modificar/Ampliar | Gestión de `main_stock.json`, jerarquía de QRs (`@` Caja, `%` Mueble, `#` Vidriera), cálculo de cantidades faltantes por contenedor, exclusión de QRs del conteo de stock físico, validación diferida de ubicación (5s debounced), reordenamiento. |
| `src/core/images.py` | Modificar | Asegurar que no se descarguen imágenes para QRs (`@`, `%`, `#`). Rutas relativas seguras a AppData/Local o subcarpeta runtime no commiteada. Descarga asíncrona no bloqueante. |
| `src/core/automation.py` | Modificar | Atajos globales no bloqueantes, exportación progresiva, compatibilidad con V8. |
| `src/gui/components/tables.py` | Modificar | Soporte para vista jerárquica / árbol plegable (doble clic en QR para colapsar/expandir productos y subcontenedores), renderizado de `✓` cuando un contenedor está completo, soporte para orden Último arriba / Último abajo. |
| `src/gui/components/selector.py` | Modificar | Actualización de textos, títulos y estilos a V8.0. |
| `src/gui/components/history.py` | Modificar | Actualización visual y soporte de metadatos V8.0. |
| `src/gui/diff_window.py` (o en main) | Refactor/Mejora | Corrección de búsqueda de imagen (usar SKU y no checkbox de exportación), atajos `F3` (toggle), `F4` (+1 unidad faltante al escaneo), `Delete/Supr` (-1 unidad sobrante), actualización en tiempo real sin bloqueo. |
| `src/main.py` | Refactorizar | Integración de cola de escaneo no bloqueante, selector de orden "Último arriba" / "Último abajo", contador de diferencias relevantes (códigos inexistentes + sobrantes), debounce de 5s para validación de movimientos, toast notifications agrupadas, actualización a V8.0. |
| `tools/license_generator.py` | Nuevo | Herramienta CLI independiente para Matías (fuera del build principal / no distribuida en el release) para generar archivos `license.dat` firmados con clave privada por 30, 60, 90 días. |
| `README.md` & docs | Modificar | Documentación técnica completa de V8.0, arquitectura, seguridad, instalación y guía de uso. |

---

## 4. Diseño Técnico Detallado

### 4.1. Escaneo Prioritario y No Bloqueante — diseño pendiente de implementación

El estado V8 inicial todavía realiza validación de proximidad, cálculos y refrescos completos dentro del evento `<Return>`. La siguiente etapa debe conservar el registro inmediato y sustituir el resto por este flujo:

1. El handler de `scan_entry` normaliza el código, llama únicamente a `inventory.add_item`, limpia el campo y devuelve el foco. Esta es la ruta crítica.
2. En el hilo principal, el handler agrega un evento inmutable `{sku, pos, is_qr}` a una `queue.Queue` de trabajo secundario y solicita un único refresco visual pendiente mediante `after_idle`.
3. Un worker procesa solamente cálculos sin UI: proximidad, sobrantes y metadatos de alerta. El estado compartido de inventario sigue protegido por `InventoryManager.lock` y los resultados se publican en una segunda cola.
4. El hilo principal consulta resultados con `root.after(intervalo, poll_results)`. Solo este hilo llama `show_toast`, modifica tablas, ventanas o widgets CustomTkinter.
5. Los refrescos se coalescen: múltiples escaneos antes del siguiente `after_idle` producen una sola reconstrucción. La vista recibe una instantánea coherente del inventario.
6. La validación diferida de movimientos manuales conserva su debounce de cinco segundos, pero no bloquea ni duplica la cola de escaneo.

Operaciones que deben salir de la ruta crítica: `check_proximity`, recorridos de secuencia, conteos de contenedores, cálculo de sobrantes/incidencias, `_refresh_tables`, `_load_master_table`, `_refresh_diff_window`, búsquedas de imágenes y creación de notificaciones. `AutomationManager` y cualquier worker deben publicar datos o callbacks seguros; nunca invocar widgets ni `show_toast` directamente desde un hilo secundario.

### 4.2. Main Stock (`main_stock.json`) y Jerarquía QR
- Estructura de `main_stock.json`:
  ```json
  {
    "version": "8.0",
    "updated_at": "2026-08-25T12:00:00",
    "containers": {
      "%MUEBLE_1": {
        "type": "mueble",
        "parent": null,
        "children": ["@CAJA_1", "@CAJA_2"]
      },
      "@CAJA_1": {
        "type": "caja",
        "parent": "%MUEBLE_1",
        "items": {
          "SKU123": 5,
          "SKU456": 10
        }
      }
    },
    "product_locations": {
      "SKU123": "@CAJA_1",
      "SKU456": "@CAJA_1"
    }
  }
  ```
- **Mapeo de QRs**:
  - `%` = Mueble (Contenedor raíz o nivel superior).
  - `@` = Caja (Contenedor dentro de mueble o libre).
  - `#` = Vidriera (Contenedor de exhibición directa).
- **Cálculo de faltantes en QR**: Al escanear un QR o listar, se computan los productos requeridos en ese contenedor según `main_stock.json` vs los ya escaneados dentro de dicho contenedor. Si faltan $N$, se muestra $N$. Si faltan $0$, se muestra `✓`.
- **Exclusión de stock**: Los QRs no incrementan `scanned_count` ni alteran el porcentaje de progreso.
- **Plegado/Desplegado**: En la tabla de escaneados, doble clic sobre un nodo QR colapsa/expande sus ítems hijos mediante gestión de visibilidad de filas en Treeview.

### 4.3. Validación de Ubicación y Delay de 5 Segundos
- Cuando un producto se escanea en un contenedor no coincidente con `main_stock.json`:
  - Se genera una alerta agrupada.
  - El usuario puede elegir **Mover** (reubica en secuencia y actualiza referencia si se confirma) o **Dejar ahí** (confirma la nueva ubicación y actualiza `main_stock.json`).
- Si el usuario arrastra/mueve un ítem en la lista, se inicia un temporizador de 5 segundos (`after(5000, ...)`). Si ocurre otro movimiento antes, se cancela y reinicia el timer (debouncing).

### 4.4. Orden de la Lista (Último arriba / Último abajo)
- Selector visible en cabecera de la tabla de escaneados: `["Último arriba", "Último abajo"]`.
- `Último arriba`: Nuevos escaneos se visualizan en el tope del Treeview.
- `Último abajo`: Nuevos escaneos se agregan al final del Treeview.
- El cursor/scroll visual sigue automáticamente el último elemento insertado.

### 4.5. Auditoría de Diferencias y Atajos F3 / F4 / Delete
- `F3`: Toggle global para abrir o cerrar la ventana de auditoría de diferencias.
- Búsqueda de imágenes en diferencias: usa el valor exacto de la columna `Código` (índice 1) en lugar del índice 0 (`No exportar`).
- `F4` (en diferencias): Agrega 1 unidad del ítem faltante seleccionado al final de los escaneados.
- `Delete/Supr` (en diferencias): Resta 1 unidad del ítem sobrante seleccionado.
- **Contador Visual de Diferencias Relevantes**:
  - En la barra superior/resumen: muestra $N$ = (Cantidad de productos con códigos inexistentes en CSV maestro) + (Cantidad de unidades sobrantes sobre el stock esperado).
  - Ignora faltantes normales por escanear.

### 4.6. Notificaciones Agrupadas
- Si un SKU tiene múltiples unidades con advertencia (ej. 20 unidades mal ubicadas o 5 sobrantes), se genera un solo Toast / notificación indicando: `SKU (X unidades) ...`.
- No genera spam repetitivo. Al resolverse la situación, se retira la notificación del historial activo.

### 4.7. Seguridad, Autenticación y Licenciamiento

#### A. Autenticación Local y Derivación de Claves (PBKDF2-HMAC-SHA256 Dinámico)
- **Diseño**:
  - Las credenciales locales se almacenan en `%LOCALAPPDATA%\StockCellularCenter\auth.json` (fuera del repositorio).
  - En el primer arranque, la aplicación detecta la ausencia de `auth.json` y solicita al usuario configurar la contraseña de acceso.
  - Almacenamiento estructurado:
    ```json
    {
      "version": "8.0",
      "algorithm": "pbkdf2_sha256",
      "iterations": 200000,
      "salt": "<64_HEX_CHARS>",
      "password_hash": "<64_HEX_CHARS>"
    }
    ```
  - **Iteraciones dinámicas y extensibilidad**: El número de iteraciones (200.000 como base recomendada para escritorio Windows en 2026) se lee directamente desde el archivo `auth.json`. Esto permite que futuras versiones aumenten el costo computacional (ej. a 300.000 o 600.000) o migren automáticamente el hash tras un login exitoso sin invalidar contraseñas previas.
  - Validación segura mediante `secrets.token_hex(32)`, `hashlib.pbkdf2_hmac('sha256', password.encode(), salt_bytes, iterations)` y `hmac.compare_digest` para evitar ataques de temporización (timing attacks).
  - Eliminación absoluta de cualquier contraseña o hash hardcodeado en el código fuente.

#### B. Sistema de Licencias Asimétrico Estándar (Ed25519 con biblioteca `cryptography`)
- **Algoritmo seleccionado**: **Ed25519** (Edwards-curve Digital Signature Algorithm, RFC 8032).
- **Biblioteca utilizada**: `cryptography` (`cryptography.hazmat.primitives.asymmetric.ed25519`), biblioteca estándar de la industria auditada y ampliamente mantenida.
- **Justificación técnica de Ed25519**:
  - **Seguridad y robustez**: Inmune por diseño a ataques de canal lateral basados en temporización; curva de alta seguridad equivalente a RSA 3072+ bits.
  - **Eficiencia y compacidad**: Las firmas son de exactamente 64 bytes y las claves públicas de 32 bytes (en formato hexadecimal o Base64), lo que mantiene el archivo `license.dat` limpio, ligero y fácil de distribuir.
  - **Rendimiento**: Verificación ultrarrápida en milisegundos sin impacto perceptible en el arranque.
- **Separación de Claves**:
  - **Clave Pública (Verificación)**: Se compila/embebe en `src/core/auth.py` como una constante de 32 bytes codificada en hexadecimal. No permite forjar licencias bajo ningún concepto.
  - **Clave Privada (Firma)**: Permanece **estrictamente fuera** del repositorio, del ejecutable y de la máquina del cliente. Se utiliza exclusivamente en la herramienta local del propietario `tools/license_generator.py` (o almacén seguro privado).
- **Estructura y Validación de `license.dat`**:
  ```json
  {
    "payload": {
      "licensee": "Stock Cellular Center",
      "issue_date": "2026-08-25",
      "expiry_date": "2026-11-25",
      "license_id": "SCC-V8-20260825-001",
      "grace_days": 2
    },
    "signature": "<HEX_ED25519_SIGNATURE>"
  }
  ```
  - La verificación canónica serializa el diccionario `payload` ordenando claves (`json.dumps(payload, sort_keys=True)`) y verifica la firma contra la clave pública Ed25519.
  - Manejo de anti-retroceso de reloj comparando con la última fecha de uso registrada localmente en `%LOCALAPPDATA%\StockCellularCenter\runtime_state.json`.

---

## 5. Estrategia de Pruebas y Validación

1. **Pruebas de Rendimiento de Escaneo**:
   - Simulación de escaneo rápido en ráfaga (50 códigos en 1 segundo).
   - Verificar 0% de pérdida de códigos, registro en orden exacto y ausencia de congelamiento de UI.
2. **Pruebas de Estructura QR**:
   - Escaneo de `%MUEBLE`, `@CAJA`, `#VIDRIERA` intercalados y anidados.
   - Plegar / desplegar nodos y verificar persistencia del orden.
   - Verificar que el progreso no supere el 100% por presencia de QRs.
   - Validar conteo de faltantes y visualización de `✓`.
3. **Pruebas de Diferencias y Atajos**:
   - Probar tecla `F3` para abrir/cerrar.
   - Probar tecla `F4` sobre faltante (+1 al escaneo).
   - Probar tecla `Delete` sobre sobrante (-1 al escaneo).
   - Verificar que el contador visual refleje exactamente `inexistentes + sobrantes`.
4. **Pruebas de Seguridad**:
   - Búsqueda completa de strings sensibles (`grep` sobre repo).
   - Probar setup de contraseña inicial, login exitoso, login fallido.
   - Probar licencia válida, licencia alterada (debe fallar), licencia vencida.
   - Generación de licencia de 30/60/90 días con `tools/license_generator.py`.
5. **Pruebas de Build**:
   - Ejecución desde script `python run_app.py`.
   - Script de compilación PyInstaller limpio sin rutas personales fijas.

---

## 6. Plan de Ejecución por Etapas
- **Etapa 1:** Arquitectura y Seguridad Base (Auth PBKDF2 + Licencias Asimétricas + Config en AppData).
- **Etapa 2:** Núcleo de Inventario y Main Stock (`main_stock.json` + QRs `@`, `%`, `#` + Exclusión de QRs en métricas de stock).
- **Etapa 3:** Pipeline de Escaneo No Bloqueante + Orden Arriba/Abajo + UI Responsiva.
- **Etapa 4:** Plegado/Desplegado de Árbol QR + Indicadores `✓` / Faltantes.
- **Etapa 5:** Auditoría de Diferencias + Atajos `F3`, `F4`, `Delete` + Contador de Diferencias Relevantes + Corrección de Búsqueda de Imágenes.
- **Etapa 6:** Sistema de Notificaciones Agrupadas + Validación Diferida de 5s.
- **Etapa 7:** Limpieza de Versión V8.0, Documentación (`README.md`), Script de Build y Auditoría Final de Seguridad.
