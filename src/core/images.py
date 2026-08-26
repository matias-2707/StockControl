import os
import requests
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
import io
import customtkinter as ctk

class ImageManager:
    def __init__(self, config_manager, progress_callback=None):
        self.config = config_manager
        self.img_folder = self.config.get("image_folder", "img")
        self.progress_callback = progress_callback
        self.total_skus = 0
        self.downloaded_count = 0
        
        if not os.path.exists(self.img_folder):
            os.makedirs(self.img_folder)
        
        self.failed_skus = []
        self.download_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=5)
        self.search_url_base = "https://cellularcenter.com.uy/productos?buscar="

    def get_local_path(self, sku):
        return os.path.join(self.img_folder, f"{sku}.png")

    def _resolve_url(self, u):
        if not u: return None
        if u.startswith('//'): return 'https:' + u
        if u.startswith('/'): return 'https://f.fcdn.app' + u
        return u

    def find_image_urls(self, sku):
        """Busca todas las URLs de imagen en la web de Cellular Center con intentos secundarios."""
        if not sku or sku.startswith(('@', '%', '#')):
            return []
        urls = []
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
        }
        
        def search(query):
            found = []
            try:
                import html
                import json
                url = f"https://cellularcenter.com.uy/catalogo?q={query}"
                response = requests.get(url, headers=headers, timeout=10)
                if response.status_code == 200:
                    # 1. Buscar JSON
                    matches = re.findall(r'class="json"[^>]*value="([^"]+)"', response.text)
                    for m in matches:
                        try:
                            data = json.loads(html.unescape(m))
                            if query in str(data): # Búsqueda más laxa para encontrar variantes
                                img_dict = data.get('variante', {}).get('img', {}) or data.get('producto', {}).get('img', {})
                                if img_dict:
                                    u = self._resolve_url(img_dict.get('u', ''))
                                    if u and u not in found: found.append(u)
                                
                                fotos = data.get('producto', {}).get('fotos', [])
                                for foto in fotos:
                                    u = self._resolve_url(foto.get('u', ''))
                                    if u and u not in found: found.append(u)
                        except: continue
                    
                    # 2. Regex CDN directa
                    cdn_matches = re.findall(r'src=[\'\"](//f\.fcdn\.app/imgs/[^\'\"]*(?:' + query + r')[^\'\"]*\.(?:jpg|png|webp))[\'\"]', response.text, re.IGNORECASE)
                    for cdn_url in cdn_matches:
                        u = 'https:' + cdn_url
                        if u not in found: found.append(u)
            except: pass
            return found

        # Intento 1: SKU exacto
        urls = search(sku)
        
        # Intento 2: Si no hay nada y el SKU tiene guiones o espacios, probar partes (Feedback Matías)
        if not urls and ("-" in sku or " " in sku):
            parts = sku.replace("-", " ").split()
            if parts:
                urls = search(parts[0])

        return urls

    def get_local_paths(self, sku):
        """Retorna lista de rutas locales para un SKU (galería)."""
        paths = []
        base = os.path.join(self.img_folder, sku)
        if os.path.exists(f"{base}.png"):
            paths.append(f"{base}.png")
            
        i = 0
        while True:
            p = f"{base}_{i}.png"
            if os.path.exists(p):
                paths.append(p)
                i += 1
            else:
                break
        return paths

    def download_image(self, sku, force=False):
        """Descarga todas las imágenes encontradas para un SKU con reintentos."""
        existing = self.get_local_paths(sku)
        if existing and not force:
            return True
        
        urls = self.find_image_urls(sku)
        if not urls:
            with self.download_lock:
                if sku not in self.failed_skus:
                    self.failed_skus.append(sku)
            return False
        
        success = False
        for idx, img_url in enumerate(urls[:5]): # Limitar a 5 imágenes por galería
            local_path = os.path.join(self.img_folder, f"{sku}.png" if idx == 0 else f"{sku}_{idx-1}.png")
            if os.path.exists(local_path) and not force:
                success = True
                continue
            
            # Reintentos (Feedback Matías: probar varias veces)
            for attempt in range(3):
                try:
                    response = requests.get(img_url, timeout=15)
                    if response.status_code == 200:
                        img_data = response.content
                        image = Image.open(io.BytesIO(img_data))
                        if image.mode in ("RGBA", "P"):
                            image = image.convert("RGB")
                        image.save(local_path, "PNG")
                        success = True
                        break # Éxito, salir del loop de reintentos
                    else:
                        time.sleep(1) # Esperar un segundo antes de reintentar
                except Exception as e:
                    print(f"Error reintento {attempt} para {sku}: {e}")
                    time.sleep(1)
        
        if success:
            with self.download_lock:
                if sku in self.failed_skus:
                    self.failed_skus.remove(sku)

        return success

    def start_background_download(self, sku_list):
        """Inicia la descarga masiva en segundo plano informando progreso."""
        # Limpiar lista para no repetir, filtrando vacíos y códigos QR de estructura
        unique_skus = list(set([s for s in sku_list if s and str(s).strip() and not str(s).strip().startswith(('@', '%', '#'))]))
        
        self.total_skus = len(unique_skus)
        self.downloaded_count = 0
        
        # Conteo inicial rápido de lo que ya tenemos
        for sku in unique_skus:
            if os.path.exists(self.get_local_path(sku)):
                self.downloaded_count += 1
                
        self._notify_progress()
        
        def worker():
            for sku in unique_skus:
                # Si no lo teniamos, intentamos descargar
                if not os.path.exists(self.get_local_path(sku)):
                    if self.download_image(sku):
                        self.downloaded_count += 1
                        self._notify_progress()
                    elif sku in self.failed_skus:
                        self.total_skus -= 1
                        self._notify_progress()
        
        threading.Thread(target=worker, daemon=True).start()

    def _notify_progress(self):
        if self.progress_callback:
            # Enviar conteo via (descargados, total)
            self.progress_callback(self.downloaded_count, self.total_skus)

    def get_tk_image(self, sku, size=(200, 200)):
        """Obtiene un objeto CTkImage para mostrar en la GUI, evitando warnings de DPI."""
        if not sku or sku.startswith(('@', '%', '#')):
            return None
        local_path = self.get_local_path(sku)
        try:
            if os.path.exists(local_path):
                img = Image.open(local_path)
            else:
                # Si ya habíamos determinado que no existe, no intentar de nuevo para evitar bloqueos
                if sku in self.failed_skus:
                    return None
                    
                # Si no existe, intentar descargar bajo demanda (síncrono aquí)
                if self.download_image(sku):
                    img = Image.open(local_path)
                else:
                    return None # Placeholder se manejará en la GUI
            
            # Optimización hiper-radical: pre-redimensionar la imagen de memoria para aliviar a CTkImage
            # Dado que el CDN de Cellular Center baja imágenes 1024x1024, esto ahorra de 1 a 2 segundos de renderizado
            if hasattr(Image, 'Resampling'):
                img.thumbnail(size, Image.Resampling.LANCZOS)
            else:
                img.thumbnail(size, Image.ANTIALIAS)
                
            # Devolver objeto CTkImage listo
            return ctk.CTkImage(light_image=img, dark_image=img, size=size)
        except Exception as e:
            print(f"Error generando CTkImage para {sku}: {e}")
            return None
