"""
Statistiques complémentaires NHL :
  0. Gardien partant : Daily Faceoff (confirmé/prévu) avec fallback NHL API
  1. Gardiens  : SV%, MPM, matchs joués (gardien #1 saison)
  2. Calendrier : back-to-back, jours de repos
  3. Face-à-face : 5 dernières rencontres cette saison
  4. Par période : probabilités de Poisson (btts, but, etc.)
"""

import json
import math
import re
import time as _time
import requests
from functools import lru_cache

# Session partagée : réutilise les connexions TCP (évite le handshake SSL répété)
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
from datetime import date, timedelta


def _get_abbrev(team_name: str) -> str | None:
    from nhl_stats import _match_abbrev
    return _match_abbrev(team_name)


# ─── 0. Gardien partant (Daily Faceoff) ───────────────────────────────────────

_DF_CACHE:    list | None = None
_DF_CACHE_TS: float       = 0.0
_DF_TTL                   = 1800.0   # 30 minutes


def _fetch_daily_faceoff() -> list[dict]:
    """
    Récupère les gardiens partants depuis Daily Faceoff.
    Parse le JSON __NEXT_DATA__ embarqué dans la page.
    Cache de 30 min.
    """
    global _DF_CACHE, _DF_CACHE_TS
    now = _time.time()
    if _DF_CACHE is not None and now - _DF_CACHE_TS < _DF_TTL:
        return _DF_CACHE

    try:
        from bs4 import BeautifulSoup
        r = _SESSION.get(
            "https://www.dailyfaceoff.com/starting-goalies/",
            timeout=10,
        )
        r.raise_for_status()
        soup  = BeautifulSoup(r.text, "lxml")
        tag   = soup.find("script", {"id": "__NEXT_DATA__"})
        if not tag:
            raise ValueError("__NEXT_DATA__ introuvable")
        data  = json.loads(tag.string)
        games = data.get("props", {}).get("pageProps", {}).get("data", [])
        _DF_CACHE    = games if isinstance(games, list) else []
        _DF_CACHE_TS = now
        return _DF_CACHE
    except Exception:
        _DF_CACHE    = []
        _DF_CACHE_TS = now
        return []


def _df_team_match(team_name: str, full_name: str) -> bool:
    """Vrai si team_name correspond au nom complet de Daily Faceoff."""
    tn = re.sub(r'\s*\(.*?\)', '', team_name.lower()).strip()
    fn = full_name.lower()
    return any(w in fn for w in tn.split() if len(w) > 2) or \
           any(w in tn for w in fn.split() if len(w) > 2)


def _enrich_goalie_recent(result: dict, team_name: str) -> dict:
    """
    Ajoute svPctRecent / gaaRecent (L10) au dict gardien.
    Cherche le playerId dans club-stats en matchant sur le nom de famille.
    """
    try:
        abbrev = _get_abbrev(team_name)
        if not abbrev:
            return result
        data    = _fetch_club_stats(abbrev)
        goalies = data.get("goalies", [])
        goalie_last = (result.get("name") or "").lower()
        # Trouver le joueur correspondant
        player_id = None
        for g in goalies:
            ln = g.get("lastName") or ""
            if isinstance(ln, dict):
                ln = ln.get("default", "")
            if ln.lower() == goalie_last or goalie_last in ln.lower():
                player_id = g.get("playerId")
                break
        # Fallback : prendre le gardien #1 si un seul candidat
        if not player_id and len(goalies) == 1:
            player_id = goalies[0].get("playerId")
        if not player_id:
            return result
        recent = _get_recent_sv(player_id, n=10)
        if recent:
            result["svPctRecent"] = recent["svPct"]
            result["gaaRecent"]   = recent["gaa"]
            result["gamesRecent"] = recent["games"]
    except Exception:
        pass
    return result


