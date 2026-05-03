"""
Mise-O-Jeu Analyzer — Serveur web Flask
"""

import json
import os
import time
import threading
import concurrent.futures
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ─── TIMEZONE FIX: Use Eastern Time (ET) for NHL ─────────────────────────────────
def _get_et_now():
    """Return current datetime in Eastern Time (ET)."""
    et_tz = timezone(timedelta(hours=-5))  # EST = UTC-5, EDT = UTC-4 (handled by system)
    # Better: use system timezone if available
    try:
        import pytz
        et = pytz.timezone('US/Eastern')
        return datetime.now(et)
    except ImportError:
        # Fallback: assume server is in UTC and convert to ET
        utc_now = datetime.now(timezone.utc)
        # Rough conversion (doesn't account for DST perfectly)
        return utc_now.astimezone(timezone(timedelta(hours=-5)))

def _get_today_et() -> str:
    """Return today's date in Eastern Time as YYYY-MM-DD string."""
    return _get_et_now().strftime("%Y-%m-%d")

# Support PyInstaller : utiliser les dossiers injectés par app_launcher.py si présents
_template_folder = os.environ.get('MISEOJEU_TEMPLATE_FOLDER') or 'templates'
_static_folder   = os.environ.get('MISEOJEU_STATIC_FOLDER')   or 'static'
app = Flask(__name__, template_folder=_template_folder, static_folder=_static_folder)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-prod')

# ─── Flask-Login Configuration ───────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    """Simple user model for login"""
    def __init__(self, username):
        self.id = username
        self.username = username

@login_manager.user_loader
def load_user(username):
    """Load user from session"""
    return User(username)

# Credentials simples : username/password depuis env vars
_LOGIN_USERNAME = os.environ.get('APP_USERNAME', 'admin')
_LOGIN_PASSWORD = os.environ.get('APP_PASSWORD', 'password')

# ─── Cache en mémoire ─────────────────────────────────────────────────────────

_PAYLOAD_CACHE_PATH = os.path.join(os.path.dirname(__file__), "payload_cache.json")

def _load_payload_cache() -> dict:
    """Charge le cache payload du jour depuis le disque (si disponible et du jour)."""
    try:
        if not os.path.exists(_PAYLOAD_CACHE_PATH):
            return {}
        with open(_PAYLOAD_CACHE_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        today = _get_today_et()
        if saved.get("date") != today:
            return {}   # cache d'un autre jour → ignorer
        return saved
    except Exception:
        return {}

def _save_payload_cache(data: dict, timestamp: str, date: str) -> None:
    """Persiste le payload sur disque pour accélérer le prochain démarrage."""
    try:
        with open(_PAYLOAD_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"data": data, "timestamp": timestamp, "date": date,
                       "epoch_ts": time.time()},
                      f, ensure_ascii=False)
    except Exception as e:
        print(f"  >> payload_cache save erreur: {e}")

# Charger le cache disque immédiatement au démarrage
_disk_cache = _load_payload_cache()
# Calculer l'âge du cache pour décider d'afficher le banner stale ou non
# Si epoch_ts est absent, initialiser avec l'heure actuelle (assume cache est frais s'il vient du disque)
_cache_epoch = _disk_cache.get("epoch_ts") or time.time()
_cache_age_min = ((time.time() - _cache_epoch) / 60) if _cache_epoch else 0
_cache_is_fresh = _cache_age_min < 30  # cache < 30 min = pas de banner

_cache = {
    "data":      _disk_cache.get("data"),
    "timestamp": _disk_cache.get("timestamp"),
    "status":    "ready" if _disk_cache.get("data") else "idle",
    "error":     None,
    # stale=True seulement si cache vieux (> 30 min) → évite banner sur reload récent
    "stale":     bool(_disk_cache.get("data")) and not _cache_is_fresh,
    "date":      _disk_cache.get("date"),
}
if _disk_cache.get("data"):
    fresh_label = "frais" if _cache_is_fresh else "obsolète"
    print(f"  >> Cache payload chargé depuis le disque ({_disk_cache.get('timestamp')}, {_cache_age_min:.0f}min, {fresh_label})")
_lock = threading.Lock()

# ─── Cache du scrape Playwright ───────────────────────────────────────────────
# Les cotes de Mise-O-Jeu ne changent pas à la minute — on re-scrape max
# toutes les 15 minutes pour éviter de relancer Chromium à chaque clic.
#
# Le cache est indexé par sport ("hockey")
# pour permettre un chargement partiel par onglet.

_SCRAPE_TTL = 45 * 60   # 45 minutes
_scrape_caches: dict = {}   # clé → (list[Match], timestamp)
_scrape_lock = threading.Lock()


def _check_date_rollover():
    """Détecte le changement de jour et marque les données comme stale.

    IMPORTANT : Ne supprime PAS les données. Les données d'hier restent
    affichables le temps que le scrape du jour tourne en arrière-plan.
    Ça évite que l'utilisateur attende plusieurs minutes devant un spinner.
    """
    today = _get_today_et()
    bg_needed = False
    with _lock:
        cached_date = _cache.get("date")
        if cached_date and cached_date != today and _cache["status"] == "ready":
            if not _cache.get("stale"):
                # Première détection du changement de jour : marquer stale
                print(f"  >> Nouveau jour ({cached_date} -> {today}) - stale, refresh BG...")
                _cache["stale"] = True

            # Déclencher le refresh BG seulement si pas déjà en cours
            # (évite doublon avec _startup_sequence qui tourne déjà)
            already_running = _cache.get("_bg_dayroll_running", False)
            # _startup_sequence met status=loading via _run_analysis → pas de BG nécessaire
            if not already_running:
                _cache["_bg_dayroll_running"] = True
                bg_needed = True
            # Ne PAS supprimer _cache["data"] ni _cache["date"] —
            # le frontend affiche les données d'hier avec bannière stale

    if bg_needed:
        def _dayroll_refresh():
            try:
                _run_analysis(demo=False, sports=["hockey"])
            finally:
                with _lock:
                    _cache.pop("_bg_dayroll_running", None)
        threading.Thread(target=_dayroll_refresh, daemon=True).start()

    with _scrape_lock:
        today_ts = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        stale_keys = [k for k, (_, ts) in _scrape_caches.items() if ts < today_ts]
        for k in stale_keys:
            del _scrape_caches[k]


def _scrape_cached(headless: bool = True,
                   sports: list | None = None) -> list:
    """Retourne les matchs NHL depuis le cache ou re-scrape si périmé.

    Note: Paramètre sports maintenu pour compatibilité, mais forcé à ["hockey"].
    """
    cache_key = ",".join(sorted(sports)) if sports else "all"
    now = time.time()
    with _scrape_lock:
        # 1. Cache direct (clé exacte)
        cached = _scrape_caches.get(cache_key)
        if cached is not None:
            data, ts = cached
            if now - ts < _SCRAPE_TTL:
                print(f"  >> Cache scrape '{cache_key}' frais ({int(now - ts)}s)")
                return data

        # 2. Si on demande un sous-ensemble, vérifier le cache "all"
        if sports:
            all_cached = _scrape_caches.get("all")
            if all_cached is not None:
                all_data, all_ts = all_cached
                if now - all_ts < _SCRAPE_TTL:
                    filtered = [m for m in all_data if m.sport in sports]
                    _scrape_caches[cache_key] = (filtered, all_ts)
                    print(f"  >> Cache scrape 'all' → filtre '{cache_key}' ({int(now - all_ts)}s)")
                    return filtered

        # 3. Re-scrape (avec retry si vide)
        print(f"  >> Cache scrape '{cache_key}' périmé — re-scrape...")
        from scraper import scrape_all_sync

        result = scrape_all_sync(headless=headless, sports=sports)

        # Retry automatique si résultat vide ou sport demandé absent
        sport_missing = sports and not any(m.sport in sports for m in (result or []))
        if not result or sport_missing:
            print(f"  >> Résultat vide/incomplet — réessai dans 3s...")
            time.sleep(3)
            result2 = scrape_all_sync(headless=headless, sports=sports)
            if result2 and (not sports or any(m.sport in sports for m in result2)):
                print(f"  >> Réessai réussi ({len(result2)} matchs)")
                result = result2
            else:
                print(f"  >> Réessai échoué — résultat toujours vide")

        ts_new = time.time()

        # Ne jamais cacher un résultat vide — permet de réessayer au prochain appel
        if result:
            _scrape_caches[cache_key] = (result, ts_new)

            # Si on vient de scraper "all", pré-peupler les caches sport-spécifiques
            # IMPORTANT: ne cacher que si non-vide pour chaque sport
            if not sports:
                hockey_m = [m for m in result if m.sport == "hockey"]
                if hockey_m:
                    _scrape_caches["hockey"] = (hockey_m, ts_new)
                    print(f"  >> Cache hockey: {len(hockey_m)} matchs")
                else:
                    print(f"  >> Hockey absent du scrape 'all' — cache hockey non mis à jour")
        else:
            print("  >> Scrape vide — résultat non mis en cache, réessai au prochain appel")

        return result or []


def _enrich_selection(bet_type: str, selection: str) -> str:
    """Enrichit les sélections tronquées ou ambiguës."""
    import re

    # Plus de / Moins de sans chiffre → extraire la valeur depuis le nom du marché
    if selection in ("Plus de", "Moins de"):
        m = re.search(r'(\d+[.,]?\d*)\s*$', bet_type)
        if not m:
            m = re.search(r'[\(\[](\d+[.,]?\d*)[\)\]]', bet_type)
        if not m:
            m = re.search(r'(\d+[.,]\d+)', bet_type)
        if m:
            val = m.group(1).replace(',', '.')
            return f"{selection} {val}"
        return selection

    # Oui / Non → description contextuelle
    if selection not in ("Oui", "Non"):
        return selection
    bt = bet_type.lower()
    if any(k in bt for k in ("2 équipes", "les 2", "both teams")):
        return "Les 2 équipes marquent" if selection == "Oui" else "Une équipe ne marque pas"
    if "barrage" in bt or "prolongation" in bt:
        return "Oui, prolongation" if selection == "Oui" else "Non, pas de prolongation"
    return selection


def _is_player_prop(o) -> bool:
    """Vrai si le pari concerne un joueur spécifique (ex: 'Matvei Michkov Total de points 0.5')."""
    import re
    bt = o.bet_type
    if '(' in bt:
        return False
    bt_lower = bt.lower()
    team_markers = (
        "gagnant", "victoire", "winner", "issues", "les 2", "both",
        "période", "double", "total de buts", "barrage", "prolongation",
    )
    if any(m in bt_lower for m in team_markers):
        return False
    # Commence par Prénom Nom (deux mots commençant par une majuscule)
    if re.match(r'^[A-ZÀ-Ü][a-zà-ü\'-]+ [A-ZÀ-Ü]', bt):
        return True
    return False


def _get_hot_notes(o) -> list[str]:
    """Retourne les notes de joueurs en feu pour les deux équipes (NHL)."""
    if _is_player_prop(o):
        return []
    try:
        from injuries import get_hot_players
        from nhl_stats import _match_abbrev
        notes = []
        for team in (o.match.away_team, o.match.home_team):
            abbrev = _match_abbrev(team) or team
            for p in get_hot_players(team):
                g = p["goals_l5"]
                a = p["pts_l5"] - g
                notes.append(
                    f"\U0001f525 [{abbrev}] {p['name']} en feu"
                    f" ({p['pts_l5']} pts en {p['gp_l5']} matchs"
                    f" — {g}B {a}A)"
                )
        return notes
    except Exception:
        return []


def _get_injury_notes(o) -> list[str]:
    """Retourne les notes de blessures pour les deux équipes (NHL)."""
    if _is_player_prop(o):
        return []
    try:
        from injuries import get_team_injuries
        from nhl_stats import _match_abbrev
        notes = []
        for team in (o.match.away_team, o.match.home_team):
            abbrev = _match_abbrev(team) or team
            tier_label = {
                "goalie_starter": "gardien partant",
                "forward_tier1":  "attaquant top-3",
                "forward_tier2":  "attaquant top-6",
                "defense_tier1":  "défenseur #1",
            }
            for inj in get_team_injuries(team):
                label = tier_label.get(inj["tier"], "")
                notes.append(f"\u26a0 [{abbrev}] {inj['name']} absent ({inj['status']}, {label})")
        return notes
    except Exception:
        return []


def _reason_text(o) -> str:
    """
    Génère une explication pour les paris bien notés.
    Priorise les stats réelles (NHL) ; complète avec des indicateurs mathématiques.
    """
    from nhl_stats import build_reason
    sports_reason = build_reason(
        home_team=o.match.home_team,
        away_team=o.match.away_team,
        bet_type=o.bet_type,
        selection=o.selection_label,
        match_date=o.match.date,
    )
    if sports_reason:
        return sports_reason

    # Fallback mathématique si les stats ne sont pas disponibles
    parts = []
    if o.house_margin < 4:
        parts.append(f"marge maison très faible ({o.house_margin:.1f}%)")
    elif o.house_margin < 6:
        parts.append(f"marge maison faible ({o.house_margin:.1f}%)")
    if 1.5 <= o.odds <= 3.5:
        parts.append("cotes dans la zone optimale")
    ratio = o.fair_prob / max(o.implied_prob, 0.001)
    if ratio > 1.06:
        parts.append("sélection sous-cotée vs le marché")
    return " · ".join(parts) if parts else "équilibre global favorable"


def _nick_team(name: str) -> str:
    """Extrait le surnom d'équipe entre parenthèses : 'Nashville (Predators)' → 'Predators'."""
    import re
    m = re.search(r'\(([^)]+)\)', name or "")
    return m.group(1) if m else (name or "").strip()


def _generate_sgp_proposals(hockey_picks: list, n: int = 3) -> list:
    """
    Génère les n meilleures propositions de Combo Même Match (Same-Game Parlay).
    Priorise : Gagnant + Total de buts > Gagnant + Prop joueur > autres combos.
    Retourne uniquement les picks Excellent.
    """
    from collections import defaultdict

    # Seulement les picks Excellent
    excellent = [p for p in hockey_picks
                 if "Excellent" in (p.get("recommendation") or "")]

    # Grouper par match
    by_match = defaultdict(list)
    for p in excellent:
        by_match[p["match"]].append(p)

    proposals = []

    for match, picks in by_match.items():
        if len(picks) < 2:
            continue

        winner_picks = [p for p in picks
                        if "Gagnant" in (p.get("bet_type") or "")
                        and "Double" not in (p.get("bet_type") or "")]
        total_picks  = [p for p in picks
                        if "Total de buts" in (p.get("bet_type") or "")]
        prop_picks   = [p for p in picks
                        if "Total de points" in (p.get("bet_type") or "")]

        # Choisir la meilleure paire
        if winner_picks and total_picks:
            pair = [winner_picks[0], total_picks[0]]
            combo_type = "Gagnant + Total de buts"
        elif winner_picks and prop_picks:
            pair = [winner_picks[0], prop_picks[0]]
            combo_type = "Gagnant + Prop joueur"
        else:
            pair = picks[:2]
            combo_type = "Double sélection"

        combined_odds = round(
            float(pair[0].get("odds") or 1.0) * float(pair[1].get("odds") or 1.0), 2
        )
        avg_score = sum((p.get("value_score") or 0) for p in pair) / 2

        # Étiquettes lisibles par pick
        def _pick_label(p):
            bt  = p.get("bet_type") or ""
            sel = p.get("selection") or ""
            if "Gagnant" in bt and "Double" not in bt:
                return f"Victoire {_nick_team(sel)}"
            elif "Total de buts" in bt:
                return sel   # ex: "Moins de 3.5"
            elif "Total de points" in bt:
                # bt = "Kiefer Sherwood Total de points 0.5"
                player = bt.split("Total de points")[0].strip()
                return f"{sel} ({player})"
            return sel

        # Match court : "Sharks @ Predators"
        match_parts = match.split(" @ ")
        short_match = (f"{_nick_team(match_parts[0])} @ {_nick_team(match_parts[1])}"
                       if len(match_parts) == 2 else match)

        proposals.append({
            "match":          match,
            "short_match":    short_match,
            "combo_type":     combo_type,
            "label":          " + ".join(_pick_label(p) for p in pair),
            "combined_odds":  combined_odds,
            "avg_score":      round(avg_score, 1),
            "mise":           None,
            "picks": [
                {
                    "bet_type":    p.get("bet_type"),
                    "selection":   p.get("selection"),
                    "odds":        p.get("odds"),
                    "fair_prob":   p.get("fair_prob"),
                    "value_score": p.get("value_score"),
                }
                for p in pair
            ],
        })

    # Trier par avg_score décroissant, retourner top N
    proposals.sort(key=lambda x: -x["avg_score"])
    return proposals[:n]


def _generate_analyst_summary(hockey_picks: list) -> str:
    """Génère un paragraphe d'analyse sportive détaillé en français pour les prédictions du soir."""
    import re
    from collections import Counter
    if not hockey_picks:
        return ""

    def _nick(name: str) -> str:
        m = re.search(r'\(([^)]+)\)', name or "")
        return m.group(1) if m else (name or "").strip()

    def _short_match(match: str) -> str:
        parts_m = match.split(" @ ")
        if len(parts_m) == 2:
            return f"{_nick(parts_m[0])} @ {_nick(parts_m[1])}"
        return match

    def _parse_reason(reason: str, team_nick: str) -> dict:
        """Extrait les stats clés du champ reason pour une équipe donnée."""
        data = {}
        if not reason:
            return data
        # Victoires à domicile / sur la route
        m = re.search(r'(\d+)%\s+victoires\s+à\s+domicile', reason)
        if m:
            data["home_win_pct"] = int(m.group(1))
        m = re.search(r'(\d+)%\s+sur\s+la\s+route', reason)
        if m:
            data["road_win_pct"] = int(m.group(1))
        # Bilan saison
        m = re.search(r'Saison\s*:\s*(\d+)V-(\d+)D', reason)
        if m:
            data["season_w"] = int(m.group(1))
            data["season_l"] = int(m.group(2))
        # 10 derniers matchs
        m = re.search(r'10 derniers\s*:\s*(\d+)V-(\d+)D', reason)
        if m:
            data["last10_w"] = int(m.group(1))
            data["last10_l"] = int(m.group(2))
        # Série en cours
        m = re.search(r'série de (\d+)(V|D)', reason)
        if m:
            data["streak_n"] = int(m.group(1))
            data["streak_type"] = "victoires" if m.group(2) == "V" else "défaites"
        # Jeu de puissance
        m = re.search(r'PP ([\d.]+)%', reason)
        if m:
            data["pp_pct"] = float(m.group(1))
        # Possession Corsi
        m = re.search(r'Possession \(Corsi\) ([\d.]+)%', reason)
        if m:
            data["corsi"] = float(m.group(1))
        # Gardien favori
        m = re.search(r'Gardien ' + re.escape(team_nick) + r'[^·]*?:\s*(\w+)\s*\(Attendu\)\s*·\s*([\d.]+)%\s*SV', reason)
        if m:
            data["goalie_name"] = m.group(1)
            data["goalie_sv"]   = float(m.group(2))
        # Gardien adverse
        m = re.search(r'Gardien (?!' + re.escape(team_nick) + r')(\w[^:]*?):\s*(\w+)\s*\(Attendu\)\s*·\s*([\d.]+)%\s*SV', reason)
        if m:
            data["opp_goalie"]    = m.group(2)
            data["opp_goalie_sv"] = float(m.group(3))
        # Voyage / jet lag adversaire
        m = re.search(r'voyage de (\d+)h', reason)
        if m:
            data["opp_travel_h"] = int(m.group(1))
        return data

    picks      = sorted(hockey_picks, key=lambda x: x.get("value_score") or 0, reverse=True)
    champions  = [p for p in picks if p.get("champion")]
    matches    = list({p.get("match", "") for p in picks if p.get("match")})
    n_matches  = len(matches)
    n_picks    = len(picks)
    high_value = [p for p in picks if (p.get("value_score") or 0) > 70]

    parts = []

    # ── Intro ──────────────────────────────────────────────────────────────────
    if n_matches == 1:
        parts.append(
            f"Ce soir, un seul match NHL est au programme, mais notre modèle y détecte "
            f"{n_picks} pari{'s' if n_picks > 1 else ''} à valeur positive."
        )
    elif n_matches <= 4:
        parts.append(
            f"Avec {n_matches} matchs NHL ce soir, notre modèle identifie "
            f"{n_picks} opportunités réparties sur plusieurs rencontres."
        )
    else:
        parts.append(
            f"Belle soirée de hockey avec {n_matches} matchs à l'affiche — "
            f"notre modèle repère {n_picks} paris à valeur positive."
        )

    # ── Champion : analyse détaillée ─────────────────────────────────────────
    if champions:
        c         = champions[0]
        sel_raw   = c.get("selection", "")
        sel       = _nick(sel_raw)
        bt        = c.get("bet_type", "")
        odds      = c.get("odds", "")
        mtch      = _short_match(c.get("match", ""))
        fair_p    = c.get("fair_prob") or 0
        impl_p    = c.get("implied_prob") or 0
        ev        = c.get("ev") or 0
        hot_notes = c.get("hot_notes", []) or []
        inj_notes = c.get("injury_notes", []) or []
        reason    = c.get("reason", "") or ""
        stats     = _parse_reason(reason, sel)

        # Phrase d'intro champion
        match_parts = c.get("match", "").split(" @ ")
        if len(match_parts) == 2:
            away_nick = _nick(match_parts[0])
            home_nick = _nick(match_parts[1])
            is_home = sel in home_nick or home_nick in sel
            venue_txt = f"reçoivent les {away_nick} à domicile" if is_home else f"se déplacent chez les {home_nick}"
            parts.append(
                f"Notre sélection phare du soir : les {sel} {venue_txt} à la cote de {odds}."
            )
        else:
            parts.append(f"Notre sélection phare du soir : les {sel} à la cote de {odds}.")

        # Forme récente — une seule phrase fluide
        streak_n   = stats.get("streak_n", 0)
        streak_ok  = streak_n >= 2 and stats.get("streak_type") == "victoires"
        last10_w   = stats.get("last10_w")
        last10_l   = stats.get("last10_l")
        home_pct   = stats.get("home_win_pct")

        if streak_ok:
            base = f"Ils enchaînent une série de {streak_n} victoires consécutives"
            if last10_w is not None:
                base += f" ({last10_w}V-{last10_l}D sur leurs {last10_w + last10_l} derniers matchs)"
            if home_pct:
                base += f", dominant à domicile à {home_pct}% cette saison"
            parts.append(base + ".")
        elif last10_w is not None:
            base = f"Ils affichent {last10_w} victoires sur leurs {last10_w + last10_l} derniers matchs"
            if home_pct:
                base += f", avec un taux de victoire à domicile de {home_pct}% cette saison"
            parts.append(base + ".")
        elif home_pct:
            parts.append(f"Ils remportent {home_pct}% de leurs matchs à domicile cette saison.")

        # Jeu de puissance + possession
        sys_parts = []
        if stats.get("pp_pct"):
            pp = stats["pp_pct"]
            if pp >= 22:
                sys_parts.append(f"leur jeu de puissance est redoutable à {pp}%")
            elif pp >= 18:
                sys_parts.append(f"leur jeu de puissance tourne à {pp}%")
        if stats.get("corsi"):
            cor = stats["corsi"]
            if cor >= 51:
                sys_parts.append(f"ils dominent la possession à {cor}% (Corsi)")
            elif cor < 48:
                sys_parts.append(f"leur possession (Corsi {cor}%) est un point à surveiller")
        if sys_parts:
            parts.append("Sur le plan du jeu, " + " et ".join(sys_parts) + ".")

        # Gardien — phrase combinée
        gn   = stats.get("goalie_name")
        gsv  = stats.get("goalie_sv")
        ogn  = stats.get("opp_goalie")
        ogsv = stats.get("opp_goalie_sv")
        if gn and gsv and ogn and ogsv:
            goalie_sentence = (
                f"{gn} sera dans les filets avec {gsv}% d'arrêts récents"
                + (f" — bien au-dessus" if gsv >= 91 else f" — un niveau solide" if gsv >= 89 else "")
                + f", tandis que le gardien adverse {ogn} affiche"
                + (f" seulement {ogsv}% d'arrêts — un avantage net pour les {sel}." if ogsv < 89
                   else f" {ogsv}% d'arrêts.")
            )
            parts.append(goalie_sentence)
        elif gn and gsv:
            if gsv >= 91:
                parts.append(f"{gn} est en excellente forme dans les filets ({gsv}% d'arrêts récents).")
            elif gsv >= 89:
                parts.append(f"{gn} assure devant le filet avec {gsv}% d'arrêts lors de ses derniers matchs.")
        elif ogn and ogsv and ogsv < 88:
            parts.append(f"Le gardien adverse {ogn} traverse une période difficile ({ogsv}% d'arrêts récents).")

        # Joueurs chauds — on convertit les notes en phrases
        home_hot = [n for n in hot_notes if f"[{sel[:3].upper()}]" in n or sel[:3].upper() in n]
        if not home_hot:
            # Fallback : notes de l'équipe sélectionnée par les 3 premières lettres du nick
            team_tag = f"[{sel[:3].upper()}]"
            home_hot = [n for n in hot_notes if team_tag in n]
        if home_hot:
            # Extraire noms et stats du premier joueur chaud
            hot_str = home_hot[0]
            m_hot = re.search(r'🔥 \[\w+\] (.+?) \((.+?)\)', hot_str)
            if m_hot:
                player_name = m_hot.group(1)
                player_stat = m_hot.group(2)
                parts.append(
                    f"Offensivement, {player_name} est en feu ({player_stat}) "
                    f"— un atout majeur pour forcer la décision."
                )
                if len(home_hot) >= 2:
                    m_hot2 = re.search(r'🔥 \[\w+\] (.+?) \((.+?)\)', home_hot[1])
                    if m_hot2:
                        parts.append(
                            f"{m_hot2.group(1)} est également sur une belle lancée ({m_hot2.group(2)})."
                        )

        # Blessures adverses
        opp_nick = _nick(match_parts[0] if len(match_parts) == 2 else "")
        opp_tag  = f"[{opp_nick[:3].upper()}]"
        opp_inj  = [n for n in inj_notes if opp_tag in n]
        if opp_inj:
            injured_players = []
            for note in opp_inj[:3]:
                m_inj = re.search(r'⚠ \[\w+\] (.+?) absent', note)
                if m_inj:
                    injured_players.append(m_inj.group(1))
            if injured_players:
                if len(injured_players) == 1:
                    parts.append(
                        f"Du côté des {opp_nick}, l'absence de {injured_players[0]} affaiblit leur alignement ce soir."
                    )
                else:
                    players_str = ", ".join(injured_players[:-1]) + f" et {injured_players[-1]}"
                    parts.append(
                        f"Du côté des {opp_nick}, les absences de {players_str} compliquent sérieusement leur soirée."
                    )

        # Voyage adverse
        if stats.get("opp_travel_h") and stats["opp_travel_h"] >= 2:
            h = stats["opp_travel_h"]
            parts.append(
                f"À cela s'ajoute la fatigue du voyage : les {opp_nick} ont effectué un déplacement "
                f"de {h} heures avant ce match — un désavantage non négligeable."
            )

        # Valeur du marché
        gap = round(fair_p - impl_p, 1)
        if gap >= 10:
            parts.append(
                f"Le marché sous-estime clairement les {sel} : notre modèle leur accorde {fair_p:.0f}% de chances "
                f"de victoire contre seulement {impl_p:.0f}% selon les cotes — un écart de {gap:.0f} points en votre faveur."
            )
        elif gap >= 5:
            parts.append(
                f"Notre modèle estime les chances des {sel} à {fair_p:.0f}% "
                f"alors que le marché les fixe à {impl_p:.0f}% — une opportunité réelle à saisir."
            )

    # ── Autres paris à surveiller ─────────────────────────────────────────────
    non_champ = [p for p in picks if not p.get("champion")]
    if len(non_champ) >= 2:
        t1, t2 = non_champ[0], non_champ[1]
        parts.append(
            f"À surveiller également ce soir : {_short_match(t1.get('match', ''))} "
            f"({t1.get('bet_type', '')} à {t1.get('odds', '')}) "
            f"et {_short_match(t2.get('match', ''))} "
            f"({t2.get('bet_type', '')} à {t2.get('odds', '')})."
        )

    # ── Résumé des opportunités ───────────────────────────────────────────────
    if len(high_value) >= 4:
        parts.append(
            f"En résumé, {len(high_value)} paris affichent un score valeur supérieur à 70 ce soir "
            f"— une soirée riche en opportunités."
        )

    return " ".join(parts)


