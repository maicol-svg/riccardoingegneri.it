"""Disegna le slide di un post con modello.html e le salva come JPG 1080x1350."""
import pathlib
from playwright.sync_api import sync_playwright

QUI = pathlib.Path(__file__).parent
MODELLO = (QUI / 'modello.html').as_uri()


class Disegnatore:
    def __enter__(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self.pagina = self._browser.new_page(viewport={'width': 1080, 'height': 1350})
        self.pagina.goto(MODELLO)
        self.pagina.evaluate('document.fonts.ready')
        return self

    def __exit__(self, *a):
        self._browser.close()
        self._pw.stop()

    def disegna(self, slide, cartella, nome):
        """Salva una JPG per slide; restituisce (percorsi, avvisi) — avviso se un testo non ci sta."""
        cartella = pathlib.Path(cartella)
        cartella.mkdir(parents=True, exist_ok=True)
        percorsi, avvisi = [], []
        for i, s in enumerate(slide, 1):
            ok = self.pagina.evaluate('([s, n, t]) => disegna(s, n, t)', [s, i, len(slide)])
            self.pagina.evaluate('document.fonts.ready')
            if not ok:
                avvisi.append(f'slide {i}: testo troppo lungo, va accorciato')
            p = cartella / f'{nome}-{i}.jpg'
            self.pagina.locator('#tela').screenshot(path=str(p), type='jpeg', quality=92)
            percorsi.append(p)
        return percorsi, avvisi
