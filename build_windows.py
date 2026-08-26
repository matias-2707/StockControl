"""
Script de compilación para Windows — Stock Cellular Center V8.0
Genera un ejecutable autocontenido (.exe) utilizando PyInstaller.
"""

import os
import sys
import subprocess
import shutil

def build():
    print("=" * 60)
    print(" Compilando Stock Cellular Center V8.0 para Windows")
    print("=" * 60)

    # 1. Verificar directorio de trabajo
    project_root = os.path.dirname(os.path.abspath(__file__))
    os.chdir(project_root)

    # 2. Rutas de recursos
    icon_path = os.path.join(project_root, "res", "app_icon.ico")
    res_dir = os.path.join(project_root, "res")

    # 3. Argumentos de PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--windowed",
        "--name", "StockCellularCenter_v8.0",
        "--add-data", f"{res_dir};res",
        "--collect-all", "customtkinter",
        "--collect-all", "cryptography",
        "--collect-all", "PIL",
        "--exclude-module", "tests",
    ]

    if os.path.exists(icon_path):
        cmd.extend(["--icon", icon_path])

    cmd.append("run_app.py")

    print(f"Ejecutando: {' '.join(cmd)}")
    result = subprocess.run(cmd)

    if result.returncode == 0:
        print("\n" + "=" * 60)
        print(" [ÉXITO] Compilación completada con éxito.")
        print(f" Ejecutable generado en: {os.path.join(project_root, 'dist', 'StockCellularCenter_v8.0')}")
        print("=" * 60)
    else:
        print("\n" + "=" * 60)
        print(" [ERROR] La compilación falló. Código de error:", result.returncode)
        print("=" * 60)
        sys.exit(result.returncode)

if __name__ == "__main__":
    build()
