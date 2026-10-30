"""Motore del piano editoriale di Riccardo.

  python motore.py piano       prepara il piano della settimana dopo e lo manda alla Scrivania
  python motore.py controllo   controllo del mattino sui post del giorno e su quelli già pubblicati
  python motore.py lavori      esegue le richieste di modifica fatte dalla Scrivania

Gira su GitHub Actions (.github/workflows/social.yml). Chiavi: ANTHROPIC_API_KEY e MOTORE_TOKEN
(il codice che la Scrivania usa per riconoscere il motore). Le immagini finiscono in social/piano/.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import anthropic
import requests

QUI = pathlib.Path(__file__).parent
RADICE = QUI.parent
CONF = json.loads((QUI / 'config.json').read_text(encoding='utf8'))
LINEA = (QUI / 'linea-editoriale.md').read_text(encoding='utf8')
ROMA = dt.timezone(dt.timedelta(hours=1))  # sostituito sotto con l'ora legale giusta
GIORNI = ['lun', 'mar', 'mer', 'gio', 'ven', 'sab', 'dom']
NOMI_GIORNI = ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica']
MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre']

try:
    from zoneinfo import ZoneInfo
    ROMA = ZoneInfo('Europe/Rome')
except Exception:
    pass


def log(*a):
    print(*a, flush=True)


def oggi():
    return dt.datetime.now(ROMA).date()


def leggibile(d):
    return f'{NOMI_GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}'


# ---------------------------------------------------------------- Scrivania

def scrivania(azione, **dati):
    """Chiama l'app web della Scrivania (Apps Script). Il POST risponde con un rinvio: lo seguiamo a mano."""
    corpo = json.dumps({'token': os.environ['MOTORE_TOKEN'], 'azione': azione, **dati}, ensure_ascii=False)
    for tentativo in range(3):
        try:
            r = requests.post(CONF['url_scrivania'], data=corpo.encode('utf8'),
                              headers={'Content-Type': 'application/json'}, allow_redirects=False, timeout=120)
            if r.status_code in (301, 302, 303):
                r = requests.get(r.headers['Location'], timeout=120)
            r.raise_for_status()
            risposta = r.json()
            if not risposta.get('ok'):
                raise RuntimeError(risposta.get('errore') or 'risposta senza ok')
            return risposta
        except Exception as e:
            log(f'Scrivania ({azione}) tentativo {tentativo + 1}: {e}')
            if tentativo == 2:
                raise
            time.sleep(10)


# ---------------------------------------------------------------- Claude

cliente = None
RICERCA_DI_RISERVA = 'web_search_20250305'
CONSUMI = []  # una riga per chiamata: serve al riepilogo dei costi a fine giro


def costo(modello, u):
    """Dollari spesi per una chiamata, con i prezzi di config.json (ricerche web comprese)."""
    p = CONF['prezzi'].get(modello) or CONF['prezzi'][CONF['modello']]
    return ((u['in'] * p['in'] + u['out'] * p['out'] + u['cache_scrittura'] * p['cache_scrittura'] +
             u['cache_lettura'] * p['cache_lettura']) / 1e6 + u['ricerche'] * CONF['prezzi']['ricerca'])


def claude(sistema, messaggi, ricerca=0, max_tokens=12000, modello=None):
    """Una conversazione con Claude; con ricerca>0 può cercare sul web. Restituisce il testo finale."""
    global cliente
    cliente = cliente or anthropic.Anthropic()
    modello = modello or CONF['modello']
    strumenti = []
    if ricerca:
        strumenti = [{'type': CONF['strumento_ricerca'], 'name': 'web_search', 'max_uses': ricerca,
                      'user_location': {'type': 'approximate', 'country': 'IT', 'timezone': 'Europe/Rome'}}]
    messaggi = list(messaggi)
    u = {'in': 0, 'out': 0, 'cache_scrittura': 0, 'cache_lettura': 0, 'ricerche': 0}
    for _ in range(6):  # pause_turn: la ricerca lunga si riprende da dove era
        try:
            # cache automatica: le riprese dopo pause_turn e i post scritti in serie rileggono lo stesso inizio a 1/20 del prezzo
            with cliente.messages.stream(model=modello, max_tokens=max_tokens, system=sistema, messages=messaggi,
                                         tools=strumenti or anthropic.NOT_GIVEN, cache_control={'type': 'ephemeral'}) as s:
                m = s.get_final_message()
        except anthropic.BadRequestError as e:
            if strumenti and strumenti[0]['type'] != RICERCA_DI_RISERVA and 'web_search' in str(e):
                log(f'  la ricerca {strumenti[0]["type"]} non è accettata, uso {RICERCA_DI_RISERVA}')
                strumenti[0]['type'] = CONF['strumento_ricerca'] = RICERCA_DI_RISERVA
                continue
            raise
        mu = m.usage
        u['in'] += mu.input_tokens
        u['out'] += mu.output_tokens
        u['cache_scrittura'] += mu.cache_creation_input_tokens or 0
        u['cache_lettura'] += mu.cache_read_input_tokens or 0
        u['ricerche'] += (mu.server_tool_use.web_search_requests or 0) if mu.server_tool_use else 0
        if m.stop_reason == 'pause_turn':
            messaggi.append({'role': 'assistant', 'content': m.content})
            continue
        testo = ''.join(b.text for b in m.content if b.type == 'text')
        testo = re.sub(r'</?cite[^>]*>', '', testo)  # i segni delle citazioni della ricerca web non devono finire nei testi
        CONSUMI.append(dict(u, modello=modello, dollari=costo(modello, u)))
        log(f'  Claude ({modello}): {u["in"]} in, {u["cache_lettura"]} dalla cache, {u["cache_scrittura"]} in cache, '
            f'{u["out"]} out, {u["ricerche"]} ricerche, ${CONSUMI[-1]["dollari"]:.3f}, stop={m.stop_reason}')
        return testo, messaggi + [{'role': 'assistant', 'content': m.content}]
    raise RuntimeError('Claude non ha finito la risposta')


