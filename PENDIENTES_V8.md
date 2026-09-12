# PENDIENTES V8.0 — Guía permanente del proyecto

> Documento vivo. Mantener actualizado: marcar `[x]` lo resuelto y `[ ]` lo pendiente.
> Última actualización: 2026-09-12.

---

## 🔴 Prioridad alta

_(Sin pendientes de alta prioridad abiertos al 2026-09-12.)_

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

## 🟢 Baja prioridad / cobertura preventiva

- [ ] **Ampliar `test_project_integrity` para verificar explícitamente que `auth.json` y
  `runtime_state.json` no aparezcan dentro del árbol del proyecto/build distribuible.**
  Mejora de cobertura **preventiva**: hoy los tests verifican que no haya clave privada
  ni `license.dat` en el árbol, pero no cubren `auth.json`/`runtime_state.json`.
  **NO es un problema actual** (el build limpio ya fue verificado y no arrastra esos
  archivos); solo se agrega la verificación automatizada para prevenir regresiones.

## 🟢 Resueltos

- [x] **Aparición de la contraseña de pruebas en la PC del trabajo.** Cerrado 2026-09-12.
  **Causa raíz: operativa.** La contraseña **NO estaba en el EXE** ni en el **paquete
  distribuido**; el **build limpio fue verificado** y **no arrastra credenciales ni
  configuración de usuario**. Lo que ocurrió fue que se copió **completa** la carpeta
  `%LOCALAPPDATA%\StockCellularCenter` desde la máquina de desarrollo a la PC del
  trabajo; esa carpeta contenía `auth.json`, por lo que se trasladó la credencial de la
  instalación de prueba. Al eliminar la carpeta y dejar que la aplicación la recreara,
  solicitó correctamente una **nueva contraseña**.
  **Regla de despliegue:** para una instalación nueva solo debe distribuirse el
  **paquete de la aplicación** y colocarse la **licencia** correspondiente;
  **NO copiar `%LOCALAPPDATA%\StockCellularCenter` desde otra máquina.**
- [x] **Auditar la columna Cantidad en Cajas / Muebles / Vidrieras.** Cerrado 2026-09-12.
  Resultado: **COMPORTAMIENTO ESPERADO**. La columna muestra faltantes respecto de las
  expectativas cargadas en `main_stock.json`; si no existe expectativa, queda vacía por
  diseño. No existe generación automática de expectativas desde el CSV; Cajas, Muebles y
  Vidrieras usan la misma lógica. Las optimizaciones de F4/Delete/caché no son
  responsables. No se implementó ningún cambio.
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
- [x] **Despliegue limpio:** para una instalación nueva distribuir solo el paquete de la
  aplicación + la licencia. **NO copiar `%LOCALAPPDATA%\StockCellularCenter`** de otra
  máquina (arrastra `auth.json`, `license.dat`, `config.json` y `runtime_state.json`).
