@echo off
echo ============================================
echo  Build Mise-O-Jeu Analyzer (.exe)
echo ============================================
venv\Scripts\pyinstaller.exe miseojeu.spec --noconfirm
if %errorlevel% == 0 (
    echo.
    echo BUILD REUSSI : dist\MiseOJeu.exe
) else (
    echo.
    echo ECHEC DU BUILD - voir les erreurs ci-dessus
)
pause
