import customtkinter as ctk
import os
import glob
from tkinter import messagebox
from src.gui.utils import center_window
from src.gui.components.history import HistoryExplorer

class SelectorWindow(ctk.CTkToplevel):
    def __init__(self, parent, inventory_manager, on_selection_callback):
        super().__init__(parent)
        self.title("Stock Cellular Center V8.0")
        self.geometry("450x600")
        self.inventory = inventory_manager
        self.on_selection = on_selection_callback
        self.app_parent = parent
        
        # Hacer la ventana modal
        self.grab_set()
        center_window(self, 450, 600)
        self.focus_force()
        
        # Protocolo de cierre: Si se cierra sin elegir, termina la app
        self.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))
        
        self._load_icons()
        self._setup_ui()

    def _load_icons(self):
        """Carga los iconos del selector."""
        # src/gui/components/selector.py -> ../../../res
        res_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "res")
        def get_icon(name):
            p = os.path.join(res_dir, f"{name}.png")
            if os.path.exists(p):
                from PIL import Image
                img = Image.open(p)
                return ctk.CTkImage(light_image=img, dark_image=img, size=(24, 24))
            return None

        self.icon_plus = get_icon("plus")
        self.icon_history = get_icon("history")

    def _setup_ui(self):
        # Header
        header = ctk.CTkLabel(self, text="¿Qué desea hacer hoy?", font=("Segoe UI", 24, "bold"))
        header.pack(pady=(30, 20))

        # --- SECCIÓN: NUEVO ESCANEO ---
        new_frame = ctk.CTkFrame(self)
        new_frame.pack(pady=10, padx=30, fill="x")
        
        ctk.CTkLabel(new_frame, text="NUEVO ESCANEO", font=("Segoe UI", 12, "bold"), text_color="grey").pack(pady=5)
        
        btn_amao = ctk.CTkButton(new_frame, text=" Nueva Sesión: AM + AO", height=50,
                                image=self.icon_plus, compound="left",
                                command=lambda: self._trigger_selection("AM_AO"))
        btn_amao.pack(pady=5, padx=20, fill="x")
        
        btn_ag = ctk.CTkButton(new_frame, text=" Nueva Sesión: AG", height=50,
                              image=self.icon_plus, compound="left",
                              command=lambda: self._trigger_selection("AG"))
        btn_ag.pack(pady=5, padx=20, fill="x")
        
        btn_comb = ctk.CTkButton(new_frame, text=" Nueva Sesión: AM-AO + AG (TODO)", height=50,
                                image=self.icon_plus, compound="left",
                                fg_color="#2c3e50", hover_color="#34495e",
                                command=lambda: self._trigger_selection("COMBINED"))
        btn_comb.pack(pady=(5, 15), padx=20, fill="x")

        # --- SECCIÓN: RECUPERACIÓN ---
        rec_frame = ctk.CTkFrame(self)
        rec_frame.pack(pady=10, padx=30, fill="x")
        
        ctk.CTkLabel(rec_frame, text="RECUPERACIÓN", font=("Segoe UI", 12, "bold"), text_color="grey").pack(pady=5)
        
        btn_last = ctk.CTkButton(rec_frame, text=" Cargar Último Stock", height=50,
                                 image=self.icon_history, compound="left",
                                 fg_color="#27ae60", hover_color="#2ecc71",
                                 command=self._load_last_autosave)
        btn_last.pack(pady=5, padx=20, fill="x")
        
        btn_hist = ctk.CTkButton(rec_frame, text=" Explorador de Historial (JSON)", height=50,
                                image=self.icon_history, compound="left",
                                fg_color="#f39c12", hover_color="#e67e22",
                                command=self._open_history_explorer)
        btn_hist.pack(pady=(5, 15), padx=20, fill="x")

        # --- LABEL DE LICENCIA ---
        days = self.app_parent.auth.get_remaining_days()
        color = "#2ecc71" if days > 7 else "#e74c3c"
        ctk.CTkLabel(self, text=f"• Licencia Válida: {days} días restantes •", font=("Segoe UI", 12, "bold"), text_color=color).pack(pady=(20, 10))


    def _trigger_selection(self, mode):
        self.grab_release()
        self.destroy()
        self.on_selection({"mode": "new", "family": mode})

    def _load_last_autosave(self):
        # El archivo de última sesión ahora está dentro de la carpeta local de Escaneos
        path = os.path.join(self.inventory.scan_dir, "ultima_sesion.json")
        if os.path.exists(path):
            self.grab_release()
            self.destroy()
            self.on_selection({"mode": "history", "path": path})
        else:
            messagebox.showwarning("Sin Sesiones", "No se encontró ningún stock reciente en la carpeta de escaneos.")

    def _open_history_explorer(self):
        HistoryExplorer(self, self.inventory.scan_dir, self._on_history_select)

    def _on_history_select(self, data):
        self.grab_release()
        self.destroy()
        self.on_selection(data)

