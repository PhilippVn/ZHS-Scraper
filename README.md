# 📌 ZHS-Scraper-Multi
ZHS-Scraper-Multi ist ein Python-Script, das regelmäßig definierte Kursseiten des Zentralen Hochschulsports (ZHS) abruft, analysiert und dich per E-Mail informiert, wenn sich der Buchungsstatus eines Kurses ändert (z. B. wenn ein Kurs plötzlich buchbar wird).

## ✨ Features
⏱️ Regelmäßiges Monitoring von beliebig vielen Kursen und Tabellen

📩 Automatische Benachrichtigung per E-Mail bei Änderungen (buchbar, Warteliste, neue Kurse etc.)

🔎 Statusvergleich: Neue, gelöschte oder geänderte Kurse werden erkannt

💾 Zustandsspeicherung, um nur neue Änderungen zu melden

🧱 Konfigurierbar über config.json

📉 Automatisches Logging & Fehler-E-Mail bei Problemen

## 🛠️ Setup
1. Abhängigkeiten installieren (Setup ohne Docker): `pip install -r requirements.txt`
2. .env Datei anlegen
Erstelle eine `.env` Datei mit deinen SMTP-Zugangsdaten:
```
SMTP_SERVER=smtp.example.com
SMTP_PORT=587
SMTP_USER=deinbenutzer@example.com
SMTP_PASSWORD=deinpasswort
EMAIL_FROM=deinbenutzer@example.com
EMAIL_TO=ziel1@example.com,ziel2@example.com
```

1. Konfiguration anlegen
Beispiel für eine `config.json`:

```json
{
  "interval": 600,
  "error_timeouts": {
    "Scraping-Fehler": 3600,
    "Parsing-Fehler": 1800
  },
  "interesting_status": [
    "buchen",
    "Warteliste",
    "buchbar_ab"
  ],
  "kurse": [
    {
      "name": "Krafttraining",
      "url": "https://www.buchung.zhs-muenchen.de/angebote/aktueller_zeitraum_0/_Krafttraining_-_Studio.html",
      "tabellen": [
        {
          "index": 0,
          "bezeichnung": "Einweisung"
        },
        {
          "index": 1,
          "bezeichnung": "Studio"
        }
      ]
    }
    ]
}
```

## ▶️ Nutzung
1. Entweder direkt mit `python scraper.py`
2. Besser: Als Docker container: `docker compose up --build`
3. Für Debug-Logging mit Terminal-Ausgabe (standardmäßig aktiviert in Docker): `python scraper.py --debug`

## 🧠 Wie es funktioniert
Das Skript lädt regelmäßig alle in config.json konfigurierten Kursseiten.

Die HTML-Tabellen werden per BeautifulSoup geparst.

Der aktuelle Zustand wird mit dem letzten gespeicherten Zustand (kurs_status.json) verglichen.

Bei relevanten Änderungen wird eine strukturierte E-Mail gesendet.

Fehler (z. B. Timeout, Parsingfehler) werden geloggt und ggf. separat gemeldet (error_log.json).

## 📂 Dateien

| Datei              | Beschreibung                                        |
|--------------------|-----------------------------------------------------|
| `scraper.py`       | Hauptprogramm                                       |
| `.env`             | Umgebungsvariablen (SMTP-Login etc.)                |
| `config.json`      | Konfiguration der zu überwachenden Kurse            |
| `kurs_status.json` | Letzter bekannter Zustand (automatisch erzeugt)     |
| `error_log.json`   | Fehlerprotokoll für E-Mail-Timeouts usw.            |
| `zhs_scraper.log`  | Logfile mit Zeitstempeln                            |