def riepilogo_consumi(cosa):
    """A fine giro: quanto è costato. Va nel log e nel riepilogo dell'esecuzione su GitHub."""
    if not CONSUMI:
        return
    tot = {k: sum(c[k] for c in CONSUMI) for k in ('in', 'out', 'cache_scrittura', 'cache_lettura', 'ricerche', 'dollari')}
    riga = (f'Costo del giro «{cosa}»: ${tot["dollari"]:.2f} — {len(CONSUMI)} chiamate, {tot["in"]} token letti, '
            f'{tot["cache_lettura"]} dalla cache, {tot["cache_scrittura"]} messi in cache, {tot["out"]} scritti, {tot["ricerche"]} ricerche')
    log(riga)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as f:
            f.write(riga + '\n')


def estrai_json(testo):
    blocchi = re.findall(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', testo, re.S)
    grezzo = blocchi[-1] if blocchi else testo[testo.find('{'):testo.rfind('}') + 1]
    return json.loads(grezzo)


def claude_json(sistema, richiesta, controlla=None, ricerca=0, max_tokens=12000, tentativi=3, modello=None):
    """Chiede un JSON; se non è valido o non passa i controlli, rimanda gli errori a Claude e riprova."""
    messaggi = [{'role': 'user', 'content': richiesta}]
    for n in range(tentativi):
        testo, storia = claude(sistema, messaggi, ricerca=ricerca if n == 0 else 0, max_tokens=max_tokens, modello=modello)
        try:
            dati = estrai_json(testo)
            errori = controlla(dati) if controlla else []
        except Exception as e:
            dati, errori = None, [f'JSON non leggibile: {e}']
        if not errori:
            return dati
        log('  da correggere:', errori)
        messaggi = storia + [{'role': 'user', 'content': 'Ci sono questi problemi, correggili e rimanda il JSON completo:\n- ' + '\n- '.join(errori)}]
    raise RuntimeError('JSON non valido dopo vari tentativi: ' + '; '.join(errori))


# ---------------------------------------------------------------- testi comuni

SISTEMA = f"""Sei il redattore dei social di Riccardo Ingegneri, consulente digitale a Messina.
Scrivi in italiano, come lui: calmo, caldo, autorevole, senza paura e senza hype. Mai inventare fatti:
ogni dato viene da una fonte verificata, e la fonte si cita. Oggi è {leggibile(oggi())} {oggi().year}.

LINEA EDITORIALE (decisa con Riccardo):
{LINEA}

L'incontro online gratuito è il giovedì alle {CONF['webinar']['ora']}; iscrizione: {CONF['webinar']['link']}. Si risponde alle domande
delle persone e si racconta la digitalizzazione del lavoro di oggi, con e senza AI.
IMPORTANTE: che si ripeta ogni settimana (e che Riccardo lo tenga solo con abbastanza iscritti) è un fatto interno.
Per il pubblico è sempre «il prossimo giovedì alle 20» («questo giovedì alle 20» da lunedì a giovedì), MAI con una data precisa
(niente «22 ottobre», «giovedì 29»). Mai «il primo incontro», «la prima puntata», numeri di edizione, «di nuovo»,
«ogni giovedì», «come ogni settimana» o simili. L'incontro si nomina solo nel promemoria: il post della settimana
con il rimando porta al sito, senza nominare l'incontro; tutti gli altri post non lo nominano.
TONO, NON PAROLE: la calma si sente da come scrivi, non si dichiara. Mai chiudere un post, un post-it o una slide
con formule come «con calma», «senza ansia», «senza fretta», «niente panico», «niente allarmismi», «una cosa alla volta».
Un «niente panico» dentro un discorso, ogni tanto, va bene; come frase finale mai: si chiude con il fatto o con il consiglio concreto."""

RASSICURA = re.compile(r"con calma|senza ansia|senza fretta|senza paura|niente panico|nessun panico|niente allarmismi|"
                       r"nessun allarmismo|una cosa alla volta|tranquill[oaie]|respira", re.I)


def frasi_rassicuranti_finali(dati):
    """Le formule rassicuranti messe in chiusura: l'ultimo paragrafo vero di ogni testo, i post-it, l'ultima slide."""
    pezzi = []
    for testo in (dati.get('testi') or {}).values():
        par = [x.strip() for x in str(testo).split('\n\n') if x.strip()]
        par = [x for x in par if not x.startswith(('Fonte', '#')) and 'http' not in x and 'link in bio' not in x.lower()]
        if par:
            pezzi.append(par[-1])
    sl = dati.get('slide') or []
    for x in sl:
        pezzi.append(((x.get('postit') or {}).get('cosa')) or '')
    if sl:
        pezzi += [sl[-1].get('invito') or ''] + list(sl[-1].get('p') or [])
    return sorted({m.group(0).lower() for t in pezzi for m in RASSICURA.finditer(t)})


SCHEMA_SLIDE = """Ogni slide è un oggetto JSON con questi campi, tutti facoltativi tranne "testa":
  "sfondo": "carta" | "giallo" | "blu"
  "testa": etichetta in alto a sinistra (es. "La notizia della settimana")
  "h1": titolo grande in serif (max ~90 caratteri in copertina, ~50 nelle altre)
  "p": [paragrafi brevi, max 2-3 per slide, ~140 caratteri l'uno]
  "numero": {"valore": "38%", "etichetta": "frase che lo spiega"}
  "barre": [{"et": "etichetta breve", "val": "38%", "pct": 38, "forte": true}]   (pct 0-100; forte = barra blu)
  "elenco": [voci numerate, 3-4, brevi]
  "parola": {"voce": "automazione", "cosa": "definizione in una riga"}   (solo formato parola)
  "sotto": esempio concreto sotto la parola (solo formato parola)
  "calendario": {"giorno": "giovedì", "ora": "20:00", "righe": ["online e gratuito", "…"]}   (MAI numero o mese: niente date)
  "postit": {"chi": "Il mio parere", "cosa": "una o due frasi a mano, max ~110 caratteri"}
  "timbro": "Aggiornamento"
  "invito": riga a mano in fondo (es. "Ti spiego cosa cambia"). Frecce: → solo se c'è un'altra slide da scorrere,
            ↓ quando inviti a commentare, nessuna freccia per «link in bio» (le aggiusta comunque il motore)
  "fonte": riga piccola in basso: "Fonte: …" oppure "Per queste e altre informazioni: riccardoingegneri.it"
Il calendarietto è SOLO del formato promemoria; post-it della parola e "sotto" SOLO del formato parola.
Non scrivere la firma: c'è già in ogni slide.
Nel testo: ==parole evidenziate== (una sola volta per slide, il pezzo chiave) e **grassetto** (con misura)."""


def descrizione_formati():
    return '\n'.join(f'- {k} (copertina {v["copertina"]}, {v["slide"][0]}-{v["slide"][1]} slide, max {v["max_settimana"]} a settimana): {v["descrizione"]}'
                     for k, v in CONF['formati'].items() if k != 'aggiornamento' and not v.get('escluso'))


def prossimo_giovedi(d):
    return d + dt.timedelta(days=(3 - d.weekday()) % 7)


# ---------------------------------------------------------------- piano settimanale

def spazi_settimana(lunedi):
    """Gli spazi di pubblicazione della settimana: (id, data, fascia, orari)."""
    spazi = []
    for i in range(7):
        d = lunedi + dt.timedelta(days=i)
        n = CONF['ritmo'][GIORNI[i]]
        fasce = [] if n == 0 else (['domenica'] if i == 6 else (['mattina', 'sera'] if n >= 2 else ['sera']))
        for f in fasce:
            spazi.append({'id': f'{d.isoformat()}-{f}', 'data': d.isoformat(), 'giorno': leggibile(d),
                          'fascia': f, 'orari': CONF['orari'][f]})
    return spazi


def controlla_scaletta(scaletta, spazi, storia, lunedi):
    errori = []
    voci = scaletta.get('post', [])
    per_spazio = {v.get('spazio'): v for v in voci}
    for s in spazi:
        if s['id'] not in per_spazio:
            errori.append(f'manca lo spazio {s["id"]}')
    for v in voci:
        if v.get('spazio') not in {s['id'] for s in spazi}:
            errori.append(f'spazio sconosciuto {v.get("spazio")}')
        if v.get('formato') not in CONF['formati'] or v.get('formato') == 'aggiornamento' or CONF['formati'][v['formato']].get('escluso'):
            errori.append(f'{v.get("spazio")}: formato non valido {v.get("formato")}')
        if v.get('formato') in ('notizia', 'numero') and not v.get('fonti'):
            errori.append(f'{v.get("spazio")}: una {v["formato"]} deve avere la fonte')
    ordinati = [per_spazio[s['id']] for s in spazi if s['id'] in per_spazio]
    for a, b in zip(ordinati, ordinati[1:]):
        ca, cb = CONF['formati'].get(a.get('formato'), {}).get('copertina'), CONF['formati'].get(b.get('formato'), {}).get('copertina')
        if a.get('formato') == b.get('formato'):
            errori.append(f'{a["spazio"]} e {b["spazio"]}: stesso formato di fila ({a["formato"]})')
        elif ca and ca == cb:
            errori.append(f'{a["spazio"]} e {b["spazio"]}: due copertine «{ca}» di fila, la griglia sembra uguale')
    conta = {}
    for v in voci:
        conta[v.get('formato')] = conta.get(v.get('formato'), 0) + 1
    for f, n in conta.items():
        if f in CONF['formati'] and n > CONF['formati'][f]['max_settimana']:
            errori.append(f'troppi post «{f}»: {n} (massimo {CONF["formati"][f]["max_settimana"]})')
    if conta.get('promemoria', 0) != 1:
        errori.append(f'serve esattamente un promemoria nella settimana (ne hai messi {conta.get("promemoria", 0)})')
    rimandi = [v for v in voci if v.get('rimando') and v.get('formato') != 'promemoria']
    if len(rimandi) > 1:
        errori.append(f'al massimo un post con "rimando": true, ne hai messi {len(rimandi)}')
    temi = [v.get('tema') for v in voci]
    if len(set(temi)) != len(temi):
        errori.append('due post con lo stesso tema nella stessa settimana')
    limite = (lunedi - dt.timedelta(days=CONF['distanze']['stesso_tema_giorni'])).isoformat()
    recenti = {p['tema'] for p in storia if p.get('tema') and p.get('data', '') >= limite}
    for v in voci:
        if v.get('tema') in recenti:
            errori.append(f'{v["spazio"]}: il tema «{v["tema"]}» è già uscito da meno di {CONF["distanze"]["stesso_tema_giorni"]} giorni')
    return errori


def scrivi_post(voce, spazio, istruzione='', ricerca=0):
    """Scrive testi e slide di un post a partire dalla sua voce di scaletta."""
    f = CONF['formati'][voce['formato']]
    d = dt.date.fromisoformat(spazio['data'])
    canali = list(spazio['orari'].keys())
    quando_inc = 'questo giovedì alle 20' if d.weekday() <= 3 else 'il prossimo giovedì alle 20'
    tipo = 'promemoria' if voce['formato'] == 'promemoria' else ('rimando' if voce.get('rimando') else 'nessuno')
    chiusura = {
        'promemoria': f"CHIUSURA: questo è il promemoria. Parla del valore dell'incontro ({quando_inc}, online e gratuito): il problema che affronta e cosa si porta a casa chi partecipa. Instagram chiude con «Link in bio!», facebook e linkedin con il link https://riccardoingegneri.it/webinar.",
        # regola di Riccardo (29/10): il rimando porta al sito, mai con giorni o orari («stasera», «giovedì alle 20»).
        # Su facebook e linkedin il link può essere la pagina più adatta al post; su instagram sempre «link in bio» (la homepage).
        'rimando': "CHIUSURA: una sola riga in fondo che rimanda al sito, SENZA giorni né orari: Instagram «Per queste e altre informazioni: link in bio!» (nessun indirizzo nella didascalia); facebook e linkedin «Per queste e altre informazioni: » seguito dalla pagina più adatta al post: riccardoingegneri.it (homepage, se nessuna è più adatta), riccardoingegneri.it/servizi/, riccardoingegneri.it/portfolio/, riccardoingegneri.it/parliamone/ o riccardoingegneri.it/webinar/.",
        'nessuno': "CHIUSURA: NON nominare l'incontro del giovedì né il link del webinar. Chiudi con una domanda a chi legge o un invito a commentare o a scrivere in privato.",
    }[tipo]
    richiesta = f"""Scrivi il post per {spazio['giorno']} ({spazio['fascia']}), canali: {', '.join(canali)}.

VOCE DI SCALETTA:
{json.dumps(voce, ensure_ascii=False, indent=1)}

FORMATO «{voce['formato']}»: {f['descrizione']}
Slide: da {f['slide'][0]} a {f['slide'][1]}. La copertina (prima slide) ha "sfondo": "{f['copertina']}"; le altre "carta" salvo il formato parola (blu).
{SCHEMA_SLIDE}

DIDASCALIE:
- instagram: calda e parlata, emoji con misura, punto di vista personale, poi la fonte (se c'è) e 3-5 hashtag.
- facebook: come instagram, al massimo 2 hashtag.
- linkedin: stessa voce, un filo più professionale, paragrafi brevi, 2-3 hashtag.
{chiusura}
Niente date precise dell'incontro: «il prossimo giovedì alle 20» («questo giovedì alle 20» da lunedì a giovedì). Mai «ogni giovedì». Non scrivere mai «ne parliamo giovedì».
Mai inventare: usa solo i fatti della voce di scaletta.
{('ISTRUZIONE DI RICCARDO PER QUESTA VERSIONE: ' + istruzione) if istruzione else ''}

Se l'istruzione ti chiede un altro argomento, cercalo e verificalo sul web, e aggiungi al JSON anche
"tema", "titolo", "angolo", "fatti" e "fonti" nuovi (stessa forma della voce di scaletta).

Rispondi solo con un blocco ```json con:
{{"testi": {{{', '.join(f'"{c}": "…"' for c in canali)}}},
  "slide": [ … ],
  "alt": ["testo alternativo di ogni slide, una frase"],
  "affermazioni": ["ogni fatto verificabile che il post afferma, in una frase, per i controlli futuri"]}}"""

    def controlla(dati):
        e = []
        sl = dati.get('slide') or []
        if not (f['slide'][0] <= len(sl) <= f['slide'][1]):
            e.append(f'servono da {f["slide"][0]} a {f["slide"][1]} slide, ne hai messe {len(sl)}')
        if sl and sl[0].get('sfondo') != f['copertina']:
            e.append(f'la copertina deve avere sfondo {f["copertina"]}')
        for c in canali:
            t = (dati.get('testi') or {}).get(c, '')
            if len(t) < 80:
                e.append(f'manca la didascalia per {c}')
            if tipo == 'promemoria' and c == 'instagram' and 'link in bio' not in t.lower():
                e.append('la didascalia instagram del promemoria deve finire con «Link in bio!»')
            if tipo == 'promemoria' and c in ('facebook', 'linkedin') and 'riccardoingegneri.it/webinar' not in t:
                e.append(f'la didascalia {c} deve contenere il link riccardoingegneri.it/webinar')
            if tipo == 'rimando' and c == 'instagram' and 'link in bio' not in t.lower():
                e.append('la didascalia instagram deve chiudere con «Per queste e altre informazioni: link in bio!»')
            if tipo == 'rimando' and c in ('facebook', 'linkedin') and 'riccardoingegneri.it' not in t:
                e.append(f'la didascalia {c} deve chiudere con «Per queste e altre informazioni: » e una pagina di riccardoingegneri.it')
            if tipo == 'rimando' and c == 'instagram' and 'riccardoingegneri.it' in t:
                e.append('su instagram il link non è cliccabile: niente indirizzi, solo «link in bio!»')
            if tipo == 'rimando' and re.search(r'stasera|domani sera|gioved[iì] alle|alle (ore )?20', t, re.I):
                e.append(f"la didascalia {c} rimanda al sito: niente giorni né orari dell'incontro")
            if tipo == 'nessuno' and re.search(r'riccardoingegneri\.it/webinar|incontro online|webinar', t, re.I):
                e.append(f"la didascalia {c} non deve nominare l'incontro del giovedì: chiudi con una domanda")
            if len(t) > 2100:
                e.append(f'la didascalia {c} è troppo lunga')
        if voce['formato'] in ('notizia', 'numero') and not (dati.get('fonti') or voce.get('fonti')):
            e.append('una notizia o un numero devono avere le fonti')
        if len(dati.get('alt') or []) != len(sl):
            e.append('serve un testo alternativo per ogni slide')
        for i, x in enumerate(sl, 1):
            if x.get('calendario') and voce['formato'] != 'promemoria':
                e.append(f'slide {i}: il calendarietto si usa solo nel formato promemoria')
            if (x.get('parola') or x.get('sotto')) and voce['formato'] != 'parola':
                e.append(f'slide {i}: "parola" e "sotto" si usano solo nel formato parola')
            if x.get('parola') and x.get('h1'):
                e.append(f'slide {i}: con il post-it della parola non serve anche il titolo h1')
        tutto = json.dumps({'testi': dati.get('testi'), 'slide': dati.get('slide')}, ensure_ascii=False).lower()
        if re.search(r'\bgioved[iì]\s+\d{1,2}\b|\b\d{1,2}\s+(' + '|'.join(MESI) + r')\b.{0,40}\b(alle 20|incontro|webinar)', tutto):
            e.append('niente date precise per l\'incontro: «il prossimo giovedì alle 20»')
        if 'ogni giovedì' in tutto or 'ogni giovedi' in tutto:
            e.append('non scrivere «ogni giovedì»: per il pubblico è «il prossimo giovedì»')
        if any((x.get('calendario') or {}).get('numero') or (x.get('calendario') or {}).get('mese') for x in sl):
            e.append('il calendarietto non porta numero né mese: solo giorno e ora')
        if re.search(r'\b(prim[oa]|second[oa]|nuov[oa]|ennesim[oa])\s+(incontro|puntata|appuntamento|edizione|webinar)', tutto):
            e.append("l'incontro del giovedì è un appuntamento a data unica: niente «primo incontro», «nuova puntata» e simili")
        for frase in frasi_rassicuranti_finali(dati):
            e.append(f'la calma è nel tono, non si dichiara: togli «{frase}» dalla chiusura (testo finale, post-it o ultima slide)')
        if 'ne parliamo giovedì' in tutto:
            e.append('non usare «ne parliamo giovedì»')
        return e

    return claude_json(SISTEMA, richiesta, controlla, ricerca=ricerca, max_tokens=12000)


FRECCE = re.compile(r'\s*(?:→|➡️?|👉|↓|⬇️?|👇|->)\s*')
COMMENTI = re.compile(r'comment|scrivimel|scrivimi|dimmi|raccontami|rispondi', re.I)
BIO = re.compile(r'link in bio|nel profilo|in bio\b', re.I)


def sistema_frecce(post):
    """Una freccia dice dove andare: → solo per scorrere il carosello, ↓ verso i commenti, nessuna per il link in bio."""
    sl = post.get('slide') or []
    for i, s in enumerate(sl):
        inv = s.get('invito')
        if not inv:
            continue
        nudo = FRECCE.sub(' ', inv).strip()
        if BIO.search(nudo):
            s['invito'] = nudo
        elif COMMENTI.search(nudo):
            s['invito'] = nudo + ' ↓'
        elif i < len(sl) - 1:
            s['invito'] = nudo + ' →'
        else:
            s['invito'] = nudo
    for canale, testo in (post.get('testi') or {}).items():
        righe = []
        for r in str(testo).split('\n'):
            if FRECCE.search(r) and BIO.search(r):
                r = FRECCE.sub(' ', r).strip()
            elif re.search(r'→|➡️?|👉', r) and COMMENTI.search(r) and 'http' not in r:
                r = re.sub(r'\s*(?:→|➡️?|👉)\s*', ' 👇 ', r).strip()
            righe.append(r)
        post['testi'][canale] = '\n'.join(righe)
    return post


def disegna_e_salva(disegnatore, post, versione):
    """Disegna le slide di un post in social/piano/<id>/; se un testo non ci sta lo fa accorciare."""
    cartella = RADICE / 'social' / 'piano' / post['id']
    nome = f'{post["id"]}-{versione}'
    sistema_frecce(post)
    percorsi, avvisi = disegnatore.disegna(post['slide'], cartella, nome)
    if avvisi:
        log('  testo lungo, accorcio:', avvisi)
        richiesta = ('Queste slide hanno testi che non entrano nella grafica: ' + '; '.join(avvisi) +
                     '. Accorciali mantenendo il senso e rimanda solo il blocco ```json {"slide": [...]}.\n\n' +
                     json.dumps({'slide': post['slide']}, ensure_ascii=False))
        post['slide'] = claude_json(SISTEMA, richiesta, lambda d: [] if len(d.get('slide', [])) == len(post['slide']) else ['stesso numero di slide'], max_tokens=6000,
                                    modello=CONF['modello_leggero'])['slide']
        percorsi, avvisi = disegnatore.disegna(post['slide'], cartella, nome)
    v = int(time.time())
    post['immagini'] = [f'{CONF["url_immagini"]}/{post["id"]}/{p.name}?v={v}' for p in percorsi]
    return percorsi


def pubblica_immagini(messaggio):
    """Carica le immagini sul sito (GitHub Pages): servono a Buffer e alle mail di anteprima."""
    git = lambda *a: subprocess.run(['git', *a], cwd=RADICE, check=True)
    # il nome serve anche al pull --rebase quando nel frattempo sul sito è arrivato un altro commit (29/10: senza, le immagini
    # della versione corretta non sono mai andate online e il post del mattino non è partito)
    git('config', 'user.name', 'Motore social')
    git('config', 'user.email', 'motore@riccardoingegneri.it')
    git('add', 'social/piano')
    if subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=RADICE).returncode == 0:
        return
    git('commit', '-q', '-m', messaggio)
    for _ in range(3):
        if subprocess.run(['git', 'pull', '-q', '--rebase'], cwd=RADICE).returncode == 0 and \
           subprocess.run(['git', 'push', '-q'], cwd=RADICE).returncode == 0:
            break
        subprocess.run(['git', 'rebase', '--abort'], cwd=RADICE, capture_output=True)
        time.sleep(5)
    else:
        # meglio fermarsi: la Scrivania non riceve post con immagini che non esistono, e il giro dell'ora dopo riprova
        raise RuntimeError('Non sono riuscito a mettere online le immagini: niente viene mandato alla Scrivania.')
    aspetta_sito()