def get_starting_goalie(team_name: str) -> dict | None:
    """
    Retourne le gardien partant du soir :
      1. Daily Faceoff (Confirmed > Expected > Unconfirmed)
      2. Fallback : gardien #1 de la saison (NHL API)

    Retourne : {name, full_name, svPct, gaa, confirmed, status, source,
                svPctRecent, gaaRecent, gamesRecent}   ← stats L10 ajoutées
    """
    games = _fetch_daily_faceoff()
    for g in games:
        for side in ("home", "away"):
            full_name = g.get(f"{side}TeamName") or ""
            if not full_name or not _df_team_match(team_name, full_name):
                continue
            goalie_name = g.get(f"{side}GoalieName") or ""
            sv          = float(g.get(f"{side}GoalieSavePercentage") or 0)
            gaa_val     = float(g.get(f"{side}GoalieGoalsAgainstAvg") or 0)
            raw_status  = g.get(f"{side}NewsStrengthName")   # "Confirmed" ou None
            confirmed   = raw_status == "Confirmed"
            status      = "Confirme" if confirmed else "Attendu"
            if not goalie_name:
                break  # équipe trouvée mais pas de gardien annoncé
            last = goalie_name.split()[-1]
            result = {
                "name":      last,
                "full_name": goalie_name,
                "svPct":     round(sv * 100, 1),
                "gaa":       round(gaa_val, 2),
                "confirmed": confirmed,
                "status":    status,
                "source":    "dailyfaceoff",
            }
            return _enrich_goalie_recent(result, team_name)

    # Fallback : gardien #1 de la saison
    season_g = get_goalie_stats(team_name)
    if season_g:
        season_g.setdefault("full_name", season_g["name"])
        season_g["confirmed"] = False
        season_g["status"]    = "Saison (non confirmé)"
        season_g["source"]    = "nhl_api"
        return _enrich_goalie_recent(season_g, team_name)
    return season_g


# ─── 1. Statistiques des gardiens ─────────────────────────────────────────────

@lru_cache(maxsize=64)
def _fetch_goalie_gamelog(player_id: int) -> list:
    """Retourne le journal de matchs du gardien (saison courante)."""
    url = f"https://api-web.nhle.com/v1/player/{player_id}/game-log/now"
    try:
        r = _SESSION.get(url, timeout=8)
        r.raise_for_status()
        return r.json().get("gameLog", [])
    except Exception:
        return []


def _get_recent_sv(player_id: int, n: int = 10) -> dict | None:
    """
    Calcule le SV% et GAA sur les N derniers matchs joués.
    Utilise les totaux bruts (shotsAgainst, goalsAgainst) pour éviter
    la moyenne des moyennes qui est biaisée par les petits échantillons.
    """
    games = _fetch_goalie_gamelog(player_id)
    started = sorted(
        [g for g in games if g.get("gamesStarted")],
        key=lambda g: g.get("gameDate", ""),
        reverse=True,
    )[:n]
    if not started:
        return None
    total_shots = sum(g.get("shotsAgainst", 0) for g in started)
    total_goals = sum(g.get("goalsAgainst", 0) for g in started)
    if total_shots == 0:
        return None
    total_toi = 0.0
    for g in started:
        toi = g.get("toi", "")
        if toi and ":" in toi:
            parts = toi.split(":")
            total_toi += int(parts[0]) + int(parts[1]) / 60
    sv_pct = round((total_shots - total_goals) / total_shots * 100, 1)
    gaa    = round(total_goals / total_toi * 60, 2) if total_toi > 0 else 0.0
    return {"svPct": sv_pct, "gaa": gaa, "games": len(started)}


