FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Installer les dépendances Python
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Installer Playwright + Chromium + toutes ses libs système au BUILD time
# (évite un timeout de health-check au démarrage sur Fly.io)
RUN python -m playwright install --with-deps chromium

# Copier le code de l'app
COPY . .

EXPOSE 8080

CMD ["python", "app.py"]
