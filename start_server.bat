@echo off
REM Mise-O-Jeu Analyzer — Démarrage silencieux (fenêtre minimisée)
cd /d "C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer"
call venv\Scripts\activate.bat
start "MiseOJeu-Analyzer" /min cmd /c "python app.py >> server.log 2>&1"
