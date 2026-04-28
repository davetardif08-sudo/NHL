#!/bin/bash
# Sur Railway : on utilise le Chromium nixpkgs (installé via nixpacks.toml)
# Pas besoin de télécharger le Chromium de Playwright (qui manque libglib sur Railway)
# En local Windows/Mac : Playwright utilise son propre Chromium automatiquement

if which chromium > /dev/null 2>&1; then
    echo ">> Chromium système trouvé : $(which chromium) — skip playwright install"
else
    echo ">> Pas de Chromium système — installation Playwright..."
    python -m playwright install chromium
fi

# Start the Flask app
python app.py
