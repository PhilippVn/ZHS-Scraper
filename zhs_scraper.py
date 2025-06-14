"""
This Python script is a web scraper and notifier that monitors ZHS Munich's gym course page and sends email alerts when course availability or status changes (e.g., from "Warteliste" to "buchen").

🧠 Core Functionalities
Scrape course information from the ZHS website.

Detect changes in course availability/status.

Send an email notification if there are any changes.

Log all events (info/debug/errors).

Run periodically in an infinite loop.
"""

import requests
from bs4 import BeautifulSoup
import smtplib
from email.mime.text import MIMEText
import time
import json
import os
import argparse
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
load_dotenv()
import os

# Konfiguration
URL = "https://www.buchung.zhs-muenchen.de/angebote/aktueller_zeitraum_0/_Krafttraining_-_Studio.html"
INTERVAL = 10*60  # alle 10 Minuten
STATE_FILE = "kurs_status.json"

SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))  # fallback optional
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM")
EMAIL_TO = os.getenv("EMAIL_TO")

ERROR_LOG_FILE = 'error_log.json'
ERROR_INTERVAL_HOURS = 24

def setup_logger(debug=False):
    """
    Initialisiert das Logging mit rotierender Logdatei und optionaler Konsolenausgabe im Debug-Modus.
    """
    log_level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger()
    logger.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Datei-Logging mit Rotation (max. 5 MB, 3 Backups)
    file_handler = RotatingFileHandler('zhs_scraper.log', maxBytes=5 * 1024 * 1024, backupCount=3)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Konsolen-Logging nur im Debug-Modus
    if debug:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)


