"""
Mise-O-Jeu — Notifications Windows pour bonnes cotes du jour.

Usage :
  python notify.py             → vérifie les cotes et envoie une notification si Excellent
  python notify.py --check     → affiche les cotes sans notification (debug)

Lancé automatiquement par le planificateur de tâches Windows.
Si le serveur Flask n'est pas démarré, il le démarre d'abord.
"""

import subprocess
import sys
import time
import urllib.request
import urllib.error
import json
import os
from datetime import datetime
from pathlib import Path

APP_DIR   = Path(__file__).parent
SERVER_URL = "http://localhost:5000"
PYTHON    = str(APP_DIR / "venv" / "Scripts" / "python.exe")
APP_PY    = str(APP_DIR / "app.py")
LOG_FILE  = APP_DIR / "notify.log"

CHECK_ONLY = "--check" in sys.argv


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _notify(title: str, message: str):
    """Envoie une notification toast Windows."""
    if CHECK_ONLY:
        _log(f"[NOTIFY] {title} — {message}")
        return
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=message,
            app_name="Mise-O-Jeu Analyzer",
            timeout=15,
        )
    except Exception as e:
        _log(f"Erreur notification: {e}")


def _server_is_up() -> bool:
    try:
        urllib.request.urlopen(f"{SERVER_URL}/api/status", timeout=3)
        return True
    except Exception:
        return False


def _start_server():
    """Démarre le serveur Flask en arrière-plan si pas déjà lancé."""
    _log("Démarrage du serveur Flask…")
    subprocess.Popen(
        [PYTHON, APP_PY],
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        cwd=str(APP_DIR),
    )
    # Attendre que le serveur soit prêt (max 60 s)
    for _ in range(30):
        time.sleep(2)
        if _server_is_up():
            _log("Serveur démarré.")
            return True
    _log("Serveur non disponible après 60 s.")
    return False


def _wait_for_data(max_wait: int = 180) -> dict | None:
    """Attend que /api/data soit prêt (max_wait secondes)."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            status_raw = urllib.request.urlopen(f"{SERVER_URL}/api/status", timeout=5).read()
            status = json.loads(status_raw)
            if status.get("status") == "ready":
                data_raw = urllib.request.urlopen(f"{SERVER_URL}/api/data", timeout=10).read()
                return json.loads(data_raw)
            if status.get("status") == "error":
                _log(f"Erreur serveur: {status.get('error')}")
                return None
            # "loading" ou "idle" → déclencher une actualisation si nécessaire
            if status.get("status") == "idle":
                try:
                    req = urllib.request.Request(
                        f"{SERVER_URL}/api/refresh",
                        data=b'{}',
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    urllib.request.urlopen(req, timeout=5)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(5)
    _log("Données non disponibles après le délai d'attente.")
    return None


# ─── Analyse des opportunités ─────────────────────────────────────────────────

def _check_opportunities(data: dict) -> list[dict]:
    """Filtre les cotes Excellent du jour."""
    today = datetime.now().strftime("%Y-%m-%d")
    opps = []
    for o in data.get("hockey", []):
        if o.get("recommendation", "").startswith("Excellent"):
            date = o.get("date") or ""
            if date == today or not date:
                opps.append(o)
    return opps


def _format_notification(opps: list[dict]) -> tuple[str, str]:
    """Formate le titre et le message de la notification."""
    n = len(opps)
    title = f"NHL: {n} cote{'s' if n > 1 else ''} Excellente{'s' if n > 1 else ''} aujourd'hui!"

    lines = []
    for o in opps[:4]:   # max 4 dans la notif (limite Windows ~256 chars)
        match   = o.get("match", "?")
        sel     = o.get("selection", "?")
        odds    = o.get("odds", "?")
        fp      = o.get("fair_prob", 0)
        lines.append(f"• {match}: {sel} @ {odds} ({fp:.0f}%)")

    if n > 4:
        lines.append(f"  …et {n - 4} autre(s)")

    message = "\n".join(lines)
    return title, message


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    _log("=== Vérification des cotes du jour ===")

    # 1. S'assurer que le serveur tourne
    if not _server_is_up():
        ok = _start_server()
        if not ok:
            _notify(
                "Mise-O-Jeu — Erreur",
                "Le serveur Flask n'a pas pu démarrer. Ouvre l'app manuellement.",
            )
            return

    # 2. Attendre les données
    _log("Attente des données…")
    data = _wait_for_data(max_wait=180)
    if not data:
        _notify("Mise-O-Jeu — Délai dépassé", "Aucune donnée reçue dans les 3 minutes.")
        return

    # 3. Filtrer les Excellents du jour
    opps = _check_opportunities(data)
    stats = data.get("stats", {})
    n_total = stats.get("excellent_h", 0)

    if opps:
        title, message = _format_notification(opps)
        _log(f"{len(opps)} cote(s) Excellente(s) trouvée(s).")
        _notify(title, message)
    else:
        _log("Aucune cote Excellente aujourd'hui.")
        if not CHECK_ONLY:
            _notify(
                "Mise-O-Jeu — Aucune cote excellente",
                "Pas de pari recommandé aujourd'hui. Reviens plus tard.",
            )


if __name__ == "__main__":
    main()
