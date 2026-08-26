"""Convierte un PNG a ICO sin depender de rutas locales personales."""

import argparse
from PIL import Image


def convert_to_ico(png_path, ico_path):
    image = Image.open(png_path)
    icon_sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    image.save(ico_path, format="ICO", sizes=icon_sizes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convertir una imagen PNG a un icono ICO")
    parser.add_argument("png_path", help="Ruta del PNG de origen")
    parser.add_argument("ico_path", help="Ruta del ICO de destino")
    arguments = parser.parse_args()
    convert_to_ico(arguments.png_path, arguments.ico_path)
    print(f"Icono creado en: {arguments.ico_path}")