def _build_payload(demo: bool = False,
                   sports: list | None = None) -> dict:
    """Scrape + analyse et retourne un dict JSON-sérialisable.

    Note: Paramètre sports maintenu pour compatibilité, mais forcé à ["hockey"].
    """
    from scraper import Match, BetGroup, Selection
    from analyzer import OddsAnalyzer
    from predictions import record_opportunity, update_outcomes

    t0 = time.time()
    if demo:
        from main import generate_demo_data
        matches = generate_demo_data()
    else:
        import concurrent.futures

        # ─── OPTIMISATION : scrape Playwright + prefetch NHL (toutes les 32 équipes)
        # tournent en PARALLÈLE pour gagner 30-60s.
        # Le prefetch NHL n'a pas besoin d'attendre les équipes du soir : il pré-charge
        # tout le league via _fetch_standings() (qui retourne les 32 abbrevs).
        def _prefetch_nhl_all():
            t = time.time()
            try:
                from extra_stats import (
                    _fetch_club_stats, _fetch_weekly_schedule,
                    _fetch_season_schedule, _current_season,
                )
                from nhl_stats import _fetch_standings
                from datetime import date, timedelta

                standings = _fetch_standings() or {}
                all_abbrevs = [a for a in standings.keys() if a]
                if not all_abbrevs:
                    print(f"  [timing] prefetch_nhl: standings vide, skip ({time.time()-t:.1f}s)")
                    return

                season  = _current_season()
                today   = date.today().isoformat()
                prev_wk = (date.today() - timedelta(days=3)).isoformat()

                def _warm(abbrev):
                    try:
                        _fetch_club_stats(abbrev)
                        _fetch_weekly_schedule(abbrev, prev_wk)
                        _fetch_weekly_schedule(abbrev, today)
                        _fetch_season_schedule(abbrev, season)
                    except Exception:
                        pass

                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
                    list(ex.map(_warm, all_abbrevs))
            except Exception:
                pass
            print(f"  [timing] prefetch_nhl (all 32): {time.time()-t:.1f}s")

        # Phase 1 : scrape + prefetch NHL en parallèle
        t_par = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as outer:
            f_scrape  = outer.submit(_scrape_cached, True, ["hockey"])
            f_pre_nhl = outer.submit(_prefetch_nhl_all)

            # Attendre le scrape pour récupérer les équipes du soir
            matches = f_scrape.result()
            print(f"  [timing] scrape: {time.time()-t0:.1f}s ({len(matches)} matchs)")

            hockey_teams = list({
                name
                for m in matches
                for name in (m.home_team, m.away_team)
            })

            # Phase 2 : prefetch des blessures (besoin des noms des équipes)
            # tourne en parallèle avec la fin du prefetch NHL
            def _prefetch_injuries():
                t = time.time()
                try:
                    from injuries import prefetch_injuries
                    if hockey_teams:
                        prefetch_injuries(hockey_teams)
                except Exception:
                    pass
                print(f"  [timing] prefetch_injuries: {time.time()-t:.1f}s")

            f_inj = outer.submit(_prefetch_injuries)
            f_pre_nhl.result()
            f_inj.result()
        print(f"  [timing] scrape+prefetch parallèle total: {time.time()-t_par:.1f}s")

        # Mettre à jour les résultats des matchs passés (NHL) — en arrière-plan,
        # sans bloquer le payload affiché à l'écran.
        def _bg_update_outcomes():
            t_upd = time.time()
            try:
                update_outcomes()
            except Exception:
                pass
            print(f"  [timing] update_outcomes (bg): {time.time()-t_upd:.1f}s")
        threading.Thread(target=_bg_update_outcomes, daemon=True).start()

    t_ana = time.time()
    analyzer = OddsAnalyzer()
    analyzed = analyzer.analyze_matches(matches)
    print(f"  [timing] analyze_matches: {time.time()-t_ana:.1f}s")

    # Déterminer quels sports ont été scrapés
    refresh_hockey = (sports is None) or ("hockey" in sports) or demo

    hockey_opps = analyzer.get_top_opportunities(analyzed, n=60, sport_filter="hockey") \
                  if refresh_hockey else None

    # ─── OPTIMISATION : record_opportunities_batch écrit sur disque mais
    # ne sert PAS au payload affiché → on déplace en BG pour libérer l'UI ~5-15s plus tôt.
    if not demo:
        from datetime import date as _date_cls
        _today = _date_cls.today().isoformat()
        _times = [m.time for m in matches if m.date == _today and m.time]
        first_match_time = min(_times) if _times else "23:59"
        print(f"  [timing] premier match aujourd'hui : {first_match_time} "
              f"({'verrouillé' if _get_et_now().strftime('%H:%M') >= first_match_time else 'fenêtre ouverte'})")

        _all_opps_snapshot = list(hockey_opps or [])
        _first_match_time  = first_match_time

        def _bg_record_opps():
            t_rec = time.time()
            try:
                from predictions import record_opportunities_batch
                n_rec = record_opportunities_batch(_all_opps_snapshot, first_match_time=_first_match_time)
                print(f"  [timing] record_batch (bg, {n_rec} prédictions): {time.time()-t_rec:.1f}s")
            except Exception as _e_rec:
                print(f"  [warn] record_opportunities_batch (bg): {_e_rec}")
                for opp in _all_opps_snapshot:
                    try:
                        record_opportunity(opp, first_match_time=_first_match_time)
                    except Exception:
                        pass
        threading.Thread(target=_bg_record_opps, daemon=True).start()

    # Zones de cotes historiquement rentables (hockey uniquement pour l'instant)
    t_prof = time.time()
    try:
        from predictions import get_profitable_odds_ranges
        _profitable = get_profitable_odds_ranges(min_samples=5)
    except Exception:
        _profitable = []
    print(f"  [timing] get_profitable_odds: {time.time()-t_prof:.1f}s")

    def _is_champion(odds: float) -> bool:
        """Vrai si la cote tombe dans une zone historiquement rentable."""
        return any(lo <= odds < hi for lo, hi, _ in _profitable)

    def _get_team_logo_url(team_name):
        """Retourne l'URL du logo NHL pour une équipe donnée."""
        if not team_name:
            return ""
        # Extraire juste le nom avant la parenthèse (ex: "Buffalo (Sabres)" → "Buffalo")
        team_name = team_name.split("(")[0].strip() if "(" in team_name else team_name
        # Mapping vers abréviations NHL (même que dans templates/index.html)
        abbrev_map = {
            "anaheim": "ANA", "ducks": "ANA",
            "arizona": "UTA", "utah": "UTA", "coyotes": "UTA",
            "boston": "BOS", "bruins": "BOS",
            "buffalo": "BUF", "sabres": "BUF",
            "calgary": "CGY", "flames": "CGY",
            "carolina": "CAR", "hurricanes": "CAR",
            "chicago": "CHI", "blackhawks": "CHI",
            "colorado": "COL", "avalanche": "COL",
            "columbus": "CBJ", "blue jackets": "CBJ",
            "dallas": "DAL", "stars": "DAL",
            "detroit": "DET", "red wings": "DET",
            "edmonton": "EDM", "oilers": "EDM",
            "florida": "FLA", "panthers": "FLA",
            "los angeles": "LAK", "kings": "LAK",
            "minnesota": "MIN", "wild": "MIN",
            "montreal": "MTL", "canadiens": "MTL",
            "nashville": "NSH", "predators": "NSH",
            "new jersey": "NJD", "devils": "NJD",
            "new york islanders": "NYI", "islanders": "NYI",
            "new york rangers": "NYR", "rangers": "NYR",
            "ottawa": "OTT", "senators": "OTT",
            "philadelphia": "PHI", "philadelphie": "PHI", "flyers": "PHI",
            "pittsburgh": "PIT", "penguins": "PIT",
            "san jose": "SJS", "sharks": "SJS",
            "seattle": "SEA", "kraken": "SEA",
            "st. louis": "STL", "blues": "STL",
            "tampa bay": "TBL", "lightning": "TBL",
            "toronto": "TOR",
            "vancouver": "VAN", "canucks": "VAN",
            "vegas": "VGK",
            "washington": "WSH", "capitals": "WSH",
            "winnipeg": "WPG", "jets": "WPG",
        }
        abbrev = abbrev_map.get(team_name.lower(), None)
        if abbrev:
            return f"https://assets.nhle.com/logos/nhl/svg/{abbrev}_light.svg"
        return ""

    def opp_to_dict(o):
        is_excellent = "Excellent" in o.recommendation
        champion_candidate = is_excellent and _is_champion(o.odds)
        ev = round(o.fair_prob * o.odds - 1.0, 4) if champion_candidate else -99.0
        _key = "|".join([
            o.match.date or "",
            (o.match.home_team or "").lower(),
            (o.match.away_team or "").lower(),
            o.bet_type.lower(),
            o.selection_label.lower(),
        ])
        return {
            "rank":           0,
            "match":          f"{o.match.away_team} @ {o.match.home_team}",
            "home_team":      o.match.home_team or "",
            "away_team":      o.match.away_team or "",
            "home_logo":      _get_team_logo_url(o.match.home_team),
            "away_logo":      _get_team_logo_url(o.match.away_team),
            "_pred_key":      _key,
            "league":         o.league,
            "sport":          o.sport,
            "date":           o.match.date,
            "time":           o.match.time,
            "bet_type":       o.bet_type,
            "selection":      _enrich_selection(o.bet_type, o.selection_label),
            "odds":           o.odds,
            "house_margin":   round(o.house_margin, 1),
            "value_score":    round(o.value_score),
            "fair_prob":      round(o.fair_prob * 100, 1),
            "implied_prob":   round(o.implied_prob * 100, 1),
            "recommendation": o.recommendation,
            "prediction_id":  o.prediction_id,
            "event_id":       o.match.event_id,
            "event_url":      o.match.event_url,
            "reason":         _reason_text(o) if is_excellent else "",
            "injury_notes":   _get_injury_notes(o),
            "hot_notes":      _get_hot_notes(o),
            "champion":       champion_candidate,
            "ev":             ev,
        }

    def _signal_score(o: dict) -> float:
        """
        Score de consensus des signaux pour un pari.
        Retourne l'EV pondérée par la fraction de signaux qui votent POUR.
        Évite qu'un seul signal dominant (ex: domicile) masque 7 signaux négatifs.
        """
        signals = o.get("signals") or {}
        bools = {k: v for k, v in signals.items()
                 if k != "is_home" and isinstance(v, bool)}
        if not bools:
            return o["ev"]
        n_for   = sum(1 for v in bools.values() if v)
        consensus = n_for / len(bools)          # 0.0 → 1.0
        # consensus=0.5 → neutre ; <0.5 → pénalité ; >0.5 → bonus
        weight  = 0.5 + (consensus - 0.5) * 0.6   # plage [0.20 – 0.80]
        return o["ev"] * weight

    def _build_sport_list(opps):
        """Construit et trie la liste de paris pour un sport donné."""
        t_lst = time.time()
        lst = [opp_to_dict(o) for o in opps]
        print(f"  [timing] opp_to_dict×{len(opps)}: {time.time()-t_lst:.1f}s")

        # Appliquer les multiplicateurs historiques par type de pari
        try:
            from predictions import get_bet_type_multipliers, classify_bet_type
            sport_key = opps[0].sport if opps else None
            bt_mults  = get_bet_type_multipliers(sport=sport_key, min_samples=5)
            for o in lst:
                cat  = classify_bet_type(
                    o.get("bet_type") or "",
                    o.get("match", "").split(" @ ")[-1] if " @ " in o.get("match","") else "",
                    o.get("match", "").split(" @ ")[0]  if " @ " in o.get("match","") else "",
                )
                mult = bt_mults.get(cat, 1.0)
                if mult != 1.0:
                    o["value_score"] = round(o["value_score"] * mult, 1)
                    o["bt_mult"]     = mult   # pour debug / affichage futur
        except Exception as _e:
            print(f"  [WARN] get_bet_type_multipliers: {_e}")
        MAX_CHAMPIONS = 5
        candidates = sorted(
            [o for o in lst if o["champion"]],
            key=_signal_score,
            reverse=True,
        )
        top5_ids = {id(o) for o in candidates[:MAX_CHAMPIONS]}
        for o in lst:
            if o["champion"] and id(o) not in top5_ids:
                o["champion"] = False
        lst.sort(key=lambda o: (
            0 if o["champion"] else 1,
            -_signal_score(o) if o["champion"] else 0,
            0 if "Excellent" in o["recommendation"] else 1,
        ))
        for i, o in enumerate(lst, 1):
            o["rank"] = i
        return lst

    # Récupérer l'ancien payload pour la fusion partielle (refresh d'un seul sport)
    with _lock:
        _old = _cache.get("data") or {}

    t_bsl = time.time()
    hockey_list = _build_sport_list(hockey_opps) if refresh_hockey \
                  else _old.get("hockey", [])
    print(f"  [timing] build_sport_lists: {time.time()-t_bsl:.1f}s")

    # Ajouter les fiches (W-L-OTL) pour home/away de chaque pick hockey
    if refresh_hockey and hockey_list:
        try:
            from nhl_stats import _fetch_standings, _match_abbrev as _nhl_abbrev
            _standings = _fetch_standings()  # dict {abbrev: {wins, losses, otLosses, ...}}
            def _team_record(team_name: str) -> str:
                if not team_name:
                    return ""
                abbrev = _nhl_abbrev(team_name.strip())
                s = _standings.get(abbrev or "", {})
                if not s:
                    return ""
                return f"{s['wins']}-{s['losses']}-{s['otLosses']}"
            for p in hockey_list:
                # home_team/away_team peuvent être None — on parse le champ match
                match_str = p.get("match", "")
                m_parts   = match_str.split(" @ ")
                away_name = m_parts[0].strip() if len(m_parts) >= 2 else ""
                home_name = m_parts[1].strip() if len(m_parts) >= 2 else ""
                p["home_record"] = _team_record(home_name)
                p["away_record"] = _team_record(away_name)
        except Exception as _e_rec2:
            print(f"  [warn] team_records: {_e_rec2}")

    # Calculer et attacher les mises Kelly /10$ à chaque pick (même logique que le JS)
    # Facteur de correction biais (actif après 20 prédictions résolues)
    t_bf = time.time()
    try:
        from predictions import get_bias_factor as _get_bf
        _bias_factor = _get_bf()
    except Exception:
        _bias_factor = 1.0
    print(f"  [timing] get_bias_factor: {time.time()-t_bf:.1f}s")

    def _hk_single(p):
        prob = float(p.get('fair_prob') or 0) / 100
        odds = float(p.get('odds') or 0)
        if odds <= 1 or prob <= 0: return 0.0
        b = odds - 1
        return max(0.0, (prob * b - (1 - prob)) / b / 2)

    def _hk_combo(sgp):
        """Kelly demi-Kelly pour un combo 2 picks — probabilité combinée avec discount corrélation 15%."""
        cp = sgp.get("picks", [])
        if len(cp) < 2: return 0.0
        p1 = float(cp[0].get('fair_prob') or 0) / 100
        p2 = float(cp[1].get('fair_prob') or 0) / 100
        if p1 <= 0 or p2 <= 0: return 0.0
        p_combo = p1 * p2 * 0.85   # -15% discount corrélation dans le même match
        co = float(sgp.get('combined_odds') or 1.0)
        if co <= 1: return 0.0
        b = co - 1
        return max(0.0, (p_combo * b - (1 - p_combo)) / b / 2)

    def _apply_mises(picks, proposals=None, budget=10.0, min_bet=0.5, max_bets=7):
        for p in picks:
            p['mise'] = None
        if proposals:
            for s in proposals:
                s['mise'] = None

        # ── Kelly pour tous les picks individuels ─────────────────────────────
        for p in picks:
            p['_hk'] = _hk_single(p)

        # ── Kelly pour les combos SGP (concourent dans le même pool) ─────────
        valid_sgp = []
        if proposals:
            for s in proposals:
                s['_hk'] = _hk_combo(s)
                if s['_hk'] > 0:
                    valid_sgp.append(s)

        # ── Pool unifié : picks individuels (top max_bets Kelly>0) + combos ──
        kelly_pos = sorted([p for p in picks if p['_hk'] > 0], key=lambda x: -x['_hk'])
        ind_selected = kelly_pos[:max_bets]

        # Compléter jusqu'à min_bets si nécessaire
        min_bets = 3
        if len(ind_selected) < min_bets:
            remaining = sorted(
                [p for p in picks if p not in ind_selected],
                key=lambda x: -(x.get('value_score') or 0)
            )
            for p in remaining:
                if len(ind_selected) >= min_bets: break
                ind_selected.append(p)

        # Pool complet = picks individuels sélectionnés + combos avec Kelly > 0
        all_selected = ind_selected + valid_sgp

        if all_selected:
            total_hk = sum(x['_hk'] for x in all_selected)
            if total_hk > 0:
                weights = [x['_hk'] for x in all_selected]
            else:
                # Fallback value_score pour picks, avg_score pour combos
                weights = [max(x.get('value_score') or x.get('avg_score') or 1, 0.01)
                           for x in all_selected]
                total_hk = sum(weights)

            amounts = [w / total_hk * budget for w in weights]
            amounts = [max(round(a * 2) / 2, min_bet) for a in amounts]
            tot = sum(amounts)
            amounts = [round(a / tot * budget * 2) / 2 for a in amounts]
            amounts = [max(a, min_bet) for a in amounts]
            diff = round((budget - sum(amounts)) * 2) / 2
            if diff != 0:
                mx = amounts.index(max(amounts))
                amounts[mx] = round((amounts[mx] + diff) * 2) / 2

            for x, amt in zip(all_selected, amounts):
                x['mise'] = amt

        any_kelly_positive = any(p.get('_hk', 0) > 0 for p in picks)

        for p in picks:
            p.pop('_hk', None)
        if proposals:
            for s in proposals:
                s.pop('_hk', None)

        return any_kelly_positive

    # Filtrer hockey_list pour ne contenir que les picks d'aujourd'hui
    # (le scraper retourne matchs d'aujourd'hui ET demain)
    today = _get_today_et()
    hockey_list_today = [p for p in hockey_list if p.get("date") == today]

    # Générer les proposals SGP avant les mises pour permettre l'allocation de budget
    sgp_proposals = _generate_sgp_proposals(hockey_list_today) if refresh_hockey \
                    else _old.get("sgp_proposals", []) if _old else []

    t_mises = time.time()
    _any_kelly_pos = True  # défaut : pas d'avertissement
    if not demo:
        _any_kelly_pos = _apply_mises(hockey_list_today, proposals=sgp_proposals if refresh_hockey else None)
    print(f"  [timing] apply_mises: {time.time()-t_mises:.1f}s")

    # Persister les mises + flag champion dans predictions.json
    t_champ = time.time()
    if not demo:
        try:
            from predictions import update_champion_flags
            from datetime import date as _date_cls2
            _today2 = _date_cls2.today().isoformat()
            all_lists = hockey_list
            all_today_keys  = {o["_pred_key"] for o in all_lists
                               if o.get("_pred_key") and o.get("date") == _today2}
            champ_keys      = {o["_pred_key"] for o in all_lists
                               if o.get("champion") and o.get("_pred_key")}
            mise_by_key     = {o["_pred_key"]: o["mise"]
                               for o in all_lists
                               if o.get("_pred_key") and o.get("mise") is not None}
            update_champion_flags(champ_keys, all_today_keys, mise_by_key)
        except Exception as _e_champ:
            print(f"  [warn] update_champion_flags: {_e_champ}")
    print(f"  [timing] update_champion_flags: {time.time()-t_champ:.1f}s")

    # Vignette "Précision prévue ce soir"
    from datetime import date as _today_date
    today_str = _today_date.today().isoformat()

    def _sim1(opps, bet=1.0):
        n           = len(opps)
        gain_max    = sum((o.odds - 1.0) * bet for o in opps)
        gain_espere = sum(o.fair_prob * (o.odds - 1.0) * bet - (1.0 - o.fair_prob) * bet for o in opps)
        avg_fp      = sum(o.fair_prob for o in opps) / n if n else 0
        avg_odds    = sum(o.odds for o in opps) / n if n else 0
        return {
            "count":         n,
            "avg_fair_prob": round(avg_fp * 100, 1),
            "avg_odds":      round(avg_odds, 2),
            "sim_risque":    round(n * bet, 2),
            "sim_gain_max":  round(gain_max, 2),
            "sim_gain_esp":  round(gain_espere, 2),
        }

    def _sim_champions(rows, bet=2.0):
        n           = len(rows)
        gain_max    = sum((r["odds"] - 1.0) * bet for r in rows)
        gain_espere = sum(
            (r["fair_prob"] / 100) * (r["odds"] - 1.0) * bet
            - (1.0 - r["fair_prob"] / 100) * bet
            for r in rows
        )
        return {
            "count":        n,
            "sim_risque":   round(n * bet, 2),
            "sim_gain_max": round(gain_max, 2),
            "sim_gain_esp": round(gain_espere, 2),
        }

    def _sport_stats(opps, sport_list, today_str):
        tonight_exc = [o for o in opps
                       if "Excellent" in o.recommendation and o.match.date == today_str]
        tonight_preview = _sim1(tonight_exc) if tonight_exc else {
            "count": 0, "avg_fair_prob": 0, "avg_odds": 0,
            "sim_risque": 0, "sim_gain_max": 0, "sim_gain_esp": 0,
        }
        champion_rows        = [o for o in sport_list if o["champion"]]
        champion_tonight_rows = [o for o in champion_rows if o.get("date") == today_str]
        champion_preview = _sim_champions(champion_rows) if champion_rows else {
            "count": 0, "sim_risque": 0, "sim_gain_max": 0, "sim_gain_esp": 0,
        }
        # Précision prévue pour les Champions ce soir uniquement
        if champion_tonight_rows:
            avg_fp = sum(o["fair_prob"] for o in champion_tonight_rows) / len(champion_tonight_rows)
            champion_tonight_acc = {
                "count":         len(champion_tonight_rows),
                "avg_fair_prob": round(avg_fp, 1),
                "avg_odds":      round(sum(o["odds"] for o in champion_tonight_rows) / len(champion_tonight_rows), 2),
            }
        else:
            champion_tonight_acc = {"count": 0, "avg_fair_prob": 0, "avg_odds": 0}
        return tonight_preview, champion_preview, champion_tonight_acc

    _old_stats = _old.get("stats", {})
    _empty_preview = {"count": 0, "avg_fair_prob": 0, "avg_odds": 0,
                      "sim_risque": 0, "sim_gain_max": 0, "sim_gain_esp": 0}
    _empty_champ   = {"count": 0, "sim_risque": 0, "sim_gain_max": 0, "sim_gain_esp": 0}
    _empty_champ_acc = {"count": 0, "avg_fair_prob": 0, "avg_odds": 0}

    t_ss = time.time()
    if refresh_hockey:
        h_tonight, h_champion, h_champion_acc = _sport_stats(hockey_opps, hockey_list, today_str)
    else:
        h_tonight      = _old_stats.get("tonight_preview",       _empty_preview)
        h_champion     = _old_stats.get("champion_preview",      _empty_champ)
        h_champion_acc = _old_stats.get("champion_tonight_acc",  _empty_champ_acc)

    print(f"  [timing] sport_stats: {time.time()-t_ss:.1f}s")

    # Compter les matchs et opportunités (fusionner avec l'ancien si partiel)
    t_cnt = time.time()
    fresh_hockey_count = len({(o.match.home_team, o.match.away_team)
                              for o in (hockey_opps or [])}) \
                         or sum(1 for m in matches if m.sport == "hockey")
    print(f"  [timing] counts: {time.time()-t_cnt:.1f}s")

    print(f"  [timing] _build_payload TOTAL: {time.time()-t0:.1f}s")

    analyst_summary = _generate_analyst_summary(hockey_list) if refresh_hockey \
                      else _old.get("analyst_summary", "") if _old else ""

    return {
        "hockey":            hockey_list,
        "analyst_summary":   analyst_summary,
        "sgp_proposals":     sgp_proposals,
        "kelly_warning":     not _any_kelly_pos,
        "low_game_count":    fresh_hockey_count < 3,
        "stats": {
            "total_matches":   len(matches),
            "hockey_count":    fresh_hockey_count if refresh_hockey
                               else _old_stats.get("hockey_count", 0),
            "excellent_h":     sum(1 for o in (hockey_opps or []) if "Excellent" in o.recommendation)
                               if refresh_hockey else _old_stats.get("excellent_h", 0),
            "demo":            demo,
            "tonight_preview":      h_tonight,
            "champion_preview":     h_champion,
            "champion_tonight_acc": h_champion_acc,
        },
    }


