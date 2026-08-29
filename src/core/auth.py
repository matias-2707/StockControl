import os
import json
import hashlib
import hmac
import secrets
import sys
import platform
import shutil
from datetime import datetime, timedelta
import customtkinter as ctk
from tkinter import messagebox, filedialog
from src.gui.utils import center_window
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.exceptions import InvalidSignature

# Clave pública Ed25519 para verificación de licencias (RFC 8032)
# Corresponde al par de claves offline del propietario.
PUBLIC_KEY_HEX = "a82b09739210d54e1dd61f4bae82b5a88db12824886e4bc40d8ae78c12b6bea9"

class AuthManager:
    """
    Gestor de Autenticación y Licencias para Stock Cellular Center V8.0.
    - Cero contraseñas hardcodeadas.
    - Credenciales locales en %LOCALAPPDATA% con salt aleatorio por instalación + PBKDF2 dinámico.
    - Licencias asimétricas firmadas con Ed25519 (clave pública embebida en cliente).
    """

    def __init__(self, config_manager):
        self.config = config_manager
        
        # Directorio de almacenamiento seguro local
        if platform.system() == "Windows":
            self.app_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "StockCellularCenter")
        else:
            self.app_dir = os.path.join(os.path.expanduser("~"), ".stock_cellular_center")
        
        os.makedirs(self.app_dir, exist_ok=True)
        self.auth_file = os.path.join(self.app_dir, "auth.json")
        self.runtime_file = os.path.join(self.app_dir, "runtime_state.json")

        # Búsqueda de archivo de licencia (license.dat)
        self.license_file = self._find_license_path()
        self.public_key_hex = PUBLIC_KEY_HEX

    def _find_license_path(self):
        """Devuelve la ubicación runtime escribible de la licencia.

        La licencia se mantiene fuera del directorio de instalación para que una
        renovación funcione tanto en desarrollo como en un EXE instalado sin
        permisos de escritura en su propia carpeta.
        """
        return os.path.join(self.app_dir, "license.dat")

    def _today(self):
        """Punto único para obtener la fecha actual y facilitar pruebas temporales."""
        return datetime.now().date()

    # ==========================================
    # GESTIÓN DE CREDENCIALES (PBKDF2 DINÁMICO)
    # ==========================================

    def is_first_run(self) -> bool:
        """Determina si es el primer arranque y se necesita configurar contraseña."""
        if not os.path.exists(self.auth_file):
            return True
        try:
            with open(self.auth_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return "password_hash" not in data or "salt" not in data
        except:
            return True

    def setup_initial_password(self, password: str, iterations: int = 200000) -> bool:
        """Configura la contraseña de acceso inicial con salt aleatorio y PBKDF2."""
        if not password or len(password.strip()) < 3:
            return False
        try:
            salt = secrets.token_hex(32) # 32 bytes de entropía criptográfica
            salt_bytes = bytes.fromhex(salt)
            hash_bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
            
            auth_data = {
                "version": "8.0",
                "algorithm": "pbkdf2_sha256",
                "iterations": iterations,
                "salt": salt,
                "password_hash": hash_bytes.hex(),
                "created_at": datetime.now().isoformat()
            }
            with open(self.auth_file, "w", encoding="utf-8") as f:
                json.dump(auth_data, f, indent=4)
            return True
        except Exception as e:
            print(f"Error al guardar credenciales iniciales: {e}")
            return False

    def verify_password_hash(self, password: str) -> bool:
        """Verifica una contraseña ingresada contra el hash PBKDF2 almacenado."""
        if not os.path.exists(self.auth_file):
            return False
        try:
            with open(self.auth_file, "r", encoding="utf-8") as f:
                auth_data = json.load(f)

            salt = auth_data.get("salt")
            stored_hash = auth_data.get("password_hash")
            iterations = int(auth_data.get("iterations", 200000))

            if not salt or not stored_hash:
                return False

            salt_bytes = bytes.fromhex(salt)
            computed_bytes = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, iterations)
            computed_hash = computed_bytes.hex()

            return hmac.compare_digest(computed_hash, stored_hash)
        except Exception as e:
            print(f"Error al verificar credenciales: {e}")
            return False

    # ==========================================
    # GESTIÓN DE LICENCIAS (Ed25519 ASIMÉTRICO)
    # ==========================================

    @staticmethod
    def _canonical_bytes(payload: dict) -> bytes:
        """Serialización canónica estricta para firma y verificación."""
        return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')

    def verify_license_data(self, raw_content: str):
        """
        Valida criptográficamente un contenido de licencia contra la clave pública Ed25519.
        Retorna (True, payload_dict) si es válida, o (False, mensaje_error).
        """
        try:
            data = json.loads(raw_content)
            if not isinstance(data, dict) or "payload" not in data or "signature" not in data:
                return False, "Estructura de licencia inválida."

            payload = data["payload"]
            sig_hex = data["signature"]

            pub_key_bytes = bytes.fromhex(self.public_key_hex)
            public_key = ed25519.Ed25519PublicKey.from_public_bytes(pub_key_bytes)
            
            sig_bytes = bytes.fromhex(sig_hex)
            payload_bytes = self._canonical_bytes(payload)

            # Verificación asimétrica Ed25519
            public_key.verify(sig_bytes, payload_bytes)
            return True, payload

        except InvalidSignature:
            return False, "Firma digital no válida. Licencia manipulada o ilegítima."
        except Exception as e:
            return False, f"Error al procesar licencia: {e}"

    def _read_runtime_state(self) -> dict:
        if os.path.exists(self.runtime_file):
            try:
                with open(self.runtime_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _write_runtime_state(self, state: dict):
        try:
            with open(self.runtime_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=4)
        except:
            pass

    def check_license(self) -> bool:
        """Verifica la licencia actual en license.dat con validación de expiración y anti-rollback."""
        if not self.license_file or not os.path.exists(self.license_file):
            self.license_file = self._find_license_path()

        if not os.path.exists(self.license_file):
            messagebox.showerror(
                "Licencia No Encontrada",
                "No se encontró el archivo de licencia (license.dat).\nPor favor proporcione un archivo de licencia válido."
            )
            return self.show_renewal_window()

        try:
            with open(self.license_file, "r", encoding="utf-8") as f:
                raw_content = f.read()

            valid, result = self.verify_license_data(raw_content)
            if not valid:
                messagebox.showerror("Error de Licencia", f"La licencia es inválida:\n{result}")
                return self.show_renewal_window()

            payload = result
            today = self._today()
            exp_date = datetime.strptime(payload["expiry_date"], "%Y-%m-%d").date()
            grace_days = int(payload.get("grace_days", 0))

            runtime = self._read_runtime_state()
            last_date_str = runtime.get("last_used_date")
            
            # Control anti-retroceso de reloj
            if last_date_str:
                last_used = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                if today < last_used:
                    messagebox.showerror(
                        "Reloj del Sistema Incorrecto",
                        f"Se detectó un cambio retroactivo en la fecha del sistema.\nÚltimo uso: {last_used}\nFecha actual: {today}"
                    )
                    return False

            # Comprobar expiración
            if today > exp_date:
                max_grace_date = exp_date + timedelta(days=grace_days)
                if today <= max_grace_date:
                    remaining_grace = (max_grace_date - today).days
                    messagebox.showwarning(
                        "Licencia Expirada - Período de Gracia",
                        f"Su licencia expiró el {exp_date}.\nSe encuentra en uso de gracia ({remaining_grace} días restantes).\nRenueve la licencia a la brevedad."
                    )
                    runtime["last_used_date"] = today.strftime("%Y-%m-%d")
                    self._write_runtime_state(runtime)
                    return True
                else:
                    messagebox.showwarning(
                        "Licencia Expirada",
                        f"La licencia expiró el {exp_date}.\nPor favor importe un nuevo archivo de licencia."
                    )
                    return self.show_renewal_window()

            # Actualizar última fecha de uso
            runtime["last_used_date"] = today.strftime("%Y-%m-%d")
            self._write_runtime_state(runtime)
            return True

        except Exception as e:
            messagebox.showerror("Error Crítico de Licencia", str(e))
            return False

    def get_remaining_days(self) -> int:
        """Devuelve los días restantes de la licencia actual."""
        if not self.license_file or not os.path.exists(self.license_file):
            self.license_file = self._find_license_path()
        if not self.license_file or not os.path.exists(self.license_file):
            return 0
        try:
            with open(self.license_file, "r", encoding="utf-8") as f:
                valid, payload = self.verify_license_data(f.read())
            if not valid or not isinstance(payload, dict):
                return 0
            today = self._today()
            exp_date = datetime.strptime(payload["expiry_date"], "%Y-%m-%d").date()
            if today > exp_date:
                return 0
            return (exp_date - today).days
        except:
            return 0

    # ==========================================
    # INTERFAZ GRÁFICA DE ACCESO Y RENOVACIÓN
    # ==========================================

    def show_initial_setup_window(self) -> bool:
        """Ventana para configurar la contraseña de acceso en el primer arranque."""
        result = [False]
        win = ctk.CTkToplevel()
        win.title("Configuración Inicial - Stock Cellular Center V8.0")
        win.attributes("-topmost", True)
        center_window(win, 400, 320)
        win.grab_set()

        ctk.CTkLabel(win, text="BIENVENIDO A STOCK V8.0", font=("Segoe UI", 16, "bold"), text_color="#3498db").pack(pady=(15, 5))
        ctk.CTkLabel(win, text="Configure su contraseña de acceso local:", font=("Segoe UI", 12)).pack(pady=5)

        error_label = ctk.CTkLabel(win, text="", text_color="#e74c3c", font=("Segoe UI", 11))
        error_label.pack(pady=0)

        ctk.CTkLabel(win, text="Nueva Contraseña:", font=("Segoe UI", 11)).pack(anchor="w", padx=50, pady=(5, 0))
        entry1 = ctk.CTkEntry(win, show="*", width=300, justify="center")
        entry1.pack(pady=3)

        ctk.CTkLabel(win, text="Confirmar Contraseña:", font=("Segoe UI", 11)).pack(anchor="w", padx=50, pady=(5, 0))
        entry2 = ctk.CTkEntry(win, show="*", width=300, justify="center")
        entry2.pack(pady=3)

        def on_save():
            p1 = entry1.get()
            p2 = entry2.get()
            if not p1:
                error_label.configure(text="La contraseña no puede estar vacía")
                return
            if len(p1) < 4:
                error_label.configure(text="La contraseña debe tener al menos 4 caracteres")
                return
            if p1 != p2:
                error_label.configure(text="Las contraseñas no coinciden")
                return

            if self.setup_initial_password(p1):
                result[0] = True
                messagebox.showinfo("Configuración Completada", "Contraseña configurada exitosamente.")
                win.destroy()
            else:
                error_label.configure(text="Error al guardar la contraseña")

        entry1.bind("<Return>", lambda e: entry2.focus_set())
        entry2.bind("<Return>", lambda e: on_save())

        ctk.CTkButton(win, text="Guardar y Continuar", command=on_save, fg_color="#27ae60", height=38, font=("Segoe UI", 12, "bold")).pack(pady=15)
        
        win.after(100, entry1.focus_set)
        win.wait_window()
        return result[0]

    def verify_password(self) -> bool:
        """Pantalla de login con máscara de contraseña, autofocus e indicadores de error."""
        if self.is_first_run():
            return self.show_initial_setup_window()

        result = [None]
        win = ctk.CTkToplevel()
        win.title("Acceso - Stock Cellular Center V8.0")
        win.attributes("-topmost", True)
        center_window(win, 360, 220)
        win.grab_set()

        ctk.CTkLabel(win, text="STOCK CELLULAR CENTER V8.0", font=("Segoe UI", 13, "bold"), text_color="#3498db").pack(pady=(15, 2))
        ctk.CTkLabel(win, text="CONTRASEÑA DE ACCESO", font=("Segoe UI", 11, "bold")).pack(pady=(2, 5))

        error_label = ctk.CTkLabel(win, text="", text_color="#e74c3c", font=("Segoe UI", 11))
        error_label.pack(pady=0)

        entry = ctk.CTkEntry(win, show="*", width=220, justify="center", font=("Segoe UI", 13))
        entry.pack(pady=10)
        entry.focus_set()

        def on_submit(event=None):
            password = entry.get()
            if self.verify_password_hash(password):
                result[0] = True
                win.destroy()
            else:
                error_label.configure(text="Contraseña incorrecta")
                entry.delete(0, 'end')
                entry.focus_set()

        entry.bind("<Return>", on_submit)
        ctk.CTkButton(win, text="Ingresar al Sistema", command=on_submit, fg_color="#3498db", height=35, font=("Segoe UI", 12, "bold")).pack(pady=10)

        win.after(100, entry.focus_set)
        win.wait_window()
        return result[0] is True

    def activate_license_file(self, path: str):
        """Valida y copia una licencia al almacenamiento runtime escribible.

        Retorna ``(True, mensaje)`` cuando la licencia quedó activada, o
        ``(False, motivo)`` si el archivo no puede utilizarse.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            valid, result = self.verify_license_data(content)
            if not valid:
                return False, result

            exp_date = datetime.strptime(result["expiry_date"], "%Y-%m-%d").date()
            today = self._today()
            if today > exp_date:
                return False, f"La licencia seleccionada ya expiró el {exp_date}."

            destination = os.path.join(self.app_dir, "license.dat")
            shutil.copy2(path, destination)
            self.license_file = destination

            runtime = self._read_runtime_state()
            runtime.pop("grace_consumed", None)  # Migración desde V8 inicial.
            runtime["last_used_date"] = today.strftime("%Y-%m-%d")
            self._write_runtime_state(runtime)
            return True, f"Licencia activada exitosamente. Válida hasta: {exp_date}."
        except Exception as e:
            return False, f"Error al importar: {e}"

    def show_renewal_window(self) -> bool:
        """Ventana interactiva para importar un nuevo archivo de licencia firmado (license.dat)."""
        result = [False]
        win = ctk.CTkToplevel()
        win.title("Renovación de Licencia - Stock Cellular Center V8.0")
        win.attributes("-topmost", True)
        center_window(win, 450, 260)
        win.grab_set()

        ctk.CTkLabel(win, text="RENOVACIÓN DE LICENCIA", font=("Segoe UI", 14, "bold"), text_color="#3498db").pack(pady=(15, 5))
        ctk.CTkLabel(
            win,
            text="Seleccione el nuevo archivo de licencia firmado (license.dat)\nproporcionado por el administrador.",
            font=("Segoe UI", 11),
            justify="center"
        ).pack(pady=5)

        lbl_status = ctk.CTkLabel(win, text="", font=("Segoe UI", 11))
        lbl_status.pack(pady=2)

        def import_file():
            path = filedialog.askopenfilename(
                title="Seleccionar archivo de licencia",
                filetypes=[("Archivos de Licencia", "*.dat;*.json"), ("Todos los archivos", "*.*")]
            )
            if not path:
                return

            try:
                activated, message = self.activate_license_file(path)
                if not activated:
                    lbl_status.configure(text=f"Error: {message}", text_color="#e74c3c")
                    return

                messagebox.showinfo("Éxito", message)
                result[0] = True
                win.destroy()

            except Exception as e:
                lbl_status.configure(text=f"Error al importar: {e}", text_color="#e74c3c")

        ctk.CTkButton(win, text="Seleccionar Archivo license.dat", command=import_file, fg_color="#27ae60", height=40, font=("Segoe UI", 12, "bold")).pack(pady=15)
        ctk.CTkButton(win, text="Cancelar", command=win.destroy, fg_color="gray", width=100).pack(pady=5)

        win.wait_window()
        return result[0]

    def ask_master_password(self, title="Seguridad V8.0", text="Ingrese contraseña de acceso:") -> bool:
        """Solicita la contraseña de acceso con máscara e indicador de error inline."""
        result = [False]
        win = ctk.CTkToplevel()
        win.title(title)
        win.attributes("-topmost", True)
        center_window(win, 350, 200)
        win.grab_set()

        ctk.CTkLabel(win, text="SEGURIDAD REQUERIDA", font=("Segoe UI", 12, "bold")).pack(pady=(15, 2))
        ctk.CTkLabel(win, text=text, font=("Segoe UI", 11), text_color="gray").pack(pady=0)

        error_label = ctk.CTkLabel(win, text="", text_color="#e74c3c", font=("Segoe UI", 11))
        error_label.pack(pady=0)

        entry = ctk.CTkEntry(win, show="*", width=200, justify="center")
        entry.pack(pady=8)
        entry.focus_set()

        def on_submit(event=None):
            password = entry.get()
            if self.verify_password_hash(password):
                result[0] = True
                win.destroy()
            else:
                error_label.configure(text="Contraseña incorrecta")
                entry.delete(0, 'end')
                entry.focus_set()

        entry.bind("<Return>", on_submit)
        ctk.CTkButton(win, text="Aceptar", command=on_submit, fg_color="#3498db", height=35, font=("Segoe UI", 12, "bold")).pack(pady=10)

        win.after(100, entry.focus_set)
        win.wait_window()
        return result[0]
