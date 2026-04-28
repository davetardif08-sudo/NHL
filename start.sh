#!/bin/bash
# Install Playwright's Chromium browser (libs provided by apt in nixpacks.toml)
python -m playwright install chromium

# Start the Flask app
python app.py