# ─── APScheduler: 5 AM Pre-scrape ─────────────────────────────────────────────────

def _preschedule_5am():
    """5 AM pré-scrape: Scrape Hockey NHL et met à jour le cache pour la journée.

    IMPORTANT: utilise _run_analysis (pas _build_payload) pour que le cache mémoire
    ET le cache disque (.payload_cache.json) soient mis à jour. Sinon le résultat
    du scrape serait jeté et l'utilisateur devrait attendre au matin.
    """
    print("[5AM] Pre-scrape démarré...")
    try:
        # Retry jusqu'à 3 fois si Mise-O-Jeu retourne 0 matchs
        for attempt in range(3):
            _run_analysis(demo=False, sports=["hockey"])
            data = _cache.get("data") or {}
            h = len(data.get("hockey") or [])
            if h > 0:
                print(f"[5AM] Pre-scrape OK — {h} matchs hockey en cache (tentative {attempt+1}/3)")
                return
            if attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"[5AM] Pre-scrape vide (tentative {attempt+1}/3) — réessai dans {wait}s...")
                time.sleep(wait)
        print("[5AM] Pre-scrape — tous les retries échoués (cache pas mis à jour)")
    except Exception as e:
        print(f"[5AM] Pre-scrape erreur: {e}")


def _init_scheduler():
    """Initialise APScheduler pour lancer le pré-scrape à 5 AM ET."""
    try:
        et_tz = pytz.timezone('US/Eastern')
        scheduler = BackgroundScheduler(timezone=et_tz)
        scheduler.add_job(
            func=_preschedule_5am,
            trigger=CronTrigger(hour=5, minute=0, timezone=et_tz),
            id='preschedule_5am',
            name='Pre-scrape at 5 AM ET',
            replace_existing=True
        )
        scheduler.start()
        print("  >> APScheduler démarré (5 AM pre-scrape activé)")
    except Exception as e:
        print(f"  >> APScheduler erreur: {e}")


def _run_analysis(demo: bool = False, sports: list | None = None):
    global _cache
    with _lock:
        # Si on a déjà des données, on marque "stale" sans bloquer l'UI
        if _cache["data"] is not None:
            _cache["stale"]  = True
        else:
            _cache["status"] = "loading"
        _cache["error"] = None

    # Timeout de 10s : si le scraping prend trop long, on affiche quand même les anciennes données
    def _timeout_callback():
        time.sleep(10)
        with _lock:
            if _cache.get("status") == "loading" or _cache.get("stale"):
                # Après 10s, forcer stale=False pour libérer l'UI
                if _cache.get("data"):
                    _cache["stale"] = False
                    print(f"  >> Timeout scrape 10s — données stale libérées")

    threading.Thread(target=_timeout_callback, daemon=True).start()

    try:
        payload = _build_payload(demo=demo, sports=sports)
        ts   = _get_et_now().strftime("%H:%M:%S")
        date = _get_today_et()
        now_epoch = time.time()
        with _lock:
            _cache["data"]      = payload
            _cache["timestamp"] = ts
            _cache["date"]      = date
            _cache["status"]    = "ready"
            _cache["stale"]     = False
            _cache["epoch_ts"]  = now_epoch   # pour cache_age_min dans /api/status
        # Persister sur disque (seulement pour les données réelles, pas la démo)
        if not demo:
            _save_payload_cache(payload, ts, date)
    except Exception as e:
        with _lock:
            # Si on avait des données stale, les garder affichables
            # plutôt que forcer un état d'erreur qui vide l'UI
            has_data = _cache.get("data") is not None
            if has_data:
                _cache["status"] = "ready"
                _cache["stale"]  = True
            else:
                _cache["status"] = "error"
                _cache["stale"]  = False
            _cache["error"] = str(e)


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    """Page de login"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # Vérifier les credentials
        if username == _LOGIN_USERNAME and password == _LOGIN_PASSWORD:
            user = User(username)
            login_user(user)
            return redirect(url_for('index'))
        else:
            return render_template("login.html", error="Identifiants invalides")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    """Logout et rediriger vers login"""
    logout_user()
    return redirect(url_for('login'))

@app.route("/api/change-password", methods=["POST"])
@login_required
def api_change_password():
    """Changer le mot de passe de l'utilisateur connecté"""
    global _LOGIN_PASSWORD

    data = request.get_json() or {}
    current_pwd = data.get("current_password", "").strip()
    new_pwd = data.get("new_password", "").strip()

    # Valider le mot de passe actuel
    if current_pwd != _LOGIN_PASSWORD:
        return jsonify({"error": "Mot de passe actuel incorrect"}), 401

    # Valider le nouveau mot de passe
    if not new_pwd or len(new_pwd) < 4:
        return jsonify({"error": "Le nouveau mot de passe doit contenir au moins 4 caracteres"}), 400

    if current_pwd == new_pwd:
        return jsonify({"error": "Le nouveau mot de passe doit etre different de l'ancien"}), 400

    # Mettre à jour le mot de passe en mémoire et en variable d'environnement
    _LOGIN_PASSWORD = new_pwd
    os.environ['APP_PASSWORD'] = new_pwd

    return jsonify({"message": "Mot de passe change avec succes"}), 200

@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/mockup_hockey.html")
def mockup_hockey():
    return app.send_static_file("../mockup_hockey.html") if False else \
        open(os.path.join(os.path.dirname(__file__), "mockup_hockey.html"), encoding="utf-8").read(), 200, {"Content-Type": "text/html; charset=utf-8"}



@app.route("/api/status")
def api_status():
    _check_date_rollover()
    with _lock:
        epoch = _cache.get("epoch_ts") or _cache_epoch
        cache_age_min = round((time.time() - epoch) / 60, 1) if epoch else 999
        return jsonify({
            "status":        _cache["status"],
            "timestamp":     _cache["timestamp"],
            "error":         _cache["error"],
            "stale":         _cache.get("stale", False),
            "cache_age_min": cache_age_min,   # âge en minutes → frontend évite refresh BG si frais
        })


@app.route("/api/data")
def api_data():
    _check_date_rollover()
    with _lock:
        if _cache["status"] != "ready":
            return jsonify({"error": "Données non disponibles"}), 404
        return jsonify(_cache["data"])


# ─── Snapshot des mises ────────────────────────────────────────────────────────
# Utilise /data/ sur Fly.io (volume persistant) ou le répertoire courant en local
_DATA_DIR          = os.environ.get('DATA_DIR', os.path.dirname(__file__))
_SNAPSHOT_PATH     = os.path.join(_DATA_DIR, "snapshot.json")
_SNAPSHOTS_DIR     = os.path.join(_DATA_DIR, "snapshots")
_REAL_BETS_DIR     = os.path.join(_DATA_DIR, "real_bets")
_BALANCE_LOG_PATH  = os.path.join(_DATA_DIR, "balance_log.json")

# ─── Initialize data from repo if volume is empty ────────────────────────────────
# On Fly.io, /data is a volume that starts empty. Copy data files from repo if needed.
def _initialize_data_from_repo():
    """Copy data files from project repo to /data volume if they don't exist."""
    import shutil

    data_dir = os.environ.get('DATA_DIR')
    if not data_dir:  # Only on Fly.io where DATA_DIR=/data
        print("[INIT] DATA_DIR not set, skipping data restore")
        return

    # Try multiple possible project root locations
    project_root = os.path.dirname(__file__)
    possible_roots = [
        project_root,
        '/app',
        '/app/src',
        os.getcwd(),
    ]

    actual_root = None
    for root in possible_roots:
        if os.path.exists(os.path.join(root, 'snapshot.json')) or os.path.isdir(os.path.join(root, 'snapshots')):
            actual_root = root
            print(f"[INIT] Found data files in {root}")
            break

    if not actual_root:
        print(f"[INIT] No data files found in any of: {possible_roots}")
        return

    project_root = actual_root
    data_files = ['snapshot.json', 'predictions.json', 'balance_log.json']
    data_dirs = ['snapshots', 'real_bets', 'nhl_cache']

    print(f"[INIT] Checking for data files in {project_root}")
    print(f"[INIT] Target data directory: {data_dir}")

    # Copy individual files
    for filename in data_files:
        repo_path = os.path.join(project_root, filename)
        data_path = os.path.join(data_dir, filename)

        repo_exists = os.path.exists(repo_path)
        data_exists = os.path.exists(data_path)

        print(f"[INIT] {filename}: repo={repo_exists}, data={data_exists}")

        # If file exists in repo but not in /data, copy it
        if repo_exists and not data_exists:
            try:
                shutil.copy2(repo_path, data_path)
                print(f"[INIT] ✓ Restored {filename} to {data_dir}")
            except Exception as e:
                print(f"[INIT] ✗ Failed to copy {filename}: {e}")
        elif data_exists:
            print(f"[INIT] {filename} already exists in {data_dir}")

    # Copy directories (important: sync if repo has more files)
    for dirname in data_dirs:
        repo_path = os.path.join(project_root, dirname)
        data_path = os.path.join(data_dir, dirname)

        repo_exists = os.path.isdir(repo_path)
        data_exists = os.path.isdir(data_path)

        repo_count = len(os.listdir(repo_path)) if repo_exists else 0
        data_count = len(os.listdir(data_path)) if data_exists else 0

        print(f"[INIT] {dirname}/: repo={repo_exists}({repo_count}), data={data_exists}({data_count})")

        # Copy if: repo has files AND (data doesn't exist OR repo has more files than data)
        if repo_exists and repo_count > 0 and (not data_exists or repo_count > data_count):
            try:
                # Remove old dir if it exists and is incomplete
                if data_exists:
                    shutil.rmtree(data_path)
                    print(f"[INIT] Removed incomplete {dirname}/")

                shutil.copytree(repo_path, data_path)
                new_count = len(os.listdir(data_path))
                print(f"[INIT] ✓ Restored {dirname}/ ({new_count} files) to {data_dir}")
            except Exception as e:
                print(f"[INIT] ✗ Failed to copy {dirname}/: {e}")
        elif data_exists and repo_count == data_count:
            print(f"[INIT] {dirname}/ complete ({data_count} files)")
        elif data_exists:
            print(f"[INIT] {dirname}/ exists but incomplete ({data_count}/{repo_count} files)")

# Call initialization on startup
print("[INIT] ===== DATA BOOTSTRAP STARTING =====", flush=True)
_initialize_data_from_repo()
print("[INIT] ===== DATA BOOTSTRAP COMPLETE =====", flush=True)

# ─── Cache NHL outcomes ────────────────────────────────────────────────────────
# Cache disque : résultats passés figés (ne changent jamais)
_NHL_CACHE_DIR     = os.path.join(_DATA_DIR, "nhl_cache")
# Cache mémoire partagé entre tous les endpoints (session serveur)
_NHL_OUTCOMES_CACHE: dict = {}

