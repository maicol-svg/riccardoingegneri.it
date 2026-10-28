"""Rende le copertine delle mail dei servizi (copertine.html) in PNG: img/mail/<servizio>.png, 1200x620."""
from pathlib import Path
from playwright.sync_api import sync_playwright

QUI = Path(__file__).resolve().parent
USCITA = QUI.parent.parent / 'img' / 'mail'

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={'width': 1300, 'height': 2200})
    pg.goto(QUI.joinpath('copertine.html').as_uri())
    pg.wait_for_timeout(1500)  # i caratteri di Google Fonts
    USCITA.mkdir(parents=True, exist_ok=True)
    for nome in ('sessione', 'automazione', 'formazione'):
        pg.locator('#' + nome).screenshot(path=str(USCITA / f'{nome}.png'))
        print(USCITA / f'{nome}.png')
    b.close()
