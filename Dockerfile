FROM python:slim


WORKDIR /app

# Installiere Abhängigkeiten
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Code
COPY . .

# Starte das Skript
CMD ["python", "zhs_scraper_mul.py", "--debug"]
