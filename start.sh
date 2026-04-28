#!/bin/bash
# Installe les libs système ET le binaire Chromium au démarrage.
# Railway ne persiste pas les apt packages entre build et runtime.
python -m playwright install --with-deps chromium
python app.py
