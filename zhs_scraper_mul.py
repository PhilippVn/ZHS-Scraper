import os
import time
import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from collections import defaultdict
import argparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from dotenv import load_dotenv

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# --- Konfiguration laden -------------------------------------------------
load_dotenv()
CONFIG_FILE     = "config.json"
STATE_FILE      = "kurs_status.json"
ERROR_LOG_FILE  = "error_log.json"

SMTP_SERVER     = os.getenv("SMTP_SERVER")
SMTP_PORT       = int(os.getenv("SMTP_PORT", 587))
SMTP_USER       = os.getenv("SMTP_USER")
SMTP_PASSWORD   = os.getenv("SMTP_PASSWORD")
EMAIL_FROM      = os.getenv("EMAIL_FROM")
EMAIL_TO        = os.getenv("EMAIL_TO", "").split(",")  # Mehrere Empfänger

# --- Logger Setup ---------------------------------------------------------
def setup_logger(debug=False):
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(level)
    fmt = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    fh = RotatingFileHandler('zhs_scraper.log', maxBytes=5*1024*1024, backupCount=3)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    if debug:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        logger.addHandler(ch)

# --- HTTP Session mit Retry -----------------------------------------------
def make_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1,
                    status_forcelist=[500, 502, 503, 504]) # Session mit exponential Backoff
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

SESSION = make_session()

# --- Error-Log Handling ---------------------------------------------------
def load_error_log():
    if os.path.exists(ERROR_LOG_FILE):
        with open(ERROR_LOG_FILE, 'r') as f:
            return json.load(f)
    return { 'last_error_email': None, 'errors': [] }


def save_error_log(log_data):
    with open(ERROR_LOG_FILE, 'w') as f:
        json.dump(log_data, f, indent=2, default=str)


def handle_error(subject, message):
    log_data = load_error_log()
    now = datetime.utcnow()
    # Fehler protokollieren
    log_data['errors'].append({
        'timestamp': now.isoformat(),
        'subject': subject,
        'message': message
    })
    # Prüfen, ob wir in der letzten Stunde eine Mail gesendet haben
    last = log_data.get('last_error_email')
    if last:
        last_dt = datetime.fromisoformat(last)
    else:
        last_dt = now - timedelta(hours=2)

    if now - last_dt >= timedelta(hours=1):
        send_error_email(subject, message)
        log_data['last_error_email'] = now.isoformat()

    save_error_log(log_data)

# --- E-Mail Versand --------------------------------------------------------
def send_email_raw(subject, body, html_body=None):
    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_FROM
    msg['To'] = ", ".join(EMAIL_TO)
    msg['Subject'] = subject

    part1 = MIMEText(body, 'plain', 'utf-8')
    msg.attach(part1)
    if html_body:
        part2 = MIMEText(html_body, 'html', 'utf-8')
        msg.attach(part2)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        logging.info(f"E-Mail gesendet an {EMAIL_TO} mit Betreff: {subject}")
    except Exception as e:
        logging.error(f"Fehler beim Senden der E-Mail: {e}")


def send_error_email(subject, message):
    body = f"Fehler im ZHS Kurs-Scraper:\n\n{message}\n\nZeit: {datetime.now().isoformat()}"
    html = f"<p><b>Fehler im ZHS Kurs-Scraper:</b></p><p>{message}</p><p>Zeit: {datetime.now().isoformat()}</p>"
    send_email_raw(subject, body, html)

# --- Config laden ---------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"Konfigurationsdatei {CONFIG_FILE} nicht gefunden!")
        return None
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

# --- State Handling -------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return { 'kurse': [] }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# --- Scraping-Funktionen --------------------------------------------------
def scrape_tabelle(soup, idx, label=None):
    tables = soup.select('table.bs_kurse') # TODO konfigurierbar?
    if idx >= len(tables):
        logging.warning(f"Tabelle Index {idx} nicht gefunden.")
        return []
    table = tables[idx] # Tabelle nach index

    # Header-Erkennung
    headers = [th.get_text(strip=True) for th in table.select('thead th')] # Header‑Zeilen erkennen (variabel!). Zuerst thead th.
    if not headers:
        # Suche erste Zeile tr mit <th>
        first_th_row = table.find('tr', lambda r: r.find_all('th'))
        if first_th_row:
            headers = [th.get_text(strip=True) for th in first_th_row.find_all('th')]
        else:
            # fallback auf erste Datenzeile
            first_row = table.select_one('tr') # TODO wollen wir das?
            headers   = [td.get_text(strip=True) for td in first_row.find_all('td')]

    kurse = []
    for row in table.select('tbody tr'): # Iteriere über alle Kurse/Zeilen
        cols = row.find_all('td')
        if len(cols) != len(headers):
            logging.debug("Spaltenanzahl passt nicht zu Header, überspringe Zeile.")
            continue
        info = { headers[i]: cols[i].get_text(strip=True) for i in range(len(headers)) }

        # Status ermitteln
        last_cell = cols[-1]
        if last_cell.find('span', class_='bs_btn_abgelaufen'):
            info['status'] = 'abgelaufen'
        elif last_cell.find('input', class_='bs_btn_warteliste'):
            info['status'] = 'Warteliste'
        elif last_cell.find('input', class_='bs_btn_buchen'):
            info['status'] = 'buchen'
        elif last_cell.find('span', class_='bs_btn_autostart'):
            info['status'] = 'buchbar_ab'
        else:
            info['status'] = 'unbekannt'

        info['tabellenname'] = label or f"Tabelle_{idx}" # Füge zu Kurs den Tabellennamen hinzu oder Index falls nicht angegeben
        kurse.append(info)
    return kurse


