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
EMAIL_TO        = os.getenv("EMAIL_TO", "").split(",")

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
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

SESSION = make_session()

# --- Fehlerprotokollierung ------------------------------------------------ (Email Timeouts)
ERROR_TIMEOUTS = {
    'Scraping-Fehler': timedelta(hours=1),
    'Ungefangener Fehler im Hauptloop': timedelta(hours=1),
}


def load_error_log():
    if os.path.exists(ERROR_LOG_FILE):
        with open(ERROR_LOG_FILE, 'r') as f:
            return json.load(f)
    return { 'last_error_email': {}, 'errors': [] }


def save_error_log(log_data):
    with open(ERROR_LOG_FILE, 'w') as f:
        json.dump(log_data, f, indent=2, default=str)


def handle_error(subject, message):
    log_data = load_error_log() # Fehler-Log laden (json Datei)
    now = datetime.utcnow()
    log_data['errors'].append({'timestamp': now.isoformat(), 'subject': subject, 'message': message}) # Neuen Fehler-Eintrag mit Zeit, Betreff und Nachricht anhängen

    # Timeout für Fehler-Mail: Standard 1 Stunde, sonst spezifisch aus ERROR_TIMEOUTS
    timeout = ERROR_TIMEOUTS.get(subject.split()[0], timedelta(hours=1))
    last_sent = log_data['last_error_email'].get(subject) # Zeitpunkt der letzten Mail für diesen Fehler
    last_dt = datetime.fromisoformat(last_sent) if last_sent else now - timedelta(hours=2) 

     # Nur wenn seit letzter Mail der Timeout überschritten ist, neue Mail senden
    if now - last_dt >= timeout:
        send_error_email(subject, message)
        log_data['last_error_email'][subject] = now.isoformat()  # Zeit der letzten Mail aktualisieren

    save_error_log(log_data) # Fehler-Log mit neuem Eintrag speichern


# --- E-Mail Versand --------------------------------------------------------
def send_email_raw(subject, body, html_body=None):
    msg = MIMEMultipart('alternative')
    msg['From'] = EMAIL_FROM
    msg['To'] = ", ".join(EMAIL_TO)
    msg['Subject'] = subject

    msg.attach(MIMEText(body, 'plain', 'utf-8'))
    if html_body:
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

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
    html = f"<h1>Fehler im ZHS Kurs-Scraper:</h1><p>{message}</p><p>Zeit: {datetime.now().isoformat()}</p>"
    send_email_raw(subject, body, html)

# --- Config, State --------------------------------------------------------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        logging.error(f"Konfigurationsdatei {CONFIG_FILE} nicht gefunden!")
        return None
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return { 'kurse': [] }


def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# --- Scraping -------------------------------------------------------------
# Helper funktion um alle Kurse in Tabelle zu ziehen
def scrape_tabelle(soup, idx, label=None):
    tables = soup.select('table.bs_kurse') # Alle Tabellen mit Klasse 'bs_kurse' holen
    if idx >= len(tables):
        logging.warning(f"Tabelle Index {idx} nicht gefunden.")
        return []
    table = tables[idx] # Tabelle mit index holen

     # Tabellen-Header aus <thead> auslesen, falls nicht da, aus erster Datenzeile
    headers = [th.get_text(strip=True) for th in table.select('thead th')]
    if not headers:
        first_row = table.select_one('tr')
        headers = [td.get_text(strip=True) for td in first_row.find_all(['td', 'th'])]

    kurse = []
    for row in table.select('tbody tr'):
        cols = row.find_all('td')
        if len(cols) != len(headers):
            continue # Inkonsistente Zeilen überspringen

        # Kursdaten als dict mit Headern als Keys und Zellen als Werte
        info = { headers[i]: cols[i].get_text(strip=True) for i in range(len(headers)) }
        info['status'] = 'unbekannt'
        last_cell = cols[-1]
        if last_cell.find('span', class_='bs_btn_abgelaufen'):
            info['status'] = 'abgelaufen'
        elif last_cell.find('input', class_='bs_btn_warteliste'):
            info['status'] = 'Warteliste'
        elif last_cell.find('input', class_='bs_btn_buchen'):
            info['status'] = 'buchen'
        elif last_cell.find('span', class_='bs_btn_autostart'):
            info['status'] = 'buchbar_ab'
        info['tabellenname'] = label or f"Tabelle_{idx}"
        kurse.append(info)
    return kurse

