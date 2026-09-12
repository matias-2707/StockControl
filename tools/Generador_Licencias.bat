@echo off
REM ============================================================
REM  Generador de Licencias V8.0 - Lanzador
REM  Uso EXCLUSIVO del propietario (Matias).
REM
REM  Ejecuta la GUI administrativa usando el Python del proyecto
REM  (.venv), de modo que no haga falta activar el entorno ni
REM  recordar rutas. Funciona tambien con doble clic.
REM ============================================================

REM Raiz del proyecto = carpeta padre de este .bat (tools\..)
set "SCRIPT_DIR=%~dp0"
set "PROJECT_ROOT=%SCRIPT_DIR%.."
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
set "APP=%SCRIPT_DIR%generador_licencias_gui.py"

REM Trabajar sobre la raiz del proyecto (imports de src/ y tools/)
cd /d "%PROJECT_ROOT%"

if not exist "%PYTHON%" (
    echo.
    echo ERROR: No se encontro el Python del proyecto:
    echo   %PYTHON%
    echo Verifique que existe la carpeta .venv en la raiz del proyecto.
    echo.
    pause
    exit /b 1
)

"%PYTHON%" "%APP%"

REM Si el programa termina con error, mantener la ventana para verlo.
if errorlevel 1 (
    echo.
    echo El generador finalizo con un error.
    pause
)
