import customtkinter as ctk
import os
import json
import glob
from datetime import datetime
from tkinter import messagebox
from src.gui.utils import center_window

class HistoryExplorer(ctk.CTkToplevel):
    def __init__(self, parent, scan_dir, on_selection_callback):
        super().__init__(parent)
        self.title("Explorador de Historial de Stocks")
        self.scan_dir = scan_dir
        self.on_selection = on_selection_callback
        
        self.grab_set()
        center_window(self, 600, 500)
        self.focus_force()
        self._setup_ui()
        self._load_history()

    def _setup_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkLabel(self, text="HISTORIAL DE ESCANEOS", font=("Segoe UI", 20, "bold"))
        header.grid(row=0, column=0, pady=20)

        self.scroll = ctk.CTkScrollableFrame(self)
        self.scroll.grid(row=1, column=0, sticky="nsew", padx=20, pady=10)

        self.btn_close = ctk.CTkButton(self, text="Cerrar", command=self.destroy)
        self.btn_close.grid(row=2, column=0, pady=10)

    def _load_history(self):
        # Buscar .json recursivamente
        pattern = os.path.join(self.scan_dir, "**", "*.json")
        files = glob.glob(pattern, recursive=True)
        
        history_items = []
        for f in files:
            file_lower = f.lower()
            if "auto_save" in file_lower or "ultima_sesion" in file_lower: continue
            
            try:
                # Intentar leer metadatos del JSON para precisión
                with open(f, 'r', encoding='utf-8') as jf:
                    data = json.load(jf)
                
                # Timestamp: Prioridad al dato interno, sino mtime
                ts_iso = data.get("timestamp")
                if ts_iso:
                    dt = datetime.fromisoformat(ts_iso)
                else:
                    dt = datetime.fromtimestamp(os.path.getmtime(f))
                
                date_display = dt.strftime("%d/%m/%Y %H:%M:%S")
                family = data.get("family_type", "COMBINED")
                
                # Contar unidades escaneadas
                scanned = data.get("scanned", {})
                count = sum(len(p) if isinstance(p, list) else 0 for p in scanned.values())
                
                history_items.append({
                    "path": f,
                    "datetime": dt,
                    "date_display": date_display,
                    "family": family,
                    "count": count,
                    "name": os.path.basename(f)
                })
            except:
                continue

        # Ordenar por fecha (más reciente arriba)
        history_items.sort(key=lambda x: x["datetime"], reverse=True)

        if not history_items:
            ctk.CTkLabel(self.scroll, text="No se encontraron sesiones guardadas.", font=("Segoe UI", 13, "italic")).pack(pady=40)
            return

        for item in history_items:
            # Verificar existencia de archivos (Feedback Matías: El patrón ahora es AG-fecha.csv)
            folder = os.path.dirname(item["path"])
            fname = os.path.basename(item["path"]).upper()
            
            # Determinar tipo de stock para colores pildora grande (Feedback Matías)
            fam = item["family"].upper()
            if "AG" in fam:
                type_text = "AG"
                type_color = "#2ecc71" # Verde
            elif "AM" in fam or "AO" in fam:
                type_text = "AM/AO"
                type_color = "#3498db" # Azul
            else:
                type_text = "COMB"
                type_color = "#9b59b6" # Púrpura

            csv_path = data.get("csv_path", "")
            has_csv = os.path.exists(csv_path) if csv_path else False

            # Card container con color sutil de fondo según tipo
            card = ctk.CTkFrame(self.scroll, fg_color=("#f0f3f5", "#242424"), corner_radius=10, border_width=1, border_color="#333")
            card.pack(fill="x", pady=5, padx=10)
            
            # 1. Pildora Grande de Tipo (Indicador visual masivo)
            type_pill = ctk.CTkLabel(card, text=type_text, font=("Segoe UI", 12, "bold"), 
                                    width=70, height=35, fg_color=type_color, text_color="white", corner_radius=8)
            type_pill.pack(side="left", padx=(10, 5), pady=10)

            # 2. Contenedor de info
            info_frame = ctk.CTkFrame(card, fg_color="transparent")
            info_frame.pack(side="left", padx=15, pady=10, fill="both", expand=True)
            
            title_frame = ctk.CTkFrame(info_frame, fg_color="transparent")
            title_frame.pack(fill="x", anchor="w")
            
            ctk.CTkLabel(title_frame, text=item["date_display"], font=("Segoe UI", 14, "bold"), text_color="#3a7ebf").pack(side="left")
            
            # Píldoras de archivos
            pill_json = ctk.CTkLabel(title_frame, text=" JSON ", font=("Segoe UI", 9, "bold"), 
                                    fg_color="#3a7ebf", text_color="white", corner_radius=10)
            pill_json.pack(side="left", padx=(10, 5))
            
            if has_csv:
                pill_csv = ctk.CTkLabel(title_frame, text=" CSV ", font=("Segoe UI", 9, "bold"), 
                                       fg_color="#27ae60", text_color="white", corner_radius=10)
                pill_csv.pack(side="left")
            else:
                pill_no_csv = ctk.CTkLabel(title_frame, text=" NO CSV ", font=("Segoe UI", 9, "bold"), 
                                         fg_color="#888", text_color="white", corner_radius=10)
                pill_no_csv.pack(side="left")

            sub_text = f"Items: {item['count']} | Archivo: {item['name']}"
            ctk.CTkLabel(info_frame, text=sub_text, font=("Segoe UI", 11), text_color="gray").pack(anchor="w")
            
            btn_load = ctk.CTkButton(card, text="ABRIR", width=80, height=35, 
                                    fg_color="#2c3e50", hover_color="#34495e", font=("Segoe UI", 12, "bold"),
                                    command=lambda p=item['path']: self._select_path(p))
            btn_load.pack(side="right", padx=15)

    def _select_path(self, path):
        self.on_selection({"mode": "history", "path": path})
        self.destroy()
