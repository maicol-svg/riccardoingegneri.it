"""Scrive nella pagina del webinar la data del prossimo incontro, così è giusta anche per chi legge la pagina
senza JavaScript (motori di ricerca, anteprime dei link, strumenti di lettura). Stessa regola dello script
della pagina: il giovedì vale fino alle 20:45 italiane, poi si passa al giovedì seguente.

Uso: python strumenti/data_webinar.py [aaaa-mm-ggThh:mm]   (senza argomento: adesso, ora italiana)
"""
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PAGINA = Path(__file__).resolve().parent.parent / 'webinar' / 'index.html'
PRIMO = date(2026, 10, 22)
FINE = (20, 45)
MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre',
        'ottobre', 'novembre', 'dicembre']


def prossimo_incontro(adesso: datetime) -> date:
    giorno = adesso.date()
    d = giorno + timedelta(days=(3 - giorno.weekday()) % 7)  # weekday(): giovedì = 3
    if d == giorno and (adesso.hour, adesso.minute) >= FINE:
        d += timedelta(days=7)
    return max(d, PRIMO)


def aggiorna(html: str, d: date) -> str:
    etichetta = f'giovedì {d.day} {MESI[d.month - 1]}'
    html = re.sub(r'(id="data" aria-label=")[^"]*(")', rf'\g<1>{etichetta}\g<2>', html)
    html = re.sub(r'(<span id="giorno">)[^<]*(</span>)', rf'\g<1>{d.day}\g<2>', html)
    html = re.sub(r'(<span id="mese">)[^<]*(</span>)', rf'\g<1>{MESI[d.month - 1]}\g<2>', html)
    return html


if __name__ == '__main__':
    roma = ZoneInfo('Europe/Rome')
    adesso = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=roma) if len(sys.argv) > 1 else datetime.now(roma)
    prima = PAGINA.read_text(encoding='utf-8')
    dopo = aggiorna(prima, prossimo_incontro(adesso))
    if dopo != prima:
        PAGINA.write_text(dopo, encoding='utf-8', newline='\n')
    print(prossimo_incontro(adesso), 'cambiata' if dopo != prima else 'già giusta')
