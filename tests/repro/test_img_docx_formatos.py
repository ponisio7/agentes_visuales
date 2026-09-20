"""Repro del lado imagen: la rama local de add_picture no valida nada."""
import os, tempfile
from PIL import Image
from docx import Document
from docx.shared import Inches

tmp = tempfile.mkdtemp()
svg = os.path.join(tmp, "dibujo.png")           # nombre .png, contenido SVG
with open(svg, "w") as f:
    f.write('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300">'
            '<rect width="400" height="300" fill="#f0f0f0"/></svg>')

jpg_con_ext_png = os.path.join(tmp, "foto.png")  # nombre .png, contenido JPEG
Image.new("RGB", (64, 64), "red").save(jpg_con_ext_png, format="JPEG")

for ruta in (svg, jpg_con_ext_png):
    print(f"--- {os.path.basename(ruta)} ---")
    with open(ruta, "rb") as fh:
        print("   magic:", fh.read(12))
    doc = Document()
    try:
        doc.add_picture(ruta, width=Inches(4.5))
        print("   add_picture: OK")
    except Exception as e:
        print(f"   add_picture: {type(e).__module__}.{type(e).__name__}: {e}")
    # Pillow, en cambio, sí sabe leer el JPEG mal nombrado:
    try:
        with Image.open(ruta) as im:
            print(f"   Pillow: OK format={im.format} size={im.size}")
    except Exception as e:
        print(f"   Pillow: {type(e).__name__}: {e}")