def scrape_kurs(cfg):
    try:
        r = SESSION.get(cfg['url'], timeout=10) # Session für Kurs URL mit Retry/Backoff, damit bei kurzen Server‑Hängern automatisch neu versucht wird.
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        all_kurse = []
        for tab in cfg.get('tabellen', []): # Untertabellen/Unterkurse iterieren
            kurse = scrape_tabelle(soup, tab['index'], tab.get('bezeichnung')) # Scrape Kurse in Tabelle,  extrahiert alle Zeilen dieser einzelnen Tabelle.
            for k in kurse: # Nach dem Parsen jeder Zeile hängen wir noch kursname und url an.
                k['kursname'] = cfg['name']
                k['url']       = cfg['url']
            all_kurse.extend(kurse)
        return all_kurse
    except Exception as e:
        logging.exception(f"Fehler beim Scrapen von {cfg['name']}: {e}")
        handle_error(f"Scraping-Fehler {cfg['name']}", str(e))
        return []

# --- Vergleich und Updates ------------------------------------------------
# eindeutige Kurs‑ID aus Nummer/Feld‑Kombination falls keine Kursnummer angegeben (für hashmap)
def headers_key(k):
    for key in ['Nr.', 'Kursnummer', 'kurs_nr', 'Nr', 'KursnrNo.']:
        if key in k:
            return k[key]
    return f"{k.get('Tag','')}_{k.get('Zeit','')}_{k.get('Leitung','')}"

# Änderungen zwischen old und new basierend auf interessanten Statusupdates interesting
def compare_kurse(old, new, interesting):
    changes = []
    old_map = { (k['kursname'], k['tabellenname'], headers_key(k)): k for k in old }
    new_map = { (k['kursname'], k['tabellenname'], headers_key(k)): k for k in new }

    # neue oder Status-Updates
    for key, nk in new_map.items():
        if key not in old_map:
            if nk['status'] in interesting:
                changes.append({'typ': 'neu', 'kurs': nk})
        else:
            ok = old_map[key]
            if ok['status'] != nk['status'] and nk['status'] in interesting:
                changes.append({'typ': 'status_update', 'alt': ok, 'neu': nk})
    # gelöschte
    for key, ok in old_map.items():
        if key not in new_map:
            changes.append({'typ': 'geloescht', 'kurs': ok})
    return changes

# --- E-Mail mit Kurs-Updates ----------------------------------------------
def format_kurs_info(k):
    prio = ['KursnrNo.', 'TagDay', 'ZeitTime', 'OrtLocation', 'LeitungGuidance', 'PreisCost'] # Priorisierte Felder. Manche Felder sind besonders wichtig (z. B. Kurs‑Nummer, Tag, Zeit, Ort, Leitung, Preis). Wenn sie im Dictionary k existieren, werden sie in genau dieser Reihenfolge direkt unterhalb der Kopfzeile ausgegeben.
    lines = [ f"<b>{k['kursname']}</b> ({k['tabellenname']})<br>Status: {k['status']}<br>Link: <a href='{k['url']}'>{k['url']}</a><br>" ] # Kopfzeile mit Kurs‑Meta (fett)
    for p in prio:
        if p in k: lines.append(f"{p}: {k[p]}<br>")
    for kk, vv in k.items(): # Alle übrigen Felder
        if kk not in prio + ['kursname','tabellenname','url','status']:
            lines.append(f"{kk}: {vv}<br>")
    return ''.join(lines)

