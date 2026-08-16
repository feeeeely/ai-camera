import os
import json
import math
import struct
import subprocess
import threading
import time
import datetime
import urllib.request
import urllib.error
import tkinter as tk
import tkinter.font as tkfont
import gpiod

try:
    from smbus2 import SMBus
except ImportError:
    SMBus = None

# --- PFADE ANPASSEN ---
HOME         = os.path.expanduser('~')
BILD_PFAD    = f'{HOME}/snap.jpg'
LOG_PFAD     = f'{HOME}/analyse_log.txt'
ARCHIV_PFAD  = f'{HOME}/archiv'

# --- KONFIGURATION ---
TASTER_PIN = 27
VISION_SCRIPT = f'{HOME}/vision.py'
PROMPT_PFAD = f'{HOME}/custom_prompt.txt'
OLLAMA_HOST = 'http://127.0.0.1:11434'
REGISTRY = 'https://registry.ollama.ai/v2/library'

# Vollstaendige Tags - genau so werden sie auch in der Oberflaeche angezeigt.
MODELLE = [
    'ministral-3:3b',        # Standard, Direkt-Modus: ~5-6s, gute Qualitaet
    'moondream:latest',      # kleines VQA-Modell, braucht kurze Fragen (~9s), knapp
    'smolvlm2:2.2b',         # ~10s Vision-Encode, detailliert - schnell UND gut
    'minicpm-v4.6',          # 752M, sehr detailreiche Beschreibung (~25s)
    'qwen3-vl:2b-instruct',  # langsam (85-230s), aber kohaerent - zum Vergleichen
    'internvl3.5:2b',        # ~90s, genaueste/detaillierteste Beschreibung von allen
    # gemma3:4b bewusst nicht gelistet: haengt >20 Min auf diesem Pi 5, blockiert die Ausstellung.
    # Weitere Augen einfach dazuschreiben, die Eintraege entstehen automatisch.
]

aktuelles_modell = MODELLE[0]
ist_am_analysieren = False   # verhindert Prellen und ungewolltes Abbrechen
preview_proc = None          # Handle auf den rpicam-Livebild-Prozess
druck_start = None           # Zeitstempel für Long-Press-Erkennung
tipp_job = None              # laufende Schreibmaschinen-Animation

# Modell -> 'aktuell' | 'update' | 'lokal' | 'unbekannt'; im Hintergrund gefuellt.
# 'lokal' = selbst importiertes GGUF-Modell, gibt es nicht in der Registry.
update_status = {m: 'unbekannt' for m in MODELLE}
update_laeuft = False


# --- MODELL-UPDATES (nur mit WLAN; blockiert die Kamera nie) ---
def _norm_tag(modell):
    """Ollamas API meldet Modelle immer mit Tag ('name:latest'); haengt ein
    fehlendes ':latest' an, damit der Abgleich nicht ins Leere greift."""
    return modell if ':' in modell else f'{modell}:latest'

def _lokale_digests():
    with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=10) as r:
        return {m['name']: m.get('digest', '') for m in json.load(r).get('models', [])}

def _remote_digest(modell):
    name, _, tag = modell.partition(':')
    req = urllib.request.Request(
        f"{REGISTRY}/{name}/manifests/{tag or 'latest'}", method='HEAD',
        headers={'Accept': 'application/vnd.docker.distribution.manifest.v2+json'})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.headers.get('ollama-content-digest', '')

def pruefe_updates(fertig_callback=None):
    """Vergleicht lokale Modell-Kennungen mit denen der Ollama-Registry.
    Laeuft in einem eigenen Thread, damit die Oberflaeche nie einfriert."""
    def arbeit():
        global update_laeuft
        update_laeuft = True
        try:
            lokal = _lokale_digests()
        except Exception:
            lokal = {}
        for m in MODELLE:
            lokal_dig = lokal.get(_norm_tag(m), '')
            try:
                remote_dig = _remote_digest(m)
                update_status[m] = 'aktuell' if lokal_dig == remote_dig else 'update'
            except urllib.error.HTTPError as e:
                # 404 = nicht in der Registry -> selbst importiertes Modell
                update_status[m] = 'lokal' if e.code == 404 else 'unbekannt'
            except Exception:
                update_status[m] = 'unbekannt'   # kein Netz o.ae.
        update_laeuft = False
        if fertig_callback:
            root.after(0, fertig_callback)
    threading.Thread(target=arbeit, daemon=True).start()

# --- AKKU (Waveshare UPS HAT (E) ueber I2C) ---
# Registerbelegung gegen die Referenz-Implementierung geprueft:
#   0x20+0..1 Pack-Spannung mV | +2..3 Strom mA (negativ = Entladung)
#   0x20+4..5 Ladestand %      | +6..7 Restkapazitaet mAh
UPS_I2C_ADRESSE = 0x2d

def batterie_status():
    """Gibt (prozent, laedt) zurueck; (None, False) wenn die USV nicht lesbar ist."""
    if SMBus is None:
        return None, False
    try:
        with SMBus(1) as bus:
            block = bus.read_i2c_block_data(UPS_I2C_ADRESSE, 0x20, 8)
        _volt, strom, prozent, _kapazitaet = struct.unpack('<HhHH', bytes(block))
        return max(0, min(100, prozent)), strom > 0
    except Exception:
        return None, False


def update_zusammenfassung():
    if update_laeuft:
        return "checking…"
    offen = sum(1 for s in update_status.values() if s == 'update')
    if offen:
        return f"{offen} available"
    # 'aktuell' und 'lokal' (selbst importiert) gelten beide als in Ordnung.
    if all(s in ('aktuell', 'lokal') for s in update_status.values()):
        return "all up to date"
    return "unknown (no Wi-Fi?)"


