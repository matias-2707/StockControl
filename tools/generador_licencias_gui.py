"""
Generador de Licencias V8.0 - Interfaz grafica administrativa.

Herramienta de uso EXCLUSIVO del propietario (Matias).

Esta es una capa administrativa fina sobre el generador CLI ya existente
(`tools/license_generator.py`). NO duplica la logica criptografica ni el
formato de licencia: reutiliza `generate_license()` y `ensure_keypair()`.

Seguridad:
- La clave privada Ed25519 permanece en la autoridad externa
  (%LOCALAPPDATA%\\StockCellularCenter\\license-authority\\) y NUNCA se
  muestra, imprime ni copia desde esta herramienta.
- El archivo de salida lo elige el administrador de forma explicita.
- Esta herramienta NO forma parte del build del cliente (no esta referenciada
  desde run_app.py ni desde el .spec de PyInstaller).
"""

import os
import sys
import tempfile
from datetime import datetime

# Permitir importar el paquete `tools` cuando se ejecuta como script directo.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tools.license_generator import (  # noqa: E402
    DEFAULT_KEYS_DIR,
    generate_license,
)


def parse_days(raw: str) -> int:
    """Valida y convierte el texto ingresado a un entero positivo de dias.

    Lanza ValueError con un mensaje legible si la entrada no es valida.
    Reglas: no vacio, entero, mayor que cero.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("Ingrese una cantidad de dias.")
    try:
        days = int(text)
    except (TypeError, ValueError):
        raise ValueError("La cantidad de dias debe ser un numero entero.")
    if days <= 0:
        raise ValueError("La cantidad de dias debe ser un entero positivo.")
    return days


def default_output_path(days: int, base_dir: str = None) -> str:
    """Ruta de salida sugerida, jamas sobre la licencia real de %LOCALAPPDATA%.

    Se genera un nombre unico con fecha/hora para no pisar archivos previos.
    """
    if base_dir is None:
        base_dir = os.path.join(tempfile.gettempdir(), "licencias_generadas")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(base_dir, f"license_{days}d_{stamp}.dat")


def _run_gui():  # pragma: no cover - requiere display
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.title("Generador de Licencias V8.0")
    root.geometry("460x300")
    root.resizable(False, False)

    tk.Label(
        root,
        text="GENERADOR DE LICENCIAS V8.0",
        font=("Segoe UI", 14, "bold"),
        fg="#3498db",
    ).pack(pady=(16, 10))

    tk.Label(root, text="Dias de licencia:", font=("Segoe UI", 11)).pack(anchor="w", padx=40)

    days_var = tk.StringVar()
    entry = tk.Entry(root, textvariable=days_var, width=14, justify="center", font=("Segoe UI", 12))
    entry.pack(pady=4)
    entry.focus_set()

    result_var = tk.StringVar()
    result_label = tk.Label(
        root,
        textvariable=result_var,
        font=("Segoe UI", 10),
        justify="left",
        anchor="w",
        wraplength=400,
    )
    result_label.pack(fill="x", padx=40, pady=10)

    def on_generate(event=None):
        try:
            days = parse_days(days_var.get())
        except ValueError as exc:
            result_var.set("")
            messagebox.showerror("Entrada invalida", str(exc))
            return

        suggested = default_output_path(days)
        out_path = filedialog.asksaveasfilename(
            title="Guardar licencia como...",
            initialdir=os.path.dirname(suggested),
            initialfile=os.path.basename(suggested),
            defaultextension=".dat",
            filetypes=[("Archivo de licencia", "*.dat"), ("Todos los archivos", "*.*")],
        )
        if not out_path:
            return  # El usuario cancelo: no se genera nada.

        # Guarda defensiva: nunca escribir sobre la licencia real del cliente.
        real_license = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "StockCellularCenter",
            "license.dat",
        )
        if os.path.abspath(out_path).lower() == os.path.abspath(real_license).lower():
            messagebox.showerror(
                "Ruta no permitida",
                "No se puede sobrescribir la licencia activa del cliente.\nElija otra ubicacion.",
            )
            return

        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            generate_license(days=days, output_file=out_path, keys_dir=DEFAULT_KEYS_DIR)
        except Exception as exc:  # sin exponer detalles de la clave privada
            result_var.set("")
            messagebox.showerror("Error", f"No se pudo generar la licencia: {exc}")
            return

        # Mostrar solo datos administrativos (sin clave publica/privada/firma).
        today = datetime.now().date()
        exp_date = today.fromordinal(today.toordinal() + days)
        result_var.set(
            "Generada correctamente.\n"
            f"Fecha de inicio: {today.strftime('%Y-%m-%d')}\n"
            f"Fecha de vencimiento: {exp_date.strftime('%Y-%m-%d')} ({days} dias)\n"
            f"Ruta del archivo: {out_path}"
        )

    tk.Button(
        root,
        text="GENERAR LICENCIA",
        command=on_generate,
        bg="#27ae60",
        fg="white",
        font=("Segoe UI", 11, "bold"),
        height=2,
        width=24,
    ).pack(pady=6)
    entry.bind("<Return>", on_generate)

    root.mainloop()


def main():
    if os.environ.get("SCC_LICENSE_GUI_HEADLESS") == "1":
        # Modo no grafico para verificaciones automatizadas.
        raw = os.environ.get("SCC_LICENSE_DAYS", "")
        out = os.environ.get("SCC_LICENSE_OUT", "")
        # Permite apuntar a la autoridad de firma de otro perfil/usuario sin
        # copiar nunca la clave privada (solo se lee en su ubicacion original).
        keys_dir = os.environ.get("SCC_LICENSE_KEYS_DIR") or DEFAULT_KEYS_DIR
        days = parse_days(raw)
        if not out:
            out = default_output_path(days)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        generate_license(days=days, output_file=out, keys_dir=keys_dir)
        print(out)
        return
    _run_gui()


if __name__ == "__main__":
    main()
