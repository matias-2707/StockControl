import customtkinter as ctk
from tkinter import ttk
import tkinter as tk

class InventoryTable(ctk.CTkFrame):
    def __init__(self, master, title, columns, config_manager=None, show_title=True, **kwargs):
        super().__init__(master, **kwargs)
        self.config = config_manager
        
        if show_title:
            self.title_label = ctk.CTkLabel(self, text=title, font=("Roboto", 16, "bold"))
            self.title_label.pack(pady=5, fill="x")
        else:
            self.title_label = None

        self.tree_frame = ctk.CTkFrame(self)
        self.tree_frame.pack(expand=True, fill="both", padx=5, pady=5)

        self.scrollbar = ctk.CTkScrollbar(self.tree_frame, orientation="vertical")
        self.scrollbar.pack(side="right", fill="y")

        self.tree = ttk.Treeview(
            self.tree_frame, 
            columns=columns, 
            show="headings", 
            yscrollcommand=self.scrollbar.set
        )
        
        for col in columns:
            self.tree.heading(col, text=col, command=lambda c=col: self.sort_column(c, False))
            self.tree.column(col, width=120, anchor="center")

        self.tree.pack(side="left", expand=True, fill="both")
        self.scrollbar.configure(command=self.tree.yview)

        # Clic derecho para copiar
        self.tree.bind("<Button-3>", self._on_right_click)

        self.refresh_styles()

    def _on_right_click(self, event):
        self.tree.focus_set() # Asegurar foco para portapapeles (Feedback Matías)
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.tree.focus(item)
            values = self.tree.item(item, 'values')
            if values:
                # Detectar columna 'Código' dinámicamente
                cols = list(self.tree["columns"])
                sku_idx = cols.index("Código") if "Código" in cols else 0
                sku = values[sku_idx]
                
                import pyperclip
                pyperclip.copy(sku)
                
                p = self.winfo_toplevel()
                app = None
                if self.config and "parent_app" in self.config:
                    app = self.config["parent_app"]
                elif hasattr(p, 'show_toast'):
                    app = p
                    
                if app and hasattr(app, 'show_toast'):
                    px = self.winfo_pointerx() - p.winfo_rootx() + 15
                    py = self.winfo_pointery() - p.winfo_rooty() + 15
                    app.show_toast(f"Copiado: {sku}", mtype="success", duration=1000, use_history=False, pos=(px, py), master=p)

    def refresh_styles(self):
        """Aplica los colores actuales de la configuración al estilo del Treeview."""
        if not self.config:
            bg, fg = "#242424", "white"
            sel_bg = "#1f538d"
        else:
            bg = self.config.get("table_bg", "#242424")
            fg = self.config.get("table_fg", "#ffffff")
            sel_bg = self.config.get("table_selected_bg", "#1f538d")

        self.style = ttk.Style()
        self.style.theme_use("default")
        self.style.configure("Treeview", 
            background=bg, 
            foreground=fg, 
            fieldbackground=bg,
            rowheight=30
        )
        self.style.map("Treeview", background=[("selected", sel_bg)])

    def insert_data(self, values, tags=None):
        if tags is not None:
            return self.tree.insert("", "end", values=values, tags=tags)
        else:
            return self.tree.insert("", "end", values=values)

    def sort_column(self, col, reverse):
        l = [(self.tree.set(k, col), k) for k in self.tree.get_children('')]
        try:
            # Ordenar como número si es posible
            l.sort(key=lambda t: float(t[0].split()[0]), reverse=reverse)
        except:
            l.sort(reverse=reverse)

        for index, (val, k) in enumerate(l):
            self.tree.move(k, '', index)

        self.tree.heading(col, command=lambda: self.sort_column(col, not reverse))

    def clear(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

    def set_row_color(self, item_id, colors):
        """Aplica un par de colores (fondo, fuente) a la fila."""
        bg, fg = colors
        tag = f"color_{bg}_{fg}".replace("#", "")
        self.tree.item(item_id, tags=(tag,))
        self.tree.tag_configure(tag, background=bg, foreground=fg)

    def set_row_style(self, item_id, colors, bold=False):
        """Aplica colores y opcionalmente estilo negrita a la fila."""
        bg, fg = colors
        tag = f"style_{bg}_{fg}_{bold}".replace("#", "")
        self.tree.item(item_id, tags=(tag,))
        font_opt = ("TkDefaultFont", 11, "bold") if bold else ("TkDefaultFont", 11)
        self.tree.tag_configure(tag, background=bg, foreground=fg, font=font_opt)