@lru_cache(maxsize=32)
def _fetch_club_stats(abbrev: str) -> dict:
    url = f"https://api-web.nhle.com/v1/club-stats/{abbrev}/now"
    try:
        r = _SESSION.get(url, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


def get_goalie_stats(team_name: str) -> dict | None:
    """
    Retourne les stats du gardien #1 (plus de matchs joués cette saison).
    Retourne : {name, playerId, svPct, gaa, gamesStarted, wins}  ou None.
    """
    abbrev = _get_abbrev(team_name)
    if not abbrev:
        return None
    data = _fetch_club_stats(abbrev)
    goalies = data.get("goalies", [])
    if not goalies:
        return None
    g = max(goalies, key=lambda x: x.get("gamesStarted", 0))
    last_name = g.get("lastName")
    if isinstance(last_name, dict):
        last_name = last_name.get("default", "?")
    return {
        "name":         last_name or "?",
        "playerId":     g.get("playerId"),
        "svPct":        round((g.get("savePercentage") or 0) * 100, 1),
        "gaa":          round(g.get("goalsAgainstAverage") or 0, 2),
        "gamesStarted": g.get("gamesStarted", 0),
        "wins":         g.get("wins", 0),
    }


# ─── 2. Contexte calendrier (back-to-back + décalage horaire) ────────────────

# Fuseau horaire UTC hiver (heure standard) de chaque aréna NHL
# Eastern -5 · Central -6 · Mountain -7 · Pacific -8
_TEAM_TIMEZONE: dict[str, int] = {
    # Eastern (-5)
    "BOS": -5, "BUF": -5, "DET": -5, "FLA": -5, "MTL": -5,
    "OTT": -5, "PHI": -5, "PIT": -5, "TBL": -5, "TOR": -5,
    "WSH": -5, "CAR": -5, "CBJ": -5, "NYI": -5, "NYR": -5, "NJD": -5,
    # Central (-6)
    "CHI": -6, "DAL": -6, "MIN": -6, "NSH": -6, "STL": -6, "WPG": -6,
    # Mountain (-7)
    "COL": -7, "UTA": -7, "ARI": -7, "EDM": -7, "CGY": -7,
    # Pacific (-8)
    "ANA": -8, "LAK": -8, "SJS": -8, "SEA": -8, "VAN": -8,
}


def get_timezone_diff(away_team: str, home_team: str) -> int:
    """
    Retourne la différence de fuseau horaire entre la ville d'origine de
    l'équipe visiteuse et la ville hôte.

    Valeur négative = l'équipe visiteuse voyage vers l'EST (difficile).
    Valeur positive = l'équipe visiteuse voyage vers l'OUEST (neutre).

    Ex : LAK (-8) à BOS (-5) → -8 − (-5) = -3  (LAK voyage 3h vers l'est)
    """
    away_abbrev = _get_abbrev(away_team)
    home_abbrev = _get_abbrev(home_team)
    if not away_abbrev or not home_abbrev:
        return 0
    away_tz = _TEAM_TIMEZONE.get(away_abbrev, 0)
    home_tz = _TEAM_TIMEZONE.get(home_abbrev, 0)
    return away_tz - home_tz

@lru_cache(maxsize=64)
def _fetch_weekly_schedule(abbrev: str, week_date: str) -> list:
    url = f"https://api-web.nhle.com/v1/club-schedule/{abbrev}/week/{week_date}"
    try:
        r = _SESSION.get(url, timeout=8)
        r.raise_for_status()
        return r.json().get("games", [])
    except Exception:
        return []


def get_schedule_context(team_name: str, game_date: str | None = None) -> dict:
    """
    Détermine si l'équipe joue en back-to-back.
    game_date : YYYY-MM-DD (défaut = aujourd'hui)
    Retourne : {is_back_to_back, days_rest, played_yesterday}
    """
    abbrev = _get_abbrev(team_name)
    default = {"is_back_to_back": False, "days_rest": 3, "played_yesterday": False}
    if not abbrev:
        return default

    target    = date.fromisoformat(game_date) if game_date else date.today()
    prev_week = (target - timedelta(days=3)).isoformat()

    games_a = _fetch_weekly_schedule(abbrev, prev_week)
    games_b = _fetch_weekly_schedule(abbrev, target.isoformat())

    past_dates = set()
    for g in games_a + games_b:
        gd = g.get("gameDate", "")
        if gd and gd < target.isoformat():
            past_dates.add(gd)

    if not past_dates:
        return default

    last_date = date.fromisoformat(max(past_dates))
    days_rest = (target - last_date).days

    return {
        "is_back_to_back":  days_rest <= 1,
        "days_rest":        days_rest,
        "played_yesterday": days_rest == 1,
    }


# ─── 3b. Forme récente — gfPG / gaPG sur les N derniers matchs ───────────────

_RECENT_GOALS_CACHE: dict = {}   # abbrev → (result, timestamp)
_RECENT_GOALS_TTL = 1800.0       # 30 minutes

def get_recent_goals_stats(team_name: str, n: int = 10, game_date: str | None = None) -> dict:
    """
    Calcule gfPG et gaPG sur les N derniers matchs complétés avant game_date.
    Retourne {"gfPG_recent": float, "gaPG_recent": float, "n": int} ou {} si
    données insuffisantes (< 3 matchs résolus).
    Cache 30 minutes par équipe.
    """
    abbrev = _get_abbrev(team_name)
    if not abbrev:
        return {}

    now = _time.time()
    cached = _RECENT_GOALS_CACHE.get(abbrev)
    if cached and now - cached[1] < _RECENT_GOALS_TTL:
        return cached[0]

    try:
        season   = _current_season()
        target   = game_date or date.today().isoformat()
        all_games = _fetch_season_schedule(abbrev, season)

        # Garder seulement les matchs complétés (score présent) avant la date cible
        completed = [
            g for g in all_games
            if g.get("gameDate", "9999") < target
            and (g.get("homeTeam") or {}).get("score") is not None
            and (g.get("awayTeam") or {}).get("score") is not None
        ]
        completed.sort(key=lambda x: x.get("gameDate", ""), reverse=True)
        recent = completed[:n]

        if len(recent) < 3:
            _RECENT_GOALS_CACHE[abbrev] = ({}, now)
            return {}

        gf = ga = 0
        for g in recent:
            ht  = g.get("homeTeam") or {}
            at  = g.get("awayTeam") or {}
            hs  = int(ht.get("score") or 0)
            as_ = int(at.get("score") or 0)
            if ht.get("abbrev", "").upper() == abbrev:
                gf += hs; ga += as_
            else:
                gf += as_; ga += hs

        m = len(recent)
        result = {
            "gfPG_recent":      round(gf / m, 2),
            "gaPG_recent":      round(ga / m, 2),
            "goal_diff_recent": round((gf - ga) / m, 2),
            "n": m,
        }
        _RECENT_GOALS_CACHE[abbrev] = (result, now)
        return result

    except Exception:
        _RECENT_GOALS_CACHE[abbrev] = ({}, now)
        return {}


# ─── 3. Face-à-face ───────────────────────────────────────────────────────────

def _current_season() -> str:
    today = date.today()
    y = today.year
    return f"{y - 1}{y}" if today.month < 7 else f"{y}{y + 1}"


@lru_cache(maxsize=32)
def _fetch_season_schedule(abbrev: str, season: str) -> list:
    url = f"https://api-web.nhle.com/v1/club-schedule-season/{abbrev}/{season}"
    try:
        r = _SESSION.get(url, timeout=10)
        r.raise_for_status()
        return r.json().get("games", [])
    except Exception:
        return []


def get_h2h_stats(
    home_team: str,
    away_team: str,
    game_date: str | None = None,
) -> dict:
    """
    5 dernières rencontres entre les deux équipes (saison courante).
    Retourne : {games, home_wins, away_wins, avg_total_goals, recent_results}
    """
    home_abbrev = _get_abbrev(home_team)
    away_abbrev = _get_abbrev(away_team)
    if not home_abbrev or not away_abbrev:
        return {}

    season      = _current_season()
    target_date = game_date or date.today().isoformat()
    games       = _fetch_season_schedule(home_abbrev, season)

    h2h = []
    for g in games:
        gd = g.get("gameDate", "9999")
        if gd >= target_date:
            continue
        ha = (g.get("homeTeam") or {}).get("abbrev", "")
        aa = (g.get("awayTeam") or {}).get("abbrev", "")
        if {ha, aa} == {home_abbrev, away_abbrev}:
            h2h.append(g)

    h2h.sort(key=lambda x: x.get("gameDate", ""), reverse=True)
    h2h = h2h[:5]

    if not h2h:
        return {}

    home_wins = away_wins = 0
    total_goals = 0
    recent_results: list[str] = []

    for g in h2h:
        ht  = g.get("homeTeam") or {}
        at  = g.get("awayTeam") or {}
        hs  = ht.get("score") or 0
        as_ = at.get("score") or 0
        ha_g = ht.get("abbrev", "")
        total_goals += hs + as_

        # Résultat du point de vue de l'équipe home_abbrev
        if ha_g == home_abbrev:
            if hs > as_:
                home_wins += 1
            else:
                away_wins += 1
            recent_results.append(f"{home_abbrev} {hs}-{as_} {away_abbrev}")
        else:
            if as_ > hs:
                home_wins += 1
            else:
                away_wins += 1
            recent_results.append(f"{home_abbrev} {as_}-{hs} {away_abbrev}")

    n = len(h2h)
    return {
        "games":           n,
        "home_wins":       home_wins,
        "away_wins":       away_wins,
        "avg_total_goals": round(total_goals / n, 1),
        "recent_results":  recent_results,
    }


# ─── 4. Probabilités par période (Poisson) ────────────────────────────────────

# Distribution des buts NHL par période (approximation saison régulière)
_PERIOD_PCT = {1: 0.31, 2: 0.37, 3: 0.32}


def get_period_scoring_probs(
    home_gfPG: float,
    away_gfPG: float,
    home_gaPG: float,
    away_gaPG: float,
) -> dict:
    """
    Probabilités de marquer par période via Poisson.
    λ_home = (gfPG_home + gaPG_away) / 2 × pct_période

    Retourne pour période 1, 2, 3 :
      home_score_p{n}  P(équipe locale marque ≥1 but)
      away_score_p{n}  P(équipe visiteuse marque ≥1 but)
      btts_p{n}        P(les deux équipes marquent)
      score_prob_p{n}  P(au moins un but au total)
    """
    lh = (home_gfPG + away_gaPG) / 2
    la = (away_gfPG + home_gaPG) / 2

    result: dict = {}
    for period, pct in _PERIOD_PCT.items():
        lh_p = lh * pct
        la_p = la * pct
        p_home = 1 - math.exp(-lh_p)
        p_away = 1 - math.exp(-la_p)
        result[f"home_score_p{period}"] = round(p_home, 4)
        result[f"away_score_p{period}"] = round(p_away, 4)
        result[f"btts_p{period}"]       = round(p_home * p_away, 4)
        result[f"score_prob_p{period}"] = round(1 - math.exp(-(lh_p + la_p)), 4)

    return result