# Parsed den Kurs mit seinen Unterkursen/Tabellen und versieht den Kurs mit Metadaten (Name, URL)
def scrape_kurs(cfg):
    try:
        r = SESSION.get(cfg['url'], timeout=10)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, 'html.parser')
        all_kurse = []
        for tab in cfg.get('tabellen', []): # Über alle definierten Tabellen iterieren
            kurse = scrape_tabelle(soup, tab['index'], tab.get('bezeichnung'))
            for k in kurse:
                k['kursname'] = cfg['name']
                k['url'] = cfg['url']
            all_kurse.extend(kurse)
        return all_kurse
    except Exception as e:
        logging.exception(f"Fehler beim Scrapen von {cfg['name']}: {e}")
        handle_error(f"Scraping-Fehler {cfg['name']}", str(e))
        return []

# --- Vergleich ------------------------------------------------------------
def headers_key(k):
    for key in ['Nr.', 'Kursnummer', 'kurs_nr', 'Nr', 'KursnrNo.']:
        if key in k:
            return k[key]
    return f"{k.get('Tag','')}_{k.get('Zeit','')}_{k.get('Leitung','')}" # Fallback: Kombination aus Tag, Zeit und Leitung als Schlüssel

# Vergleicht alte Kurse in Json mit neuen um interessante Statusädnerungen (buchbar) und neue/gelöschte kurse zu finden und gibt diese zurück
def compare_kurse(old, new, interesting):
    changes = []
     # Mapping mit (kursname, tabellenname, key) als Schlüssel
    old_map = { (k['kursname'], k['tabellenname'], headers_key(k)): k for k in old }
    new_map = { (k['kursname'], k['tabellenname'], headers_key(k)): k for k in new }

    for key, nk in new_map.items():
        if key not in old_map:
            if nk['status'] in interesting: # Nur interessante Statusänderungen aka. jetzt Buchbar oder Warteliste oder buchbar_ab.
                changes.append({'typ': 'neu', 'kurs': nk}) # Neuer Kurs
        else:
            ok = old_map[key]
            if ok['status'] != nk['status'] and nk['status'] in interesting:
                changes.append({'typ': 'status_update', 'alt': ok, 'neu': nk}) # Statusänderung

    for key, ok in old_map.items():
        if key not in new_map:
            changes.append({'typ': 'geloescht', 'kurs': ok}) # Gelöschter Kurs

    return changes

# --- E-Mail Formatting ----------------------------------------------------
# formatiert einen Kurs als HTML-String (bzw. Plaintext als Fallback) mit wichtigen Feldern in priorisierter Reihenfolge und allem Rest darunter.
def format_kurs_info(k):
    prio = ['KursnrNo.', 'TagDay', 'ZeitTime', 'OrtLocation', 'LeitungGuidance', 'PreisCost']
    lines = [f"<b>{k['kursname']}</b> ({k['tabellenname']})<br>Status: {k['status']}<br><a href='{k['url']}'>{k['url']}</a><br>"]
    for p in prio:
        if p in k:
            lines.append(f"{p}: {k[p]}<br>")
    for kk, vv in k.items():
        if kk not in prio + ['kursname', 'tabellenname', 'url', 'status']:
            lines.append(f"{kk}: {vv}<br>")
    return ''.join(lines)

