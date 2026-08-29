import json
import os
import sys
import platform
import base64

from src.logger import logger

# Guardar config.json en el directorio AppData/Local del usuario para aislamiento y persistencia
if platform.system() == "Windows":
    config_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "StockCellularCenter")
else:
    config_dir = os.path.join(os.path.expanduser("~"), ".stock_cellular_center")

os.makedirs(config_dir, exist_ok=True)
CONFIG_FILE = os.path.join(config_dir, "config.json")

# Carpeta de imágenes por defecto en AppData o relativa
DEFAULT_IMAGE_FOLDER = os.path.join(config_dir, "img")
os.makedirs(DEFAULT_IMAGE_FOLDER, exist_ok=True)

DEFAULT_CONFIG = {
    "speed_multiplier": 1.0,
    "paste_mode": "typing",
    "auto_save_seconds": 15,
    "last_family": "AM-AO",
    "theme": "dark",
    "image_folder": DEFAULT_IMAGE_FOLDER,
    "excluded_skus": [],
    "list_order": "bottom",               # "top" (Último arriba) o "bottom" (Último abajo)
    "location_validation_delay": 5,        # Segundos de espera para validación diferida
    # Colores base de tabla
    "table_bg": "#242424",
    "table_fg": "#ffffff",
    "table_selected_bg": "#1f538d",
    # Colores por estado (Fondo / Fuente)
    "row_finished_bg": "#90ee90",          # lightgreen
    "row_finished_fg": "#000000",
    "row_pending_bg": "#f0e68c",           # khaki
    "row_pending_fg": "#000000",
    "row_excess_bg": "#fa8072",            # salmon
    "row_excess_fg": "#000000",
    "row_unknown_bg": "#ffa500",           # orange
    "row_unknown_fg": "#000000",
    "viewer_size": [400, 400],
    "proximity_window": 50,
    "proximity_threshold": 30
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                encoded_data = f.read().strip()
                decoded_json = base64.b64decode(encoded_data.encode('utf-8')).decode('utf-8')
                loaded = json.loads(decoded_json)
                # Migración: si viene un image_folder con formato antiguo de V7, actualizar a default
                if "image_folder" in loaded and ("Stock V7" in loaded["image_folder"] or not os.path.isabs(loaded["image_folder"])):
                    loaded["image_folder"] = DEFAULT_IMAGE_FOLDER
                return {**DEFAULT_CONFIG, **loaded}
        except Exception as e:
            logger.error("Error al leer config.json: %s", e)
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)

def save_config(config):
    data_to_save = {k: v for k, v in config.items() if k != "parent_app"}
    try:
        json_str = json.dumps(data_to_save, indent=4)
        encoded_data = base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write(encoded_data)
    except Exception as e:
        logger.error("Error al guardar config: %s", e)
        print(f"Error al guardar config: {e}")

current_config = load_config()