# Änderungen pro (kursname, tabellenname), damit eine Mail pro Untertabelle kommt.
def send_changes_emails(changes, config):
    if not changes:
        return
    grp = defaultdict(list) #  Erstellt ein Dictionary namens grp, dessen Werte automatisch als leere Listen initialisiert werden. Wir wollen alle Änderungen gruppieren nach (kursname, tabellenname), damit pro Untertabelle nur eine Mail kommt.
    for c in changes: # Für jede Änderung
        k = c['kurs'] if c['typ'] in ['neu','geloescht'] else c['neu'] # Wenn c['typ'] "neu" oder "geloescht" ist, steckt das Kurs‑Objekt unter c['kurs']. Bei "status_update" liegt das neue Kurs‑Objekt unter c['neu'].
        grp[(k['kursname'], k['tabellenname'])].append(c) # Wir fügen die Änderung c der Liste hinzu, die zum Schlüssel (kursname, tabellenname) gehört. Dadurch entsteht z.B. grp[("Krafttraining","Einweisung")] = [ Änderung1, Änderung2, … ]

    for (name, tab), items in grp.items(): # Iterieren über alle Gruppen von Änderungen. items = Liste aller Änderungs‑Dicts für genau diese Kombination
        subject = f"ZHS Kurs-Update: {name} - {tab}"
        text = f"Änderungen für {name} / {tab}:\n"
        html = "<h2>Änderungen für {}/{}:</h2>".format(name, tab)
        # neu
        neu = [c['kurs'] for c in items if c['typ']=='neu'] # Extrahiert alle Kurse, die neu hinzugekommen sind, in eine Liste neu
        if neu: # Neue Kurse => Neuer Abschnitt
            text += "Neue Kurse:\n"
            html += "<h3>🟢 Neue Kurse</h3>"
            for k in neu:
                text += format_kurs_info(k).replace('<br>','\n') + "\n\n" #  liefert einen HTML‑String mit <br> als Zeilen­umbruch. wandelt diese in echte Zeilen­umbrüche um.
                html += format_kurs_info(k)
        
        text += "\n\n"
        html += "<br><br>"

        # status_update
        upd = [c for c in items if c['typ']=='status_update'] # Extrahiert alle Status‑Änderungen in upd.
        if upd:
            text += "Status-Updates:\n"
            html += "<h3>🔁 Status-Updates</h3>"
            for c in upd:
                nk = c['neu']; ok = c['alt'] # Neuer Kurs und Alter Kurs
                line = format_kurs_info(nk) + f"Status: {ok['status']} → {nk['status']}<br><br>"
                text += line.replace('<br>','\n') + "\n\n"
                html += line

        text += "\n\n"
        html += "<br><br>"

        # deleted
        dl = [c['kurs'] for c in items if c['typ']=='geloescht'] # Extrahiere alle gelöschten Kurse in dl
        if dl:
            text += "Gelöschte Kurse:\n"
            html += "<h3>❌ Gelöschte Kurse</h3>"
            for k in dl:
                text += format_kurs_info(k).replace('<br>','\n') + "\n\n"
                html += format_kurs_info(k)
        send_email_raw(subject, text, html)

# --- Main Loop -------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ZHS Kurs-Scraper")
    parser.add_argument("-d", "--debug", action="store_true", help="Debug-Logging aktivieren")
    args = parser.parse_args()
    setup_logger(debug=args.debug)
    try:
        # Initialer Zustand
        state = load_state()  # Letzten Zustand aus Datei oder leer
        interval = None
        interesting = None

        while True:
            # Config bei jedem Durchlauf frisch einlesen
            config = load_config()
            if not config:
                logging.error("Keine gültige Konfiguration, breche ab.")
                return

            # Intervall und interessante Stati aus Config übernehmen
            interval = config.get("interval", interval or 600)
            interesting = config.get(
                "interesting_status",
                interesting or ["buchen", "Warteliste", "buchbar_ab"]
            )

            # Alle Kurse scrapen
            all_new = []
            for cfg in config.get('kurse', []):
                all_new.extend(scrape_kurs(cfg))

            # Änderungen ermitteln
            changes = compare_kurse(state.get('kurse', []), all_new, interesting)

            if changes:
                send_changes_emails(changes, config)
                save_state({'kurse': all_new})
                state = {'kurse': all_new}
            else:
                logging.info("Keine Änderungen gefunden.")

            logging.info(f"Warte {interval} Sekunden...")
            time.sleep(interval)

    except KeyboardInterrupt:
        logging.info("Scraper durch Benutzer beendet.")
    except Exception as e:
        logging.exception(f"Ungefangener Fehler im Haupt: {e}")
        handle_error("Ungefangener Fehler im Hauptloop", str(e))

if __name__ == "__main__":
    main()
