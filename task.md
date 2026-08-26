# Stock Cellular Center V8 — Task Tracker

## 1. Arquitectura y Análisis Inicial
- [x] Analizar estructura V7.1 y módulos existentes
- [x] Crear IMPLEMENTATION_PLAN.md con especificación criptográfica (Ed25519 + PBKDF2 dinámico)
- [x] Crear task.md con memoria persistente del proyecto
- [x] Aprobación inicial del plan por el usuario

## 2. Seguridad, Autenticación y Licencias (Etapa 1)
- [x] Diseñar e implementar nuevo `src/core/auth.py` sin contraseñas ni salts hardcodeados
- [x] Implementar almacenamiento local de credenciales con salt por instalación + PBKDF2-HMAC-SHA256 con iteraciones configurables
- [x] Implementar flujo de primer inicio (UI modal) para configuración de contraseña de acceso
- [x] Implementar sistema de licencias con clave pública Ed25519 embebida y verificación criptográfica estándar (`cryptography`)
- [x] Crear herramienta `tools/license_generator.py` para emisión de licencias firmadas con autoridad externa
- [x] Aislar clave privada y licencia runtime fuera del árbol distribuible
- [x] Corregir período de gracia temporal y renovación hacia AppData
- [x] Limpiar `src/config.py` de hashes fijos y rutas personales
- [x] Pruebas unitarias/regresión de Auth:
  - [x] Test hash PBKDF2 y verificación con timing safe compare
  - [x] Test soporte de cambio dinámico de iteraciones
  - [x] Test verificación de licencia válida firmada con Ed25519
  - [x] Test rechazo de licencia alterada (payload manipulado)
  - [x] Test rechazo de licencia con firma inválida
  - [x] Test detección de licencia vencida, días de gracia y reinicio durante gracia
  - [x] Test anti-retroceso de reloj

## 3. Main Stock y Modelo de Ubicaciones (Etapa 2)
- [~] Diseñar e implementar estructura de `main_stock.json` para referencia de ubicaciones
- [x] Implementar lectura, validación y persistencia de `main_stock.json`
- [~] Implementar soporte para escaneo no lineal de contenedores y productos
- [x] Implementar lógica de detección de discrepancias contra `main_stock.json`
- [x] Implementar diálogo interactivo "Mover" / "Dejar ahí" con actualización de `main_stock.json` solo tras confirmación
- [x] Implementar temporizador de validación diferida (5 segundos con debounce) al mover o soltar ítems
- [x] Pruebas unitarias/regresión de Main Stock:
  - [x] Test carga y persistencia de `main_stock.json`
  - [x] Test escaneo fuera de orden (muebles intercalados, productos antes de caja)
  - [ ] Test debounce de 5s en validación de movimiento
- [ ] Implementar jerarquía persistente completa de muebles, cajas y vidrieras

## 4. Núcleo de Escaneo y Prioridad Absoluta (Etapa 3)
- [~] Preparar diseño del pipeline de escaneo de alta velocidad no bloqueante
- [ ] Desacoplar tareas secundarias (imágenes, auditorías, validaciones pesadas) del camino crítico
- [ ] Garantizar registro fiel del 100% de escaneos consecutivos rápidos
- [x] Implementar selector de orden: "Último arriba" vs "Último abajo"
- [x] Asegurar foco y autoscroll constante en el último código escaneado
- [x] Pruebas unitarias/regresión de Escaneo:
  - [ ] Test ráfaga de escaneos rápidos sin pérdida de códigos
  - [ ] Test renderizado correcto en modo "Último arriba" y "Último abajo"

## 5. Jerarquía de QRs (@, %, #) y Progreso (Etapa 4)
- [x] Integrar QRs como contenedores estructurales (@ Caja, % Mueble, # Vidriera)
- [x] Excluir QRs de la búsqueda y descarga de imágenes
- [x] Excluir QRs de las métricas de stock físico (escaneados, esperados, porcentaje y barra)
- [x] Implementar columna de cantidad en QRs: mostrar faltantes según `main_stock.json` y `✓` al completar
- [x] Implementar plegado y desplegado de contenedores en la tabla de escaneados mediante doble clic
- [x] Pruebas unitarias/regresión de QRs y Progreso:
  - [x] Test exclusión de QRs del total de stock escaneado (máx 100%)
  - [x] Test cálculo de faltantes por caja y visualización de `✓`
  - [ ] Test colapso y expansión jerárquica en tabla

## 6. Auditoría de Diferencias y Atajos (F3, F4, Delete) (Etapa 5)
- [x] Corregir índice de búsqueda de imágenes en la tabla de diferencias (usar columna Código/SKU)
- [x] Implementar atajo global `F3` para toggle de apertura/cierre de la ventana de diferencias
- [x] Implementar atajo `F4` dentro de diferencias (+1 unidad faltante al escaneo)
- [x] Implementar atajo `Delete/Supr` dentro de diferencias (-1 unidad sobrante)
- [x] Implementar contador visual de diferencias relevantes (códigos inexistentes + sobrantes) en la barra principal
- [x] Pruebas unitarias/regresión de Diferencias:
  - [x] Test atajo F3 toggle
  - [x] Test atajo F4 agregando 1 unidad al escaneo
  - [x] Test atajo Delete restando 1 unidad al escaneo
  - [x] Test contador de diferencias (solo inexistentes + sobrantes)

## 7. Notificaciones Agrupadas (Etapa 6)
- [ ] Implementar agrupación de alertas por código y situación (evitar alertas repetidas por unidad)
- [ ] Mantener notificaciones fuera del hilo crítico de escaneo
- [~] Limpiar alertas del historial cuando la condición es resuelta
- [x] Pruebas unitarias/regresión de Notificaciones:
  - [ ] Test agrupación de unidades del mismo SKU en 1 sola notificación
  - [ ] Test limpieza automática tras corrección

## 8. Limpieza de Versión, Documentación y Build (Etapa 7)
- [x] Actualizar todas las referencias de versión a "Stock Cellular Center V8.0" (títulos, UI, logs)
- [x] Actualizar `README.md` y documentación técnica
- [x] Crear script de build / empaquetado para Windows (.exe)
- [x] Corregir rutas personales, licencia runtime y clave privada fuera del árbol distribuible

## 9. Verificación y Pruebas Finales Integradas
- [~] Suite unitaria ejecutada; faltan pruebas de UI, ráfagas y concurrencia
- [~] Verificación de arranque, licencia y seguridad del árbol; falta prueba manual de flujo GUI