@app.route("/api/save-snapshot", methods=["POST"])
def api_save_snapshot():
    """Sauvegarde le tableau affiché (paris + mises) dans snapshot.json
    ET dans snapshots/YYYY-MM-DD.json pour l'historique."""
    body = request.json or {}
    picks = body.get("picks", [])
    if not picks:
        return jsonify({"error": "Aucun pari fourni"}), 400

    today = _get_today_et()
    snapshot = {
        "saved_at":      _get_et_now().isoformat(),
        "date":          today,
        "time":          datetime.now().strftime("%H:%M"),
        "sgp_proposals": body.get("sgp_proposals") or _generate_sgp_proposals(picks),
        "picks":    [
            {
                "key":            p.get("key", ""),
                "match":          p.get("match", ""),
                "home_team":      p.get("home_team", ""),
                "away_team":      p.get("away_team", ""),
                "selection":      p.get("selection", ""),
                "bet_type":       p.get("bet_type", ""),
                "odds":           p.get("odds"),
                "fair_prob":      p.get("fair_prob"),
                "value_score":    p.get("value_score"),
                "mise":           p.get("mise"),
                "recommendation": p.get("recommendation", ""),
                "champion":       p.get("champion", False),
            }
            for p in picks
        ],
    }

    # Snapshot courant (affiché dans Résultats NEW)
    with open(_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    # Historique : snapshots/YYYY-MM-DD.json (écrase si même jour)
    os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
    with open(daily_path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  >> Snapshot sauvegardé : {len(snapshot['picks'])} paris à {snapshot['time']}")
    return jsonify({"ok": True, "saved": len(snapshot["picks"]), "time": snapshot["time"]})


# ─── Cron auto-snapshot (request-driven, idempotent) ───────────────────────────
_AUTO_SNAPSHOT_LOCK = os.path.join(_DATA_DIR, "last_auto_snapshot.txt")

@app.route("/api/cron/auto-snapshot", methods=["GET", "POST"])
def api_cron_auto_snapshot():
    """Endpoint cron déclenché par UptimeRobot (~5 min). Idempotent.

    Logique :
    1. Lit la lockfile pour voir si déjà fait aujourd'hui → skip
    2. Récupère les picks du jour depuis le cache
    3. Calcule l'heure cible = premier match - 30 min
    4. Si now >= target ET pas fait → save snapshot + email
    5. Écrit la lockfile

    Avantage vs thread Python : exécuté à chaque requête HTTP, donc tolère
    le sleep des plateformes hosting (Render, Fly.io, etc.).
    """
    today = _get_today_et()
    now_et = _get_et_now()

    # 0. Auto-update des outcomes en BG (throttle : max 1×/10 min)
    #    Ça permet aux résultats des matchs d'hier de se résoudre automatiquement
    #    sans clic manuel sur "Actualiser".
    global _LAST_AUTO_OUTCOMES_TS
    try:
        _LAST_AUTO_OUTCOMES_TS
    except NameError:
        _LAST_AUTO_OUTCOMES_TS = 0
    if time.time() - _LAST_AUTO_OUTCOMES_TS > 600:  # 10 min
        _LAST_AUTO_OUTCOMES_TS = time.time()
        def _bg_auto_outcomes():
            try:
                from predictions import update_outcomes
                update_outcomes()
                print(f"  [auto-outcomes] update_outcomes done at {now_et.strftime('%H:%M ET')}")
            except Exception as e:
                print(f"  [auto-outcomes] erreur: {e}")
        threading.Thread(target=_bg_auto_outcomes, daemon=True).start()

    # 1. Lock : déjà fait aujourd'hui ?
    if os.path.exists(_AUTO_SNAPSHOT_LOCK):
        try:
            with open(_AUTO_SNAPSHOT_LOCK) as f:
                last_date = f.read().strip()
            if last_date == today:
                return jsonify({
                    "ok": True,
                    "skipped": "already_done_today",
                    "last_date": last_date,
                    "now": now_et.strftime("%Y-%m-%d %H:%M ET"),
                })
        except Exception:
            pass

    # 2. Picks du jour depuis le cache
    all_picks = (_cache.get("data") or {}).get("hockey") or []
    today_picks = [p for p in all_picks if p.get("date") == today]

    if not today_picks:
        return jsonify({
            "ok": True,
            "skipped": "no_picks_today",
            "cache_size": len(all_picks),
            "now": now_et.strftime("%Y-%m-%d %H:%M ET"),
        })

    # 3. Heure du premier match → calcul de la fenêtre cible
    times = sorted({p.get("time") for p in today_picks if p.get("time")})
    if not times:
        return jsonify({"ok": True, "skipped": "no_match_times"})

    first_time = times[0]  # ex. "19:00"
    try:
        h, m = map(int, first_time.split(":"))
        target = now_et.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(minutes=30)
    except Exception as e:
        return jsonify({"ok": False, "error": f"format heure invalide: {first_time}"}), 400

    if now_et < target:
        return jsonify({
            "ok": True,
            "skipped": "before_target_window",
            "now": now_et.strftime("%H:%M"),
            "target": target.strftime("%H:%M"),
            "first_match": first_time,
            "minutes_until": int((target - now_et).total_seconds() // 60),
        })

    # 4. Déclenchement : save snapshot + email
    try:
        snapshot = {
            "saved_at":      now_et.isoformat(),
            "date":          today,
            "time":          now_et.strftime("%H:%M"),
            "auto":          True,
            "first_match":   first_time,
            "sgp_proposals": _generate_sgp_proposals(today_picks),
            "picks": [
                {
                    "key":            p.get("key", ""),
                    "match":          p.get("match", ""),
                    "home_team":      p.get("home_team", ""),
                    "away_team":      p.get("away_team", ""),
                    "selection":      p.get("selection", ""),
                    "bet_type":       p.get("bet_type", ""),
                    "odds":           p.get("odds"),
                    "fair_prob":      p.get("fair_prob"),
                    "value_score":    p.get("value_score"),
                    "mise":           p.get("mise"),
                    "recommendation": p.get("recommendation", ""),
                    "champion":       p.get("champion", False),
                }
                for p in today_picks
            ],
        }

        # Snapshot courant
        with open(_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        # Snapshot historique (par date)
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
        daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
        with open(daily_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        print(f"  [auto-snapshot] {len(today_picks)} paris sauvegardés à {snapshot['time']} (1er match: {first_time})")

        # Email (best-effort, n'empêche pas le snapshot d'être marqué OK)
        email_result = {"ok": False, "message": "non tenté"}
        try:
            from email_service import send_betting_summary
            MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
                       "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
            ds = f"{now_et.day} {MOIS_FR[now_et.month - 1]} {now_et.year}"
            email_result = send_betting_summary(today_picks, ds, sgp_proposals=snapshot["sgp_proposals"])
            print(f"  [auto-snapshot] Email: {email_result.get('message', 'envoyé')}")
        except Exception as e:
            print(f"  [auto-snapshot] Email erreur: {e}")
            email_result = {"ok": False, "message": str(e)}

        # 5. Lock pour idempotence
        try:
            with open(_AUTO_SNAPSHOT_LOCK, "w") as f:
                f.write(today)
        except Exception as e:
            print(f"  [auto-snapshot] Lock write erreur: {e}")

        return jsonify({
            "ok": True,
            "snapshot_saved": True,
            "picks_count": len(today_picks),
            "first_match": first_time,
            "snapshot_time": snapshot["time"],
            "email": email_result,
        })

    except Exception as e:
        print(f"  [auto-snapshot] ERREUR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/snapshot-sgp")
def api_snapshot_sgp():
    """Retourne les sgp_proposals du snapshot sauvegardé aujourd'hui (figés)."""
    today = _get_today_et()
    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
    if not os.path.exists(daily_path):
        return jsonify({"sgp_proposals": [], "saved_at": None})
    try:
        with open(daily_path, encoding="utf-8-sig") as f:
            snap = json.load(f)
        return jsonify({
            "sgp_proposals": snap.get("sgp_proposals", []),
            "saved_at":      snap.get("time", ""),
        })
    except Exception:
        return jsonify({"sgp_proposals": [], "saved_at": None})


@app.route("/api/snapshot")
def api_snapshot():
    """Retourne le snapshot sauvegardé (ou 404 si absent)."""
    if not os.path.exists(_SNAPSHOT_PATH):
        return jsonify({"error": "Aucun snapshot disponible"}), 404
    with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return jsonify(data)


@app.route("/api/live-snapshot")
def api_live_snapshot():
    """Retourne le snapshot du jour courant (snapshots/YYYY-MM-DD.json)."""
    today = _get_today_et()
    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")

    if os.path.exists(daily_path):
        with open(daily_path, encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)

    if os.path.exists(_SNAPSHOT_PATH):
        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == today:
            return jsonify(data)

    return jsonify({"error": "Aucun snapshot pour aujourd'hui"}), 404


@app.route("/api/live-results")
def api_live_results():
    """Snapshot du jour + résultats NHL en temps réel depuis nhl.com."""
    today = _get_today_et()

    # Charger le snapshot du jour
    snap = None
    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
    if os.path.exists(daily_path):
        with open(daily_path, encoding="utf-8") as f:
            snap = json.load(f)
    elif os.path.exists(_SNAPSHOT_PATH):
        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
            candidate = json.load(f)
        if candidate.get("date") == today:
            snap = candidate

    if not snap:
        # Pas de snapshot : retourner quand même les scores NHL du jour pour le carrousel
        snap = {"picks": [], "date": today, "time": None}

    # Fetch scores NHL du jour via /v1/score/now (matchs en cours + terminés)
    import requests as _req
    try:
        resp = _req.get(
            "https://api-web.nhle.com/v1/score/now",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=10,
        )
        resp.raise_for_status()
        nhl_date = resp.json().get("gameDate", today)
        # Si l'API retourne la date d'hier (pas encore de matchs aujourd'hui), fallback
        if nhl_date != today:
            resp2 = _req.get(
                f"https://api-web.nhle.com/v1/score/{today}",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            resp2.raise_for_status()
            nhl_map = _nhl_outcomes_for_date(today)
        else:
            nhl_map = _nhl_outcomes_for_date(today)
    except Exception:
        nhl_map = {}

    # Enrichir chaque pick avec son outcome
    picks_out = []
    for p in snap.get("picks", []):
        raw = _resolve_pick_outcome(p, nhl_map)
        # _resolve_pick_outcome peut retourner un dict {"outcome":..,"score":..} ou une string
        if isinstance(raw, dict):
            outcome_str = raw.get("outcome", "pending")
            score_str   = raw.get("score", "")
        else:
            outcome_str = raw or "pending"
            score_str   = ""
        picks_out.append({**p, "outcome": outcome_str, "score": score_str})

    # Trouver les scores bruts pour affichage
    game_scores = {}
    for (away_nick, home_nick), g in nhl_map.items():
        key = f"{away_nick}@{home_nick}"
        game_scores[key] = {
            "away_score":      g["away_score"],
            "home_score":      g["home_score"],
            "state":           g["state"],
            "away_name":       g["away_name"],
            "home_name":       g["home_name"],
            "away_abbrev":     g.get("away_abbrev", ""),
            "home_abbrev":     g.get("home_abbrev", ""),
            "start_time_utc":  g.get("start_time_utc", ""),
            "period":          g.get("period", 0),
            "period_type":     g.get("period_type", "REG"),
            "time_remaining":  g.get("time_remaining", ""),
            "in_intermission": g.get("in_intermission", False),
        }

    return jsonify({
        "date":        snap.get("date"),
        "time":        snap.get("time"),
        "picks":       picks_out,
        "game_scores": game_scores,
        "fetched_at":  _get_et_now().strftime("%H:%M:%S"),
    })


def _nhl_outcomes_for_date(date_str: str) -> dict:
    """Fetches nhl.com scores and returns {(away_nick, home_nick): {winner, away_score, home_score, state}}.

    Utilise un cache à 2 niveaux pour les dates passées :
      1. Cache mémoire  (_NHL_OUTCOMES_CACHE) — partagé, persiste toute la session serveur
      2. Cache disque   (nhl_cache/YYYY-MM-DD.json) — survit aux redémarrages
    Aujourd'hui n'est jamais mis en cache disque (résultats pas encore finals).
    """
    import re

    today = _get_today_et()
    is_past = date_str < today

    # ── 1. Cache mémoire (dates passées seulement — aujourd'hui est toujours refetché) ──
    if is_past and date_str in _NHL_OUTCOMES_CACHE:
        return _NHL_OUTCOMES_CACHE[date_str]

    # ── 2. Cache disque (dates passées seulement) ─────────────────────────────
    if is_past:
        disk_path = os.path.join(_NHL_CACHE_DIR, f"{date_str}.json")
        if os.path.exists(disk_path):
            try:
                with open(disk_path, encoding="utf-8") as _f:
                    cached = json.load(_f)
                # Reconstruire les clés tuple depuis les strings "A|B"
                results = {tuple(k.split("|", 1)): v for k, v in cached.items()}
                # Valider : tous les matchs doivent être finaux ("OFF"/"FINAL")
                # Sinon le cache est obsolète (match était en cours quand sauvegardé) → refetch
                if results and all(g.get("state") in ("OFF", "FINAL") for g in results.values()):
                    _NHL_OUTCOMES_CACHE[date_str] = results
                    return results
                # Cache obsolète → on continue vers le refetch API
            except Exception:
                pass  # Cache corrompu → on refetch

    # ── 3. Appel API NHL ──────────────────────────────────────────────────────
    url = f"https://api-web.nhle.com/v1/score/{date_str}"
    try:
        import requests as _req
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp  = _req.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        games = resp.json().get("games", [])
    except Exception as e:
        print(f"  >> NHL API erreur: {e}")
        return {}

    def nick(name_str):
        """Extrait le surnom de 'City (Nickname)' ou retourne le mot final de 'short name'."""
        m = re.search(r'\(([^)]+)\)', name_str or "")
        if m:
            return m.group(1).lower()
        return (name_str or "").lower().strip()

    results = {}
    for g in games:
        home = g.get("homeTeam", {})
        away = g.get("awayTeam", {})
        hs = home.get("score", 0) or 0
        as_ = away.get("score", 0) or 0
        state = g.get("gameState", "")
        home_name = home.get("name", {}).get("default", home.get("abbrev", ""))
        away_name = away.get("name", {}).get("default", away.get("abbrev", ""))
        winner = None
        if state in ("OFF", "FINAL"):
            winner = "home" if hs > as_ else "away"
        period_desc = g.get("periodDescriptor", {})
        clock       = g.get("clock", {})
        period_num  = period_desc.get("number", 0)
        period_type = period_desc.get("periodType", "REG")
        in_intermission = clock.get("inIntermission", False)
        time_remaining  = clock.get("timeRemaining", "")
        game_data = {
            "winner": winner,
            "home_score": hs,
            "away_score": as_,
            "state": state,
            "home_name": home_name,
            "away_name": away_name,
            "home_abbrev": home.get("abbrev", ""),
            "away_abbrev": away.get("abbrev", ""),
            "start_time_utc": g.get("startTimeUTC", ""),
            "period": period_num,
            "period_type": period_type,
            "time_remaining": time_remaining,
            "in_intermission": in_intermission,
        }
        away_ab = away.get("abbrev", "").upper()
        home_ab = home.get("abbrev", "").upper()
        if away_ab and home_ab:
            results[(away_ab, home_ab)] = game_data
        results[(nick(away_name), nick(home_name))] = game_data

    # ── Mettre en cache UNIQUEMENT si TOUS les matchs sont terminés ──
    # Empêche de figer un cache obsolète quand un match late-night n'est pas encore fini
    # (ex: match Vegas qui commence à 22h ET = 2h UTC lendemain)
    all_final = all(g.get("state") in ("OFF", "FINAL") for g in results.values())

    if is_past and all_final:
        _NHL_OUTCOMES_CACHE[date_str] = results

    # Cache disque uniquement pour les dates passées dont tous les matchs sont finals
    if is_past and all_final and results:
        try:
            os.makedirs(_NHL_CACHE_DIR, exist_ok=True)
            disk_path = os.path.join(_NHL_CACHE_DIR, f"{date_str}.json")
            # Sérialiser les clés tuple en strings "A|B" pour JSON
            serializable = {f"{k[0]}|{k[1]}": v for k, v in results.items()}
            with open(disk_path, "w", encoding="utf-8") as _f:
                json.dump(serializable, _f, ensure_ascii=False)
        except Exception as e:
            print(f"  >> NHL cache disque erreur: {e}")

    return results


def _warm_nhl_cache(dates: list) -> None:
    """Précharge les outcomes NHL pour plusieurs dates en parallèle.
    Utilise ThreadPoolExecutor pour éviter les appels séquentiels.
    Seules les dates absentes du cache mémoire sont fetchées.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    missing = [d for d in dates if d not in _NHL_OUTCOMES_CACHE]
    if not missing:
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_nhl_outcomes_for_date, d): d for d in missing}
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"  >> warm_nhl_cache erreur {futures[fut]}: {e}")


def _get_player_points_for_game(player_name: str, away_abbrev: str, home_abbrev: str, game_date: str) -> int | None:
    """
    Récupère le nombre de points d'un joueur dans un match spécifique.
    Utilise l'endpoint /score/{YYYY-MM-DD} de l'API NHL et parse les goals/assists
    directement depuis le tableau goals[] pour éviter un 2e appel API.
    Retourne le nombre de points (goals + assists) ou None si non trouvé.
    """
    try:
        import requests
        # L'API NHL utilise le format YYYY-MM-DD (avec tirets)
        date_formatted = game_date  # Déjà en format YYYY-MM-DD

        resp = requests.get(
            f"https://api-web.nhle.com/v1/score/{date_formatted}",
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if not resp.ok:
            print(f"  >> _get_player_points_for_game: API {resp.status_code} pour {date_formatted}")
            return None

        games = resp.json().get("games", [])

        # Trouver le match par abréviations
        game = None
        for g in games:
            g_away = g.get("awayTeam", {}).get("abbrev", "")
            g_home = g.get("homeTeam", {}).get("abbrev", "")
            if g_away == away_abbrev and g_home == home_abbrev:
                game = g
                break
            # Fallback: chercher sans contraindre les deux équipes (si une seule abbrev connue)
            if away_abbrev and g_away == away_abbrev:
                game = g
            elif home_abbrev and g_home == home_abbrev:
                game = g

        if not game:
            print(f"  >> _get_player_points_for_game: match {away_abbrev}@{home_abbrev} non trouvé le {date_formatted}")
            return None

        # Compter goals + assists du joueur depuis le tableau goals[]
        player_name_lower = player_name.lower().strip()
        goals_count = 0
        assists_count = 0

        for goal in game.get("goals", []):
            # Vérifier si le joueur a marqué ce but
            scorer_name = goal.get("name", {}).get("default", "").lower()
            if _names_match(player_name_lower, scorer_name):
                goals_count += 1

            # Vérifier si le joueur a une passe sur ce but
            for assist in goal.get("assists", []):
                assist_name = assist.get("name", {}).get("default", "").lower()
                if _names_match(player_name_lower, assist_name):
                    assists_count += 1

        total_points = goals_count + assists_count
        print(f"  >> _get_player_points_for_game: {player_name} = {total_points} pts ({goals_count}G {assists_count}A)")
        return total_points

    except Exception as e:
        print(f"  >> _get_player_points_for_game erreur: {e}")
        return None


def _names_match(query: str, full_name: str) -> bool:
    """Vérifie si un nom de joueur matche partiellement (ex: 'P. Martone' matche 'Porter Martone')."""
    if not query or not full_name:
        return False
    # Match exact
    if query in full_name or full_name in query:
        return True
    # Match par nom de famille seulement (ex: "martone" dans "porter martone")
    query_parts = query.split()
    full_parts  = full_name.split()
    if len(query_parts) >= 2 and len(full_parts) >= 2:
        # Comparer nom de famille (dernier mot)
        if query_parts[-1] == full_parts[-1]:
            return True
    return False


def _resolve_pick_outcome(pick: dict, nhl_map: dict, game_date: str = ""):
    """Détermine win/loss/pending pour un pari en croisant avec les résultats NHL."""
    import re
    bet_type_raw = (pick.get("bet_type") or "")   # Conserver la casse originale pour prop detection
    bet_type  = bet_type_raw.lower()
    selection = (pick.get("selection") or "").lower()
    snap_date = game_date or pick.get("date", "")

    # ── Prop bets joueur — vérifier EN PREMIER (avant les checks "total"/"moins") ──
    # Format: "Prénom Nom Total de points plus/moins 0.5"
    # _is_player_prop() a besoin de la casse originale (majuscules) pour détecter le nom
    if _is_player_prop(type('o', (), {'bet_type': bet_type_raw})()):
        m_thresh = re.search(r"(\d+[.,]?\d*)", bet_type)
        threshold = float(m_thresh.group(1).replace(',', '.')) if m_thresh else None
        if threshold is not None:
            # Extraire le nom du joueur (les 2 premiers mots du bet_type original)
            player_name = ' '.join(bet_type_raw.split()[:2]).strip()
            # Résoudre les abréviations d'équipes pour la recherche
            try:
                from nhl_stats import _match_abbrev as _ma
                _away = (pick.get("away_team") or "").lower()
                _home = (pick.get("home_team") or "").lower()
                _away_ab = (_ma(_away) or "").upper()
                _home_ab = (_ma(_home) or "").upper()
            except Exception:
                _away_ab = _home_ab = ""
            player_points = _get_player_points_for_game(player_name, _away_ab, _home_ab, snap_date)
            if player_points is not None:
                is_under = "moins" in selection or "under" in selection
                is_over  = "plus"  in selection or "over"  in selection
                if is_under:
                    return {"outcome": "win" if player_points < threshold else "loss", "score": "?"}
                if is_over:
                    return {"outcome": "win" if player_points > threshold else "loss", "score": "?"}
        return {"outcome": "unsupported", "score": "?"}
    home_team = (pick.get("home_team") or "").lower()
    away_team = (pick.get("away_team") or "").lower()

    # Fallback : extraire depuis le champ match "AWAY @ HOME" si home/away vides
    if not home_team or not away_team:
        match_str = pick.get("match") or ""
        if " @ " in match_str:
            parts = match_str.split(" @ ", 1)
            if not away_team: away_team = parts[0].lower()
            if not home_team: home_team = parts[1].lower()

    def nick(s):
        m = re.search(r'\(([^)]+)\)', s)
        return m.group(1).lower() if m else s.strip()

    # Essayer d'abord le matching par abréviation NHL (robuste vs noms français/anglais)
    try:
        from nhl_stats import _match_abbrev as _ma
        away_ab = (_ma(away_team) or "").upper()
        home_ab = (_ma(home_team) or "").upper()
    except Exception:
        away_ab = home_ab = ""

    # Toujours calculer away_nick/home_nick (utilisés plus loin pour sel_is_home/away)
    away_nick = nick(away_team)
    home_nick = nick(home_team)

    game = None
    if away_ab and home_ab:
        game = nhl_map.get((away_ab, home_ab))

    # Fallback : matching par nick (sous-chaîne)
    if game is None:
        if not away_nick or not home_nick:
            return {"outcome": "not_found", "score": "?"}
        for (an, hn), g in nhl_map.items():
            if (away_nick in an or an in away_nick) and (home_nick in hn or hn in home_nick):
                game = g
                break

    if game is None:
        return {"outcome": "not_found", "score": "?"}

    state = game["state"]
    score_txt = f"{game['away_score']}-{game['home_score']}"

    if state in ("LIVE", "CRIT"):
        return {"outcome": "in_progress", "score": score_txt}
    if state not in ("OFF", "FINAL"):
        # Pas encore commencé (PRE, FUT, etc.) → not_found permet au frontend d'afficher l'heure
        return {"outcome": "not_found", "score": "?"}

    winner = game["winner"]  # "home" | "away"

    # Trouver quelle équipe est sélectionnée
    sel_nick = nick(selection) if "(" in selection else selection
    sel_is_home = home_nick in sel_nick or sel_nick in home_nick
    sel_is_away = away_nick in sel_nick or sel_nick in away_nick

    # Victoire / Gagnant / Double chance (équipe)
    moneyline_kw = ["victoire", "gagnant", "vainqueur", "à 2 issues", "a 2 issues", "2 issues", "double chance"]
    if any(kw in bet_type for kw in moneyline_kw):
        if sel_is_home:
            outcome = "win" if winner == "home" else "loss"
        elif sel_is_away:
            outcome = "win" if winner == "away" else "loss"
        else:
            outcome = "unsupported"
        return {"outcome": outcome, "score": score_txt}

    # Plus/Moins de buts
    if any(kw in bet_type for kw in ["plus", "moins", "total", "over", "under"]):
        # Détecter si c'est un total d'équipe (ex: "Ottawa (Sénateurs) Total de buts")
        # vs total du match (ex: "Total de buts plus/moins 6.5")
        # Utiliser le nom de ville (avant la parenthèse) pour éviter les problèmes d'accents
        def city_name(s):
            return s.split('(')[0].strip().lower()
        bt_lower   = bet_type.lower()
        home_city  = city_name(home_team)
        away_city  = city_name(away_team)
        if home_city and "total" in bt_lower and home_city in bt_lower:
            score_to_use = game["home_score"]
        elif away_city and "total" in bt_lower and away_city in bt_lower:
            score_to_use = game["away_score"]
        else:
            score_to_use = game["home_score"] + game["away_score"]

        m = re.search(r"(\d+\.?\d*)", bet_type)
        threshold = float(m.group(1)) if m else None
        if threshold is not None:
            if "plus" in selection or "over" in selection:
                outcome = "win" if score_to_use > threshold else "loss"
            else:
                outcome = "win" if score_to_use < threshold else "loss"
            return {"outcome": outcome, "score": score_txt}

    # Les 2 équipes marquent
    if any(kw in bet_type for kw in ["les 2", "both", "2 équipes", "2 equipes"]):
        hs = game["home_score"]
        as_ = game["away_score"]
        outcome = "win" if hs > 0 and as_ > 0 else "loss"
        return {"outcome": outcome, "score": score_txt}


    return {"outcome": "unsupported", "score": score_txt}


@app.route("/api/snapshot-results")
def api_snapshot_results():
    """Snapshot de la VEILLE + résultats depuis nhl.com (aucun scraping Mise-O-Jeu).
    Toujours afficher le snapshot d'hier — peu importe si un nouveau snapshot a été sauvegardé aujourd'hui."""
    from datetime import timedelta
    today     = _get_today_et()

    # 1. Chercher d'abord le fichier du jour précédent dans snapshots/
    snap = None
    if os.path.isdir(_SNAPSHOTS_DIR):
        # Tous les fichiers de dates passées (avant aujourd'hui), triés décroissant
        past_files = sorted(
            [f for f in os.listdir(_SNAPSHOTS_DIR) if f.endswith(".json") and f[:10] < today],
            reverse=True,
        )
        if past_files:
            with open(os.path.join(_SNAPSHOTS_DIR, past_files[0]), encoding="utf-8") as f:
                snap = json.load(f)

    # 2. Fallback sur snapshot.json s'il est d'hier
    if snap is None:
        if not os.path.exists(_SNAPSHOT_PATH):
            return jsonify({"error": "Aucun snapshot disponible"}), 404
        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
            snap = json.load(f)
        if snap.get("date", "") == today:
            return jsonify({"error": "snapshot_today", "date": today}), 404

    snap_date = snap.get("date", "")
    nhl_map   = _nhl_outcomes_for_date(snap_date)

    # Aussi chercher les résultats du jour suivant (matchs qui ont terminé après minuit)
    try:
        next_day = (datetime.strptime(snap_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        nhl_map_next = _nhl_outcomes_for_date(next_day)
        nhl_map = {**nhl_map, **nhl_map_next}  # Fusionner (jour suivant écrase en cas de doublon)
    except:
        pass  # Fallback silencieux si parsing de date échoue

    enriched = []
    for pick in snap.get("picks", []):
        resolved = _resolve_pick_outcome(pick, nhl_map, snap_date)
        enriched.append({**pick, **resolved})

    # Calculer kelly_warning rétroactivement (même logique que /api/history)
    def _hk_snap(p):
        fp = float(p.get("fair_prob") or 0)
        if fp > 1: fp /= 100
        b = float(p.get("odds") or 1) - 1
        if b <= 0: return 0
        return max(0, (fp * b - (1 - fp)) / b / 2)

    # Nombre de matchs uniques dans le snapshot
    n_matches_snap = len({p.get("match") or "" for p in enriched if p.get("match")})
    kelly_warning_snap = not any(_hk_snap(p) > 0 for p in enriched)

    return jsonify({
        "date":          snap_date,
        "saved_at":      snap.get("saved_at"),
        "time":          snap.get("time"),
        "nhl_games":     len(nhl_map),
        "n_matches":     n_matches_snap,
        "kelly_warning": kelly_warning_snap,
        "picks":         enriched,
    })


@app.route("/api/debug-scrape")
def api_debug_scrape():
    """Endpoint temporaire pour diagnostiquer le scraper dans le contexte Flask."""
    import traceback
    try:
        from scraper import scrape_all_sync
        matches = scrape_all_sync(headless=True)
        return jsonify({"count": len(matches), "sports": [m.sport for m in matches[:5]]})
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    body = request.json or {}
    demo = body.get("demo", False)

    # sport = "hockey" | null (tous)
    # Mapper le nom d'onglet vers la liste de sports du scraper
    _sport_map = {
        "hockey": ["hockey"],
    }
    sport_param = body.get("sport", None)
    sports = _sport_map.get(sport_param, None)  # None = tous les sports

    data_cur = _cache.get("data") or {}
    has_data = bool(data_cur.get("hockey") or [])
    if _cache["status"] == "loading" or (_cache.get("stale") and has_data):
        return jsonify({"error": "Analyse déjà en cours"}), 409
    thread = threading.Thread(target=_run_analysis, args=(demo, sports), daemon=True)
    thread.start()
    return jsonify({"status": "started", "sports": sports or "all"})


@app.route("/api/balance", methods=["GET"])
def api_balance_get():
    """Retourne l'historique des soldes enregistrés manuellement."""
    if not os.path.exists(_BALANCE_LOG_PATH):
        return jsonify({"entries": []})
    with open(_BALANCE_LOG_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    return jsonify({"entries": entries})


@app.route("/api/balance", methods=["POST"])
def api_balance_post():
    """Enregistre un nouveau solde."""
    body = request.json or {}
    balance = body.get("balance")
    if balance is None:
        return jsonify({"error": "Solde manquant"}), 400
    try:
        balance = round(float(balance), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "Solde invalide"}), 400

    from datetime import timedelta
    yesterday = datetime.now() - timedelta(days=1)
    entry = {
        "date":    yesterday.strftime("%Y-%m-%d"),
        "time":    "23:59",
        "balance": balance,
        "note":    body.get("note", ""),
    }

    entries = []
    if os.path.exists(_BALANCE_LOG_PATH):
        with open(_BALANCE_LOG_PATH, encoding="utf-8") as f:
            entries = json.load(f)

    # Écraser si même date
    entries = [e for e in entries if e.get("date") != entry["date"]]
    entries.append(entry)
    entries.sort(key=lambda e: (e["date"], e["time"]))

    with open(_BALANCE_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)

    return jsonify({"ok": True, "entry": entry})


@app.route("/api/save-real-bets", methods=["POST"])
def api_save_real_bets():
    """Sauvegarde les mises réelles saisies manuellement par l'utilisateur."""
    body = request.json or {}
    picks = body.get("picks", [])
    if not picks:
        return jsonify({"error": "Aucune mise saisie"}), 400

    now = datetime.now()
    session = {
        "saved_at": now.isoformat(),
        "date":     now.strftime("%Y-%m-%d"),
        "time":     now.strftime("%H:%M"),
        "picks":    picks,
    }

    os.makedirs(_REAL_BETS_DIR, exist_ok=True)
    fname = now.strftime("%Y-%m-%d") + ".json"  # 1 seul fichier par jour, écrase l'ancien
    with open(os.path.join(_REAL_BETS_DIR, fname), "w", encoding="utf-8") as f:
        json.dump(session, f, ensure_ascii=False, indent=2)

    print(f"  >> Mises réelles sauvegardées : {len(picks)} paris à {session['time']}")
    return jsonify({"ok": True, "saved": len(picks), "time": session["time"]})


@app.route("/api/real-bets")
def api_real_bets():
    """Retourne toutes les sessions de mises réelles avec outcomes résolus via l'API NHL."""
    if not os.path.isdir(_REAL_BETS_DIR):
        return jsonify({"sessions": []})

    sessions = []
    for fname in sorted(os.listdir(_REAL_BETS_DIR), reverse=True):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(_REAL_BETS_DIR, fname), encoding="utf-8") as f:
                session = json.load(f)
        except Exception:
            continue

        date    = session.get("date", "")
        nhl_map = _nhl_outcomes_for_date(date)

        # Aussi chercher les résultats du jour suivant (matchs qui ont terminé après minuit)
        try:
            next_day = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
            nhl_map_next = _nhl_outcomes_for_date(next_day)
            nhl_map = {**nhl_map, **nhl_map_next}  # Fusionner
        except:
            pass

        enriched = []
        for p in session.get("picks", []):
            outcome_data = _resolve_pick_outcome(p, nhl_map)
            mise  = float(p.get("mise_reelle") or 0)
            odds  = float(p.get("odds") or 1)
            outcome = outcome_data.get("outcome")
            if outcome == "win":
                net = round(mise * (odds - 1), 2)
            elif outcome == "loss":
                net = round(-mise, 2)
            else:
                net = None
            enriched.append({**p, **outcome_data, "net": net})

        total_mise = round(sum(float(p.get("mise_reelle") or 0) for p in session["picks"]), 2)
        net_total  = round(sum(p["net"] for p in enriched if p["net"] is not None), 2)

        sessions.append({
            "saved_at":   session.get("saved_at"),
            "date":       date,
            "time":       session.get("time"),
            "picks":      enriched,
            "total_mise": total_mise,
            "net_total":  net_total,
        })

    return jsonify({"sessions": sessions})


@app.route("/api/compare-systems")
def api_compare_systems():
    """Compare 4 stratégies de mise sur les mêmes picks historiques.

    Système A : ½ Kelly (sizing existant, champ `mise` du snapshot)
    Système B : Mise plate ($X fixe, même picks que A)
    Système C : ½ Kelly, filtré sur edge >= seuil (haute conviction seulement)
    Système D : Kelly plafonné (mise = min(Kelly, plafond))

    Query params :
        flat     : montant mise plate Système B (défaut 10)
        edge_min : seuil edge Système C en % (défaut 7)
        cap      : plafond Système D en $ (défaut 10)
    """
    flat_amt       = float(request.args.get("flat", 10))
    edge_min       = float(request.args.get("edge_min", 7))
    cap_amt        = float(request.args.get("cap", 5))
    bankroll_start = float(request.args.get("bankroll_start", 100))
    nightly_pct    = float(request.args.get("nightly_pct", 10))
    topn           = max(1, int(request.args.get("topn", 3)))
    excluded       = {"2026-03-19", "2026-03-20"}
    _MIN_BET       = 0.50  # mise minimale pour E

    snap_dir = _SNAPSHOTS_DIR
    if not os.path.isdir(snap_dir):
        return jsonify({"days": [], "summary": {}, "picks": []})

    today      = _get_today_et()
    all_picks  = []
    days_out   = []
    bankroll_e = bankroll_start  # bankroll dynamique Système E

    for fname in sorted(os.listdir(snap_dir)):
        if not fname.endswith(".json"):
            continue
        date_str = fname[:-5]
        if date_str in excluded or date_str >= today:
            continue

        try:
            with open(os.path.join(snap_dir, fname), encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            continue

        nhl_map = _nhl_outcomes_for_date(date_str)

        # ── Filtre kelly_warning : logique identique à /api/history ─────────
        # Soirée exclue si aucun pick avec Kelly > 0 OU ≤ 3 matchs uniques
        # Support both old format (picks) and new format (sgp_proposals)
        snap_picks = snap.get("picks", [])
        if not snap_picks and snap.get("sgp_proposals"):
            # Extract picks from sgp_proposals (new format)
            for proposal in snap.get("sgp_proposals", []):
                for pick in proposal.get("picks", []):
                    # Flatten the structure to match old format
                    snap_picks.append({
                        **pick,
                        "match": proposal.get("match"),
                        "bet_type": pick.get("bet_type"),
                    })

        def _hk_day(p):
            fp = float(p.get("fair_prob") or 0)
            if fp > 1: fp /= 100          # normalise 0-100 → 0-1 si besoin
            b = float(p.get("odds") or 1) - 1
            if b <= 0: return 0.0
            return max(0.0, (fp * b - (1 - fp)) / b / 2)

        n_matches_day = len({p.get("match") or "" for p in snap_picks if p.get("match")})
        kelly_warning = not any(_hk_day(p) > 0 for p in snap_picks)
        if kelly_warning:
            continue  # soirée non conseillée → ignorée dans tous les systèmes

        # ── Passe 1 : collecter tous les picks valides du jour ────────────────
        # (nécessaire pour distribuer le budget E proportionnellement)
        valid_day = []
        for p in snap_picks:
            mise_a = float(p.get("mise") or 0)
            if mise_a <= 0:
                continue
            odds = float(p.get("odds") or 0)
            fp   = float(p.get("fair_prob") or 0)
            if odds <= 1 or fp <= 0:
                continue
            outcome_data = _resolve_pick_outcome(p, nhl_map)
            outcome = outcome_data.get("outcome")
            if outcome not in ("win", "loss"):
                continue
            # Fraction ½ Kelly brute (pour distribuer le budget E)
            b  = odds - 1
            hk = max(0.0, (fp / 100 * b - (1 - fp / 100)) / b / 2)
            valid_day.append({
                "p": p, "mise_a": mise_a, "odds": odds, "fp": fp,
                "outcome": outcome, "edge": fp - (100.0 / odds), "hk": hk,
            })

        if not valid_day:
            continue  # aucun pick résolu ce soir → on saute

        # ── Calcul des mises Système E (Kelly × bankroll dynamique) ──────────
        budget_e = round(bankroll_e * nightly_pct / 100, 2) if bankroll_e >= _MIN_BET else 0.0
        e_mises  = []
        if valid_day and budget_e >= _MIN_BET:
            total_hk = sum(pk["hk"] for pk in valid_day)
            if total_hk > 0:
                raw = [pk["hk"] / total_hk * budget_e for pk in valid_day]
            else:
                eq  = budget_e / len(valid_day)
                raw = [eq] * len(valid_day)
            # Arrondir à 0.50$ près, minimum _MIN_BET
            e_mises = [max(round(r * 2) / 2, _MIN_BET) for r in raw]
            # Réajuster pour coller exactement au budget (même logique que _apply_mises)
            diff = round((budget_e - sum(e_mises)) * 2) / 2
            if diff != 0 and e_mises:
                mx = e_mises.index(max(e_mises))
                e_mises[mx] = max(round((e_mises[mx] + diff) * 2) / 2, _MIN_BET)
        else:
            e_mises = [0.0] * len(valid_day)

        # ── Calcul redistribution Système C (Haute conviction) ────────────────
        total_a = sum(pk["mise_a"] for pk in valid_day)
        total_c_active = sum(pk["mise_a"] for pk in valid_day if pk["edge"] >= edge_min)
        montant_redistribue = total_a - total_c_active if total_c_active > 0 else 0

        # ── Calcul mises Système F — Edge² (quadratique) ──────────────────────
        # Mise proportionnelle à edge², total = total_a. Favorise les très hauts edges.
        edge_sq = [max(pk["edge"], 0) ** 2 for pk in valid_day]
        total_edge_sq = sum(edge_sq)
        if total_edge_sq > 0:
            f_mises = [max(round(sq / total_edge_sq * total_a * 2) / 2, 0.0) for sq in edge_sq]
        else:
            eq = round(total_a / len(valid_day) * 2) / 2 if valid_day else 0
            f_mises = [eq] * len(valid_day)

        # ── Calcul mises Système G — Top N du soir ────────────────────────────
        # Sélectionner les topn meilleurs edges, redistribuer total_a proportionnellement
        sorted_by_edge = sorted(range(len(valid_day)), key=lambda i: valid_day[i]["edge"], reverse=True)
        top_n_set = set(sorted_by_edge[:topn])
        top_n_total_hk = sum(valid_day[i]["hk"] for i in top_n_set)
        g_mises = []
        for i in range(len(valid_day)):
            if i in top_n_set and top_n_total_hk > 0:
                g_mises.append(max(round(valid_day[i]["hk"] / top_n_total_hk * total_a * 2) / 2, 0.0))
            else:
                g_mises.append(0.0)

        # ── Passe 2 : calculer A/B/C/D/E pour chaque pick et accumuler ───────
        day = {
            "date": date_str,
            "A": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0},
            "B": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0},
            "C": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0},
            "D": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0},
            "E": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0,
                  "bankroll_before": round(bankroll_e, 2)},
            "F": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0},
            "G": {"mise": 0, "net": 0, "picks": 0, "wins": 0, "losses": 0},
        }

        day_e_net = 0.0
        for i, pk in enumerate(valid_day):
            p       = pk["p"]
            mise_a  = pk["mise_a"]
            odds    = pk["odds"]
            fp      = pk["fp"]
            edge    = pk["edge"]
            outcome = pk["outcome"]
            mise_e  = e_mises[i]
            mise_f  = f_mises[i]
            mise_g  = g_mises[i]

            # A : ½ Kelly (fixe)
            net_a = round(mise_a * (odds - 1), 2) if outcome == "win" else round(-mise_a, 2)
            # B : mise plate
            mise_b = flat_amt
            net_b  = round(mise_b * (odds - 1), 2) if outcome == "win" else round(-mise_b, 2)
            # C : haute conviction (avec redistribution proportionnelle)
            c_active = edge >= edge_min
            if c_active and total_c_active > 0 and montant_redistribue > 0:
                # Redistribuer proportionnellement au poids de ce pick dans total_c_active
                proportion = mise_a / total_c_active
                mise_c = round((mise_a + proportion * montant_redistribue) * 2) / 2
            else:
                mise_c = mise_a if c_active else 0
            net_c    = (round(mise_c * (odds - 1), 2) if outcome == "win" else round(-mise_c, 2)) if c_active else 0
            # D : Kelly plafonné
            mise_d = min(mise_a, cap_amt)
            net_d  = round(mise_d * (odds - 1), 2) if outcome == "win" else round(-mise_d, 2)
            # E : Kelly dynamique
            net_e  = round(mise_e * (odds - 1), 2) if (outcome == "win" and mise_e > 0) else (round(-mise_e, 2) if mise_e > 0 else 0)
            day_e_net += net_e
            # F : Edge² (quadratique)
            net_f  = round(mise_f * (odds - 1), 2) if (outcome == "win" and mise_f > 0) else (round(-mise_f, 2) if mise_f > 0 else 0)
            # G : Top N du soir
            net_g  = round(mise_g * (odds - 1), 2) if (outcome == "win" and mise_g > 0) else (round(-mise_g, 2) if mise_g > 0 else 0)

            all_picks.append({
                "date": date_str,
                "match": p.get("match", ""),
                "selection": p.get("selection", ""),
                "bet_type": p.get("bet_type", ""),
                "odds": odds, "fair_prob": fp, "edge": round(edge, 2), "outcome": outcome,
                "A_mise": mise_a, "A_net": net_a,
                "B_mise": mise_b, "B_net": net_b,
                "C_active": c_active, "C_mise": mise_c, "C_net": net_c,
                "D_mise": mise_d, "D_net": net_d, "D_capped": mise_a > cap_amt,
                "E_mise": mise_e, "E_net": net_e,
                "E_bankroll": round(bankroll_e, 2),
                "F_mise": mise_f, "F_net": net_f,
                "G_mise": mise_g, "G_net": net_g, "G_active": mise_g > 0,
            })

            for sys_key, mise_v, net_v, active in [
                ("A", mise_a, net_a, True),
                ("B", mise_b, net_b, True),
                ("C", mise_c, net_c, c_active),
                ("D", mise_d, net_d, True),
                ("E", mise_e, net_e, mise_e > 0),
                ("F", mise_f, net_f, mise_f > 0),
                ("G", mise_g, net_g, mise_g > 0),
            ]:
                if not active:
                    continue
                day[sys_key]["picks"]  += 1
                day[sys_key]["mise"]   += mise_v
                day[sys_key]["net"]    += net_v
                if outcome == "win":
                    day[sys_key]["wins"]   += 1
                else:
                    day[sys_key]["losses"] += 1

        # Mettre à jour le bankroll E (plancher à 0)
        bankroll_e = max(0.0, round(bankroll_e + day_e_net, 2))
        day["E"]["bankroll_after"] = bankroll_e

        for sys_key in ("A", "B", "C", "D", "E", "F", "G"):
            day[sys_key]["net"]  = round(day[sys_key]["net"], 2)
            day[sys_key]["mise"] = round(day[sys_key]["mise"], 2)
        days_out.append(day)

    # ── Cumulatifs jour par jour ──────────────────────────────────────────────
    cum = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0, "E": 0.0, "F": 0.0, "G": 0.0}
    for d in days_out:
        for sys_key in ("A", "B", "C", "D", "E", "F", "G"):
            cum[sys_key] = round(cum[sys_key] + d[sys_key]["net"], 2)
            d[sys_key]["cumulative"] = cum[sys_key]

    # ── Summary global ────────────────────────────────────────────────────────
    def _sys_summary(sys_key):
        picks      = sum(d[sys_key]["picks"]  for d in days_out)
        wins       = sum(d[sys_key]["wins"]   for d in days_out)
        losses     = sum(d[sys_key]["losses"] for d in days_out)
        total_mise = sum(d[sys_key]["mise"]   for d in days_out)
        net        = cum[sys_key]
        roi        = round(net / total_mise * 100, 1) if total_mise else 0
        win_rate   = round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0
        result = {
            "picks": picks, "wins": wins, "losses": losses,
            "cumulative": net, "roi": roi, "win_rate": win_rate,
            "total_mise": round(total_mise, 2),
        }
        if sys_key == "E":
            result["initial_bankroll"] = bankroll_start
            result["final_bankroll"]   = round(bankroll_e, 2)
            result["roi"] = round((bankroll_e - bankroll_start) / bankroll_start * 100, 1) if bankroll_start else 0
        return result

    return jsonify({
        "days":    days_out,
        "summary": {"A": _sys_summary("A"), "B": _sys_summary("B"),
                    "C": _sys_summary("C"), "D": _sys_summary("D"),
                    "E": _sys_summary("E"), "F": _sys_summary("F"),
                    "G": _sys_summary("G")},
        "picks":   all_picks,
        "params":  {"flat": flat_amt, "edge_min": edge_min, "cap": cap_amt,
                    "bankroll_start": bankroll_start, "nightly_pct": nightly_pct,
                    "topn": topn},
    })


