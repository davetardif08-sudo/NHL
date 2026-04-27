"""
Point d'entrée pour l'exécutable PyInstaller.
Lance Flask sur le port 5000 et ouvre le navigateur automatiquement.
"""
import sys
import os
import threading
import webbrowser
import time


def resource_path(relative_path):
    """Retourne le chemin absolu vers une ressource (fonctionne en .exe et en dev)."""
    if hasattr(sys, '_MEIPASS'):
        # Mode PyInstaller : ressources extraites dans un dossier temporaire
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


def data_path(relative_path):
    """Chemin vers les fichiers de données (snapshots, predictions, etc.).
    Toujours relatif à l'emplacement de l'exe, pas dans _MEIPASS."""
    if hasattr(sys, '_MEIPASS'):
        # En mode exe, les données sont à côté de l'exe
        exe_dir = os.path.dirname(sys.executable)
        return os.path.join(exe_dir, relative_path)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)


# Injecter les chemins avant d'importer app
os.environ['MISEOJEU_TEMPLATE_FOLDER'] = resource_path('templates')
os.environ['MISEOJEU_STATIC_FOLDER']   = resource_path('static')
os.environ['MISEOJEU_DATA_DIR']         = data_path('.')

# Changer le répertoire de travail pour que les chemins relatifs (snapshots/, etc.) fonctionnent
if hasattr(sys, '_MEIPASS'):
    os.chdir(data_path('.'))

import app as flask_app


def open_browser():
    """Ouvre le navigateur après un court délai."""
    time.sleep(1.5)
    webbrowser.open('http://127.0.0.1:5000')


if __name__ == '__main__':
    # Lancer l'ouverture du navigateur dans un thread séparé
    threading.Thread(target=open_browser, daemon=True).start()

    print("=" * 50)
    print("  Mise-O-Jeu Analyzer")
    print("  http://127.0.0.1:5000")
    print("  Fermez cette fenêtre pour arrêter le serveur")
    print("=" * 50)

    flask_app.app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
