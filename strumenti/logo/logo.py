"""Rende la sigla scelta (C3 in logo.html) in PNG e ICO: img/logo/. Serve un server locale sulla radice del sito:
python -m http.server 8765  →  python strumenti/logo/logo.py"""
from pathlib import Path
from io import BytesIO
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

USCITA = Path(__file__).resolve().parent.parent.parent / 'img' / 'logo'
USCITA.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={'width': 1400, 'height': 1200}, device_scale_factor=4)
    pg.goto('http://127.0.0.1:8765/strumenti/logo/logo.html')
    pg.wait_for_timeout(1800)
    grande = Image.open(BytesIO(pg.locator('#sigla').screenshot(omit_background=True))).convert('RGBA')
    b.close()
grande = grande.resize((1024, 1024), Image.LANCZOS)
# angoli trasparenti: si tiene solo il cerchio
maschera = Image.new('L', (4096, 4096), 0)
ImageDraw.Draw(maschera).ellipse((0, 0, 4095, 4095), fill=255)
grande.putalpha(maschera.resize((1024, 1024), Image.LANCZOS))
grande.save(USCITA / 'sigla-1024.png')
for lato, nome in ((512, 'sigla-512.png'), (192, 'icona-192.png'), (32, 'favicon-32.png')):
    grande.resize((lato, lato), Image.LANCZOS).save(USCITA / nome)
# l'icona del telefono Apple non vuole trasparenze: cerchio su fondo carta
fondo = Image.new('RGBA', (1024, 1024), '#f8f9fb')
fondo.alpha_composite(grande)
fondo.convert('RGB').resize((180, 180), Image.LANCZOS).save(USCITA / 'apple-touch-icon.png')
grande.save(USCITA / 'favicon.ico', sizes=[(16, 16), (32, 32), (48, 48)])
print(sorted(x.name for x in USCITA.iterdir()))
