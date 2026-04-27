@echo off
echo ================================
echo  Mise-O-Jeu Analyzer — Install
echo ================================
echo.

REM Creer un environnement virtuel
python -m venv venv
if errorlevel 1 (
    echo ERREUR: Python n'est pas installe ou introuvable.
    pause
    exit /b 1
)

REM Activer l'environnement
call venv\Scripts\activate.bat

REM Installer les dependances
echo Installation des dependances...
pip install -r requirements.txt

REM Installer Playwright et son navigateur Chromium
echo Installation de Playwright + Chromium...
playwright install chromium

echo.
echo ================================
echo  Installation terminee !
echo ================================
echo.
echo Pour lancer l'analyseur :
echo   venv\Scripts\activate
echo   python main.py
echo.
echo Options disponibles :
echo   python main.py --demo          (mode demo, sans internet)
echo   python main.py --hockey        (hockey seulement)
echo   python main.py --football      (football seulement)
echo   python main.py --top 15        (afficher top 15)
echo   python main.py --detail        (detail par match)
echo   python main.py --visible       (navigateur visible)
echo.
pause
