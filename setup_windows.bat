@echo off
REM ============================================================
REM  Mise-O-Jeu Analyzer — Configuration demarrage Windows
REM  Double-clique sur ce fichier pour installer les taches
REM ============================================================
setlocal

set APP_DIR=C:\Users\DaveTardif\Documents\Claude\miseojeu-analyzer
set PYTHON=%APP_DIR%\venv\Scripts\python.exe
set PYTHONW=%APP_DIR%\venv\Scripts\pythonw.exe

echo.
echo ============================================================
echo   Mise-O-Jeu Analyzer - Installation demarrage Windows
echo ============================================================
echo.

REM ── 1. Serveur Flask au demarrage de session ──────────────────────────────
echo [1/2] Tache : demarrage serveur a l'ouverture de session...

schtasks /Delete /TN "MiseOJeu-Serveur" /F 2>nul

schtasks /Create /TN "MiseOJeu-Serveur" ^
  /TR "\"%PYTHONW%\" \"%APP_DIR%\app.py\"" ^
  /SC ONLOGON ^
  /DELAY 0001:00 ^
  /F

if %ERRORLEVEL%==0 (
    echo    OK - Serveur demarrera automatiquement a chaque connexion.
) else (
    echo    Echec tache planifiee. Ajout dans le dossier Demarrage...
    copy "%APP_DIR%\start_server.bat" "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MiseOJeu-Serveur.bat"
    if %ERRORLEVEL%==0 (
        echo    OK - Raccourci ajoute dans le dossier Demarrage.
    ) else (
        echo    ERREUR - Impossible d'installer le demarrage automatique.
    )
)

echo.

REM ── 2. Notification quotidienne ──────────────────────────────────────────
echo [2/2] Tache : notification quotidienne a 9h00...

schtasks /Delete /TN "MiseOJeu-Notification" /F 2>nul

schtasks /Create /TN "MiseOJeu-Notification" ^
  /TR "\"%PYTHON%\" \"%APP_DIR%\notify.py\"" ^
  /SC DAILY ^
  /ST 09:00 ^
  /F

if %ERRORLEVEL%==0 (
    echo    OK - Notification chaque jour a 9h00.
) else (
    echo    ERREUR creation tache notification.
)

echo.
echo ============================================================
echo   Installation terminee!
echo.
echo   - Serveur    : demarre automatiquement a la connexion Windows
echo   - Notification: chaque jour a 9h00 si bonnes cotes disponibles
echo.
echo   Pour changer l'heure : Planificateur de taches > MiseOJeu-Notification
echo ============================================================
echo.

REM Ouvrir le planificateur pour confirmer
start taskschd.msc

pause
