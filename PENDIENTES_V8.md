# PENDIENTES V8.0 — Guía permanente del proyecto

> Documento vivo. Mantener actualizado: marcar `[x]` lo resuelto y `[ ]` lo pendiente.
> Última actualización: 2026-09-11.

---

## 🔴 Prioridad alta

- [ ] **Investigar la aparición de la contraseña de pruebas en la PC del trabajo.**
  Al pasar el programa a la PC del trabajo, apareció la contraseña usada durante las
  pruebas. **NO asumir que está embebida en el EXE**: auditar de dónde provino
  (¿`auth.json` copiado junto al build? ¿datos de usuario en la carpeta del proyecto?
  ¿asset capturado por PyInstaller? ¿algún archivo de sesión/config?).
  **Objetivo**: garantizar que el proceso de build produzca una **instalación limpia**,
  sin `auth.json`, `license.dat`, claves, datos de usuario ni credenciales.
  Entregable: informe con causa raíz + verificación de que el build/dist queda limpio.

- [ ] **Auditar la columna Cantidad en Cajas / Muebles / Vidrieras.**
  Revisar el cálculo/mostrado de la columna Cantidad en esas vistas.
  (Etapa siguiente; **primero auditar, luego decidir cambios**.)

## 🟡 Prioridad media / mejoras conocidas

- [ ] **Silenciar la clave pública en el modo headless del generador.**
  `tools/generador_licencias_gui.py` en modo headless reutiliza `generate_license()`,
  que imprime la clave pública (hex) por consola. La GUI real no la muestra.
  Evaluar si conviene suprimir ese print en el flujo automatizado (solo si se autoriza).

- [ ] **Revisar tests `test_auth.py` que cuelgan sin display.**
  `test_grace_period_last_day_and_after_expiry` invoca `check_license()` con licencia
  vencida → `show_renewal_window()` → `CTkToplevel().wait_window()` bloquea en
  entornos sin sesión gráfica (y probablemente también en la real). Pre-existente.
  Nota: el `--deselect` requiere el nodeid con **barras normales** para matchear.

## 🟢 Resueltos

- [x] **Motor de exportación V8.0** (TYPING rápido + CLIPBOARD modo seguro). Cerrado 2026-09-10.
- [x] **Sistema de licencias asimétricas Ed25519** (clave pública embebida, privada fuera del repo).
- [x] **Login local con PBKDF2 + salt** (sin contraseñas hardcodeadas).
- [x] **Anti-rollback de reloj** en `runtime_state.json`.
- [x] **Fix del mensaje de éxito con `parent=win`** en configuración/renovación (pendiente de commit).
- [x] **Generador administrativo de licencias (GUI + CLI)** — `tools/Generador_Licencias.bat`
  + `tools/generador_licencias_gui.py`, con tests. Cerrado 2026-09-11.

## ⚠️ Reglas permanentes

- [x] **V7.1 estable NO se toca.** Mantener intacta la versión estable V7.1.
- [x] **La clave privada Ed25519 permanece exclusivamente en la VM** del propietario
  (`%LOCALAPPDATA%\StockCellularCenter\license-authority\`). No entra al repo, ni al
  build, ni a logs, ni se copia/mueve.
- [x] **La autoridad de firma vive en el perfil de `matia`** (el propietario); no forma
  parte del build ni del runtime del cliente.