# gruppiert die Änderungen nach Kurs und Tabelle, erstellt eine schöne HTML-/Text-Mail mit Überschriften und Auflistungen der neuen, geänderten oder gelöschten Kurse und versendet diese Mail.
def send_changes_email(changes):
    if not changes:
        return
    
    # changes ist eine Liste von Kursänderungen. Jede Änderung ist ein Dict mit unterschiedlichen Feldern, z.B.:
    # Bei neuen oder gelöschten Kursen: {'typ': 'neu', 'kurs': {... Kurs-Daten ...}}
    # Bei Statusänderungen: {'typ': 'status_update', 'alt': {...}, 'neu': {...}}

    # Gruppiert die Kurse mit ihren Tabllen/Unterkursen
    structured = defaultdict(lambda: defaultdict(list)) # Sorgt dafür, dass sich neue Unter-Dicts und Listen automatisch anlegen, wenn sie noch nicht existieren.
    for c in changes:
        # Kurs-Datensatz herausholen
        # Bei neuen oder gelöschten Kursen ist der Kurs unter c['kurs']
        # Bei Statusänderungen ist der neue Kurs (der relevant für die Gruppierung ist) unter c['neu']
        k = c['kurs'] if c['typ'] in ['neu', 'geloescht'] else c['neu']
        structured[k['kursname']][k['tabellenname']].append(c) # Änderung c in die passende Gruppe eingeordnet, nämlich unter structured[kursname][tabellenname]

    subject = "ZHS Kurs-Update: Gesamtübersicht"
    # Wir erstellen zwei Versionen des E-Mail-Inhalts:  
    # - 'html' für formatierte Darstellung mit Überschriften, Links und Zeilenumbrüchen  
    # - 'body' als reine Textversion für E-Mail-Clients, die kein HTML unterstützen  
    # So wird sichergestellt, dass die Nachricht bei allen Empfängern korrekt und lesbar ankommt.
    body = ""
    html = ""
    for kursname, tabellen in structured.items():
        html += f"<h1>{kursname}</h1>"
        body += f"{kursname}\n\n"
        for tabname, items in tabellen.items():
            html += f"<h2>{tabname}</h2>"
            body += f"{tabname}\n"
            for c in items:
                if c['typ'] == 'neu':
                    html += "<h3>🟢 Neuer Kurs</h3>" + format_kurs_info(c['kurs']) + "<br><br>"
                    body += format_kurs_info(c['kurs']).replace('<br>', '\n') + "\n\n"
                elif c['typ'] == 'status_update':
                    nk = c['neu']
                    ok = c['alt']
                    html += f"<h3>🔁 Statusänderung</h3>" + format_kurs_info(nk) + f"Status: {ok['status']} → {nk['status']}<br><br>"
                    body += format_kurs_info(nk).replace('<br>', '\n') + f"\nStatus: {ok['status']} → {nk['status']}\n\n"
                elif c['typ'] == 'geloescht':
                    html += "<h3>❌ Gelöscht</h3>" + format_kurs_info(c['kurs']) + "<br><br>"
                    body += format_kurs_info(c['kurs']).replace('<br>', '\n') + "\n\n"
            body += "\n"

    send_email_raw(subject, body, html)

# --- Main Loop ------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ZHS Kurs-Scraper")
    parser.add_argument("-d", "--debug", action="store_true", help="Debug-Logging aktivieren")
    args = parser.parse_args()
    setup_logger(debug=args.debug)

    try:
        state = load_state()
        interval = None
        interesting = None

        while True:
            config = load_config()
            if not config:
                logging.error("Keine gültige Konfiguration, breche ab.")
                return

            interval = config.get("interval", interval or 600)
            interesting = config.get("interesting_status", interesting or ["buchen", "Warteliste", "buchbar_ab"])

            all_new = []
            for cfg in config.get('kurse', []):
                try:
                    all_new.extend(scrape_kurs(cfg))
                except Exception as e:
                    logging.error(f"Kurs {cfg['name']} ausgelassen: {e}")

            changes = compare_kurse(state.get('kurse', []), all_new, interesting)

            if changes:
                send_changes_email(changes)
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