# ─── Endpoint /api/mispricing ────────────────────────────────────────────────────
# Détection d'anomalies de cotation sur 10 sportsbooks (Phase 2: Anomalies)
_mispricing_cache = {"data": None, "timestamp": None, "ttl": 300}  # 5 min TTL

@app.route("/api/mispricing")
def api_mispricing():
    """
    Fetch NHL odds from multiple sportsbooks and detect mispricing anomalies.

    Returns:
        {
            "anomalies": [ {...}, ... ],
            "count": int,
            "timestamp": float,
            "cached": bool,
            "sources": ["mise-o-jeu", "draftkings", "fanduel", ...]
        }
    """
    global _mispricing_cache

    try:
        # Check cache validity (5 min TTL)
        now = time.time()
        if (_mispricing_cache["data"] is not None and
            _mispricing_cache["timestamp"] is not None and
            now - _mispricing_cache["timestamp"] < _mispricing_cache["ttl"]):

            return jsonify({
                "anomalies": [a.to_dict() for a in _mispricing_cache["data"]],
                "count": len(_mispricing_cache["data"]),
                "timestamp": _mispricing_cache["timestamp"],
                "cached": True,
                "sources": ["mise-o-jeu", "draftkings", "fanduel", "betmgm", "caesars",
                           "betano", "unibet", "888sport", "pointsbet", "playolg"]
            })

        # Fetch all odds from all sources
        all_odds = []

        # 1. Mise-o-Jeu (existing scraper)
        try:
            moj_picks = _scrape_cached(sports=["hockey"])
            for pick in moj_picks:
                if pick.selection:
                    all_odds.append({
                        "away": pick.away_team,
                        "home": pick.home_team,
                        "selection": pick.selection.label,
                        "odds": pick.selection.odds,
                        "bet_type": pick.bet_group.bet_type,
                        "source": "Mise-o-Jeu",
                        "date": _get_today_et(),
                    })
        except Exception as e:
            print(f"[WARNING] Mise-o-Jeu scrape error: {e}")

        # 2. The Odds API (covers 8 sportsbooks)
        try:
            from scrapers.odds_api import fetch_odds_api_nhl
            odds_api_picks = fetch_odds_api_nhl()
            all_odds.extend(odds_api_picks)
        except Exception as e:
            print(f"[WARNING] The Odds API error: {e}")

        # 3. DraftKings (optional - requires draftkings library)
        try:
            from scrapers.draftkings import fetch_draftkings_nhl
            dk_picks = fetch_draftkings_nhl()
            all_odds.extend(dk_picks)
        except Exception as e:
            print(f"[WARNING] DraftKings fetch error: {e}")

        # Detect anomalies
        if all_odds:
            from scrapers.compare_odds import detect_anomalies
            anomalies = detect_anomalies(all_odds, min_deviation_pct=2.0)  # Réduit de 10% à 2% pour plus de sensibilité
        else:
            anomalies = []

        # Save anomalies to history
        if anomalies:
            today_date = _get_today_et()
            history_dir = os.path.join(os.path.dirname(__file__), "anomalies_history")
            os.makedirs(history_dir, exist_ok=True)
            history_file = os.path.join(history_dir, f"{today_date}.json")

            try:
                # Load existing data for today
                history_data = []
                if os.path.exists(history_file):
                    with open(history_file, 'r', encoding='utf-8') as f:
                        history_data = json.load(f)

                # Add new anomalies (deduplicate by creating a set of keys)
                existing_keys = {(a['match'], a['selection'], a['outlier']['sportsbook']) for a in history_data}

                for anom in anomalies:
                    anom_dict = anom.to_dict()
                    key = (anom_dict['match'], anom_dict['selection'], anom_dict['outlier']['sportsbook'])
                    if key not in existing_keys:
                        anom_dict['detected_at'] = _get_et_now().isoformat()
                        history_data.append(anom_dict)

                # Save updated history
                with open(history_file, 'w', encoding='utf-8') as f:
                    json.dump(history_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[WARNING] Could not save anomaly history: {e}")

        # Cache results
        _mispricing_cache["data"] = anomalies
        _mispricing_cache["timestamp"] = now

        return jsonify({
            "anomalies": [a.to_dict() for a in anomalies],
            "count": len(anomalies),
            "timestamp": now,
            "cached": False,
            "sources": ["mise-o-jeu", "draftkings", "fanduel", "betmgm", "caesars",
                       "betano", "unibet", "888sport", "pointsbet", "playolg"]
        })

    except Exception as e:
        print(f"[ERROR] api_mispricing error: {e}")
        return jsonify({"error": str(e), "anomalies": [], "count": 0}), 500


@app.route("/api/anomalies-history")
def api_anomalies_history():
    """Get anomaly detection history (trend over time)"""
    try:
        history_dir = os.path.join(os.path.dirname(__file__), "anomalies_history")
        history_by_date = {}

        if not os.path.exists(history_dir):
            return jsonify({"days": [], "summary": {}})

        # Load all days
        for fname in sorted(os.listdir(history_dir)):
            if not fname.endswith('.json'):
                continue

            date = fname[:-5]
            filepath = os.path.join(history_dir, fname)

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    anomalies = json.load(f)

                # Count by severity
                high = sum(1 for a in anomalies if a.get('severity') == 'high')
                medium = sum(1 for a in anomalies if a.get('severity') == 'medium')
                low = sum(1 for a in anomalies if a.get('severity') == 'low')

                history_by_date[date] = {
                    "date": date,
                    "total": len(anomalies),
                    "high": high,
                    "medium": medium,
                    "low": low
                }
            except Exception as e:
                print(f"[WARNING] Error reading {fname}: {e}")

        # Calculate summary stats
        total_anomalies = sum(d['total'] for d in history_by_date.values())
        avg_per_day = total_anomalies / len(history_by_date) if history_by_date else 0

        return jsonify({
            "days": list(history_by_date.values()),
            "summary": {
                "total_anomalies": total_anomalies,
                "days_tracked": len(history_by_date),
                "avg_per_day": round(avg_per_day, 1)
            }
        })

    except Exception as e:
        print(f"[ERROR] api_anomalies_history error: {e}")
        return jsonify({"error": str(e), "days": [], "summary": {}}), 500


@app.route("/api/yesterday-hockey")
def api_yesterday_hockey():
    """Retourne les prédictions hockey d'hier avec leurs résultats."""
    try:
        from predictions import _load, get_profitable_odds_ranges
        from nhl_stats import _match_abbrev
        from datetime import date, timedelta

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Mettre à jour les résultats manquants avant de retourner les données
        try:
            update_outcomes()
        except Exception:
            pass
        all_preds = _load()

        # Filtrer pour hier + hockey — on utilise le champ sport (fiable après
        # _repair_missing_sport appelé dans update_outcomes ci-dessus)
        hockey_preds = [
            p for p in all_preds
            if p.get("date") == yesterday
            and p.get("sport") == "hockey"
        ]

        # Zones de cotes rentables (fallback pour les anciennes prédictions sans flag stocké)
        profitable = get_profitable_odds_ranges(min_samples=5)

        def _recalc_champion(odds, rec):
            return (
                "Excellent" in (rec or "")
                and any(lo <= odds < hi for lo, hi, _ in profitable)
            )

        BET = 2.0
        result = []
        for p in hockey_preds:
            odds = float(p.get("odds") or 0)
            gain = round((odds - 1.0) * BET, 2)
            rec  = p.get("recommendation") or ""
            # Utiliser le flag persisté si disponible, sinon recalculer (anciennes prédictions)
            stored = p.get("champion")
            champ  = bool(stored) if stored is not None else _recalc_champion(odds, rec)
            result.append({
                "date":           p.get("date"),
                "home_team":      p.get("home_team"),
                "away_team":      p.get("away_team"),
                "bet_type":       p.get("bet_type"),
                "selection":      p.get("selection"),
                "odds":           odds,
                "gain_2":         gain,
                "recommendation": rec,
                "outcome":        p.get("outcome"),
                "fair_prob":      round(float(p.get("fair_prob") or 0) * 100, 1),
                "champion":       champ,
                "mise":           p.get("mise"),   # Mise Kelly persistée au moment de la présentation
            })

        # Pour les anciennes prédictions sans flag stocké : appliquer limite des 5 champions
        has_stored = any(p.get("champion") is not None for p in hockey_preds)
        if not has_stored:
            MAX_CHAMPIONS = 5
            champ_candidates = sorted(
                [r for r in result if r["champion"]],
                key=lambda r: (r["fair_prob"] / 100) * r["odds"] - 1.0,
                reverse=True,
            )
            top5_champ_ids = {id(r) for r in champ_candidates[:MAX_CHAMPIONS]}
            for r in result:
                if r["champion"] and id(r) not in top5_champ_ids:
                    r["champion"] = False

        # Tri principal : gain potentiel décroissant
        result.sort(key=lambda x: -x["gain_2"])

        # Résumé financier
        resolved   = [r for r in result if r["outcome"] in ("win", "loss")]
        wins       = [r for r in resolved if r["outcome"] == "win"]
        total_gain = sum(r["gain_2"] for r in wins)
        total_lost = BET * sum(1 for r in resolved if r["outcome"] == "loss")
        net        = round(total_gain - total_lost, 2)

        # Heure de la dernière lecture avant le début des matchs
        saved_times = [p.get("saved_at") for p in hockey_preds if p.get("saved_at")]
        snapshot_time = None
        if saved_times:
            last_saved = max(saved_times)          # ISO datetime la plus récente
            try:
                from datetime import datetime as _dt
                snapshot_time = _dt.fromisoformat(last_saved).strftime("%H:%M")
            except Exception:
                snapshot_time = last_saved[:16]    # fallback : prend les 16 premiers chars

        return jsonify({
            "date":          yesterday,
            "snapshot_time": snapshot_time,
            "bets":          result,
            "summary": {
                "total":    len(result),
                "resolved": len(resolved),
                "wins":     len(wins),
                "net":      net,
                "spent":    round(len(resolved) * BET, 2),
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/history")
def api_history():
    """Historique basé uniquement sur les snapshots enregistrés (snapshots/YYYY-MM-DD.json).
    Les outcomes viennent de la même logique que /api/snapshot-results (NHL API).
    Toutes les prédictions du snapshot sont comptées (pas seulement celles avec mise)."""
    try:
        today = _get_today_et()
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)

        # Dates exclues manuellement de l'historique (données non représentatives)
        _excluded_dates = {"2026-03-19", "2026-03-20"}

        # Lire tous les fichiers snapshot passés (exclure aujourd'hui — pas de résultats encore)
        snap_files = sorted([
            f for f in os.listdir(_SNAPSHOTS_DIR)
            if f.endswith(".json")
            and f.replace(".json", "") < today
            and f.replace(".json", "") not in _excluded_dates
        ])

        # ── Précharger tous les outcomes NHL en parallèle ──────────────────
        all_dates = [f.replace(".json", "") for f in snap_files]
        _warm_nhl_cache(all_dates)

        days = []
        for fname in snap_files:
            d = fname.replace(".json", "")
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8-sig") as fh:
                snap = json.load(fh)

            picks = snap.get("picks", [])
            # Backward compatibility: extract from sgp_proposals if picks is empty
            if not picks and snap.get("sgp_proposals"):
                for proposal in snap.get("sgp_proposals", []):
                    for pick in proposal.get("picks", []):
                        picks.append({
                            **pick,
                            "match": proposal.get("match"),
                            "bet_type": pick.get("bet_type"),
                        })
            if not picks:
                continue

            # Outcomes NHL — maintenant depuis le cache (pas d'appel réseau)
            nhl_map = _nhl_outcomes_for_date(d)
            # ⚠️ Ordre correct: pick | resolved → resolved écrase pick (et non l'inverse)
            enriched = [p | _resolve_pick_outcome(p, nhl_map, game_date=d) for p in picks]

            # Stats sur TOUS les picks du snapshot
            resolved = [p for p in enriched if p.get("outcome") in ("win", "loss")]
            wins     = [p for p in resolved if p.get("outcome") == "win"]
            losses   = [p for p in resolved if p.get("outcome") == "loss"]
            win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else None

            # Kelly net = uniquement picks avec mise
            kelly_preds = [p for p in resolved if p.get("mise") is not None]
            k_wins      = [p for p in kelly_preds if p.get("outcome") == "win"]
            k_losses    = [p for p in kelly_preds if p.get("outcome") == "loss"]
            kelly_net   = round(
                sum(p["mise"] * (float(p["odds"]) - 1) for p in k_wins)
                - sum(p["mise"] for p in k_losses),
                2,
            )

            # Kelly warning : aucun pick n'avait d'avantage mathématique positif ce soir-là
            # OU soirée à faible volume (≤ 3 matchs NHL joués ce soir-là)
            def _hk_day(p):
                fp = float(p.get("fair_prob") or 0)
                if fp > 1: fp /= 100
                b  = float(p.get("odds") or 1) - 1
                if b <= 0: return 0
                return max(0, (fp * b - (1 - fp)) / b / 2)
            # Nombre de matchs uniques dans le snapshot ce soir-là
            n_matches_day = len({p.get("match") or "" for p in picks if p.get("match")})
            kelly_warning = not any(_hk_day(p) > 0 for p in picks)

            days.append({
                "date":          d,
                "total":         len(picks),
                "resolved":      len(resolved),
                "wins":          len(wins),
                "losses":        len(losses),
                "win_rate":      win_rate,
                "kelly_bets":    len(kelly_preds),
                "kelly_wins":    len(k_wins),
                "kelly_losses":  len(k_losses),
                "kelly_net":     kelly_net,
                "kelly_warning": kelly_warning,
                "n_matches":     n_matches_day,
            })

        # Cumul progressif
        cumul = 0.0
        for day in days:
            cumul = round(cumul + day["kelly_net"], 2)
            day["cumulative_net"] = cumul

        # Totaux globaux (jours avec au moins 1 résultat connu)
        active = [d for d in days if d["resolved"] > 0]
        total_wins   = sum(d["wins"]   for d in active)
        total_losses = sum(d["losses"] for d in active)
        total_bets   = total_wins + total_losses
        global_win_rate = round(total_wins / total_bets * 100, 1) if total_bets else None
        best_day  = max(active, key=lambda d: d["kelly_net"]) if active else None
        worst_day = min(active, key=lambda d: d["kelly_net"]) if active else None

        return jsonify({
            "days":    days,
            "summary": {
                "total_days":      len(active),
                "total_bets":      total_bets,
                "total_wins":      total_wins,
                "total_losses":    total_losses,
                "global_win_rate": global_win_rate,
                "cumulative_net":  round(cumul, 2),
                "best_day":      best_day,
                "worst_day":     worst_day,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/history-by-bettype")
def api_history_by_bettype():
    """Historique des win rates par type de pari (bet_type) et par date."""
    try:
        today = _get_today_et()
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)

        # Dates exclues manuellement
        _excluded_dates = {"2026-03-19", "2026-03-20"}

        # Lire tous les fichiers snapshot passés
        snap_files = sorted([
            f for f in os.listdir(_SNAPSHOTS_DIR)
            if f.endswith(".json")
            and f[:10] < today
            and f[:10] not in _excluded_dates
        ])

        # Pré-charger les outcomes NHL
        all_dates = list({f[:10] for f in snap_files})
        _warm_nhl_cache(all_dates)

        # Dictionnaire : {bet_type -> [(date, resolved, wins, win_rate), ...]}
        by_bettype = {}

        # D'abord traiter les snapshots individuels
        for fname in snap_files:
            d = fname[:10]
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8-sig") as fh:
                snap = json.load(fh)

            picks = snap.get("picks", [])
            if not picks:
                continue

            # Résoudre les outcomes
            nhl_map = _nhl_outcomes_for_date(d)
            enriched = [p | _resolve_pick_outcome(p, nhl_map, game_date=d) for p in picks]
            resolved_picks = [p for p in enriched if p.get("outcome") in ("win", "loss")]

            # Grouper par bet_type catégorisé (généraliser)
            from collections import defaultdict
            def _categorize_bettype(bt_str):
                """Catégoriser le bet_type en type générique (Total de buts, Combos même match, etc)."""
                bt = (bt_str or "Autre").lower().strip()
                if "combo" in bt and "même" in bt:
                    return "Combos même match"
                elif "total" in bt or "buts" in bt or "plus/moins" in bt:
                    return "Total de buts"
                elif "gagnant" in bt or "victoire" in bt or "winner" in bt or "2 issues" in bt:
                    return "Gagnant"
                elif "points" in bt:
                    return "Total de points"
                else:
                    return "Autre"

            by_type = defaultdict(list)
            for p in resolved_picks:
                bt = _categorize_bettype(p.get("bet_type"))
                by_type[bt].append(p)

            # Calculer stats par type
            for bt, picks_of_type in by_type.items():
                if bt not in by_bettype:
                    by_bettype[bt] = []

                wins = sum(1 for p in picks_of_type if p.get("outcome") == "win")
                resolved = len(picks_of_type)
                wr = round(wins / resolved * 100, 1) if resolved > 0 else None

                by_bettype[bt].append({
                    "date": d,
                    "resolved": resolved,
                    "wins": wins,
                    "win_rate": wr,
                })

        # Ajouter aussi les Combos même match (SGP) — grouper par date les combos résolus
        if "Combos même match" not in by_bettype:
            by_bettype["Combos même match"] = []

        try:
            # Recharger les combos résolus (même logique que /api/sgp-history)
            from collections import defaultdict
            _warm_nhl_cache(all_dates)

            by_combo_date = defaultdict(lambda: {"resolved": 0, "wins": 0})

            for fname in snap_files:
                d = fname[:10]
                snap_path = os.path.join(_SNAPSHOTS_DIR, fname)
                try:
                    with open(snap_path, encoding="utf-8-sig") as fh:
                        snap = json.load(fh)
                except Exception:
                    continue

                sgp_saved = snap.get("sgp_proposals", [])
                if not sgp_saved:
                    continue

                # Ajouter home_team/away_team aux picks
                for sgp in sgp_saved:
                    mparts = (sgp.get("match") or "").split(" @ ")
                    away_fb = mparts[0] if len(mparts) == 2 else ""
                    home_fb = mparts[1] if len(mparts) == 2 else ""
                    for p in sgp.get("picks", []):
                        p.setdefault("match",     sgp.get("match", ""))
                        p.setdefault("home_team", home_fb)
                        p.setdefault("away_team", away_fb)

                # Résoudre les outcomes pour cette date
                nhl_map = _nhl_outcomes_for_date(d)

                # Résoudre chaque combo
                for sgp in sgp_saved:
                    raw_picks = sgp.get("picks", [])
                    if len(raw_picks) < 2:
                        continue

                    resolved_picks = []
                    for p in raw_picks:
                        outcome_info = _resolve_pick_outcome(p, nhl_map, game_date=d)
                        rp = dict(p)
                        rp["outcome"] = outcome_info.get("outcome", "not_found") if isinstance(outcome_info, dict) else "not_found"
                        resolved_picks.append(rp)

                    outcomes = [p["outcome"] for p in resolved_picks]
                    if all(o == "win" for o in outcomes):
                        combo_outcome = "win"
                        by_combo_date[d]["resolved"] += 1
                        by_combo_date[d]["wins"] += 1
                    elif any(o == "loss" for o in outcomes):
                        combo_outcome = "loss"
                        by_combo_date[d]["resolved"] += 1
                    elif all(o == "not_found" for o in outcomes):
                        combo_outcome = "not_found"
                    else:
                        combo_outcome = "partial"

            # Ajouter aux résultats
            for d, stats in sorted(by_combo_date.items()):
                resolved = stats["resolved"]
                wins = stats["wins"]
                wr = round(wins / resolved * 100, 1) if resolved > 0 else None
                if resolved > 0:
                    by_bettype["Combos même match"].append({
                        "date": d,
                        "resolved": resolved,
                        "wins": wins,
                        "win_rate": wr,
                    })
        except Exception as e:
            # Ignorer les erreurs SGP
            pass

        return jsonify({"by_bettype": by_bettype})
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/send-email", methods=["POST", "GET"])
def api_send_email():
    """
    Déclenche manuellement l'envoi du courriel de mises NHL.
    GET  /api/send-email          → envoie avec les données actuelles du cache
    POST /api/send-email          → idem
    """
    from email_service import send_betting_summary, _et_now

    data = _cache.get("data") or {}
    picks        = data.get("hockey") or []
    sgp_proposals = data.get("sgp_proposals") or []

    now_et  = _et_now()
    MOIS_FR = ["janvier","février","mars","avril","mai","juin",
               "juillet","août","septembre","octobre","novembre","décembre"]
    date_str = f"{now_et.day} {MOIS_FR[now_et.month-1]} {now_et.year}"

    result = send_betting_summary(picks, date_str, sgp_proposals=sgp_proposals)
    status_code = 200 if result.get("ok") else 400
    return jsonify(result), status_code



@app.route("/api/send-real-bets-email", methods=["POST"])
def api_send_real_bets_email():
    """Envoie un courriel récapitulatif de toutes les mises réelles."""
    from email_service import send_real_bets_summary

    # Charger les sessions enrichies (réutiliser la logique de api_real_bets)
    sessions = []
    if os.path.isdir(_REAL_BETS_DIR):
        for fname in sorted(os.listdir(_REAL_BETS_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(_REAL_BETS_DIR, fname), encoding="utf-8") as f:
                    session = json.load(f)
            except Exception:
                continue
            date    = session.get("date", "")
            nhl_map = _nhl_outcomes_for_date(date)
            enriched = []
            for p in session.get("picks", []):
                od   = _resolve_pick_outcome(p, nhl_map)
                mise = float(p.get("mise_reelle") or 0)
                odds = float(p.get("odds") or 1)
                oc   = od.get("outcome")
                net  = round(mise * (odds - 1), 2) if oc == "win" else (round(-mise, 2) if oc == "loss" else None)
                enriched.append({**p, **od, "net": net})
            net_total = round(sum(p["net"] for p in enriched if p["net"] is not None), 2)
            sessions.append({**session, "picks": enriched, "net_total": net_total})

    # Balance info depuis balance_log.json
    balance_info = None
    bal_path = _BALANCE_LOG_PATH
    if os.path.isfile(bal_path):
        try:
            with open(bal_path, encoding="utf-8") as f:
                entries = json.load(f)
            if entries:
                entries_sorted = sorted(entries, key=lambda e: e.get("date","") + e.get("time",""))
                balance_info = {
                    "first":  float(entries_sorted[0].get("balance", 0)),
                    "latest": float(entries_sorted[-1].get("balance", 0)),
                }
        except Exception:
            pass

    result = send_real_bets_summary(sessions, balance_info=balance_info)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/records")
def api_records():
    """Retourne les fiches W-L-OTL + L10 pour toutes les équipes NHL."""
    try:
        from nhl_stats import _fetch_standings
        standings = _fetch_standings()
        records = {
            abbrev: {
                "record": f"{s['wins']}-{s['losses']}-{s['otLosses']}",
                "l10":    f"{s['l10Wins']}-{s.get('l10Losses', s['l10GP'] - s['l10Wins'])}",
                "pts":    s.get("points", 0),
            }
            for abbrev, s in standings.items()
        }
        return jsonify(records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sgp-history")
def api_sgp_history():
    """
    Historique des Combos Même Match résolus, basé sur les snapshots sauvegardés.
    Pour chaque snapshot, regroupe les picks Excellent par match, forme des paires
    (Gagnant + Total de buts en priorité) et résout les outcomes via l'API NHL.
    Un combo gagne uniquement si TOUS ses picks gagnent.
    """
    from collections import defaultdict
    today = _get_today_et()
    _excluded = {"2026-03-19", "2026-03-20"}

    if not os.path.isdir(_SNAPSHOTS_DIR):
        return jsonify({"combos": []})

    snap_files = sorted([
        f for f in os.listdir(_SNAPSHOTS_DIR)
        if f.endswith(".json")
        and f[:10] < today
        and f[:10] not in _excluded
    ])

    # ── Précharger tous les outcomes NHL en parallèle ──────────────────────
    _warm_nhl_cache([f[:10] for f in snap_files])

    combos_history = []

    for fname in snap_files:
        d = fname[:10]
        try:
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8-sig") as fh:
                snap = json.load(fh)
        except Exception:
            continue

        # Seulement les combos explicitement sauvegardés avec le snapshot
        sgp_saved = snap.get("sgp_proposals", [])
        if not sgp_saved:
            continue

        # Ajouter home_team/away_team aux picks (nécessaire pour _resolve_pick_outcome)
        for sgp in sgp_saved:
            mparts = (sgp.get("match") or "").split(" @ ")
            away_fb = mparts[0] if len(mparts) == 2 else ""
            home_fb = mparts[1] if len(mparts) == 2 else ""
            for p in sgp.get("picks", []):
                p.setdefault("match",     sgp.get("match", ""))
                p.setdefault("home_team", home_fb)
                p.setdefault("away_team", away_fb)

        # Résoudre les outcomes pour cette date
        nhl_map = _nhl_outcomes_for_date(d)

        # Résoudre chaque combo
        for sgp in sgp_saved:
            raw_picks = sgp.get("picks", [])
            if len(raw_picks) < 2:
                continue

            resolved_picks = []
            for p in raw_picks:
                outcome_info = _resolve_pick_outcome(p, nhl_map, game_date=d)
                rp = dict(p)
                rp["outcome"] = outcome_info.get("outcome", "not_found") if isinstance(outcome_info, dict) else "not_found"
                resolved_picks.append(rp)

            outcomes = [p["outcome"] for p in resolved_picks]
            if all(o == "win" for o in outcomes):
                combo_outcome = "win"
            elif any(o == "loss" for o in outcomes):
                combo_outcome = "loss"
            elif all(o == "not_found" for o in outcomes):
                combo_outcome = "not_found"
            else:
                combo_outcome = "partial"

            combined_odds = float(sgp.get("combined_odds") or 1.0)
            combo_type    = sgp.get("combo_type", "Combo")
            match         = sgp.get("match", "")
            mparts        = match.split(" @ ")
            short_match   = sgp.get("short_match") or (
                f"{_nick_team(mparts[0])} @ {_nick_team(mparts[1])}" if len(mparts) == 2 else match
            )

            combos_history.append({
                "date":          d,
                "match":         match,
                "short_match":   short_match,
                "combo_type":    combo_type,
                "combined_odds": combined_odds,
                "combo_outcome": combo_outcome,
                "combo_mise":    None,
                "combo_gain":    None,
                "picks": [
                    {"bet_type": p.get("bet_type"), "selection": p.get("selection"),
                     "odds": p.get("odds"), "outcome": p.get("outcome")}
                    for p in resolved_picks
                ],
            })

    # Statistiques globales
    resolved = [c for c in combos_history if c["combo_outcome"] in ("win", "loss")]
    wins     = [c for c in resolved if c["combo_outcome"] == "win"]
    win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else None
    total_gain = sum((c["combo_gain"] or 0) for c in wins)
    total_mise = sum((c["combo_mise"] or 0) for c in resolved if c["combo_mise"])
    roi = round((total_gain - total_mise) / total_mise * 100, 1) if total_mise > 0 else None

    return jsonify({
        "combos": sorted(combos_history, key=lambda x: x["date"], reverse=True),
        "summary": {
            "total":    len(combos_history),
            "resolved": len(resolved),
            "wins":     len(wins),
            "win_rate": win_rate,
            "roi":      roi,
        },
    })


@app.route("/api/calibration-snapshots")
def api_calibration_snapshots():
    """
    Performance réelle jour par jour basée sur les snapshots sauvegardés.
    N'inclut que les picks avec une mise Kelly (paris réellement joués).
    Outcomes résolus via l'API NHL pour chaque date passée.
    """
    today = _get_today_et()
    if not os.path.isdir(_SNAPSHOTS_DIR):
        return jsonify({"daily_accuracy": []})

    _excluded_snap_dates = {"2026-03-19", "2026-03-20"}

    past_files = sorted([
        f for f in os.listdir(_SNAPSHOTS_DIR)
        if f.endswith(".json") and f[:10] < today
        and f[:10] not in _excluded_snap_dates
    ])

    # Cache outcomes NHL par date (immuable pour les dates passées)
    _nhl_cache: dict = {}

    daily_accuracy = []
    for fname in past_files:
        date_str = fname[:10]
        try:
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8-sig") as f:
                snap = json.load(f)
        except Exception:
            continue

        picks_all = snap.get("picks", [])
        # Garder uniquement les picks avec une mise Kelly attribuée
        picks = [p for p in picks_all if p.get("mise") is not None]
        if not picks:
            continue

        # Résoudre les outcomes via l'API NHL (avec cache)
        if date_str not in _nhl_cache:
            _nhl_cache[date_str] = _nhl_outcomes_for_date(date_str)
        nhl_map = _nhl_cache[date_str]

        total = len(picks)
        wins = losses = 0
        invested = net = 0.0
        champ_total = champ_wins = 0

        for p in picks:
            resolved = _resolve_pick_outcome(p, nhl_map)
            outcome = resolved.get("outcome", "")
            mise = float(p.get("mise") or 0)
            odds = float(p.get("odds") or 1)

            if outcome == "win":
                wins += 1
                invested += mise
                net += mise * (odds - 1)
            elif outcome == "loss":
                losses += 1
                invested += mise
                net -= mise

        # Réussite champions : tous les picks champion (avec ou sans mise)
        for p in picks_all:
            if not p.get("champion"):
                continue
            resolved = _resolve_pick_outcome(p, nhl_map)
            outcome  = resolved.get("outcome", "")
            if outcome == "win":
                champ_total += 1; champ_wins += 1
            elif outcome == "loss":
                champ_total += 1

        resolved_count = wins + losses
        if resolved_count == 0:
            continue

        win_rate = round(wins / resolved_count * 100, 1)
        roi = round(net / invested * 100, 1) if invested > 0 else 0.0
        champ_win_rate = round(champ_wins / champ_total * 100, 1) if champ_total > 0 else None

        # Nombre de matchs uniques dans le snapshot ce jour-là
        game_count = len(set(p.get("match", "") or p.get("key", "") for p in picks_all if p.get("match") or p.get("key")))

        # Kelly warning : aucun pick n'avait d'avantage mathématique positif ce soir-là
        # OU soirée à faible volume (≤ 3 parties NHL jouées)
        def _hk(p):
            fp = float(p.get("fair_prob") or 0)
            if fp > 1: fp /= 100
            b = float(p.get("odds") or 1) - 1
            if b <= 0: return 0
            return max(0, (fp * b - (1 - fp)) / b / 2)
        kelly_warning_day = not any(_hk(p) > 0 for p in picks_all) or game_count <= 3

        daily_accuracy.append({
            "date":           date_str,
            "total":          resolved_count,
            "wins":           wins,
            "win_rate":       win_rate,
            "roi":            roi,
            "champ_win_rate": champ_win_rate,
            "net":            round(net, 2),
            "invested":       round(invested, 2),
            "game_count":     game_count,
            "kelly_warning":  bool(kelly_warning_day),
        })

    # ── Agrégats globaux ──────────────────────────────────────────────────────
    total_bets = sum(d["total"] for d in daily_accuracy)
    total_wins = sum(d["wins"]  for d in daily_accuracy)
    total_net  = sum(d["net"]   for d in daily_accuracy)
    total_inv  = sum(d["invested"] for d in daily_accuracy)

    global_win_rate = round(total_wins / total_bets * 100, 1) if total_bets > 0 else 0.0
    global_roi      = round(total_net  / total_inv  * 100, 1) if total_inv  > 0 else 0.0

    # Réussite champions (agrégé)
    champ_tot_all  = sum(1 for d in daily_accuracy for _ in range(0))  # reset
    champ_win_all  = 0
    champ_inv_all  = 0.0
    champ_net_all  = 0.0

    # Re-parcourir pour champions (les daily n'ont pas le détail par pick)
    # On utilise les fichiers déjà chargés via _nhl_cache
    for fname in past_files:
        date_str = fname[:10]
        if date_str not in _nhl_cache:
            continue
        try:
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8-sig") as f:
                snap = json.load(f)
        except Exception:
            continue
        nhl_map = _nhl_cache[date_str]
        for p in snap.get("picks", []):
            if not p.get("champion"):
                continue
            resolved = _resolve_pick_outcome(p, nhl_map)
            outcome  = resolved.get("outcome", "")
            mise     = float(p.get("mise") or 0)
            odds     = float(p.get("odds") or 1)
            if outcome == "win":
                champ_tot_all += 1; champ_win_all += 1
                if mise > 0:
                    champ_inv_all += mise; champ_net_all += mise * (odds - 1)
            elif outcome == "loss":
                champ_tot_all += 1
                if mise > 0:
                    champ_inv_all += mise; champ_net_all -= mise

    champ_acc = None
    if champ_tot_all > 0:
        champ_acc = {
            "total":    champ_tot_all,
            "wins":     champ_win_all,
            "win_rate": round(champ_win_all / champ_tot_all * 100, 1),
            "roi":      round(champ_net_all / champ_inv_all * 100, 1) if champ_inv_all > 0 else 0.0,
        }

    # ── Performance par tranche de cotes (depuis snapshots) ───────────────────
    _odds_ranges = [
        ("<1.50",     0.0,  1.50),
        ("1.50-1.70", 1.50, 1.70),
        ("1.70-1.90", 1.70, 1.90),
        ("1.90-2.20", 1.90, 2.20),
        ("2.20+",     2.20, 99.0),
    ]
    # Collecter tous les picks résolus de tous les snapshots
    all_resolved_picks = []
    for fname in past_files:
        date_str = fname[:10]
        if date_str not in _nhl_cache:
            continue
        try:
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8-sig") as f:
                snap = json.load(f)
        except Exception:
            continue
        nhl_map = _nhl_cache[date_str]
        for p in snap.get("picks", []):
            if p.get("mise") is None:
                continue
            resolved = _resolve_pick_outcome(p, nhl_map)
            outcome  = resolved.get("outcome", "")
            if outcome in ("win", "loss"):
                all_resolved_picks.append({
                    "odds":      float(p.get("odds") or 0),
                    "mise":      float(p.get("mise") or 0),
                    "outcome":   outcome,
                    "home_team": p.get("home_team", ""),
                    "away_team": p.get("away_team", ""),
                    "bet_type":  p.get("bet_type", ""),
                    "fair_prob": float(p.get("fair_prob") or 0),
                })

    odds_accuracy = []
    for label, lo, hi in _odds_ranges:
        group = [p for p in all_resolved_picks if lo <= p["odds"] < hi]
        if not group:
            continue
        g_wins   = sum(1 for p in group if p["outcome"] == "win")
        g_wr     = g_wins / len(group)
        avg_odds = sum(p["odds"] for p in group) / len(group)
        roi      = round((g_wr * avg_odds - 1) * 100, 1)
        seuil    = round((1 / avg_odds) * 100, 1) if avg_odds > 0 else 0
        odds_accuracy.append({
            "range":    label,
            "total":    len(group),
            "wins":     g_wins,
            "win_rate": round(g_wr * 100, 1),
            "avg_odds": round(avg_odds, 2),
            "seuil":    seuil,
            "roi":      roi,
            "positive": g_wr * 100 > seuil,
        })

    # ── Réussite par équipe (depuis snapshots) ───────────────────────────────
    team_stats: dict = {}
    for p in all_resolved_picks:
        for team in (p.get("home_team", ""), p.get("away_team", "")):
            if not team:
                continue
            if team not in team_stats:
                team_stats[team] = {"wins": 0, "total": 0}
            team_stats[team]["total"] += 1
            if p["outcome"] == "win":
                team_stats[team]["wins"] += 1

    team_accuracy = sorted(
        [
            {
                "team":     team,
                "total":    v["total"],
                "wins":     v["wins"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1),
            }
            for team, v in team_stats.items()
            if v["total"] >= 1
        ],
        key=lambda x: (-x["wins"], -x["total"]),
    )

    # ── Réussite par type de pari (depuis snapshots) ─────────────────────────
    bt_stats: dict = {}
    for p in all_resolved_picks:
        bt = (p.get("bet_type") or "Inconnu").strip()
        if not bt:
            bt = "Inconnu"
        if bt not in bt_stats:
            bt_stats[bt] = {"wins": 0, "total": 0, "invested": 0.0, "net": 0.0}
        bt_stats[bt]["total"] += 1
        mise = p.get("mise", 0) or 0
        odds = p.get("odds", 0) or 0
        bt_stats[bt]["invested"] += mise
        if p["outcome"] == "win":
            bt_stats[bt]["wins"] += 1
            bt_stats[bt]["net"] += mise * (odds - 1)
        else:
            bt_stats[bt]["net"] -= mise

    bet_type_accuracy = sorted(
        [
            {
                "category": bt,
                "total":    v["total"],
                "wins":     v["wins"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1),
                "roi":      round(v["net"] / v["invested"] * 100, 1) if v["invested"] > 0 else 0.0,
            }
            for bt, v in bt_stats.items()
            if v["total"] >= 1
        ],
        key=lambda x: (-x["wins"], -x["total"]),
    )

    # ── Analyse : performance selon le nombre de parties par jour ────────────
    # Grouper les jours par nombre de parties, calculer win_rate et ROI agrégés
    gc_buckets: dict = {}
    for d in daily_accuracy:
        gc = d.get("game_count", 0)
        if gc == 0:
            continue
        if gc not in gc_buckets:
            gc_buckets[gc] = {"wins": 0, "total": 0, "net": 0.0, "invested": 0.0, "days": 0}
        gc_buckets[gc]["wins"]     += d["wins"]
        gc_buckets[gc]["total"]    += d["total"]
        gc_buckets[gc]["net"]      += d["net"]
        gc_buckets[gc]["invested"] += d["invested"]
        gc_buckets[gc]["days"]     += 1

    games_per_day_stats = sorted(
        [
            {
                "game_count": gc,
                "days":       v["days"],
                "total_bets": v["total"],
                "wins":       v["wins"],
                "win_rate":   round(v["wins"] / v["total"] * 100, 1) if v["total"] > 0 else 0.0,
                "roi":        round(v["net"] / v["invested"] * 100, 1) if v["invested"] > 0 else 0.0,
            }
            for gc, v in gc_buckets.items()
        ],
        key=lambda x: x["game_count"],
    )

    # ── Réussite par jour de la semaine ──────────────────────────────────────────
    _DOW_ORDER = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    _DOW_FR    = {"Monday":"Lundi","Tuesday":"Mardi","Wednesday":"Mercredi",
                  "Thursday":"Jeudi","Friday":"Vendredi","Saturday":"Samedi","Sunday":"Dimanche"}
    dow_buckets: dict = {}
    for d in daily_accuracy:
        try:
            dow = datetime.strptime(d["date"], "%Y-%m-%d").strftime("%A")
        except Exception:
            continue
        if dow not in dow_buckets:
            dow_buckets[dow] = {"wins": 0, "total": 0, "net": 0.0, "invested": 0.0, "dates": set()}
        dow_buckets[dow]["wins"]     += d["wins"]
        dow_buckets[dow]["total"]    += d["total"]
        dow_buckets[dow]["net"]      += d["net"]
        dow_buckets[dow]["invested"] += d["invested"]
        dow_buckets[dow]["dates"].add(d["date"])

    dow_stats = [
        {
            "dow":      _DOW_FR[dow],
            "soirees":  len(dow_buckets[dow]["dates"]),
            "total":    dow_buckets[dow]["total"],
            "wins":     dow_buckets[dow]["wins"],
            "win_rate": round(dow_buckets[dow]["wins"] / dow_buckets[dow]["total"] * 100, 1) if dow_buckets[dow]["total"] else 0,
            "roi":      round(dow_buckets[dow]["net"] / dow_buckets[dow]["invested"] * 100, 1) if dow_buckets[dow]["invested"] else 0,
            "net":      round(dow_buckets[dow]["net"], 2),
        }
        for dow in _DOW_ORDER if dow in dow_buckets
    ]

    # ── Courbe de calibration : prob prédite vs taux de victoire réel ───────────
    # Bins : [50-55), [55-60), [60-65), [65-70), [70-75), [75+)
    _cal_bins = [(50,55),(55,60),(60,65),(65,70),(70,75),(75,100)]
    calibration_curve = []
    for lo, hi in _cal_bins:
        grp = [p for p in all_resolved_picks if lo <= p["fair_prob"] < hi]
        if not grp:
            continue
        wins   = sum(1 for p in grp if p["outcome"] == "win")
        actual = round(wins / len(grp) * 100, 1)
        calibration_curve.append({
            "label":     f"{lo}-{hi}%" if hi < 100 else f"{lo}%+",
            "predicted": round((lo + min(hi, 100)) / 2, 1),  # milieu du bin
            "actual":    actual,
            "total":     len(grp),
            "wins":      wins,
        })

    # ── Stats par tranche d'edge (mises réelles) ─────────────────────────────
    # Edge = fair_prob - (100 / odds) — mesure l'avantage perçu vs le bookmaker
    _edge_buckets = [
        ("< 0%",   -99,  0),
        ("0–5%",     0,  5),
        ("5–10%",    5, 10),
        ("> 10%",   10, 99),
    ]
    _edge_picks = []
    if os.path.isdir(_REAL_BETS_DIR):
        for _ef in sorted(os.listdir(_REAL_BETS_DIR)):
            if not _ef.endswith(".json"):
                continue
            try:
                with open(os.path.join(_REAL_BETS_DIR, _ef), encoding="utf-8") as _f:
                    _sess = json.load(_f)
            except Exception:
                continue
            _date = _sess.get("date", "")
            _nmap = _nhl_outcomes_for_date(_date)
            for _p in _sess.get("picks", []):
                _odds = float(_p.get("odds") or 0)
                _fp   = float(_p.get("fair_prob") or 0)
                _mise = float(_p.get("mise_reelle") or 0)
                if not _odds or not _fp or not _mise:
                    continue
                _edge_val = _fp - (100.0 / _odds)
                _res = _resolve_pick_outcome(_p, _nmap)
                _oc  = _res.get("outcome", "")
                if _oc not in ("win", "loss"):
                    continue
                _net = _mise * (_odds - 1) if _oc == "win" else -_mise
                _edge_picks.append({"edge": _edge_val, "outcome": _oc, "mise": _mise, "net": _net})

    edge_stats = []
    for _label, _lo, _hi in _edge_buckets:
        _grp = [p for p in _edge_picks if _lo <= p["edge"] < _hi]
        if not _grp:
            continue
        _wins = sum(1 for p in _grp if p["outcome"] == "win")
        _inv  = sum(p["mise"] for p in _grp)
        _net  = sum(p["net"]  for p in _grp)
        edge_stats.append({
            "label":    _label,
            "total":    len(_grp),
            "wins":     _wins,
            "win_rate": round(_wins / len(_grp) * 100, 1),
            "roi":      round(_net / _inv * 100, 1) if _inv else 0.0,
            "net":      round(_net, 2),
        })

    # Simulation hier = dernier jour dans daily_accuracy
    sim_yesterday = None
    if daily_accuracy:
        last = daily_accuracy[-1]
        sim_yesterday = {
            "bets":   last["total"],
            "spent":  last["invested"],
            "profit": last["net"],
        }

    sim_total = {
        "bets":   total_bets,
        "spent":  round(total_inv, 2),
        "profit": round(total_net, 2),
    } if total_bets > 0 else None

    return jsonify({
        "daily_accuracy":   daily_accuracy,
        "count":            total_bets,
        "wins":             total_wins,
        "win_rate":         global_win_rate,
        "roi":              global_roi,
        "sim_yesterday":    sim_yesterday,
        "sim_total":        sim_total,
        "champion_accuracy": champ_acc,
        "odds_accuracy":      odds_accuracy,
        "team_accuracy":        team_accuracy,
        "bet_type_accuracy":    bet_type_accuracy,
        "games_per_day_stats":  games_per_day_stats,
        "calibration_curve":    calibration_curve,
        "dow_stats":            dow_stats,
        "edge_stats":           edge_stats,
    })


@app.route("/api/backfill-predictions", methods=["POST"])
def api_backfill_predictions():
    """Importe les picks des snapshots passés dans predictions.json et résout les outcomes."""
    try:
        from predictions import backfill_from_snapshots, update_outcomes
        bf         = backfill_from_snapshots()
        n_resolved = update_outcomes() if bf["added"] > 0 else 0
        return jsonify({"ok": True, "added": bf["added"], "resolved": n_resolved,
                        "dates": bf["dates"]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/calibration")
def api_calibration():
    try:
        from predictions import compute_calibration
        return jsonify(compute_calibration(sport="hockey"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Cache des données playoff NHL (refresh quotidien) ──────────────────────────
_playoff_cache = {"data": None, "date": None}
_playoff_cache_lock = threading.Lock()

@app.route("/api/playoff-nhl")
def api_playoff_nhl():
    """Tableau des séries éliminatoires NHL projetées avec probabilités de victoire."""
    import math
    import requests as _rq
    from datetime import datetime as _dt, timedelta as _td

    # ── Vérifier le cache (refresh toutes les heures) ───────────────────────────
    now = _dt.now()
    today_hour = now.strftime("%Y-%m-%d %H:00")
    with _playoff_cache_lock:
        if (_playoff_cache["data"] is not None and
            _playoff_cache["date"] == today_hour):
            return jsonify(_playoff_cache["data"])

    try:
        resp = _rq.get(
            "https://api-web.nhle.com/v1/standings/now",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        standings = resp.json().get("standings", [])
    except Exception as e:
        # Si erreur et cache existe, retourner le cache même s'il est périmé
        with _playoff_cache_lock:
            if _playoff_cache["data"] is not None:
                return jsonify(_playoff_cache["data"])
        return jsonify({"error": f"NHL API indisponible: {e}"}), 503

    # ── Scores de séries en cours (endpoint NHL unique) ──────────────────────
    def _fetch_series_scores():
        """
        Retourne {frozenset({teamA, teamB}): {'top_ab','bot_ab','top_wins','bot_wins'}}
        depuis le bracket NHL officiel. Un seul appel API !
        """
        # Cache mémoire 120s pour éviter re-scrape sur rafraîchissements répétés
        cache_key = "_series_scores_cache"
        now_ts = time.time()
        cached = _cache.get(cache_key)
        if cached and (now_ts - cached.get("ts", 0)) < 120:
            return cached["data"]

        series = {}
        try:
            # Endpoint officiel NHL qui retourne le bracket complet
            r = _rq.get(
                "https://api-web.nhle.com/v1/playoff-bracket/2026",
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
            )
            if r.ok:
                bracket = r.json()
                for s in bracket.get("series", []):
                    # Sauter les séries futures (wins=0 pour les deux et c'est pas la 1ère ronde)
                    if not s.get("topSeedTeam") or not s.get("bottomSeedTeam"):
                        continue
                    top_ab = s.get("topSeedTeam", {}).get("abbrev", "")
                    bot_ab = s.get("bottomSeedTeam", {}).get("abbrev", "")
                    if not top_ab or not bot_ab:
                        continue
                    key = frozenset([top_ab, bot_ab])
                    series[key] = {
                        "top_ab":   top_ab,
                        "bot_ab":   bot_ab,
                        "top_wins": s.get("topSeedWins", 0),
                        "bot_wins": s.get("bottomSeedWins", 0),
                    }
        except Exception:
            pass  # Fallback: retourner dict vide, ui affichera "N/A"

        _cache[cache_key] = {"data": series, "ts": now_ts}
        return series

    _series_scores = _fetch_series_scores()

    def pyth(gf, ga):
        """Esperance Pythagorienne de victoire."""
        if gf + ga == 0:
            return 0.5
        return (gf ** 2) / (gf ** 2 + ga ** 2)

    def log5(pa, pb):
        """Probabilité que A batte B en un match (formule log5)."""
        if pa + pb - 2 * pa * pb == 0:
            return 0.5
        return (pa - pa * pb) / (pa + pb - 2 * pa * pb)

    def series_prob(p, games=7):
        """P(A gagne une série au meilleur de `games` matchs) avec p = proba par match."""
        wins_needed = (games + 1) // 2
        total = 0.0
        for w in range(wins_needed, games + 1):
            # A gagne exactement `w` matchs (dernier match = victoire de A)
            # Donc en w-1 premiers matchs A a wins_needed-1 victoires
            coef = math.comb(w - 1, wins_needed - 1)
            total += coef * (p ** wins_needed) * ((1 - p) ** (w - wins_needed))
        return round(total * 100, 1)

    def fmt_team(t, seed, conf_seq, playoff_wins=0):
        gf = t.get("goalFor", 0)
        ga = t.get("goalAgainst", 0)
        l10 = t.get("l10Wins", 0)
        l10l = t.get("l10Losses", 0)
        l10o = t.get("l10OtLosses", 0)
        return {
            "seed":         seed,
            "abbrev":       t.get("teamAbbrev", {}).get("default", ""),
            "name":         t.get("teamName", {}).get("default", ""),
            "common_name":  t.get("teamCommonName", {}).get("default", ""),
            "logo":         t.get("teamLogo", ""),
            "wins":         t.get("wins", 0),
            "losses":       t.get("losses", 0),
            "ot_losses":    t.get("otLosses", 0),
            "points":       t.get("points", 0),
            "gp":           t.get("gamesPlayed", 0),
            "gf":           gf,
            "ga":           ga,
            "diff":         t.get("goalDifferential", 0),
            "point_pct":    round(t.get("pointPctg", 0) * 100, 1),
            "l10":          f"{l10}-{l10l}-{l10o}",
            "l10_pts":      t.get("l10Points", 0),
            "streak":       f"{t.get('streakCode','')}{t.get('streakCount','')}",
            "clinch":       t.get("clinchIndicator", ""),
            "div_seq":      t.get("divisionSequence", 99),
            "wc_seq":       t.get("wildcardSequence", 0),
            "div_name":     t.get("divisionName", ""),
            "pyth":         round(pyth(gf, ga) * 100, 1),
            "playoff_wins": playoff_wins,
        }

    def build_conference(conf_abbrev):
        teams = [t for t in standings if t.get("conferenceAbbrev") == conf_abbrev]

        # ── Identifier les deux divisions de la conférence ────────────────
        div_abbrevs = sorted(set(
            t.get("divisionAbbrev", "") for t in teams
            if t.get("divisionSequence", 99) <= 3
        ))

        # Top 3 de chaque division (divisionSequence 1, 2, 3)
        div_teams = {}
        for div in div_abbrevs:
            div_teams[div] = sorted(
                [t for t in teams if t.get("divisionAbbrev") == div
                 and t.get("divisionSequence", 99) <= 3],
                key=lambda x: x.get("divisionSequence", 99),
            )

        # Wildcards : wildcardSequence 1 et 2
        wc1 = next((t for t in teams if t.get("wildcardSequence") == 1), None)
        wc2 = next((t for t in teams if t.get("wildcardSequence") == 2), None)

        # ── Seeding : meilleur vainqueur de division = seed 1 ─────────────
        if len(div_abbrevs) >= 2:
            pts_a = div_teams[div_abbrevs[0]][0].get("points", 0) if div_teams.get(div_abbrevs[0]) else 0
            pts_b = div_teams[div_abbrevs[1]][0].get("points", 0) if div_teams.get(div_abbrevs[1]) else 0
            top_div = div_abbrevs[0] if pts_a >= pts_b else div_abbrevs[1]
            bot_div = div_abbrevs[1] if top_div == div_abbrevs[0] else div_abbrevs[0]
        else:
            top_div = bot_div = div_abbrevs[0] if div_abbrevs else ""

        # Format seed : seed 1-2 = vainqueurs, 3-4 = div top 2e/3e,
        #               5-6 = div bot 2e/3e, 7 = WC1, 8 = WC2
        def get_div(div, idx):
            lst = div_teams.get(div, [])
            return lst[idx] if idx < len(lst) else None

        seed_map = [
            (1, get_div(top_div, 0)),
            (2, get_div(bot_div, 0)),
            (3, get_div(top_div, 1)),
            (4, get_div(top_div, 2)),
            (5, get_div(bot_div, 1)),
            (6, get_div(bot_div, 2)),
            (7, wc1),
            (8, wc2),
        ]

        # ── Calcul du nombre de victoires en séries pour chaque équipe ─────
        def get_playoff_wins(team_abbrev):
            """Compte le nombre de victoires en séries de l'équipe."""
            total = 0
            for series_data in _series_scores.values():
                if series_data.get("top_ab") == team_abbrev:
                    total += series_data.get("top_wins", 0) or 0
                elif series_data.get("bot_ab") == team_abbrev:
                    total += series_data.get("bot_wins", 0) or 0
            return total

        result = [fmt_team(t, seed, seed, get_playoff_wins(t.get("teamAbbrev", {}).get("default", "")))
                  for seed, t in seed_map if t is not None]
        by_seed = {t["seed"]: t for t in result}

        # ── Bubble (9e-12e) ────────────────────────────────────────────────
        playoff_abbrevs = {t["abbrev"] for t in result}
        bubble_raw = sorted(
            [t for t in teams if t.get("teamAbbrev", {}).get("default", "") not in playoff_abbrevs],
            key=lambda x: -x.get("points", 0),
        )[:4]
        bubble = [fmt_team(t, 9 + i, 9 + i, get_playoff_wins(t.get("teamAbbrev", {}).get("default", "")))
                  for i, t in enumerate(bubble_raw)]

        # ── Matchups 1er tour — format NHL divisionnaire ───────────────────
        # Bracket div top : Seed1(div winner) vs Seed8(WC2), Seed3 vs Seed4
        # Bracket div bot : Seed2(div winner) vs Seed7(WC1), Seed5 vs Seed6
        def make_matchup(a, b):
            if not a or not b:
                return None
            pa = pyth(a["gf"], a["ga"])
            pb = pyth(b["gf"], b["ga"])
            p_game = log5(pa, pb)
            form_factor = (a["l10_pts"] / 20.0 - b["l10_pts"] / 20.0) * 0.05
            p_adj = min(0.95, max(0.05, p_game + form_factor))

            # Score de la série
            key = frozenset([a["abbrev"], b["abbrev"]])
            sd  = _series_scores.get(key, {})
            top_ab = sd.get("top_ab", "")
            # topSeed NHL = high_seed (meilleure tête de série = a dans notre modèle)
            if top_ab == a["abbrev"]:
                high_wins = sd.get("top_wins", 0) or 0
                low_wins  = sd.get("bot_wins", 0) or 0
            elif top_ab == b["abbrev"]:
                high_wins = sd.get("bot_wins", 0) or 0
                low_wins  = sd.get("top_wins", 0) or 0
            else:
                high_wins, low_wins = 0, 0
            total_played = high_wins + low_wins
            series_started = total_played > 0

            if series_started:
                if high_wins > low_wins:
                    series_label = f"{a['abbrev']} mène {high_wins}‑{low_wins}"
                elif low_wins > high_wins:
                    series_label = f"{b['abbrev']} mène {low_wins}‑{high_wins}"
                else:
                    series_label = f"Égalité {high_wins}‑{low_wins}"
            else:
                series_label = ""

            return {
                "high_seed":      a,
                "low_seed":       b,
                "p_game":         round(p_game * 100, 1),
                "p_series":       series_prob(p_adj),
                "favorite":       a["abbrev"] if p_adj >= 0.5 else b["abbrev"],
                "series_started": series_started,
                "high_wins":      high_wins,
                "low_wins":       low_wins,
                "series_label":   series_label,
            }

        matchups = [m for m in [
            make_matchup(by_seed.get(1), by_seed.get(8)),  # Div top winner vs WC2
            make_matchup(by_seed.get(3), by_seed.get(4)),  # Div top 2e vs 3e
            make_matchup(by_seed.get(2), by_seed.get(7)),  # Div bot winner vs WC1
            make_matchup(by_seed.get(5), by_seed.get(6)),  # Div bot 2e vs 3e
        ] if m]

        return {"seeds": result, "bubble": bubble, "matchups": matchups}

    east = build_conference("E")
    west = build_conference("W")

    result = {
        "east": east,
        "west": west,
        "updated_at": _get_et_now().strftime("%Y-%m-%d %H:%M"),
    }

    # ── Cacher le résultat pour l'heure ────────────────────────────────────
    with _playoff_cache_lock:
        _playoff_cache["data"] = result
        _playoff_cache["date"] = today_hour

    return jsonify(result)


# ─── Dev Backlog ──────────────────────────────────────────────────────────────
_BACKLOG_PATH = os.path.join(_DATA_DIR, "backlog.json")
_backlog_lock = threading.Lock()

def _load_backlog():
    if not os.path.exists(_BACKLOG_PATH):
        return []
    with open(_BACKLOG_PATH, encoding="utf-8") as f:
        return json.load(f)

def _save_backlog(items):
    # Ensure data folder exists
    os.makedirs(os.path.dirname(_BACKLOG_PATH), exist_ok=True)
    with open(_BACKLOG_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

@app.route("/api/backlog", methods=["GET"])
def backlog_list():
    with _backlog_lock:
        return jsonify(_load_backlog())

@app.route("/api/backlog", methods=["POST"])
def backlog_create():
    import uuid
    body = request.json or {}
    with _backlog_lock:
        items = _load_backlog()
        item = {
            "id":          str(uuid.uuid4()),
            "title":       body.get("title", "").strip(),
            "description": body.get("description", "").strip(),
            "priority":    body.get("priority", "moyenne"),
            "type":        body.get("type", "feature"),
            "status":      body.get("status", "a_faire"),
            "created_at":  _get_et_now().isoformat(),
            "updated_at":  _get_et_now().isoformat(),
        }
        if not item["title"]:
            return jsonify({"error": "Titre requis"}), 400
        items.append(item)
        _save_backlog(items)
    return jsonify(item), 201

@app.route("/api/backlog/<item_id>", methods=["PUT"])
def backlog_update(item_id):
    body = request.json or {}
    with _backlog_lock:
        items = _load_backlog()
        for item in items:
            if item["id"] == item_id:
                for field in ("title", "description", "priority", "type", "status"):
                    if field in body:
                        item[field] = body[field]
                item["updated_at"] = _get_et_now().isoformat()
                _save_backlog(items)
                return jsonify(item)
    return jsonify({"error": "Non trouvé"}), 404

@app.route("/api/backlog/<item_id>", methods=["DELETE"])
def backlog_delete(item_id):
    with _backlog_lock:
        items = _load_backlog()
        new_items = [i for i in items if i["id"] != item_id]
        if len(new_items) == len(items):
            return jsonify({"error": "Non trouvé"}), 404
        _save_backlog(new_items)
    return jsonify({"ok": True})


def _refresh_advanced_stats_bg():
    """Rafraîchit les stats avancées en arrière-plan (NHL REST + Evolving Hockey)."""
    try:
        from advanced_stats import refresh_advanced_stats
        refresh_advanced_stats()
    except Exception:
        pass


def _refresh_injuries_bg():
    """Pré-charge le cache de blessures pour les équipes du jour.

    OPTIMISATION : utilise le payload en mémoire ou le cache disque au lieu
    de scraper Mise-O-Jeu. Évite un lancement Playwright inutile au démarrage
    quand le cache est déjà frais.
    """
    try:
        from injuries import prefetch_injuries

        # 1. Essayer d'extraire les équipes depuis le payload cache en mémoire
        with _lock:
            cached_data = _cache.get("data") or {}
        hockey_picks = cached_data.get("hockey") or []
        teams = list({
            name
            for p in hockey_picks
            for name in (p.get("home_team", ""), p.get("away_team", ""))
            if name
        })

        # 2. Fallback : utiliser le cache scrape s'il est déjà chaud (sans relancer Playwright)
        if not teams:
            with _scrape_lock:
                cached_scrape = _scrape_caches.get("hockey")
            if cached_scrape:
                matches, _ = cached_scrape
                teams = list({
                    name
                    for m in matches
                    for name in (m.home_team, m.away_team)
                    if name
                })

        if teams:
            prefetch_injuries(teams)
    except Exception:
        pass


# ─── EXPORT ANALYTICS ENDPOINT ────────────────────────────────────────────────────
@app.route("/api/export-analytics")
def api_export_analytics():
    """Export all analytics data for analysis: snapshots, outcomes, calibration."""
    import csv
    from io import StringIO

    try:
        # Load snapshots directory
        snap_dir = _SNAPSHOTS_DIR
        snapshots = {}
        outcomes_by_date = {}

        if os.path.isdir(snap_dir):
            for fname in sorted(os.listdir(snap_dir)):
                if not fname.endswith(".json"):
                    continue
                date = fname[:-5]  # Remove .json
                try:
                    with open(os.path.join(snap_dir, fname), encoding="utf-8") as f:
                        snap_data = json.load(f)
                    snapshots[date] = snap_data

                    # Extract outcomes for each date
                    nhl_map = _nhl_outcomes_for_date(date)
                    outcomes_by_date[date] = nhl_map
                except Exception as e:
                    print(f"  [export] Snapshot {date} error: {e}")

        # Get calibration metrics
        from predictions import compute_calibration
        calibration = compute_calibration(sport="hockey")

        # Build CSV export (flat structure for easy analysis)
        csv_buffer = StringIO()
        csv_writer = csv.writer(csv_buffer)

        # Headers for picks
        csv_writer.writerow([
            "date", "match", "selection", "bet_type", "predicted_prob", "odds",
            "kelly_fraction", "fair_prob", "edge_pct", "outcome", "actual_result"
        ])

        # Write all picks with outcomes
        for date in sorted(snapshots.keys()):
            snap = snapshots[date]
            outcomes = outcomes_by_date.get(date, {})

            for pick in snap.get("picks", []):
                match = pick.get("match", "")
                # Try to find outcome for this pick
                outcome = "pending"
                actual = None
                for key, result in outcomes.items():
                    if match.lower() in key.lower() or key.lower() in match.lower():
                        outcome = result.get("outcome", "pending")
                        actual = result.get("score", "")
                        break

                csv_writer.writerow([
                    date,
                    match,
                    pick.get("selection", ""),
                    pick.get("bet_type", ""),
                    f"{pick.get('fair_prob', 0):.1f}%",
                    f"{pick.get('odds', 0):.2f}",
                    pick.get("mise", ""),
                    f"{pick.get('fair_prob', 0):.1f}%",
                    pick.get("edge", ""),
                    outcome,
                    actual or ""
                ])

        # Return JSON with all data
        return jsonify({
            "snapshots": snapshots,
            "outcomes_by_date": outcomes_by_date,
            "calibration": calibration,
            "csv_export": csv_buffer.getvalue(),
            "export_date": _get_et_now().isoformat(),
            "total_snapshots": len(snapshots),
            "total_picks": sum(len(s.get("picks", [])) for s in snapshots.values())
        })

    except Exception as e:
        print(f"  [export] Error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/export-csv")
def api_export_csv():
    """Export analytics data as CSV file (downloadable)."""
    try:
        snap_dir = _SNAPSHOTS_DIR
        snapshots = {}
        outcomes_by_date = {}

        if os.path.isdir(snap_dir):
            for fname in sorted(os.listdir(snap_dir)):
                if not fname.endswith(".json"):
                    continue
                date = fname[:-5]
                try:
                    with open(os.path.join(snap_dir, fname), encoding="utf-8") as f:
                        snapshots[date] = json.load(f)
                    outcomes_by_date[date] = _nhl_outcomes_for_date(date)
                except Exception:
                    pass

        # Build CSV
        csv_buffer = StringIO()
        csv_writer = csv.writer(csv_buffer)
        csv_writer.writerow([
            "date", "match", "selection", "bet_type", "predicted_prob", "odds",
            "kelly_fraction", "outcome"
        ])

        for date in sorted(snapshots.keys()):
            snap = snapshots[date]
            outcomes = outcomes_by_date.get(date, {})

            for pick in snap.get("picks", []):
                match = pick.get("match", "")
                outcome = "pending"
                for key, result in outcomes.items():
                    if match.lower() in key.lower() or key.lower() in match.lower():
                        outcome = result.get("outcome", "pending")
                        break

                csv_writer.writerow([
                    date,
                    match,
                    pick.get("selection", ""),
                    pick.get("bet_type", ""),
                    f"{pick.get('fair_prob', 0):.1f}%",
                    f"{pick.get('odds', 0):.2f}",
                    pick.get("mise", ""),
                    outcome
                ])

        # Return as downloadable CSV
        csv_content = csv_buffer.getvalue()
        response = app.response_class(
            response=csv_content,
            status=200,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=analytics_export.csv"}
        )
        return response

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Rafraîchir les stats avancées au démarrage (NHL REST API + Evolving Hockey)
    threading.Thread(target=_refresh_advanced_stats_bg, daemon=True).start()
    # Pré-charger le cache de blessures
    threading.Thread(target=_refresh_injuries_bg, daemon=True).start()

    # Planificateur snapshot dynamique (30 min avant le premier match NHL)
    try:
        from email_service import schedule_dynamic_snapshots
        def _get_hockey_picks():
            # Retourner SEULEMENT les picks d'aujourd'hui (pas demain/après)
            today = _get_today_et()
            all_picks = (_cache.get("data") or {}).get("hockey") or []
            return [p for p in all_picks if p.get("date") == today]
        def _get_sgp_proposals():
            # Générer les SGP dynamiquement à partir des picks actuels du jour
            # au lieu de retourner ceux en cache (qui peuvent être de la veille)
            picks = _get_hockey_picks()
            return _generate_sgp_proposals(picks) if picks else []
        schedule_dynamic_snapshots(_get_hockey_picks, get_sgp_fn=_get_sgp_proposals)
    except Exception as _e:
        print(f"[snapshot] Planificateur dynamique non démarré : {_e}")

    def _backup_stats_today():
        """Sauvegarde les tableaux Stats dans stats_backups/YYYY-MM-DD.json.
        Idempotent : ne crée le fichier qu'une seule fois par jour."""
        today      = _get_today_et()
        backup_dir = Path(__file__).parent / "stats_backups"
        backup_dir.mkdir(exist_ok=True)
        out_path   = backup_dir / f"{today}.json"
        if out_path.exists():
            print(f"  [startup] Backup stats {today} déjà présent — ignoré.")
            return

        # Récupérer les données depuis les fonctions internes (sans HTTP)
        from predictions import compute_calibration
        from predictions import get_feature_weights
        cal   = compute_calibration()
        fw    = get_feature_weights()

        # Snapshots calibration
        try:
            import importlib, app as _app
            with _app.app.test_client() as c:
                snaps = c.get("/api/calibration-snapshots").get_json()
        except Exception:
            snaps = {}

        backup = {
            "date":                   today,
            "heure":                  datetime.now().strftime("%H:%M"),
            "calibration":            cal,
            "feature_weights":        fw,
            "calibration_snapshots":  snaps,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(backup, f, indent=2, ensure_ascii=False, default=str)

        count    = (snaps or {}).get("count", cal.get("count", 0))
        win_rate = (snaps or {}).get("win_rate", cal.get("win_rate", "—"))
        print(f"  [startup] Backup stats {today} sauvegardé — {count} picks, {win_rate}% win rate → {out_path.name}")

    def _retry_hockey_scrape(max_attempts: int = 3) -> bool:
        """Retries Hockey NHL scrape avec backoff réduit.

        Retourne True si scrape réussi, False si tous les retries ont échoué.
        """
        for attempt in range(max_attempts):
            _run_analysis(demo=False, sports=["hockey"])
            data = _cache.get("data") or {}
            h = len(data.get("hockey") or [])
            if h > 0:
                print(f"  [startup] Hockey NHL OK — {h} matchs (tentative {attempt+1}/{max_attempts}).")
                return True
            wait = 10 * (attempt + 1)  # 10s, 20s, 30s (au lieu de 5s, 10s, 15s, 20s, 25s, 30s)
            if attempt < max_attempts - 1:  # Ne pas attendre après le dernier essai
                print(f"  [startup] Hockey vide (tentative {attempt+1}/{max_attempts}) — réessai dans {wait}s...")
                time.sleep(wait)
        print(f"  [startup] Hockey NHL — tous les retries échoués après {max_attempts} tentatives")
        return False

    def _startup_sequence():
        """Scrape réel au démarrage. Si le cache disque du jour est disponible,
        l'UI affiche les dernières données pendant que le scrape tourne en arrière-plan.

        OPTIMISATION : si le cache disque est récent (< 30 min), on skip le scrape
        immédiat — l'utilisateur peut toujours forcer via le bouton manuel.
        Évite l'affichage du banner stale après un simple redémarrage du serveur.
        """
        # Marquer qu'un refresh BG est déjà en cours → _check_date_rollover n'en lance pas un 2ème
        with _lock:
            _cache["_bg_dayroll_running"] = True

        # Skip scrape si cache ultra-frais
        if _cache.get("data") and _cache_is_fresh:
            print(f"  [startup] Cache disque très frais ({_cache_age_min:.0f}min) — scrape skippé")
            with _lock:
                _cache.pop("_bg_dayroll_running", None)
            try:
                _backup_stats_today()
            except Exception:
                pass
            return

        if _cache.get("data"):
            print(f"  [startup] Cache disque ({_cache_age_min:.0f}min) — scrape réel en arrière-plan...")
        else:
            print("  [startup] Aucun cache — scrape réel en cours...")

        # 2. Backfill predictions.json depuis les snapshots (idempotent, ~0s si déjà fait)
        try:
            from predictions import backfill_from_snapshots, update_outcomes
            bf = backfill_from_snapshots()
            if bf["added"] > 0:
                n_resolved = update_outcomes()
                print(f"  [startup] Backfill : {bf['added']} picks importés, {n_resolved} outcomes résolus.")
            else:
                print("  [startup] Backfill : predictions.json déjà à jour.")
        except Exception as e:
            print(f"  [startup] Backfill ignoré : {e}")

        try:
            # OPTIMISATION : Paralléliser Hockey NHL + Résultats au démarrage
            # Réduire retries de 6 à 3 (avec pré-scrape 5 AM, moins de retries nécessaires)
            def _load_results_bg():
                """Pré-charger le snapshot du jour en arrière-plan (I/O-bound)."""
                try:
                    today = _get_today_et()
                    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
                    if os.path.exists(daily_path):
                        with open(daily_path, encoding="utf-8") as f:
                            json.load(f)
                        print(f"  [startup] Résultats pré-chargés ({today}.json)")
                    elif os.path.exists(_SNAPSHOT_PATH):
                        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
                            snap = json.load(f)
                        if snap.get("date") == today:
                            print(f"  [startup] Résultats pré-chargés (snapshot.json)")
                except Exception as e:
                    print(f"  [startup] Résultats pré-charge erreur: {e}")

            # Lancer Hockey NHL et Résultats en parallèle
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                hockey_future = executor.submit(
                    lambda: _retry_hockey_scrape(max_attempts=3)
                )
                results_future = executor.submit(_load_results_bg)

                # Attendre Hockey NHL (priorité 1)
                hockey_success = hockey_future.result()

                # Résultats chargent en background (pas de bloc sur hockey)
                # Les deux finissent avant que le startup_sequence se termine

        finally:
            # Libérer le verrou pour permettre les refreshs manuels ultérieurs
            with _lock:
                _cache.pop("_bg_dayroll_running", None)

        # 4. Backup Stats du jour (une seule fois par jour, à la première ouverture)
        try:
            _backup_stats_today()
        except Exception as e:
            print(f"  [startup] Backup stats ignoré : {e}")

    threading.Thread(target=_startup_sequence, daemon=True).start()

    # Initialiser APScheduler pour pré-scrape à 5 AM
    _init_scheduler()

    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, port=port, host='0.0.0.0', use_reloader=False)
