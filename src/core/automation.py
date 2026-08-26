import time
import threading
from pynput import keyboard
import pyperclip

class AutomationManager:
    def __init__(self, config_manager, inventory_manager):
        self.config = config_manager
        self.inventory = inventory_manager
        self.controller = keyboard.Controller()
        self.listener = None
        self.stop_event = threading.Event()
        self.is_paused = False
        
        # QR especial para borrar el último escaneado
        self.QR_DELETE_CODE = "DEL_LAST_SCAN_QR"

    def start_global_listener(self, on_f2_callback):
        """Inicia el escucha de teclas globales (F2, F8)."""
        def on_press(key):
            try:
                if key == keyboard.Key.f2:
                    on_f2_callback()
                elif key == keyboard.Key.f8:
                    self.is_paused = not self.is_paused
                    p = self.inventory.config.get("parent_app")
                    if p:
                        status = "PAUSADO" if self.is_paused else "REANUDADO"
                        p.show_toast(f"Vaciado: {status}", mtype="warning" if self.is_paused else "success", duration=2000, use_history=False)
            except AttributeError:
                pass

        self.listener = keyboard.Listener(on_press=on_press)
        self.listener.start()

    def type_string(self, text):
        """Simula el tecleo de un string carácter por carácter."""
        speed = self.config.get("speed_multiplier", 1.0)
        delay = 0.008 / speed
        
        for char in text:
            if self.stop_event.is_set(): break
            while self.is_paused: time.sleep(0.1)
            
            self.controller.type(char)
            time.sleep(delay)
        
        self.controller.press(keyboard.Key.enter)
        self.controller.release(keyboard.Key.enter)
        time.sleep(0.01 / speed)

    def paste_string(self, text):
        """Simula pegar el texto usando Ctrl+V."""
        speed = self.config.get("speed_multiplier", 1.0)
        pyperclip.copy(text)
        time.sleep(0.05 / speed)
        
        with self.controller.pressed(keyboard.Key.ctrl):
            self.controller.press('v')
            self.controller.release('v')
        
        time.sleep(0.05 / speed)
        self.controller.press(keyboard.Key.enter)
        self.controller.release(keyboard.Key.enter)

    def process_export(self, code_list, mode="typing", progress_callback=None):
        """Procesa una lista de códigos para exportar con soporte para feedback en tiempo real."""
        self.stop_event.clear()
        self.is_paused = False # Asegurar que empiece despausado
        total = len(code_list)
        for i, code in enumerate(code_list):
            if self.stop_event.is_set(): break
            
            # Chequear si la exportación está pausada (F8)
            while self.is_paused:
                if self.stop_event.is_set(): break
                time.sleep(0.1)
                
            if self.stop_event.is_set(): break
            
            if progress_callback:
                progress_callback(code, i + 1, total)

            if mode == "typing":
                self.type_string(code)
            else:
                self.paste_string(code)
            
            # Pequeña pausa entre códigos (ajustada por multiplicador)
            time.sleep(0.02 / self.config.get("speed_multiplier", 1.0))

    def export_data(self):
        """Inicia el proceso de vaciado con cuenta regresiva."""
        p = self.inventory.config.get("parent_app")
        if not p: return

        # Obtener lista de códigos escaneados (todos los acumulados)
        all_codes = []
        # El manual dice "pasa los códigos escaneados", típicamente en el orden que se escanearon
        scans_with_pos = []
        for code, positions in self.inventory.scanned_items.items():
            for pos in positions:
                scans_with_pos.append((pos, code))
        scans_with_pos.sort() # Orden cronológico
        all_codes = [s[1] for s in scans_with_pos]

        if not all_codes:
            p.show_toast("No hay códigos para exportar.", mtype="error")
            return

        def run_export():
            # Cuenta regresiva de 10s (personalizable en config)
            delay = self.config.get("export_delay_seconds", 10)
            for i in range(delay, 0, -1):
                if self.stop_event.is_set(): return
                p.show_toast(f"Vaciado en {i}s... ¡Prepara la ventana!", mtype="info", duration=1000, use_history=False)
                time.sleep(1)
            
            p.show_toast("Iniciando Vaciado...", mtype="success", duration=2000)
            mode = self.config.get("paste_mode", "typing")
            self.process_export(all_codes, mode=mode)
            p.show_toast("Vaciado Completo", mtype="success")

        threading.Thread(target=run_export, daemon=True).start()

    def check_qr_command(self, scanned_code):
        """Verifica si el código escaneado es un comando (ej. borrar último)."""
        if scanned_code == self.QR_DELETE_CODE:
            return self.inventory.delete_last()
        return False

    def stop_automation(self):
        self.stop_event.set()