def aspetta_sito():
    """GitHub Pages impiega un minuto o due a mettere online le immagini nuove."""
    time.sleep(60)


def piano():
    from grafiche import Disegnatore
    o = oggi()
    lunedi = o + dt.timedelta(days=(7 - o.weekday()) % 7 or 7)
    if os.environ.get('SETTIMANA'):
        lunedi = dt.date.fromisoformat(os.environ['SETTIMANA'])
    log(f'Piano della settimana da {leggibile(lunedi)}')
    st = scrivania('storia')
    storia = sorted(CONF.get('storico_esterno', []) + st.get('post', []), key=lambda p: p.get('data', ''))
    spazi = spazi_settimana(lunedi)
    giovedi = [leggibile(lunedi + dt.timedelta(days=3))]
    elenco_storia = '\n'.join(f'- {p["data"]} · {p.get("formato")} · tema «{p.get("tema")}» · {p.get("titolo", "")}' for p in storia[-80:]) or '(ancora niente)'

    richiesta = f"""Prepara la scaletta dei post per la settimana da {leggibile(lunedi)} a {leggibile(lunedi + dt.timedelta(days=6))}.
L'incontro di questa settimana è {giovedi[0]} alle 20.

1. Cerca sul web le notizie degli ultimi 7-10 giorni utili al pubblico di Riccardo (titolari di piccole imprese,
   professionisti, artigiani in Italia): intelligenza artificiale e strumenti digitali per il lavoro, norme, dati,
   novità degli strumenti che usano (Google, WhatsApp, Microsoft, ChatGPT…). Verifica ogni fatto sulla fonte originale
   (ente, giornale serio, comunicato ufficiale) e annota data e link.
2. Riempi ESATTAMENTE questi spazi, uno per post:
{json.dumps([{k: s[k] for k in ('id', 'giorno', 'fascia')} for s in spazi], ensure_ascii=False, indent=1)}

FORMATI disponibili:
{descrizione_formati()}

REGOLE:
- Rotazione varia e non schematica: mai lo stesso formato due volte di fila, mai due copertine dello stesso colore di fila
  (carta / giallo / blu), al massimo i limiti settimanali indicati.
- Il «tema» è un'etichetta breve in minuscolo con trattini (es. «decreto-ai-italia», «scheda-google»): i temi degli
  ultimi {CONF['distanze']['stesso_tema_giorni']} giorni NON si ripetono; un tema si può riprendere (con un taglio nuovo) dopo {CONF['distanze']['ripresa_giorni']} giorni.
- Webinar con misura (regola di Riccardo): ESATTAMENTE un post «promemoria» a settimana (meglio la domenica sera o il
  giovedì mattina) e, al massimo, UN altro post con "rimando": true (in chiusura un rimando al sito, senza nominare l'incontro).
  Tutti gli altri post non nominano l'incontro. Il promemoria parla del valore (il problema concreto che affronta,
  cosa si porta a casa chi partecipa), non chiede attenzione.
- Niente doppioni con lo storico: un fatto, un'indagine o una norma già raccontati non tornano (neanche come «numero» o «parere»),
  anche se hanno un'etichetta di tema diversa. Non si ripete nemmeno il MESSAGGIO di fondo di un post recente con un'altra
  fonte: per esempio, dopo «più di un imprenditore su due non sa a cosa gli serve l'AI» non va un'altra indagine che dice
  «molte PMI non usano l'AI / non sanno da dove cominciare». Se c'è una novità vera su un tema già trattato, va bene solo con un taglio nuovo e dichiarato.
- I post di notizia o numero devono avere fonti verificate. Gli altri possono non averle, ma niente fatti inventati.
- Pensa alla settimana come a un racconto: varia tono e argomento (AI, presenza online, tempo risparmiato, privacy, strumenti gratuiti…).
- Aggiungi 2 post di «riserva» senza scadenza (formato a scelta tra consiglio, domanda, parere): sostituiscono un post bloccato.

STORICO DEI POST (più vecchi in alto):
{elenco_storia}

Rispondi con un blocco ```json:
{{"post": [{{"spazio": "id dello spazio", "formato": "…", "tema": "…", "titolo": "titolo interno breve", "rimando": false,
            "angolo": "cosa dice il post e perché interessa a chi lavora, 2-3 frasi",
            "fatti": ["fatti verificati con numeri e date, uno per riga"],
            "fonti": [{{"titolo": "…", "url": "…", "data": "aaaa-mm-gg"}}]}}],
  "riserva": [ stessi campi, senza "spazio" ]}}"""

    scaletta = claude_json(SISTEMA, richiesta, lambda d: controlla_scaletta(d, spazi, storia, lunedi),
                           ricerca=15, max_tokens=32000)
    log(f'Scaletta pronta: {len(scaletta["post"])} post + {len(scaletta.get("riserva", []))} di riserva')

    per_spazio = {s['id']: s for s in spazi}
    lavori = [(v, per_spazio[v['spazio']]) for v in scaletta['post']]
    for i, r in enumerate(scaletta.get('riserva', [])[:2]):
        lavori.append((dict(r, spazio=f'riserva-{lunedi.isoformat()}-{i + 1}'),
                       {'id': f'riserva-{lunedi.isoformat()}-{i + 1}', 'data': lunedi.isoformat(), 'giorno': 'un giorno qualsiasi',
                        'fascia': 'sera', 'orari': CONF['orari']['sera']}))

    def uno(coppia):
        voce, spazio = coppia
        log(f'Scrivo {spazio["id"]} ({voce["formato"]})')
        scritto = scrivi_post(voce, spazio)
        return {'id': spazio['id'], 'data': spazio['data'] if not spazio['id'].startswith('riserva') else '',
                'fascia': spazio['fascia'], 'orari': spazio['orari'], 'formato': voce['formato'], 'tema': voce['tema'],
                'titolo': voce['titolo'], 'angolo': voce.get('angolo', ''), 'fatti': voce.get('fatti', []),
                'fonti': voce.get('fonti', []), 'riserva': spazio['id'].startswith('riserva'), **scritto}

    with cf.ThreadPoolExecutor(4) as ex:
        posts = list(ex.map(uno, lavori))
    with Disegnatore() as d:
        for p in posts:
            disegna_e_salva(d, p, 'v1')
    pubblica_immagini(f'Piano social: settimana dal {lunedi.isoformat()}')
    # lanciato a mano: la mail del piano parte subito; dal calendario, domenica alle 17
    scrivania('piano', settimana=lunedi.isoformat(), post=posts, subito=os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch')
    log('Piano inviato alla Scrivania.')


# ---------------------------------------------------------------- controllo del mattino

def giorno_da_controllare():
    """Dalle 14 si controllano i post di domani (giro della sera): GitHub fa partire i giri anche con ore di ritardo,
    e un controllo che arriva dopo l'uscita dei post delle 9 non serve più a niente."""
    adesso = dt.datetime.now(ROMA)
    return adesso.date() + dt.timedelta(days=1 if adesso.hour >= 14 else 0)


def controllo():
    from grafiche import Disegnatore
    o = giorno_da_controllare()
    dati = scrivania('oggi', data=o.isoformat())
    di_oggi, recenti = dati.get('oggi', []), dati.get('recenti', [])
    # i post già pubblicati si rileggono una volta a settimana, non a ogni controllo: un aggiornamento pubblico
    # è un fatto raro (Riccardo, 26/10), e rileggerli ogni giorno costava più di metà della spesa del motore
    if GIORNI[o.weekday()] != CONF['controllo_recenti']:
        recenti = []
    log(f'Controllo di {leggibile(o)}: {len(di_oggi)} post in uscita, {len(recenti)} già pubblicati da tenere d\'occhio')
    if not di_oggi and not recenti:
        scrivania('controllo', data=o.isoformat(), esiti=[], aggiornamenti=[], notizie=[])
        log('Niente da controllare: nessuna chiamata a Claude.')
        return

    def riassunto(p):
        return {k: p.get(k) for k in ('id', 'data', 'orari', 'formato', 'titolo', 'testi', 'slide', 'fonti', 'affermazioni')}

    richiesta = f"""Controllo dei post in uscita {leggibile(o)} {o.year} (oggi è {leggibile(oggi())}).

A. POST IN USCITA QUEL GIORNO (già approvati da Riccardo):
{json.dumps([riassunto(p) for p in di_oggi], ensure_ascii=False, indent=1) if di_oggi else '(nessuno)'}

B. AFFERMAZIONI DEI POST GIÀ PUBBLICATI NELLE ULTIME SETTIMANE:
{json.dumps([{k: p.get(k) for k in ('id', 'data', 'titolo', 'affermazioni', 'fonti')} for p in recenti], ensure_ascii=False, indent=1) if recenti else '(nessuno)'}

Per ogni post di oggi verifica, cercando sul web:
1. che ogni fatto sia ancora vero oggi e che le fonti dicano davvero quello che il post afferma;
2. che date e riferimenti siano giusti rispetto al giorno di uscita (es. il giovedì giusto per l'incontro);
3. se nelle ultime 48 ore è uscita una notizia importante che cambia il contesto o rende il post fuori luogo.
Poi guarda le affermazioni già pubblicate (B): se un fatto è CAMBIATO DOPO la pubblicazione, con una fonte datata dopo il post,
proponi un aggiornamento pubblico. Al massimo uno. «Non trovo più la fonte» o «forse il dato era un altro» NON sono motivi
per un aggiornamento pubblico: scrivili nelle notizie per Riccardo. Correggersi in pubblico è raro, e mai per smentire sé stessi.
Sii prudente: segnala solo con una fonte concreta. Se è tutto a posto, dillo.
Non sono da verificare né da cambiare i dati dell'incontro di Riccardo: il titolo «{CONF['webinar']['titolo']}», il giorno, l'ora,
il link e il fatto che sia gratuito sono ufficiali. Il campo «titolo» è un'etichetta interna che non si pubblica: non è mai
un motivo per fermare un post. «modifica» solo per un errore vero (fatto sbagliato, data sbagliata), non per gusto.
Le notizie: al massimo 5, una frase semplice ciascuna (niente codici, niente segni di citazione), scritte per Riccardo.

Rispondi con un blocco ```json:
{{"post": [{{"id": "…", "esito": "ok" | "modifica" | "blocca", "motivo": "spiegazione semplice per Riccardo",
            "correzione": "cosa cambiare (solo se modifica)", "fonti": [{{"titolo": "…", "url": "…"}}]}}],
  "aggiornamenti": [{{"riferito_a": "id del post pubblicato", "motivo": "cosa è cambiato", "fatti": ["…"], "fonti": [{{"titolo": "…", "url": "…", "data": "…"}}]}}],
  "notizie": ["eventuali notizie importanti di oggi, una riga ciascuna, anche se non toccano i post"]}}"""

    def controlla(d):
        ids = {p['id'] for p in di_oggi}
        visti = {x.get('id') for x in d.get('post', [])}
        return [f'manca l\'esito per {i}' for i in ids - visti]

    esito = claude_json(SISTEMA, richiesta, controlla, ricerca=8 if (di_oggi or recenti) else 3, max_tokens=8000)

    proposte = []
    with Disegnatore() as dis:
        for x in esito.get('post', []):
            if x.get('esito') != 'modifica':
                continue
            p = next(p for p in di_oggi if p['id'] == x['id'])
            log(f'Correggo {p["id"]}: {x.get("correzione")}')
            spazio = {'id': p['id'], 'data': p['data'], 'giorno': leggibile(dt.date.fromisoformat(p['data'])),
                      'fascia': p.get('fascia', 'sera'), 'orari': p['orari']}
            voce = {k: p.get(k) for k in ('formato', 'tema', 'titolo', 'angolo', 'fatti', 'fonti')}
            voce['fonti'] = (voce.get('fonti') or []) + x.get('fonti', [])
            nuovo = dict(p, **scrivi_post(voce, spazio, 'Correggi così: ' + (x.get('correzione') or x.get('motivo'))))
            nuovo['fonti'] = voce['fonti']
            disegna_e_salva(dis, nuovo, f'c{o.strftime("%m%d")}')
            x['proposta'] = nuovo
        for i, a in enumerate(esito.get('aggiornamenti', [])[:1]):
            orig = next((p for p in recenti if p['id'] == a.get('riferito_a')), None)
            if not orig:
                continue
            d = o + dt.timedelta(days=1)
            fascia = 'domenica' if d.weekday() == 6 else 'sera'
            if CONF['ritmo'][GIORNI[d.weekday()]] == 0:
                d += dt.timedelta(days=1)
                fascia = 'sera'
            pid = f'agg-{o.isoformat()}-{i + 1}'
            spazio = {'id': pid, 'data': d.isoformat(), 'giorno': leggibile(d), 'fascia': fascia, 'orari': CONF['orari'][fascia]}
            voce = {'formato': 'aggiornamento', 'tema': (orig.get('tema') or 'aggiornamento') + '-agg',
                    'titolo': 'Aggiornamento: ' + (orig.get('titolo') or ''), 'angolo': a.get('motivo'),
                    'fatti': a.get('fatti', []), 'fonti': a.get('fonti', []), 'post_originale': orig.get('titolo')}
            nuovo = {'id': pid, 'data': d.isoformat(), 'fascia': fascia, 'orari': CONF['orari'][fascia], **voce, 'riferito_a': orig['id'],
                     **scrivi_post(voce, spazio)}
            disegna_e_salva(dis, nuovo, 'v1')
            proposte.append(nuovo)
    pubblica_immagini(f'Controllo social del {o.isoformat()}')
    scrivania('controllo', data=o.isoformat(), esiti=esito.get('post', []), aggiornamenti=proposte,
              notizie=esito.get('notizie', []))
    log('Esito inviato alla Scrivania.')


# ---------------------------------------------------------------- richieste dalla Scrivania

def controllo_mancante(risposta):
    """Il controllo della sera a volte GitHub lo fa partire ore dopo, o mai: il giro orario lo recupera
    (dalle 19 per i post di domani, dalle 6 per quelli di oggi se la sera è saltata)."""
    adesso = dt.datetime.now(ROMA)
    if 'ultimoControllo' not in risposta:  # Scrivania non ancora aggiornata: meglio non rifare il controllo ogni ora
        return False
    serve = adesso.date() + dt.timedelta(days=1) if adesso.hour >= 19 else (adesso.date() if adesso.hour >= 6 else None)
    return bool(serve) and (risposta.get('ultimoControllo') or '') < serve.isoformat()


def lavori():
    risposta = scrivania('lavori')
    if controllo_mancante(risposta):
        log('Il controllo di oggi non è ancora stato fatto: lo faccio adesso.')
        controllo()
        risposta = scrivania('lavori')
    elenco = risposta.get('lavori', [])
    if not elenco:
        log('Nessuna richiesta in attesa.')
        return
    from grafiche import Disegnatore
    with Disegnatore() as dis:
        for lav in elenco:
            p = lav['post']
            log(f'Rifaccio {p["id"]}: {lav["istruzione"]}')
            try:
                d = dt.date.fromisoformat(p['data']) if p.get('data') else oggi()
                spazio = {'id': p['id'], 'data': d.isoformat(), 'giorno': leggibile(d), 'fascia': p.get('fascia', 'sera'), 'orari': p['orari']}
                voce = {k: p.get(k) for k in ('formato', 'tema', 'titolo', 'angolo', 'fatti', 'fonti')}
                voce['versione_precedente'] = {'testi': p.get('testi'), 'slide': p.get('slide')}
                nuovo = dict(p, **scrivi_post(voce, spazio, lav['istruzione'], ricerca=6))
                disegna_e_salva(dis, nuovo, 'r' + str(int(time.time()))[-6:])
                lav['risultato'] = nuovo
            except Exception as e:
                lav['errore'] = str(e)[:500]
    pubblica_immagini('Social: modifiche richieste dalla Scrivania')
    for lav in elenco:
        scrivania('risultato', id=lav['post']['id'], risultato=lav.get('risultato'), errore=lav.get('errore'))
    log(f'{len(elenco)} richieste eseguite.')


def lavori_in_attesa():
    """Esce con 0 se ci sono richieste da eseguire (così l'Action installa il browser solo quando serve)."""
    risposta = scrivania('lavori')
    n = len(risposta.get('lavori', []))
    log(f'Richieste in attesa: {n}')
    sys.exit(0 if n or controllo_mancante(risposta) else 1)


if __name__ == '__main__':
    cosa = sys.argv[1]
    if not os.environ.get('MOTORE_TOKEN') or not os.environ.get('ANTHROPIC_API_KEY'):
        # chiavi non ancora inserite su GitHub: i giri automatici non fanno nulla (e non mandano mail d'errore)
        log('Mancano le chiavi MOTORE_TOKEN e/o ANTHROPIC_API_KEY nei secrets di GitHub.')
        if cosa == 'piano':
            sys.exit(2)
        sys.exit(1 if cosa == 'lavori-in-attesa' else 0)
    try:
        {'piano': piano, 'controllo': controllo, 'lavori': lavori, 'lavori-in-attesa': lavori_in_attesa}[sys.argv[1]]()
    finally:
        riepilogo_consumi(cosa)
