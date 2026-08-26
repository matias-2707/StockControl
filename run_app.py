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

class DualLogger:
    def __init__(self, filepath):
        self.terminal = sys.stdout
        self.log = open(filepath, "w", encoding='utf-8')

    def write(self, message):
        if self.terminal:
            try:
                self.terminal.write(message)
            except:
                pass
        self.log.write(message)
        self.log.flush()

    def flush(self):
        if self.terminal:
            try:
                self.terminal.flush()
            except:
                pass
        self.log.flush()

# Redirigir consola a un archivo log que se pisa en cada ejecución
try:
    log_path = os.path.join(project_root, "session_log.txt")
    sys.stdout = DualLogger(log_path)
    sys.stderr = sys.stdout
except:
    pass

from src.main import StockApp

if __name__ == "__main__":
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
            print("Error al solicitar permisos de administrador.")
        sys.exit()

    try:
        app = StockApp()
        app.mainloop()
    except Exception as e:
        import traceback
        traceback.print_exc()
        input("Error fatal. Presione Enter para salir...")
