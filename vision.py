import sys
import os
import re
import time
import json
import base64
import datetime
import urllib.request

# UTF-8 erzwingen
sys.stdout.reconfigure(encoding='utf-8')

# --- PFADE ANPASSEN ---
HOME         = os.path.expanduser('~')
BILD_PFAD    = f'{HOME}/snap.jpg'
LOG_PFAD     = f'{HOME}/analyse_log.txt'
ARCHIV_PFAD  = f'{HOME}/archiv'

PROMPT_PFAD = f'{HOME}/custom_prompt.txt'
STATS_PFAD  = f'{HOME}/last_stats.json'
VLM_MODELL  = sys.argv[1] if len(sys.argv) > 1 else 'ministral-3:3b'
TEXT_MODELL = 'ministral-3:3b'
KEEP_ALIVE  = '24h'

def lies_custom_prompt():
    """Liest die vom Nutzer ueber das Prompt-Menu gesetzte Zusatzanweisung,
    falls vorhanden. Leerer String, wenn keine gesetzt ist."""
    try:
        with open(PROMPT_PFAD, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''

CUSTOM_PROMPT = lies_custom_prompt()

# Wenn Auge und Gehirn dasselbe Modell sind, schaut das Gehirn direkt aufs Bild
DIREKT = (VLM_MODELL == TEXT_MODELL)


def log(text):
    """Schreibt in die Log-Datei - auch Fehlerfaelle, damit man debuggen kann."""
    try:
        with open(LOG_PFAD, 'a', encoding='utf-8') as f:
            f.write(text + '\n')
    except Exception:
        pass


def _feld(obj, name, default=''):
    """Holt ein Feld robust aus dict ODER Antwort-Objekt der ollama-Bibliothek."""
    try:
        if hasattr(obj, 'get'):
            wert = obj.get(name, default)
        else:
            wert = getattr(obj, name, default)
        return wert if wert is not None else default
    except Exception:
        return default


def ohne_denken(text):
    """Entfernt <think>-Bloecke, die Reasoning-Modelle manchmal mitschicken."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    if '</think>' in text:
        text = text.split('</think>')[-1]
    return text.strip()


def bereinige(text):
    """Holt den EIGENTLICHEN Satz aus der Modellausgabe. Viele VLMs stellen
    eine Einleitung voran ('Here's a joke:', 'Sure!', 'Joke:'). Frueher nahm
    diese Funktion nur die erste Zeile - dann blieb manchmal nur die
    Ankuendigung stehen und die Pointe (naechste Zeile) ging verloren. Jetzt
    werden Einleitungen gezielt uebersprungen bzw. der Teil nach dem Doppelpunkt
    genommen. Bricht ein Modell mitten im Wort ab, wird bis zum letzten
    vollstaendigen Satz gekuerzt, damit die Ausgabe nie zerhackt aussieht."""
    ANKUENDIGUNG = (
        "here's", "here is", "here are", "sure", "of course", "certainly",
        "absolutely", "okay", "ok", "based on", "looking at", "the joke",
        "a joke", "joke", "one-liner", "oneliner", "caption", "answer",
        "response", "output", "result",
    )
    LABELS = ("joke", "one-liner", "oneliner", "sentence", "caption", "answer",
              "response", "observation", "output", "result", "here", "sure",
              "based on", "of course")

    def ist_ankuendigung(zeile):
        z = zeile.lower().strip().rstrip("!.").strip()
        return any(z == a or z.startswith(a + " ") or z.startswith(a + ",")
                   for a in ANKUENDIGUNG)

    kandidat = ""
    for roh in text.strip().splitlines():
        zeile = roh.strip().strip("*").strip()
        if not zeile:
            continue
        if ":" in zeile:
            links, rechts = zeile.split(":", 1)
            rechts = rechts.strip()
            links_l = links.lower()
            ist_label = ist_ankuendigung(links) or any(w in links_l for w in LABELS)
            if ist_label:
                if rechts:            # "Here's a joke: <Pointe>" -> Pointe nehmen
                    kandidat = rechts
                    break
                continue              # "Joke:" am Zeilenende -> naechste Zeile
            kandidat = zeile          # Doppelpunkt gehoert zum Satz selbst
            break
        if ist_ankuendigung(zeile):   # reine Ankuendigungszeile ('Sure!') ueberspringen
            continue
        kandidat = zeile
        break

    text = kandidat if kandidat else (text.strip().splitlines() or [""])[0].strip()
    text = text.strip().strip('"').strip("'").strip('*').strip()
    # Wenn nicht mit Satzzeichen abgeschlossen (also mitten im Wort gekappt),
    # bis zum letzten vollstaendigen Satz zurueckschneiden.
    if len(text) > 40 and text[-1:] not in ('.', '!', '?', '"', "'", '…'):
        treffer = list(re.finditer(r'[.!?]', text))
        if treffer:
            text = text[:treffer[-1].end()]
    return text.strip()


OLLAMA_URL = 'http://127.0.0.1:11434/api/chat'

# Kennzahlen des letzten Modellaufrufs - fuer die Technik-Anzeige unter dem Satz.
LETZTE_METRIK = {}


def frage_modell(modell, prompt, bild=None, system=None, optionen=None):
    """Fragt ein Modell direkt per HTTP (kein ollama-Python-Paket mehr,
    das war eine bestaetigte Quelle unerklaerlicher Verzoegerungen).
    Gibt (antwort, denk_text) zurueck."""
    nachrichten = []
    if system:
        nachrichten.append({'role': 'system', 'content': system})
    user_msg = {'role': 'user', 'content': prompt}
    if bild:
        with open(bild, 'rb') as f:
            user_msg['images'] = [base64.b64encode(f.read()).decode('ascii')]
    nachrichten.append(user_msg)

    finale_optionen = dict(optionen or {})
    finale_optionen.setdefault('num_thread', 3)  # 1 Kern frei fuer Display/GUI/Kamera

    payload = {
        'model': modell,
        'messages': nachrichten,
        'options': finale_optionen,
        'keep_alive': KEEP_ALIVE,
        'think': False,
        'stream': False,
    }

    t_start = time.time()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        res = json.loads(resp.read())
    t_ende = time.time()

    p_cnt = res.get('prompt_eval_count') or 0
    e_cnt = res.get('eval_count') or 0
    p_dur = (res.get('prompt_eval_duration', 0) or 0) / 1e9
    e_dur = (res.get('eval_duration', 0) or 0) / 1e9
    l_dur = (res.get('load_duration', 0) or 0) / 1e9
    global LETZTE_METRIK
    LETZTE_METRIK = {
        'modell': modell,
        'wandzeit': round(t_ende - t_start, 1),
        'input_tokens': p_cnt,       # Bild + Prompt zusammen
        'output_tokens': e_cnt,
        'vision_s': round(p_dur, 1), # Zeit zum Verarbeiten von Bild+Prompt
        'gen_s': round(e_dur, 1),    # reine Textgenerierung
        'load_s': round(l_dur, 1),   # einmaliges Modell-Laden in den RAM
        # Nur sinnvoll ab messbarer Dauer; sonst (z.B. leere Antwort mit 1 Token
        # und ~0s) waere der Wert absurd hoch -> auf 0 setzen.
        'tok_pro_s': round(e_cnt / e_dur, 1) if (e_dur >= 0.1 and e_cnt >= 1) else 0.0,
    }

    log(f"DEBUG frage_modell({modell}): wandzeit={t_ende - t_start:.1f}s "
        f"prompt_eval_count={p_cnt} eval_count={e_cnt} "
        f"prompt_eval_duration={p_dur:.1f}s eval_duration={e_dur:.1f}s "
        f"load_duration={l_dur:.1f}s "
        f"total_duration={(res.get('total_duration', 0) or 0) / 1e9:.1f}s")

    msg     = res.get('message', {}) or {}
    inhalt  = msg.get('content', '') or ''
    denken  = msg.get('thinking', '') or ''
    log(f"DEBUG denken_laenge={len(denken)} inhalt_laenge={len(inhalt)}")
    return ohne_denken(inhalt.strip()), denken.strip()


# ----------------------------------------------------------------------
# ARCHIV: Foto + Satz-Karte im Display-Design als JPG speichern
# ----------------------------------------------------------------------

def _finde_schrift():
    """Sucht JetBrains Mono, sonst DejaVu Sans Mono (immer vorhanden)."""
    import glob
    kandidaten = (
        glob.glob('/usr/share/fonts/**/JetBrainsMono-Regular.ttf', recursive=True) +
        glob.glob('/usr/share/fonts/**/JetBrainsMonoNL-Regular.ttf', recursive=True) +
        glob.glob('/usr/share/fonts/**/DejaVuSansMono.ttf', recursive=True)
    )
    return kandidaten[0] if kandidaten else None


def _umbrechen(draw, text, schrift, max_breite):
    """Bricht den Text so um, dass jede Zeile in max_breite passt."""
    zeilen, aktuelle = [], ""
    for wort in text.split():
        test = (aktuelle + " " + wort).strip()
        if draw.textlength(test, font=schrift) <= max_breite:
            aktuelle = test
        else:
            if aktuelle:
                zeilen.append(aktuelle)
            aktuelle = wort
    if aktuelle:
        zeilen.append(aktuelle)
    return zeilen


def erstelle_satzbild(witz, modell_name, pfad):
    """Rendert den One-Liner als 800x480-Karte im Display-Design."""
    from PIL import Image, ImageDraw, ImageFont

    BG, FG, MUTED = (15, 17, 19), (236, 234, 228), (124, 128, 133)
    B, H = 800, 480
    bild = Image.new('RGB', (B, H), BG)
    draw = ImageDraw.Draw(bild)

    schrift_pfad = _finde_schrift()

    # Schriftgroesse dynamisch wie auf dem Display
    n = len(witz)
    groesse = 34 if n <= 70 else 28 if n <= 140 else 22 if n <= 260 else 17
    schrift = ImageFont.truetype(schrift_pfad, groesse)
    fuss    = ImageFont.truetype(schrift_pfad, 13)

    zeilen = _umbrechen(draw, witz, schrift, 680)
    zeilenhoehe = int(groesse * 1.4)
    gesamt = len(zeilen) * zeilenhoehe
    y = (H - gesamt) // 2 - 15
    for zeile in zeilen:
        breite = draw.textlength(zeile, font=schrift)
        draw.text(((B - breite) / 2, y), zeile, font=schrift, fill=FG)
        y += zeilenhoehe

    # Jedes Modell schreibt seinen Satz selbst - daher nur noch EIN Modellname.
    footer = f"Model: {modell_name}"
    fb = draw.textlength(footer, font=fuss)
    draw.text(((B - fb) / 2, H - 42), footer, font=fuss, fill=MUTED)

    bild.save(pfad, 'JPEG', quality=92)


def archiviere(witz):
    """Speichert Foto + Satz-Karte mit Zeitstempel in ~/archiv/.
    Fehler hier stoppen die Kamera nie."""
    try:
        import shutil
        os.makedirs(ARCHIV_PFAD, exist_ok=True)
        stempel = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

        shutil.copy(BILD_PFAD, f"{ARCHIV_PFAD}/{stempel}_foto.jpg")

        anzeige = VLM_MODELL.split('/')[-1]   # voller Tag, z.B. smolvlm2:2.2b
        if len(anzeige) > 28:
            anzeige = anzeige[:28] + "..."
        erstelle_satzbild(witz, anzeige, f"{ARCHIV_PFAD}/{stempel}_satz.jpg")

        log(f"Archiviert: {stempel}")
    except Exception as e:
        log(f"Archiv-Fehler: {e}")


# ----------------------------------------------------------------------
# PROMPT & VERARBEITUNG
# ----------------------------------------------------------------------
# Vergleichs-Konzept: JEDES gewaehlte Modell erzeugt den sichtbaren Text
# selbst - kein Ministral-Zwischenschritt mehr. Alle Modelle bekommen exakt
# dieselbe Anweisung und dieselben Sampling-Parameter. So sind die Unterschiede
# in der Ausgabe reine Modell-Eigenschaft (Wahrnehmung, Wortwahl, Stil) und
# damit fair und direkt vergleichbar.

# Anweisung, wenn im Menue kein eigener Prompt gesetzt ist. Bewusst KURZ, damit
# auch winzige VQA-Modelle wie moondream sie verarbeiten (lange, mehrteilige
# Prompts lassen sie sofort mit leerer Antwort abbrechen).
STANDARD_PROMPT = (
    "Look at this photo and reply with exactly ONE short, dry, witty sentence "
    "about a specific detail you can actually see. No preamble, just the sentence."
)

ANWEISUNG = CUSTOM_PROMPT if CUSTOM_PROMPT else STANDARD_PROMPT

# Identische Sampling-Parameter fuer ALLE Modelle - Voraussetzung fuer einen
# fairen Vergleich (gleiche Kreativitaet, gleiche Laenge).
OPTIONEN = {
    'num_predict': 100,
    'temperature': 0.6,
    'top_p': 0.9,
}

if not os.path.exists(BILD_PFAD):
    print("Error: Image file missing.")
    sys.exit(1)

zeit = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
log(f"\n=== {zeit} | Modell: {VLM_MODELL}")
log(f"Anweisung: {ANWEISUNG}")

try:
    text_roh, denken = frage_modell(
        VLM_MODELL,
        prompt=ANWEISUNG,
        bild=BILD_PFAD,
        optionen=OPTIONEN,
    )
    if denken:
        log(f"Thinking (Auszug): {denken[:300]}")
    finaler_text = bereinige(text_roh)

    log(f"Ergebnis: {finaler_text if finaler_text else '(LEER!)'}")

    # Kennzahlen IMMER wegschreiben - auch bei leerer Antwort. Dann zeigt die
    # Technik-Anzeige an 'reply 1 tok', dass das Modell die Anweisung sofort
    # mit einem Stop-Token abgelehnt hat (legitimes Vergleichs-Ergebnis).
    try:
        with open(STATS_PFAD, 'w', encoding='utf-8') as f:
            json.dump(LETZTE_METRIK, f)
    except Exception as e:
        log(f"Stats-Fehler: {e}")

    if finaler_text:
        archiviere(finaler_text)
        print(finaler_text)
    else:
        # Kein Absturz: winzige Modelle lehnen komplexe Prompts teils ab
        # (sofortiges Stop-Token, kein Text). Ruhig und informativ anzeigen.
        meldung = "— no answer —"
        archiviere(meldung)
        print(meldung)

except Exception as e:
    log(f"FEHLER: {e}")
    print(f"Error: {str(e)}")
    sys.exit(1)
