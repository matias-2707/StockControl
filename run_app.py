import sys
import os
import ctypes

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

# Asegurar que el directorio raíz esté en el path
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Logger persistente (reemplaza al DualLogger/session_log.txt).
# Se importa lo antes posible para capturar errores de arranque.
from src.logger import logger

def handle_exception(exc_type, exc_value, exc_traceback):
    """Excepción global no capturada -> log (incluye hilos secundarios)."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Excepción no capturada", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception
threading_excepthook_installed = False
try:
    import threading
    if hasattr(threading, "excepthook"):
        _orig_threading_excepthook = threading.excepthook
        def _thread_excepthook(args):
            logger.critical(
                "Excepción en hilo: %s", args.thread.name if args.thread else "?",
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            _orig_threading_excepthook(args)
        threading.excepthook = _thread_excepthook
        threading_excepthook_installed = True
except Exception:
    pass

from src.main import StockApp

if __name__ == "__main__":
    logger.info("=== Inicio de aplicación ===")
    # Auto-elevación (Feedback Matías: Evita escudo en el icono)
    if not is_admin():
        # Re-lanzar con privilegios de administrador
        script = os.path.abspath(sys.argv[0])
        if script.endswith('.exe'):
            executable = script
            params = " ".join(sys.argv[1:])
        else:
            executable = sys.executable
            # Cuando corremos por script, el primer parámetro DEBE ser el propio script
            params = f'"{script}" ' + " ".join(sys.argv[1:])
            
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, None, 1)
        if ret <= 32:
            logger.error("Error al solicitar permisos de administrador (ShellExecuteW=%s)", ret)
            print("Error al solicitar permisos de administrador.")
        sys.exit()

    try:
        app = StockApp()
        app.mainloop()
    except Exception:
        logger.exception("Error fatal en mainloop")
    finally:
        logger.info("=== Cierre de aplicación ===")
