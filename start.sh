#!/bin/bash
# Libs système installées au build (nixpacks.toml phases.install --with-deps)
# Le binaire Chromium doit être téléchargé au démarrage (pas persisté entre builds)
python -m playwright install chromium
python app.py
