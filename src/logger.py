"""
Logger persistente para Stock Cellular Center V8.0.

Reemplaza el DualLogger de run_app.py (que pisaba session_log.txt en cada
ejecución) por el módulo estándar `logging` con rotación.

Características:
- Ubicación: %LOCALAPPDATA%\\StockCellularCenter\\logs\\app.log
  (en no-Windows: ~/.stock_cellular_center/logs/app.log)
- RotatingFileHandler: 1 MB por archivo, hasta 5 backups (app.log.1..5).
- Formato: %(asctime)s - %(levelname)s - %(message)s
- Thread-safe (el módulo logging sincroniza por handler).
- No depende de consola ni sys.stdout: funciona en .exe --windowed.
- Acumulativo: nunca se pisa entre ejecuciones.
- build_logger() acepta override de directorio/tamaño para tests.
"""

import logging
import os
import platform
from logging.handlers import RotatingFileHandler

DEFAULT_MAX_BYTES = 1024 * 1024  # 1 MB
DEFAULT_BACKUP_COUNT = 5
DEFAULT_LOGGER_NAME = "StockCellularCenter"


def default_log_dir():
    """Directorio base de logs, portable Windows/no-Windows."""
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
        return os.path.join(base, "StockCellularCenter", "logs")
    return os.path.join(os.path.expanduser("~"), ".stock_cellular_center", "logs")


def build_logger(log_dir=None, name=DEFAULT_LOGGER_NAME, level=logging.INFO,
                 max_bytes=DEFAULT_MAX_BYTES, backup_count=DEFAULT_BACKUP_COUNT):
    """
    Crea (o reconfigura) un logger rotativo y devuelve (logger, log_file).

    - Si el logger ya tenía handlers, los cierra y reemplaza: permite
      reinicializar limpio (usado por los tests de persistencia).
    - Devuelve la ruta real del archivo de log.
    """
    log_dir = log_dir or default_log_dir()
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False  # No duplicar mensajes vía root

    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    return logger, log_file


def read_log_lines(path=None, max_lines=2000):
    """
    Devuelve una lista de líneas del log para mostrar en la UI.

    Maneja de forma amigable los casos:
    - log inexistente  -> mensaje descriptivo
    - log vacío        -> mensaje descriptivo
    - archivo grande   -> últimas max_lines líneas (evita colgar la UI)
    - error de lectura -> mensaje de error sin excepción
    """
    path = path or default_log_file()
    if not os.path.exists(path):
        return [f"[LOG] No existe archivo de log en: {path}"]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return [f"[LOG] Error al leer el archivo de log: {e}"]

    if not lines:
        return ["[LOG] El archivo de log está vacío."]
    if len(lines) > max_lines:
        return [f"[LOG] Mostrando las últimas {max_lines} de {len(lines)} líneas."] + lines[-max_lines:]
    return lines


def default_log_file():
    """Ruta del log por defecto (sin crear el logger global)."""
    return os.path.join(default_log_dir(), "app.log")


# Logger global de la aplicación (se configura en import).
logger, LOG_FILE = build_logger()
