#!/bin/bash
# Cherche le Chromium système (installé par nixpkgs)
CHROM=$(which chromium 2>/dev/null || which chromium-browser 2>/dev/null)

if [ -n "$CHROM" ]; then
    echo ">> Chromium système: $CHROM — skip playwright install"
    export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="$CHROM"
else
    echo ">> Pas de Chromium système dans PATH — installation Playwright..."
    python -m playwright install chromium
fi

# Start the Flask app
python app.py