def scrape_kurse():
    """
    Lädt und parst die Kursliste von der ZHS-Webseite.

    Extrahiert: kurs_nr, details, tag, zeit, zeitraum, leitung, preis, status

    Rückgabewert: Liste von Kurs-Dictionaries oder None bei Fehler.
    """
    try:
        response = requests.get(URL, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        table = soup.select_one('table.bs_kurse')  # Haupt-Tabelle mit Kursen finden
        if not table:
            sende_error_email("Tabelle nicht gefunden", "Die Kurs-Tabelle konnte nicht auf der Seite gefunden werden.")
            return None

        kurse = []
        for row in table.select('tbody tr'):
            cols = row.find_all('td')
            # Inhalte der Spalten extrahieren
            kurs_info = {
                "kurs_nr": cols[0].get_text(strip=True),
                "details": cols[1].get_text(strip=True),
                "tag": cols[2].get_text(strip=True),
                "zeit": cols[3].get_text(strip=True),
                "zeitraum": cols[5].get_text(strip=True),
                "leitung": cols[6].get_text(strip=True),
                "preis": cols[7].get_text(strip=True),
            }

            # Status anhand des HTML-Aufbaus bestimmen
            buchung = cols[8]
            if buchung.find('span', class_='bs_btn_abgelaufen'):
                kurs_info["status"] = "abgelaufen"
            elif buchung.find('input', class_='bs_btn_warteliste'):
                kurs_info["status"] = "Warteliste"
            elif buchung.find('input', class_='bs_btn_buchen'):
                kurs_info["status"] = "buchen"
            else:
                kurs_info["status"] = "unbekannt"

            logging.debug(f"Kurs gescraped: {kurs_info}")
            kurse.append(kurs_info)

        return kurse
    except Exception as e:
        logging.exception(f"Fehler beim Scrapen: {e}")
        return None


def lade_zustand():
    """
    Lädt den zuletzt gespeicherten Kurszustand aus einer JSON-Datei.
    Gibt {} zurück, wenn keine Datei vorhanden ist.
    """
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            zustand = json.load(f)
            logging.debug("Zustand geladen.")
            return zustand
    logging.debug("Keine vorherige Zustandsdatei gefunden.")
    return {}


def speichere_zustand(zustand):
    """
    Speichert den aktuellen Kurszustand in die JSON-Datei.
    """
    with open(STATE_FILE, 'w') as f:
        json.dump(zustand, f, indent=2)
    logging.debug("Zustand gespeichert.")


def vergleiche_kurse(alt, neu):
    """
    Vergleicht den alten und den neuen Kursstand.

    Gibt eine Liste von Änderungen zurück:
    - neue Kurse (nur wenn 'buchen' oder 'Warteliste')
    - Statusänderungen (nur wenn neu verfügbar)
    - gelöschte Kurse (immer)
    """
    aenderungen = []
    alter_kurse = {kurs['kurs_nr']: kurs for kurs in alt}
    neue_kurse = {kurs['kurs_nr']: kurs for kurs in neu}

    # Neue Kurse erkennen
    for kurs_nr, kurs in neue_kurse.items():
        if kurs_nr not in alter_kurse:
            if kurs['status'] in ('buchen', 'Warteliste'):
                aenderungen.append({
                    "typ": "neu",
                    "kurs_nr": kurs_nr,
                    "neuer_status": kurs['status'],
                    "details": kurs
                })
        else:
            # Statusänderung erkennen, nur wenn neu buchbar
            alt_status = alter_kurse[kurs_nr]['status']
            neu_status = kurs['status']
            if alt_status != neu_status:
                if neu_status in ('buchen', 'Warteliste') and alt_status not in ('buchen', 'Warteliste'):
                    aenderungen.append({
                        "typ": "status_update",
                        "kurs_nr": kurs_nr,
                        "alter_status": alt_status,
                        "neuer_status": neu_status,
                        "details": kurs
                    })

    # Gelöschte Kurse erkennen
    for kurs_nr, kurs in alter_kurse.items():
        if kurs_nr not in neue_kurse:
            aenderungen.append({
                "typ": "geloescht",
                "kurs_nr": kurs_nr,
                "alter_status": kurs['status'],
                "neuer_status": "entfernt",
                "details": kurs
            })

    return aenderungen


def sende_email(aenderungen):
    """
    Sendet eine Email mit den Änderungen.

    Gruppiert die Änderungen in drei Abschnitte:
    - Neue Kurse
    - Verfügbarkeitsänderungen
    - Gelöschte Kurse

    Lässt leere Abschnitte weg.
    """
    if not aenderungen:
        return

    neue = [a for a in aenderungen if a["typ"] == "neu"]
    geloescht = [a for a in aenderungen if a["typ"] == "geloescht"]
    updates = [a for a in aenderungen if a["typ"] == "status_update"]

    nachricht = ""
    if neue:
        nachricht += "🟢 Neue buchbare Kurse:\n\n"
        for a in neue:
            nachricht += (
                f"Kursnummer: {a['kurs_nr']}\n"
                f"Status: {a['neuer_status']}\n"
                f"Tag: {a['details']['tag']}\n"
                f"Zeit: {a['details']['zeit']}\n"
                f"Zeitraum: {a['details']['zeitraum']}\n"
                f"Leitung: {a['details']['leitung']}\n"
                f"Preis: {a['details']['preis']}\n"
                f"{'-'*40}\n"
            )
        nachricht += "\n"

    if updates:
        nachricht += "🔁 Verfügbarkeitsänderungen:\n\n"
        for a in updates:
            nachricht += (
                f"Kursnummer: {a['kurs_nr']}\n"
                f"Status: {a['alter_status']} → {a['neuer_status']}\n"
                f"Tag: {a['details']['tag']}\n"
                f"Zeit: {a['details']['zeit']}\n"
                f"Zeitraum: {a['details']['zeitraum']}\n"
                f"Leitung: {a['details']['leitung']}\n"
                f"Preis: {a['details']['preis']}\n"
                f"{'-'*40}\n"
            )
        nachricht += "\n"

    if geloescht:
        nachricht += "❌ Entfernte Kurse:\n\n"
        for a in geloescht:
            nachricht += (
                f"Kursnummer: {a['kurs_nr']}\n"
                f"Letzter bekannter Status: {a['alter_status']}\n"
                f"Tag: {a['details']['tag']}\n"
                f"Zeit: {a['details']['zeit']}\n"
                f"Zeitraum: {a['details']['zeitraum']}\n"
                f"Leitung: {a['details']['leitung']}\n"
                f"Preis: {a['details']['preis']}\n"
                f"{'-'*40}\n"
            )
        nachricht += "\n"

    if not nachricht.strip():
        return

    # Email zusammenbauen
    msg = MIMEText(nachricht)
    msg['Subject'] = f"ZHS Kursänderung ({len(aenderungen)} Änderungen)"
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO

    # Senden
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        logging.info("E-Mail erfolgreich gesendet.")
    except Exception as e:
        logging.exception(f"Fehler beim Senden der E-Mail: {e}")

def sende_error_email(betreff, fehlertext):
    """
    Sendet eine Fehlerbenachrichtigung per Mail – maximal einmal pro 24h pro Fehlertyp.
    """
    now = datetime.now()

    # Vorherige Fehlermeldungen laden
    error_log = {}
    if os.path.exists(ERROR_LOG_FILE):
        try:
            with open(ERROR_LOG_FILE, 'r') as f:
                raw = json.load(f)
                error_log = {k: datetime.fromisoformat(v) for k, v in raw.items()}
        except Exception as e:
            logging.warning(f"Fehler beim Laden der Fehlerlog-Datei: {e}")

    last_sent = error_log.get(betreff)
    if last_sent and (now - last_sent).total_seconds() < ERROR_INTERVAL_HOURS * 3600:
        logging.info(f"Fehlermeldung '{betreff}' wurde bereits heute gesendet – überspringe.")
        return

    # Mail vorbereiten
    msg = MIMEText(fehlertext)
    msg['Subject'] = f"[ZHS-Scraper ERROR] {betreff}"
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
        logging.info(f"Fehlermeldung '{betreff}' gesendet.")
        error_log[betreff] = now.isoformat()

        # Fehlerlog speichern
        try:
            with open(ERROR_LOG_FILE, 'w') as f:
                json.dump({k: v.isoformat() for k, v in error_log.items()}, f, indent=2)
        except Exception as e:
            logging.warning(f"Fehler beim Speichern der Fehlerlog-Datei: {e}")

    except Exception as e:
        logging.exception(f"Fehler beim Senden der Fehler-E-Mail: {e}")


def main():
    """
    Hauptfunktion: prüft regelmäßig auf Kursänderungen und benachrichtigt per Mail.
    """
    parser = argparse.ArgumentParser(description="ZHS Kursüberwachung")
    parser.add_argument("-d", "--debug", action="store_true", help="Aktiviere Debug-Modus")
    args = parser.parse_args()

    setup_logger(debug=args.debug)

    logging.info("ZHS-Kursüberwachung gestartet.")
    logging.info(f"Überprüfungsintervall: {INTERVAL // 60} Minuten")

    while True:
        try:
            kurse = scrape_kurse()
            if not kurse:
                logging.info("Keine Kurse gefunden.")
                sende_error_email("Keine Kurse gefunden", "Scraping erfolgreich, aber keine Kurse in der Tabelle.")
                time.sleep(INTERVAL)
                continue

            zustand = lade_zustand()
            aenderungen = vergleiche_kurse(zustand.get('kurse', []), kurse)

            if aenderungen:
                logging.info(f"{len(aenderungen)} Änderung(en) erkannt.")
                sende_email(aenderungen)
            else:
                logging.info("Keine Änderungen erkannt.")

            # Gelöschte Kurse aus Cache entfernen
            entfernte_kurse = {a['kurs_nr'] for a in aenderungen if a['typ'] == 'geloescht'}
            neuer_zustand = {
                'kurse': [kurs for kurs in kurse if kurs['kurs_nr'] not in entfernte_kurse],
                'letzte_pruefung': str(datetime.now())
            }
            speichere_zustand(neuer_zustand)

        except Exception as e:
            logging.exception("Fehler im Hauptprozess")
            sende_error_email("Exception im Hauptprozess", str(e))

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