# --- WLAN (ueber nmcli / NetworkManager) ---
def wlan_aktuell():
    """Name des aktuell verbundenen Netzes oder 'not connected'."""
    try:
        out = subprocess.run(['nmcli', '-t', '-f', 'GENERAL.CONNECTION',
                              'device', 'show', 'wlan0'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        name = out.split(':', 1)[1] if ':' in out else ''
        return name if name and name != '--' else 'not connected'
    except Exception:
        return 'unknown'

def wlan_scan():
    """Liste (ssid, signal, gesichert) der sichtbaren Netze, staerkste zuerst."""
    try:
        out = subprocess.run(['nmcli', '-t', '-f', 'SSID,SIGNAL,SECURITY',
                              'device', 'wifi', 'list', '--rescan', 'yes'],
                             capture_output=True, text=True, timeout=25).stdout
    except Exception:
        return []
    netze = {}
    for zeile in out.splitlines():
        # nmcli maskiert Doppelpunkte im SSID als '\:' - zuerst schuetzen.
        teile = zeile.replace('\\:', '\x00').split(':')
        if len(teile) < 3:
            continue
        ssid = teile[0].replace('\x00', ':').strip()
        if not ssid:
            continue
        try:
            signal = int(teile[1])
        except ValueError:
            signal = 0
        gesichert = bool(teile[2].strip())
        if ssid not in netze or signal > netze[ssid][0]:
            netze[ssid] = (signal, gesichert)
    return sorted(([s, sig, sec] for s, (sig, sec) in netze.items()),
                  key=lambda x: -x[1])

def wlan_verbinden(ssid, passwort):
    """Versucht die Verbindung; gibt (erfolg, meldung) zurueck."""
    cmd = ['nmcli', 'device', 'wifi', 'connect', ssid]
    if passwort:
        cmd += ['password', passwort]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if r.returncode == 0:
            return True, "connected"
        return False, (r.stderr or r.stdout).strip().split('\n')[-1][:60]
    except Exception as e:
        return False, str(e)[:60]


# --- CUSTOM PROMPT ---
def lade_custom_prompt():
    try:
        with open(PROMPT_PFAD, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except FileNotFoundError:
        return ''

def speichere_custom_prompt(text):
    with open(PROMPT_PFAD, 'w', encoding='utf-8') as f:
        f.write(text.strip())

aktueller_prompt = lade_custom_prompt()


# --- DYNAMISCHER GPIO CHIP FINDER ---
chip_path = None
for dev in os.listdir('/dev/'):
    if dev.startswith('gpiochip'):
        full_path = f"/dev/{dev}"
        try:
            with gpiod.Chip(full_path) as test_chip:
                label = test_chip.get_info().label
                if 'rp1' in label:
                    chip_path = full_path
                    break
                if 'gpio' in label.lower():
                    chip_path = full_path
        except Exception:
            pass

if not chip_path:
    for c in ['gpiochip4', 'gpiochip8', 'gpiochip2', 'gpiochip0']:
        full_path = f"/dev/{c}"
        if os.path.exists(full_path):
            chip_path = full_path
            break

# --- GPIO CONFIG ---
# Beide Flanken: kurz drücken = auslösen (beim Loslassen),
# >= 3 Sekunden halten = sauber herunterfahren.
edge_setting = None
bias_setting = None
for obj in [gpiod, getattr(gpiod, 'line', None)]:
    if obj:
        if edge_setting is None and hasattr(obj, 'Edge'):
            edge_class = getattr(obj, 'Edge')
            if hasattr(edge_class, 'BOTH'):
                edge_setting = edge_class.BOTH
            elif hasattr(edge_class, 'BOTH_EDGES'):
                edge_setting = edge_class.BOTH_EDGES
        if bias_setting is None and hasattr(obj, 'Bias'):
            bias_class = getattr(obj, 'Bias')
            if hasattr(bias_class, 'PULL_UP'):
                bias_setting = bias_class.PULL_UP

if edge_setting is None: edge_setting = "both"
if bias_setting is None: bias_setting = "pull-up"

try:
    line_settings = gpiod.LineSettings(
        edge_detection=edge_setting,
        bias=bias_setting,
        debounce_period=datetime.timedelta(milliseconds=30),
    )
except TypeError:
    # ältere gpiod-Version ohne debounce_period
    line_settings = gpiod.LineSettings(edge_detection=edge_setting, bias=bias_setting)

request = gpiod.request_lines(
    chip_path,
    consumer="Kamera",
    config={TASTER_PIN: line_settings},
)

# --- UI INITIALISIERUNG ---
BG, FG, MUTED = "#0f1113", "#eceae4", "#7c8085"

root = tk.Tk()
# Die Menueleiste bekommt die oberen BAR_HOEHE Pixel als eigene Spur; das
# Hauptfenster liegt darunter. So ueberlappen sich die beiden overrideredirect-
# Fenster nie (der Compositor stapelt sonst das Vollbild-root ueber die Leiste).
BAR_HOEHE = 44

root.title("AI Analysis")
root.geometry(f"800x{480 - BAR_HOEHE}+0+{BAR_HOEHE}")
root.overrideredirect(True)
root.configure(bg=BG, cursor="none")   # kein Mauszeiger = Kamera-Feeling

# JetBrains Mono, falls installiert (sudo apt install fonts-jetbrains-mono),
# sonst automatisch DejaVu Sans Mono (auf Raspberry Pi OS vorhanden).
FONT = "JetBrains Mono" if "JetBrains Mono" in tkfont.families(root) else "DejaVu Sans Mono"

# --- OBERE MENUELEISTE: eigenes Fenster, sichtbar in Live-View und
# Sentence-Display (im "Developer Mode" ausgeblendet) - liegt als Overlay ueber
# der Kamera-Vorschau. Bewusst reduziert: links das Hamburger-Symbol als
# Einstieg ins Menue, rechts der Akkustand. Alles andere steckt im Menue. ---
top_bar = tk.Toplevel(root)
top_bar.geometry(f"800x{BAR_HOEHE}+0+0")
top_bar.overrideredirect(True)
top_bar.configure(bg=BG)
top_bar.attributes('-topmost', True)

cv_burger = tk.Canvas(top_bar, width=BAR_HOEHE, height=BAR_HOEHE, bg=BG,
                       highlightthickness=0)
cv_burger.pack(side='left', padx=(10, 0))
for y in (16, 22, 28):
    cv_burger.create_line(14, y, 30, y, fill=FG, width=2)

# Akku-Symbol rechts aussen, gleiche Groesse wie das Hamburger-Symbol
cv_batterie = tk.Canvas(top_bar, width=BAR_HOEHE, height=BAR_HOEHE, bg=BG,
                         highlightthickness=0)
cv_batterie.pack(side='right', padx=(0, 12))

def _akku_farbe(prozent):
    """Fliessender Verlauf gruen -> gelb -> rot, je leerer desto roter."""
    if prozent >= 60:
        return "#5fbf6a"       # gruen
    if prozent >= 35:
        return "#c9a83f"       # gelb
    if prozent >= 15:
        return "#d17f3a"       # orange
    return "#cf4a3a"           # rot

def zeichne_batterie(prozent, laedt):
    cv_batterie.delete("all")
    x0, y0, x1, y1 = 6, 14, 32, 30
    cv_batterie.create_rectangle(x0, y0, x1, y1, outline=FG, width=2)
    cv_batterie.create_rectangle(x1 + 2, y0 + 5, x1 + 5, y1 - 5,
                                  fill=FG, outline="")      # Pluspol
    if prozent is None:
        cv_batterie.create_line(x0 + 4, y0 + 4, x1 - 4, y1 - 4, fill=MUTED, width=2)
        return
    innen_max = (x1 - x0) - 6
    breite = innen_max * prozent / 100.0
    if breite >= 1:
        cv_batterie.create_rectangle(x0 + 3, y0 + 3, x0 + 3 + breite, y1 - 3,
                                      fill=_akku_farbe(prozent), outline="")
    if laedt:   # kleiner Blitz als Ladehinweis
        cv_batterie.create_polygon(20, 15, 15, 23, 19, 23, 17, 29, 24, 21, 20, 21,
                                    fill=BG, outline=FG, width=1)

def akku_aktualisieren():
    prozent, laedt = batterie_status()
    zeichne_batterie(prozent, laedt)
    root.after(20000, akku_aktualisieren)   # alle 20 s nachsehen

def aktualisiere_top_bar():
    """Die Leiste zeigt nur Hamburger-Symbol und Akku - alle Details
    (aktives Modell, Prompt, Updates) stehen im Menue dahinter."""
    pass

lbl_text = tk.Label(root, text="", wraplength=680, justify="center",
                    fg=FG, bg=BG, font=(FONT, 22))
lbl_text.pack(expand=True, fill="both", padx=60, pady=(30, 10))

# Technik-Kennzahlen unter dem Satz (nur im Ergebnis-Bildschirm sichtbar).
STATS_PFAD = f'{HOME}/last_stats.json'
lbl_stats = tk.Label(root, text="", justify="center", fg=MUTED, bg=BG,
                     font=(FONT, 10))

def _stats_text():
    """Liest die Kennzahlen des letzten Laufs und formatiert sie als kompakten,
    selbsterklaerenden Benchmark-Block - auf Modellvergleich ausgelegt.
      image->N tok = wie viele Tokens der Encoder aus dem Bild macht
                     (Effizienz-Fingerabdruck: kleiner = schneller)
      reply N tok / tok/s = Ausgabelaenge und reine Generierungs-Geschwindigkeit
      total/vision/load   = Gesamtzeit, Bild-Verarbeitung (Flaschenhals), Kaltstart"""
    try:
        with open(STATS_PFAD, 'r', encoding='utf-8') as f:
            m = json.load(f)
    except Exception:
        return ""
    zeile1 = m.get('modell', '')
    zeile2 = (f"image → {m.get('input_tokens', 0)} tok   ·   "
              f"reply {m.get('output_tokens', 0)} tok   ·   "
              f"{m.get('tok_pro_s', 0)} tok/s")
    zeile3 = (f"total {m.get('wandzeit', 0)} s   ·   "
              f"vision {m.get('vision_s', 0)} s   ·   "
              f"load {m.get('load_s', 0)} s")
    return f"{zeile1}\n{zeile2}\n{zeile3}"

# Wartezustand: drei kleine Icons (Uhr - Sanduhr - Schnecke) als Reihe,
# darunter der Text, alles als EINE Gruppe gemeinsam vertikal zentriert.
frame_warten = tk.Frame(root, bg=BG)
frame_icons = tk.Frame(frame_warten, bg=BG)
frame_icons.pack(pady=(0, 22))

ICON_GROESSE = dict(width=56, height=64, bg=BG, highlightthickness=0)
cv_sanduhr = tk.Canvas(frame_icons, **ICON_GROESSE)
cv_sanduhr.pack()

lbl_warten = tk.Label(frame_warten, text="", fg=FG, bg=BG,
                      font=(FONT, 20), justify="center")
lbl_warten.pack()

def zeichne_sanduhr(phase):
    """Minimalistische weiss-auf-schwarz Sanduhr, ein Sandkorn faellt (phase 0..1)."""
    cv_sanduhr.delete("all")
    cx, cy, w, h = 28, 32, 18, 22
    cv_sanduhr.create_line(cx - w, cy - h, cx + w, cy - h, cx, cy,
                            cx - w, cy - h, fill=FG, width=2)
    cv_sanduhr.create_line(cx - w, cy + h, cx + w, cy + h, cx, cy,
                            cx - w, cy + h, fill=FG, width=2)
    oben_b = w * 0.55 * (1 - phase)
    if oben_b > 1:
        cv_sanduhr.create_polygon(cx - oben_b, cy - h + 4, cx + oben_b, cy - h + 4,
                                   cx, cy - 2, fill=FG, outline="")
    unten_b = w * 0.55 * phase
    if unten_b > 1:
        cv_sanduhr.create_polygon(cx - unten_b, cy + h - 2, cx + unten_b, cy + h - 2,
                                   cx, cy + h - 2 - unten_b * 0.9, fill=FG, outline="")
    korn_y = cy + phase * h
    cv_sanduhr.create_oval(cx - 2, korn_y - 2, cx + 2, korn_y + 2, fill=FG, outline="")

# --- EIGENE BILDSCHIRMTASTATUR ---
# Direkt in Tkinter gezeichnet: ein Tastendruck ruft sofort entry.insert() auf,
# daher keine Wayland-Fokus-Probleme wie bei externen Tastaturen. Bedient sowohl
# das Prompt-Feld als auch die WLAN-Passworteingabe.
tastatur_ziel = None          # das aktuell bediente Entry-Widget
_kb_shift = False             # Grossschreibung fuer den naechsten Buchstaben
_kb_symbole = False           # Symbol-Ebene statt Buchstaben

_KB_BUCHSTABEN = [
    list("1234567890"),
    list("qwertyuiop"),
    list("asdfghjkl"),
    list("zxcvbnm"),
]
_KB_SYMBOLE = [
    list("1234567890"),
    list("@#$%&*-+()"),
    list("!?/:;'\"_="),
    list(".,~[]{}"),
]

frame_kb = tk.Frame(root, bg="#0b0c0d")

def _kb_cursor_sichtbar():
    """Sorgt dafuer, dass die Cursor-Position im Feld sichtbar bleibt (das Feld
    scrollt mit, statt den Text hinter dem rechten Rand verschwinden zu lassen)."""
    if tastatur_ziel is not None:
        try:
            tastatur_ziel.xview(tastatur_ziel.index(tk.INSERT))
        except Exception:
            pass

def _kb_taste_druecken(zeichen):
    if tastatur_ziel is None:
        return
    global _kb_shift
    if _kb_shift and not _kb_symbole:
        zeichen = zeichen.upper()
    tastatur_ziel.insert(tk.INSERT, zeichen)   # an der Cursor-Position einfuegen
    _kb_cursor_sichtbar()
    if _kb_shift:
        _kb_shift = False
        _kb_render()

def _kb_backspace():
    if tastatur_ziel is None:
        return
    pos = tastatur_ziel.index(tk.INSERT)
    if pos > 0:
        tastatur_ziel.delete(pos - 1, pos)   # Zeichen VOR dem Cursor loeschen
        _kb_cursor_sichtbar()

def _kb_cursor_bewegen(delta):
    if tastatur_ziel is None:
        return
    pos = tastatur_ziel.index(tk.INSERT)
    neu = max(0, min(len(tastatur_ziel.get()), pos + delta))
    tastatur_ziel.icursor(neu)
    _kb_cursor_sichtbar()

def _kb_shift_umschalten():
    global _kb_shift
    _kb_shift = not _kb_shift
    _kb_render()

def _kb_symbole_umschalten():
    global _kb_symbole, _kb_shift
    _kb_symbole = not _kb_symbole
    _kb_shift = False
    _kb_render()

def _kb_taste(eltern, text, befehl, breite=1, aktiv=False):
    farbe = "#2a2d30" if aktiv else "#1a1d20"
    b = tk.Button(eltern, text=text, command=befehl,
                  bg=farbe, fg=FG, activebackground="#3a3d40", activeforeground=FG,
                  font=(FONT, 13, "bold"), relief="flat", bd=0, highlightthickness=0)
    b.pack(side='left', expand=True, fill='both', padx=2, pady=2)
    return b

def _kb_render():
    for w in frame_kb.winfo_children():
        w.destroy()
    reihen = _KB_SYMBOLE if _kb_symbole else _KB_BUCHSTABEN
    for zeichen in reihen:
        reihe = tk.Frame(frame_kb, bg="#0b0c0d")
        reihe.pack(fill='both', expand=True)
        for z in zeichen:
            anzeige = z.upper() if (_kb_shift and not _kb_symbole and z.isalpha()) else z
            _kb_taste(reihe, anzeige, lambda z=z: _kb_taste_druecken(z))

    letzte = tk.Frame(frame_kb, bg="#0b0c0d")
    letzte.pack(fill='both', expand=True)
    if not _kb_symbole:
        _kb_taste(letzte, "⇧", _kb_shift_umschalten, aktiv=_kb_shift)
    _kb_taste(letzte, "?123" if not _kb_symbole else "ABC", _kb_symbole_umschalten)
    _kb_taste(letzte, "◀", lambda: _kb_cursor_bewegen(-1))
    _kb_taste(letzte, "space", lambda: _kb_taste_druecken(" "))
    _kb_taste(letzte, "▶", lambda: _kb_cursor_bewegen(1))
    _kb_taste(letzte, "⌫", _kb_backspace)

def zeige_tastatur(ziel):
    global tastatur_ziel, _kb_shift, _kb_symbole
    tastatur_ziel = ziel
    _kb_shift = False
    _kb_symbole = False
    _kb_render()
    frame_kb.place(relx=0, rely=1.0, anchor='sw', relwidth=1.0, height=210)
    frame_kb.lift()

def verstecke_tastatur():
    global tastatur_ziel
    tastatur_ziel = None
    frame_kb.place_forget()

# --- BILDSCHIRM-VERWALTUNG ---
# Alle Menue-Ansichten liegen im selben Fenster; immer nur eine ist gepackt.
def _alle_ansichten_aus():
    for w in (lbl_text, lbl_stats, frame_menu, frame_modell, frame_prompt,
              frame_update, frame_wifi):
        w.pack_forget()

def _zurueck_zur_liveansicht():
    """Gemeinsamer Ausgang aus jedem Menue: zurueck in die Live-Ansicht."""
    verstecke_tastatur()
    _alle_ansichten_aus()
    lbl_text.pack(expand=True, fill="both", padx=60, pady=(30, 10))
    root.withdraw()
    aktualisiere_top_bar()
    top_bar.deiconify()
    starte_live_bild()

# --- MENUE (ueber das Hamburger-Symbol) ---
frame_menu = tk.Frame(root, bg=BG)

tk.Label(frame_menu, text="Menu", fg=FG, bg=BG, font=(FONT, 20)).pack(pady=(26, 18))

frame_menu_liste = tk.Frame(frame_menu, bg=BG)
frame_menu_liste.pack(fill='both', padx=70)

btn_menu_model = tk.Button(frame_menu_liste, anchor='w',
                            command=lambda: zeige_modell_bildschirm(),
                            bg="#1a1d20", fg=FG, activebackground="#2a2d30",
                            activeforeground=FG, font=(FONT, 13, "bold"),
                            relief="flat", bd=0, highlightthickness=0,
                            padx=18, pady=14)
btn_menu_model.pack(fill='x', pady=5)

btn_menu_prompt = tk.Button(frame_menu_liste, anchor='w',
                             command=lambda: zeige_prompt_bildschirm(),
                             bg="#1a1d20", fg=FG, activebackground="#2a2d30",
                             activeforeground=FG, font=(FONT, 13, "bold"),
                             relief="flat", bd=0, highlightthickness=0,
                             padx=18, pady=14)
btn_menu_prompt.pack(fill='x', pady=5)

btn_menu_updates = tk.Button(frame_menu_liste, anchor='w',
                              command=lambda: zeige_update_bildschirm(),
                              bg="#1a1d20", fg=FG, activebackground="#2a2d30",
                              activeforeground=FG, font=(FONT, 13, "bold"),
                              relief="flat", bd=0, highlightthickness=0,
                              padx=18, pady=14)
btn_menu_updates.pack(fill='x', pady=5)

btn_menu_wifi = tk.Button(frame_menu_liste, anchor='w',
                           command=lambda: zeige_wifi_bildschirm(),
                           bg="#1a1d20", fg=FG, activebackground="#2a2d30",
                           activeforeground=FG, font=(FONT, 13, "bold"),
                           relief="flat", bd=0, highlightthickness=0,
                           padx=18, pady=14)
btn_menu_wifi.pack(fill='x', pady=5)

tk.Button(frame_menu, text="CLOSE", command=lambda: _zurueck_zur_liveansicht(),
          bg=BG, fg=MUTED, activebackground="#1a1d20", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=34, pady=12).pack(pady=14)

def aktualisiere_menue_texte():
    btn_menu_model.configure(text=f"CHANGE MODEL     {aktuelles_modell}")
    if aktueller_prompt:
        kurz = aktueller_prompt if len(aktueller_prompt) <= 20 else aktueller_prompt[:20] + "…"
    else:
        kurz = "default"
    btn_menu_prompt.configure(text=f"CHANGE PROMPT    {kurz}")
    btn_menu_updates.configure(text=f"MODEL UPDATES    {update_zusammenfassung()}")
    btn_menu_wifi.configure(text=f"WI-FI            {wlan_aktuell()}")

def zeige_menue(event=None):
    if ist_am_analysieren:
        return
    aktualisiere_menue_texte()
    top_bar.withdraw()
    _alle_ansichten_aus()
    frame_menu.pack(expand=True, fill='both')
    root.deiconify()
    root.update()

cv_burger.bind("<Button-1>", zeige_menue)   # ganze 44x44-Flaeche als Tippziel

def zurueck_zum_menue():
    """Einen Schritt zurueck: aus einem Unterpunkt zurueck ins Menue -
    nicht ganz raus in die Live-Ansicht. Die Vorschau laeuft ohnehin verdeckt
    weiter, daher kein Neustart noetig."""
    verstecke_tastatur()
    aktualisiere_menue_texte()
    _alle_ansichten_aus()
    frame_menu.pack(expand=True, fill='both')
    root.update()

# --- MODELL-BILDSCHIRM: Liste aller Augen, aktives Modell hervorgehoben ---
frame_modell = tk.Frame(root, bg=BG)

tk.Label(frame_modell, text="Select model", fg=FG, bg=BG,
         font=(FONT, 18)).pack(pady=(16, 10))

frame_modell_liste = tk.Frame(frame_modell, bg=BG)
frame_modell_liste.pack(fill='both', padx=70)

def modell_waehlen(m):
    global aktuelles_modell
    if ist_am_analysieren:
        return
    aktuelles_modell = m
    verlasse_modell_bildschirm()

def baue_modell_liste():
    """Zeigt den vollstaendigen Tag jedes Modells; ein Punkt markiert eine
    verfuegbare neue Version."""
    for widget in frame_modell_liste.winfo_children():
        widget.destroy()
    for m in MODELLE:
        aktiv = (m == aktuelles_modell)
        marke = "  •" if update_status.get(m) == 'update' else ""
        text = ("›  " if aktiv else "   ") + m + marke
        btn = tk.Button(frame_modell_liste, text=text, anchor='w',
                        command=lambda m=m: modell_waehlen(m),
                        bg=("#1a1d20" if aktiv else BG), fg=(FG if aktiv else MUTED),
                        activebackground="#1a1d20", activeforeground=FG,
                        font=(FONT, 12, "bold" if aktiv else "normal"),
                        relief="flat", bd=0, highlightthickness=0, pady=8)
        btn.pack(fill='x', pady=2)

tk.Button(frame_modell, text="BACK", command=lambda: verlasse_modell_bildschirm(),
          bg=BG, fg=MUTED, activebackground="#1a1d20", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, padx=30, pady=10
          ).pack(pady=(14, 8))

def zeige_modell_bildschirm():
    if ist_am_analysieren:
        return
    top_bar.withdraw()
    _alle_ansichten_aus()
    baue_modell_liste()
    frame_modell.pack(expand=True, fill='both')
    root.deiconify()
    root.update()

def verlasse_modell_bildschirm():
    zurueck_zum_menue()

# --- UPDATE-BILDSCHIRM: Versionsstand aller Modelle, Nachladen auf Wunsch ---
frame_update = tk.Frame(root, bg=BG)

tk.Label(frame_update, text="Model updates", fg=FG, bg=BG,
         font=(FONT, 18)).pack(pady=(20, 4))
lbl_update_info = tk.Label(frame_update, text="", fg=MUTED, bg=BG, font=(FONT, 10))
lbl_update_info.pack(pady=(0, 12))

frame_update_liste = tk.Frame(frame_update, bg=BG)
frame_update_liste.pack(fill='both', padx=60)

STATUS_TEXT = {'aktuell': 'up to date', 'update': 'UPDATE',
               'lokal': 'local model', 'unbekannt': '– no check'}

def baue_update_liste():
    for widget in frame_update_liste.winfo_children():
        widget.destroy()
    for m in MODELLE:
        st = update_status.get(m, 'unbekannt')
        zeile = tk.Frame(frame_update_liste, bg=BG)
        zeile.pack(fill='x', pady=3)
        tk.Label(zeile, text=m, anchor='w', bg=BG, fg=FG,
                 font=(FONT, 11)).pack(side='left')
        tk.Label(zeile, text=STATUS_TEXT[st], anchor='e', bg=BG,
                 fg=(FG if st == 'update' else MUTED),
                 font=(FONT, 10, "bold" if st == 'update' else "normal")
                 ).pack(side='right')
    lbl_update_info.configure(text=update_zusammenfassung())

def _update_ansicht_neu():
    baue_update_liste()
    btn_update_install.configure(
        state=('normal' if any(s == 'update' for s in update_status.values()) else 'disabled'))

def update_pruefen_gedrueckt():
    lbl_update_info.configure(text="checking…")
    root.update()
    pruefe_updates(_update_ansicht_neu)

def updates_installieren():
    """Laedt neue Versionen nach. Laeuft im Hintergrund; die Oberflaeche bleibt
    bedienbar, der Fortschritt steht in der Infozeile."""
    offen = [m for m in MODELLE if update_status.get(m) == 'update']
    if not offen:
        return
    btn_update_install.configure(state='disabled')

    def arbeit():
        for i, m in enumerate(offen, 1):
            root.after(0, lambda m=m, i=i: lbl_update_info.configure(
                text=f"downloading {i}/{len(offen)}: {m}"))
            subprocess.run(['ollama', 'pull', m],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pruefe_updates(_update_ansicht_neu)
    threading.Thread(target=arbeit, daemon=True).start()

frame_update_buttons = tk.Frame(frame_update, bg=BG)
frame_update_buttons.pack(pady=(16, 0))
tk.Button(frame_update_buttons, text="CHECK", command=update_pruefen_gedrueckt,
          bg="#1a1d20", fg=FG, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=26, pady=11).pack(side='left', padx=8)
btn_update_install = tk.Button(frame_update_buttons, text="INSTALL",
                                command=updates_installieren,
                                bg="#1a1d20", fg=FG, activebackground="#2a2d30",
                                activeforeground=FG, disabledforeground="#4a4d50",
                                font=(FONT, 11, "bold"), relief="flat", bd=0,
                                highlightthickness=0, padx=26, pady=11)
btn_update_install.pack(side='left', padx=8)
tk.Button(frame_update_buttons, text="BACK", command=lambda: zurueck_zum_menue(),
          bg="#1a1d20", fg=MUTED, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=26, pady=11).pack(side='left', padx=8)

def zeige_update_bildschirm():
    if ist_am_analysieren:
        return
    top_bar.withdraw()
    _alle_ansichten_aus()
    _update_ansicht_neu()
    frame_update.pack(expand=True, fill='both')
    root.deiconify()
    root.update()

# --- WLAN-BILDSCHIRM: Netze scannen und mit Passwort verbinden ---
frame_wifi = tk.Frame(root, bg=BG)
wifi_gewaehlt = {'ssid': None, 'gesichert': False}

# Ansicht A: Netzliste
wifi_liste_ansicht = tk.Frame(frame_wifi, bg=BG)
tk.Label(wifi_liste_ansicht, text="Wi-Fi", fg=FG, bg=BG,
         font=(FONT, 18)).pack(pady=(18, 2))
lbl_wifi_status = tk.Label(wifi_liste_ansicht, text="", fg=MUTED, bg=BG, font=(FONT, 10))
lbl_wifi_status.pack(pady=(0, 10))
frame_wifi_liste = tk.Frame(wifi_liste_ansicht, bg=BG)
frame_wifi_liste.pack(fill='both', padx=60)

def _wifi_liste_fuellen(netze):
    for w in frame_wifi_liste.winfo_children():
        w.destroy()
    if not netze:
        tk.Label(frame_wifi_liste, text="no networks found", fg=MUTED, bg=BG,
                 font=(FONT, 11)).pack(pady=10)
        return
    for ssid, signal, gesichert in netze[:6]:
        schloss = " ⌁" if gesichert else ""
        balken = "▂▄▆█"[min(3, signal // 25)]
        b = tk.Button(frame_wifi_liste, anchor='w',
                      text=f"{balken}  {ssid}{schloss}",
                      command=lambda s=ssid, g=gesichert: wifi_netz_gewaehlt(s, g),
                      bg="#1a1d20", fg=FG, activebackground="#2a2d30",
                      activeforeground=FG, font=(FONT, 12), relief="flat", bd=0,
                      highlightthickness=0, padx=16, pady=11)
        b.pack(fill='x', pady=3)

def wifi_scannen():
    lbl_wifi_status.configure(text="scanning…")
    root.update()
    def arbeit():
        netze = wlan_scan()
        def zeigen():
            _wifi_liste_fuellen(netze)
            lbl_wifi_status.configure(text=f"connected: {wlan_aktuell()}")
        root.after(0, zeigen)
    threading.Thread(target=arbeit, daemon=True).start()

wifi_liste_buttons = tk.Frame(wifi_liste_ansicht, bg=BG)
wifi_liste_buttons.pack(pady=(14, 0))
tk.Button(wifi_liste_buttons, text="RESCAN", command=lambda: wifi_scannen(),
          bg="#1a1d20", fg=FG, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=26, pady=11).pack(side='left', padx=8)
tk.Button(wifi_liste_buttons, text="BACK", command=lambda: zurueck_zum_menue(),
          bg="#1a1d20", fg=MUTED, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=26, pady=11).pack(side='left', padx=8)

# Ansicht B: Passworteingabe (kompakt oben, Tastatur darunter)
wifi_pw_ansicht = tk.Frame(frame_wifi, bg=BG)
lbl_wifi_pw_titel = tk.Label(wifi_pw_ansicht, text="", fg=FG, bg=BG, font=(FONT, 16))
lbl_wifi_pw_titel.pack(pady=(16, 4))
lbl_wifi_pw_info = tk.Label(wifi_pw_ansicht, text="Enter password", fg=MUTED, bg=BG,
                             font=(FONT, 10))
lbl_wifi_pw_info.pack(pady=(0, 10))
entry_wifi_pw = tk.Entry(wifi_pw_ansicht, font=(FONT, 15), fg=FG, bg="#1a1d20",
                          insertbackground=FG, relief="flat", justify="left", show="•")
entry_wifi_pw.pack(fill='x', padx=60, ipady=8)
wifi_pw_buttons = tk.Frame(wifi_pw_ansicht, bg=BG)
wifi_pw_buttons.pack(pady=(14, 0))

def wifi_verbinden_gedrueckt():
    ssid = wifi_gewaehlt['ssid']
    pw = entry_wifi_pw.get()
    verstecke_tastatur()
    lbl_wifi_pw_info.configure(text="connecting…")
    root.update()
    def arbeit():
        ok, meldung = wlan_verbinden(ssid, pw)
        def zeigen():
            if ok:
                pruefe_updates()   # jetzt evtl. wieder Netz -> Updates neu pruefen
                zurueck_zum_menue()
            else:
                lbl_wifi_pw_info.configure(text=meldung or "failed")
                zeige_tastatur(entry_wifi_pw)
        root.after(0, zeigen)
    threading.Thread(target=arbeit, daemon=True).start()

tk.Button(wifi_pw_buttons, text="CONNECT", command=wifi_verbinden_gedrueckt,
          bg="#1a1d20", fg=FG, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=26, pady=11).pack(side='left', padx=8)
tk.Button(wifi_pw_buttons, text="CANCEL", command=lambda: wifi_zurueck_zur_liste(),
          bg="#1a1d20", fg=MUTED, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=26, pady=11).pack(side='left', padx=8)

def wifi_netz_gewaehlt(ssid, gesichert):
    wifi_gewaehlt['ssid'] = ssid
    wifi_gewaehlt['gesichert'] = gesichert
    if gesichert:
        lbl_wifi_pw_titel.configure(text=ssid)
        lbl_wifi_pw_info.configure(text="Enter password")
        entry_wifi_pw.delete(0, tk.END)
        wifi_liste_ansicht.pack_forget()
        wifi_pw_ansicht.pack(expand=True, fill='both')
        root.update()
        zeige_tastatur(entry_wifi_pw)
    else:
        lbl_wifi_status.configure(text="connecting…")
        root.update()
        def arbeit():
            ok, meldung = wlan_verbinden(ssid, "")
            root.after(0, lambda: (zurueck_zum_menue() if ok
                                    else lbl_wifi_status.configure(text=meldung)))
        threading.Thread(target=arbeit, daemon=True).start()

def wifi_zurueck_zur_liste():
    verstecke_tastatur()
    wifi_pw_ansicht.pack_forget()
    wifi_liste_ansicht.pack(expand=True, fill='both')
    lbl_wifi_status.configure(text=f"connected: {wlan_aktuell()}")
    root.update()

def zeige_wifi_bildschirm():
    if ist_am_analysieren:
        return
    top_bar.withdraw()
    _alle_ansichten_aus()
    wifi_pw_ansicht.pack_forget()
    wifi_liste_ansicht.pack(expand=True, fill='both')
    _wifi_liste_fuellen([])
    lbl_wifi_status.configure(text=f"connected: {wlan_aktuell()}")
    frame_wifi.pack(expand=True, fill='both')
    root.deiconify()
    root.update()
    wifi_scannen()

# --- PROMPT-BILDSCHIRM: eigene Anweisung fuer die Bildunterschrift eingeben ---
# Alles kompakt im oberen Drittel, damit die Bildschirmtastatur (unteres ~40%
# des Displays) die SAVE/CANCEL-Knoepfe nie verdeckt.
frame_prompt = tk.Frame(root, bg=BG)

tk.Label(frame_prompt, text="Custom instruction", fg=FG, bg=BG,
         font=(FONT, 18)).pack(pady=(16, 4))
tk.Label(frame_prompt,
         text="Tell the camera what to focus on.\nLeave empty for the default style.",
         fg=MUTED, bg=BG, font=(FONT, 10), justify="center").pack(pady=(0, 10))

entry_prompt = tk.Entry(frame_prompt, font=(FONT, 15), fg=FG, bg="#1a1d20",
                         insertbackground=FG, relief="flat", justify="left")
entry_prompt.pack(fill='x', padx=60, ipady=8)

frame_prompt_buttons = tk.Frame(frame_prompt, bg=BG)
frame_prompt_buttons.pack(pady=(16, 0))

def prompt_speichern():
    global aktueller_prompt
    aktueller_prompt = entry_prompt.get().strip()
    speichere_custom_prompt(aktueller_prompt)
    verlasse_prompt_bildschirm()

def prompt_abbrechen():
    verlasse_prompt_bildschirm()

def prompt_leeren():
    entry_prompt.delete(0, tk.END)

tk.Button(frame_prompt_buttons, text="SAVE", command=prompt_speichern,
          bg="#1a1d20", fg=FG, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=28, pady=12).pack(side='left', padx=8)
tk.Button(frame_prompt_buttons, text="CLEAR", command=prompt_leeren,
          bg="#1a1d20", fg=MUTED, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=28, pady=12).pack(side='left', padx=8)
tk.Button(frame_prompt_buttons, text="CANCEL", command=prompt_abbrechen,
          bg="#1a1d20", fg=MUTED, activebackground="#2a2d30", activeforeground=FG,
          font=(FONT, 11, "bold"), relief="flat", bd=0, highlightthickness=0,
          padx=28, pady=12).pack(side='left', padx=8)

def zeige_prompt_bildschirm():
    if ist_am_analysieren:
        return
    top_bar.withdraw()
    _alle_ansichten_aus()
    entry_prompt.delete(0, tk.END)
    entry_prompt.insert(0, aktueller_prompt)
    frame_prompt.pack(expand=True, fill='both')
    root.deiconify()
    root.update()
    zeige_tastatur(entry_prompt)

def verlasse_prompt_bildschirm():
    zurueck_zum_menue()

aktualisiere_top_bar()

root.withdraw()

# --- TEXT-DARSTELLUNG ---
def tippe(text, i=0):
    """Schreibmaschinen-Effekt: baut den Text Zeichen für Zeichen auf."""
    global tipp_job
    lbl_text.configure(text=text[:i])
    if i < len(text):
        tipp_job = root.after(24, tippe, text, i + 1)
    else:
        tipp_job = None

def setze_ergebnis(text, schreibmaschine=True):
    """Wählt die Schriftgröße passend zur Textlänge und zeigt den Text an."""
    global tipp_job
    if tipp_job:
        root.after_cancel(tipp_job)
        tipp_job = None
    n = len(text)
    groesse = 26 if n <= 70 else 21 if n <= 140 else 16 if n <= 260 else 13
    lbl_text.configure(font=(FONT, groesse))
    if schreibmaschine:
        tippe(text)
    else:
        lbl_text.configure(text=text)

# --- KAMERA ---
def starte_live_bild():
    global preview_proc
    stoppe_live_bild()
    # Vorschau laesst oben Platz fuer die Menueleiste (kein --fullscreen mehr,
    # da das die Fenstergeometrie ignorieren wuerde).
    preview_proc = subprocess.Popen([
        "rpicam-still", "-t", "0",
        "--preview", f"0,{BAR_HOEHE},800,{480 - BAR_HOEHE}",
        "--viewfinder-width", "800", "--viewfinder-height", str(480 - BAR_HOEHE),
    ])

def stoppe_live_bild():
    global preview_proc
    if preview_proc and preview_proc.poll() is None:
        preview_proc.terminate()
        try:
            preview_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            preview_proc.kill()
    preview_proc = None

def foto_schiessen():
    global ist_am_analysieren
    ist_am_analysieren = True   # Taster sperren
    # Menueleiste im "Developer Mode" (Aufnahme + Entwicklung) ganz ausblenden -
    # sichtbar nur in Live-View und Sentence-Display.
    top_bar.withdraw()

    stoppe_live_bild()

    # Sofort Feedback geben, bevor die Aufnahme kurz blockiert
    setze_ergebnis("Capturing...", schreibmaschine=False)
    root.deiconify()
    root.update()

    # 800x600 reicht dem VLM voellig und halbiert die Bild-Rechenarbeit.
    # --shutter 20000: feste Verschlusszeit 1/50s gegen Verwackeln. Der Gain
    # bleibt automatisch, passt die Helligkeit also ans Licht an (hell genug
    # auch in dimmen Raeumen, sauberer bei viel Licht). 1/50s statt der
    # Automatik-typischen 1/15s = 3x kuerzer, friert normale Bewegung ein.
    # -t 1500: Einschwingzeit fuer Weissabgleich/Gain vor dem Ausloesen.
    os.system(f"rpicam-still -t 1500 --shutter 20000 "
              f"--width 800 --height 600 -o {BILD_PFAD} -n")

    anzeige_name = aktuelles_modell.split('/')[-1]   # voller Tag fuer den Vergleich
    if len(anzeige_name) > 28:
        anzeige_name = anzeige_name[:28] + "..."

    # Alte Kennzahlen entfernen, damit wir nach dem Lauf nur die frischen lesen.
    try:
        os.remove(STATS_PFAD)
    except FileNotFoundError:
        pass

    process = subprocess.Popen(
        ['python3', VISION_SCRIPT, aktuelles_modell],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    lbl_text.pack_forget()
    frame_warten.pack(expand=True)

    SANDUHR_ZYKLUS = 1.6    # Sekunden pro Sandkorn-Fall
    zaehler = 0.0
    while process.poll() is None:
        time.sleep(0.4)
        zaehler += 0.4
        punkte = "." * (int(zaehler / 0.5) % 4)
        lbl_warten.configure(
            text=f"{anzeige_name} is developing the photo.\nPlease wait{punkte}")
        zeichne_sanduhr((zaehler % SANDUHR_ZYKLUS) / SANDUHR_ZYKLUS)
        root.update()
        if zaehler > 900:   # 15 Minuten Geduld, dann Abbruch
            process.kill()
            break

    frame_warten.pack_forget()
    lbl_stats.pack(side='bottom', pady=(0, 14))   # kleine Kennzahlen ganz unten
    lbl_text.pack(expand=True, fill="both", padx=60, pady=(30, 6))

    stdout, stderr = process.communicate()
    ki_antwort = stdout.strip()

    if not ki_antwort:
        if stderr:
            ki_antwort = f"System Error:\n\n{stderr.strip()}"
        else:
            ki_antwort = "The vision script returned an empty result. Please try again."

    setze_ergebnis(ki_antwort, schreibmaschine=len(ki_antwort) < 200)
    lbl_stats.configure(text=_stats_text())   # Technik-Details unter dem Satz
    root.deiconify()
    top_bar.deiconify()   # Sentence-Display: Leiste wieder einblenden

    # Taster erst nach 1 s wieder freigeben (nervöse Finger)
    root.after(1000, taster_freigeben)

def taster_freigeben():
    global ist_am_analysieren
    ist_am_analysieren = False

def fahre_herunter():
    stoppe_live_bild()
    top_bar.withdraw()
    root.deiconify()
    setze_ergebnis("Shutting down...", schreibmaschine=False)
    root.update()
    os.system("sudo shutdown -h now")

# --- TASTER-LOGIK ---
# Ausloesung passiert sofort beim DRUECKEN (wie bei einer echten Kamera),
# nicht erst beim Loslassen. Das 3-Sekunden-Halten fuer "Herunterfahren"
# wird weiterhin ueber die gemessene Haltedauer beim Loslassen erkannt.
def gpio_check_schleife():
    global druck_start
    try:
        if request.wait_edge_events(timeout=datetime.timedelta(milliseconds=50)):
            for ev in request.read_edge_events():
                if ev.event_type == ev.Type.FALLING_EDGE:
                    druck_start = ev.timestamp_ns
                    if not ist_am_analysieren:
                        if root.winfo_viewable():
                            root.withdraw()
                            starte_live_bild()
                        else:
                            foto_schiessen()
                elif ev.event_type == ev.Type.RISING_EDGE and druck_start is not None:
                    dauer = (ev.timestamp_ns - druck_start) / 1e9
                    druck_start = None
                    if dauer >= 3.0:
                        fahre_herunter()
    except Exception as e:
        print(f"GPIO Error: {e}")
    root.after(50, gpio_check_schleife)

# --- WARM-UP: Standardmodell im Hintergrund in den RAM laden ---
# Nur das Standardmodell, nicht alle - gleichzeitiges Laden mehrerer grosser
# Modelle hat frueher zu RAM-Druck und Verlangsamung gefuehrt. Andere Modelle
# laden beim ersten Wechsel einmalig kalt nach.
def modelle_vorwaermen():
    # Best-effort: laeuft parallel zum Start der Kamera-Vorschau und kann daher
    # (Speicherbus-Konkurrenz) lange dauern. Grosszuegiger Timeout, Fehler
    # werden geschluckt - schlimmstenfalls laedt das Modell beim ersten Foto.
    subprocess.Popen(['python3', '-c',
        "import json, urllib.request\n"
        "try:\n"
        "    req = urllib.request.Request('http://127.0.0.1:11434/api/generate',\n"
        "        data=json.dumps({'model': 'ministral-3:3b', 'prompt': 'ok',\n"
        "                         'keep_alive': '24h'}).encode(),\n"
        "        headers={'Content-Type': 'application/json'})\n"
        "    urllib.request.urlopen(req, timeout=900)\n"
        "except Exception:\n"
        "    pass\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

# --- START ---
starte_live_bild()
modelle_vorwaermen()
akku_aktualisieren()
# Update-Pruefung einmal beim Start, leicht verzoegert, damit sie nicht mit dem
# Kamerastart konkurriert. Ohne WLAN schlaegt sie still fehl.
root.after(8000, lambda: pruefe_updates())
root.after(50, gpio_check_schleife)
root.mainloop()
