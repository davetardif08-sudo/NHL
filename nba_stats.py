"""
Stats NBA depuis l'API ESPN (gratuite, sans clé).

Logique :
  - Standings ESPN → win%, home/road win%, L10, pts/match, pts encaissés/match
  - get_adjusted_prob() → blend 55% stats / 45% math (même modèle que NHL)
  - build_reason()      → explication textuelle pour les paris Excellent
"""

import json
import math
import re
import time
from pathlib import Path

CACHE_FILE = Path(__file__).parent / ".nba_stats_cache.json"
CACHE_TTL  = 6 * 3600   # 6 heures

# ESPN API
_ESPN_STANDINGS  = "https://site.api.espn.com/apis/v2/sports/basketball/nba/standings"
_ESPN_TEAMS      = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
_ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
_ESPN_INJURIES   = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
_ESPN_ADV_STATS  = "https://site.api.espn.com/apis/v2/sports/basketball/nba/statistics?limit=50&seasontype=2"

# Caches back-to-back et blessures (en mémoire, session uniquement)
_B2B_CACHE: dict = {}   # date_str → set(abbrevs qui ont joué ce jour)
_INJ_CACHE: dict = {}   # ts → {display_name_lower: count_weighted}
_INJ_TTL = 3600         # 1 heure

# ─── Mapping équipes ──────────────────────────────────────────────────────────

# Abréviations ESPN (peuvent différer du standard NBA officiel)
# GS, NO, NY, SA, UTAH au lieu de GSW, NOP, NYK, SAS, UTA
_TEAM_KEYWORDS: dict[str, str] = {
    "atlanta":        "ATL",  "hawks":         "ATL",
    "boston":         "BOS",  "celtics":        "BOS",
    "brooklyn":       "BKN",  "nets":           "BKN",
    "charlotte":      "CHA",  "hornets":        "CHA",
    "chicago":        "CHI",  "bulls":          "CHI",
    "cleveland":      "CLE",  "cavaliers":      "CLE",
    "dallas":         "DAL",  "mavericks":      "DAL",
    "denver":         "DEN",  "nuggets":        "DEN",
    "detroit":        "DET",  "pistons":        "DET",
    "golden state":   "GS",   "warriors":       "GS",   "golden": "GS",
    "houston":        "HOU",  "rockets":        "HOU",
    "indiana":        "IND",  "pacers":         "IND",
    "la clippers":    "LAC",  "clippers":       "LAC",
    "la lakers":      "LAL",  "lakers":         "LAL",
    "memphis":        "MEM",  "grizzlies":      "MEM",
    "miami":          "MIA",  "heat":           "MIA",
    "milwaukee":      "MIL",  "bucks":          "MIL",
    "minnesota":      "MIN",  "timberwolves":   "MIN",
    "new orleans":    "NO",   "pelicans":       "NO",
    "new york":       "NY",   "knicks":         "NY",
    "oklahoma":       "OKC",  "thunder":        "OKC",
    "orlando":        "ORL",  "magic":          "ORL",
    "philadelphia":   "PHI",  "76ers":          "PHI",  "sixers": "PHI",
    "phoenix":        "PHX",  "suns":           "PHX",
    "portland":       "POR",  "trail blazers":  "POR",  "blazers": "POR",
    "sacramento":     "SAC",  "kings":          "SAC",
    "san antonio":    "SA",   "spurs":          "SA",
    "toronto":        "TOR",  "raptors":        "TOR",
    "utah":           "UTAH", "jazz":           "UTAH",
    "washington":     "WSH",  "wizards":        "WSH",
}

# ─── Normalisation & matching ─────────────────────────────────────────────────

def _normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[àáâã]", "a", s)
    s = re.sub(r"[éèêë]", "e", s)
    s = re.sub(r"[íìîï]", "i", s)
    s = re.sub(r"[óòôõ]", "o", s)
    s = re.sub(r"[úùûü]", "u", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _word_in(keyword: str, text: str) -> bool:
    """Vérifie si keyword apparaît comme mot entier dans text (évite 'nets' ⊂ 'hornets')."""
    return bool(re.search(r'\b' + re.escape(keyword) + r'\b', text))


def _match_abbrev(team_name: str) -> str | None:
    # 1re passe : utiliser le nom entre parenthèses (le plus spécifique)
    #   ex: "(Hornets)" → "hornets" → CHA  |  "(Nets)" → "nets" → BKN
    paren = re.search(r'\(([^)]+)\)', team_name)
    if paren:
        team_part = _normalize(paren.group(1))
        for keyword, abbrev in _TEAM_KEYWORDS.items():
            if _word_in(keyword, team_part) or _word_in(team_part, keyword):
                return abbrev
    # 2e passe : nom complet normalisé
    n = _normalize(team_name)
    for keyword, abbrev in _TEAM_KEYWORDS.items():
        if _word_in(keyword, n) or _word_in(n, keyword):
            return abbrev
    return None


# ─── Cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(data: dict) -> None:
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── Fetch ESPN standings ─────────────────────────────────────────────────────

def _fetch_standings() -> dict[str, dict]:
    """
    Retourne un dict {abbrev: stats} depuis ESPN standings.
    Vrais noms de champs ESPN : winPercent, avgPointsFor, avgPointsAgainst,
    wins, losses, streak (négatif = défaites), playoffSeed.
    """
    try:
        import requests
        resp = requests.get(
            _ESPN_STANDINGS,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result: dict[str, dict] = {}

    # ESPN standings : data.children[] (conférences) → standings.entries[] (équipes)
    for conference in data.get("children", []):
        for entry in conference.get("standings", {}).get("entries", []):
            team_data = entry.get("team", {})
            abbrev    = team_data.get("abbreviation", "").upper()
            if not abbrev:
                continue

            # Construire un dict name→value depuis le tableau stats[]
            sr: dict[str, float] = {}
            for stat in entry.get("stats", []):
                name = stat.get("name") or stat.get("type", "")
                try:
                    sr[name] = float(stat.get("value") or 0)
                except (TypeError, ValueError):
                    sr[name] = 0.0

            wins   = int(sr.get("wins",   0))
            losses = int(sr.get("losses", 0))
            gp     = wins + losses or 1

            win_pct    = sr.get("winPercent",        wins / gp)
            pts_pg     = sr.get("avgPointsFor",      sr.get("avgPoints",        0.0))
            opp_pts_pg = sr.get("avgPointsAgainst",  sr.get("avgPointsAllowed", 0.0))
            seq        = int(sr.get("playoffSeed",   sr.get("leagueRank", 99)))

            # Streak : positif = victoires, négatif = défaites
            streak_raw   = sr.get("streak", 0)
            streak_code  = "W" if streak_raw >= 0 else "L"
            streak_count = int(abs(streak_raw))

            # Essayer d'extraire les vrais splits home/road/L10 depuis records[]
            records_arr = entry.get("records", [])
            home_wins = road_wins = home_losses = road_losses = l10w = l10l = None
            for rec in records_arr:
                ab = (rec.get("abbreviation") or rec.get("type") or "").lower()
                w  = rec.get("wins")
                l  = rec.get("losses")
                if ab in ("home", "domicile") and w is not None:
                    home_wins, home_losses = int(w), int(l or 0)
                elif ab in ("road", "away", "extérieur") and w is not None:
                    road_wins, road_losses = int(w), int(l or 0)
                elif ab in ("l10", "last 10", "last ten", "10") and w is not None:
                    l10w, l10l = int(w), int(l or 0)

            # Utiliser vrais splits si disponibles, sinon approximation
            if home_wins is not None and (home_wins + home_losses) > 0:
                home_win_pct = home_wins / (home_wins + home_losses)
            else:
                home_win_pct = min(0.95, win_pct + 0.08)

            if road_wins is not None and (road_wins + road_losses) > 0:
                road_win_pct = road_wins / (road_wins + road_losses)
            else:
                road_win_pct = max(0.05, win_pct - 0.08)

            if l10w is not None:
                l10_wins, l10_gp = l10w, l10w + (l10l or 0)
            else:
                l10_wins, l10_gp = 5, 10

            result[abbrev] = {
                "gamesPlayed":  gp,
                "wins":         wins,
                "losses":       losses,
                "winPct":       win_pct,
                "homeWinPct":   home_win_pct,
                "roadWinPct":   road_win_pct,
                "l10Wins":      l10_wins,
                "l10GP":        l10_gp,
                "ptsPG":        pts_pg,
                "oppPtsPG":     opp_pts_pg,
                "netRating":    round(pts_pg - opp_pts_pg, 1),
                "homeWins":     home_wins or 0,
                "roadWins":     road_wins or 0,
                "leagueSeq":    seq,
                "streakCode":   streak_code,
                "streakCount":  streak_count,
            }

    return result


def _fetch_adv_stats() -> dict[str, dict]:
    """
    Récupère pace, ORtg, DRtg depuis l'API ESPN statistics.
    Retourne {abbrev: {pace, ortg, drtg}} — dict vide si l'API échoue.
    """
    try:
        import requests
        resp = requests.get(
            _ESPN_ADV_STATS,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result: dict[str, dict] = {}

    # L'endpoint peut retourner plusieurs structures selon la version ESPN
    # Stratégie 1 : data["items"] = [{team: {abbreviation}, categories: [...]}]
    items = data.get("items") or data.get("statistics", {}).get("items", [])
    for item in items:
        team_info = item.get("team", {})
        abbrev = team_info.get("abbreviation", "").upper()
        if not abbrev:
            continue
        adv: dict[str, float] = {}
        categories = item.get("categories") or []
        for cat in categories:
            stats_list = cat.get("stats") or cat.get("values", [])
            for s in stats_list:
                name = (s.get("name") or s.get("type") or "").lower()
                try:
                    val = float(s.get("value") or s.get("displayValue") or 0)
                except (TypeError, ValueError):
                    continue
                if "pace" in name:
                    adv["pace"] = val
                elif name in ("offensiverating", "offrating", "ortg", "offensive rating"):
                    adv["ortg"] = val
                elif name in ("defensiverating", "defrating", "drtg", "defensive rating"):
                    adv["drtg"] = val
        if adv:
            result[abbrev] = adv

    # Stratégie 2 : données plates dans data["rows"] ou data["groups"]
    if not result:
        groups = data.get("groups") or data.get("rows") or []
        for group in groups:
            for row in group.get("rows") if isinstance(group, dict) else []:
                abbrev = ""
                adv = {}
                for cell in row if isinstance(row, list) else []:
                    # ESPN sometimes returns flat [team_abbrev, stat1, stat2...]
                    if isinstance(cell, str) and len(cell) <= 4 and cell.isupper():
                        abbrev = cell
                if abbrev and adv:
                    result[abbrev] = adv

    return result


def _get_all_stats() -> dict[str, dict]:
    """Retourne les stats de toutes les équipes NBA (avec cache 6h)."""
    cache = _load_cache()
    ts    = cache.get("_ts", 0)
    teams = cache.get("teams", {})

    if teams and time.time() - ts < CACHE_TTL:
        return teams

    fetched = _fetch_standings()
    if fetched:
        # Enrichir avec les stats avancées (pace, ORtg, DRtg)
        adv = _fetch_adv_stats()
        if adv:
            for abbrev, team in fetched.items():
                # Normaliser les abréviations ESPN (GS vs GSW, etc.)
                adv_data = adv.get(abbrev) or adv.get(abbrev[:3]) or {}
                if adv_data:
                    team.update(adv_data)
        _save_cache({"_ts": time.time(), "teams": fetched})
        return fetched
    return teams   # retour stale si l'API échoue


# ─── Interface publique ───────────────────────────────────────────────────────

def get_team_stats(team_name: str) -> dict | None:
    """Retourne les stats d'une équipe NBA, ou None si introuvable."""
    abbrev = _match_abbrev(team_name)
    if not abbrev:
        return None
    return _get_all_stats().get(abbrev)


# ─── Back-to-back ─────────────────────────────────────────────────────────────

def _teams_played_on(date_str: str) -> set:
    """
    Retourne les abréviations ESPN des équipes qui ont joué le jour donné.
    date_str format : 'YYYY-MM-DD'
    Résultat mis en cache en mémoire (historique immuable).
    """
    if date_str in _B2B_CACHE:
        return _B2B_CACHE[date_str]
    try:
        import requests
        espn_date = date_str.replace("-", "")
        resp = requests.get(
            _ESPN_SCOREBOARD,
            params={"dates": espn_date},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        played: set = set()
        for event in resp.json().get("events", []):
            for comp in event.get("competitions", [{}]):
                for c in comp.get("competitors", []):
                    abbrev = c.get("team", {}).get("abbreviation", "").upper()
                    if abbrev:
                        played.add(abbrev)
        _B2B_CACHE[date_str] = played
        return played
    except Exception:
        return set()


def get_back_to_back_info(home_team: str, away_team: str, match_date: str) -> dict:
    """
    Vérifie si une équipe joue un back-to-back (match la veille).
    Retourne {home_b2b, away_b2b, rest_adv}
    rest_adv > 0 = avantage pour l'équipe à domicile
    rest_adv < 0 = avantage pour l'équipe visiteuse
    """
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(match_date, "%Y-%m-%d")
        yesterday = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        played = _teams_played_on(yesterday)
        home_abbrev = _match_abbrev(home_team) or ""
        away_abbrev = _match_abbrev(away_team) or ""
        home_b2b = bool(home_abbrev and home_abbrev in played)
        away_b2b = bool(away_abbrev and away_abbrev in played)
        if home_b2b and not away_b2b:
            rest_adv = -1.0  # away a l'avantage repos
        elif away_b2b and not home_b2b:
            rest_adv = 1.0   # home a l'avantage repos
        else:
            rest_adv = 0.0
        return {"home_b2b": home_b2b, "away_b2b": away_b2b, "rest_adv": rest_adv}
    except Exception:
        return {"home_b2b": False, "away_b2b": False, "rest_adv": 0.0}


# ─── Blessures ────────────────────────────────────────────────────────────────

def _fetch_injury_counts() -> dict:
    """
    Récupère le nombre pondéré de blessés par équipe depuis ESPN.
    Out = 1.0 pt, Doubtful = 0.5, Day-To-Day/Questionable = 0.25
    Retourne {display_name_lower: score_blessures}
    """
    now = time.time()
    if _INJ_CACHE and (now - _INJ_CACHE.get("_ts", 0)) < _INJ_TTL:
        return _INJ_CACHE.get("data", {})
    try:
        import requests
        resp = requests.get(
            _ESPN_INJURIES,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        resp.raise_for_status()
        weights_map = {"out": 1.0, "doubtful": 0.75, "day-to-day": 0.25, "questionable": 0.25}
        result: dict = {}
        for entry in resp.json().get("injuries", []):
            name = _normalize(entry.get("displayName", ""))
            if not name:
                continue
            score = 0.0
            for inj in entry.get("injuries", []):
                status = inj.get("status", "").lower()
                score += weights_map.get(status, 0.0)
            result[name] = round(score, 2)
        _INJ_CACHE["data"] = result
        _INJ_CACHE["_ts"]  = now
        return result
    except Exception:
        return _INJ_CACHE.get("data", {})


def get_injury_advantage(home_team: str, away_team: str) -> dict:
    """
    Calcule l'avantage net de blessures (score away - score home).
    injury_adv > 0 = avantage home (away plus blessée)
    injury_adv < 0 = avantage away (home plus blessée)
    """
    try:
        counts = _fetch_injury_counts()
        home_n = _normalize(re.sub(r"\s*\(.*?\)", "", home_team))
        away_n = _normalize(re.sub(r"\s*\(.*?\)", "", away_team))
        home_score = 0.0
        away_score = 0.0
        for team_name, score in counts.items():
            if any(w in team_name for w in home_n.split() if len(w) > 3):
                home_score = score
            elif any(w in team_name for w in away_n.split() if len(w) > 3):
                away_score = score
        injury_adv = round(away_score - home_score, 2)
        return {
            "home_injured": home_score,
            "away_injured": away_score,
            "injury_adv":   injury_adv,
        }
    except Exception:
        return {"home_injured": 0.0, "away_injured": 0.0, "injury_adv": 0.0}


# ─── Probabilité ajustée ──────────────────────────────────────────────────────

def get_adjusted_prob(
    home_team: str,
    away_team: str,
    bet_type: str,
    selection: str,
    math_prob: float,
    match_date: str | None = None,
) -> float:
    """
    Probabilité ajustée par les stats NBA.
    Blend : 55% stats / 45% probabilité mathématique des cotes.
    """
    try:
        from predictions import get_nba_feature_weights
        fw     = get_nba_feature_weights()
        stat_w = fw.get("stat_vs_math", 0.55)
    except Exception:
        stat_w = 0.55
    math_w = 1.0 - stat_w

    bt  = bet_type.lower()
    sel = selection.lower()

    home = get_team_stats(home_team)
    away = get_team_stats(away_team)
    if not home or not away:
        return math_prob

    # ── Gagnant du match ──────────────────────────────────────────────────────
    if any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        home_n = re.sub(r"\s*\(.*?\)", "", home_team.lower()).strip()
        away_n = re.sub(r"\s*\(.*?\)", "", away_team.lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)

        sel_team = home if home_sel else away
        opp_team = away if home_sel else home

        # Utiliser les vrais splits domicile/route (plus précis qu'un +4% fixe)
        sel_base = sel_team["homeWinPct"] if home_sel else sel_team["roadWinPct"]
        opp_base = opp_team["roadWinPct"] if home_sel else opp_team["homeWinPct"]

        # Normaliser
        total = (sel_base + opp_base) or 1.0
        stat_prob = sel_base / total

        # Ajustement forme récente (L10)
        sel_l10 = sel_team["l10Wins"] / max(sel_team["l10GP"], 1)
        opp_l10 = opp_team["l10Wins"] / max(opp_team["l10GP"], 1)
        form_adj = (sel_l10 - opp_l10) * 0.06
        stat_prob = max(0.05, min(0.95, stat_prob + form_adj))

        # Ajustement back-to-back
        if match_date:
            try:
                b2b = get_back_to_back_info(home_team, away_team, match_date)
                rest_adv = b2b["rest_adv"]       # +1=avantage home, -1=avantage away
                if rest_adv != 0.0:
                    adj = rest_adv * 0.04        # ±4% pour un B2B
                    stat_prob = max(0.05, min(0.95, stat_prob + (adj if home_sel else -adj)))
            except Exception:
                pass

        # Ajustement blessures
        try:
            inj = get_injury_advantage(home_team, away_team)
            inj_adv = inj["injury_adv"]          # >0=avantage home, <0=avantage away
            if abs(inj_adv) >= 0.5:
                adj = min(0.06, inj_adv * 0.025)
                stat_prob = max(0.05, min(0.95, stat_prob + (adj if home_sel else -adj)))
        except Exception:
            pass

        return round(stat_w * stat_prob + math_w * math_prob, 4)

    # ── Total de points (Over/Under) ──────────────────────────────────────────
    if any(k in bt for k in ("total", "points", "plus/moins")):
        # Chercher la ligne dans sel d'abord, puis dans bt (ex: "Plus de" sans nombre)
        m = re.search(r"(\d+[.,]\d+|\d+)", sel) or re.search(r"(\d+[.,]\d+)", bt)
        if not m:
            return math_prob
        line  = float(m.group(1).replace(",", "."))
        over  = any(k in sel for k in ("plus", "over", "surpasse"))

        pts_h = home.get("ptsPG", 112.0)
        pts_a = away.get("ptsPG", 112.0)
        opp_h = home.get("oppPtsPG", 112.0)
        opp_a = away.get("oppPtsPG", 112.0)

        # Projection pace-ajustée si ORtg/DRtg/pace disponibles
        pace_h = home.get("pace")
        pace_a = away.get("pace")
        ortg_h = home.get("ortg")
        drtg_h = home.get("drtg")
        ortg_a = away.get("ortg")
        drtg_a = away.get("drtg")

        if pace_h and pace_a and ortg_h and drtg_h and ortg_a and drtg_a:
            game_pace = (pace_h + pace_a) / 2
            proj_h = (ortg_h + drtg_a) / 2 * game_pace / 100
            proj_a = (ortg_a + drtg_h) / 2 * game_pace / 100
        else:
            proj_h = (pts_h + opp_a) / 2
            proj_a = (pts_a + opp_h) / 2

        # Détecter le type de pari : plein match / équipe spécifique / mi-temps
        home_n = re.sub(r"\s*\(.*?\)", "", home_team).lower().strip()
        away_n = re.sub(r"\s*\(.*?\)", "", away_team).lower().strip()
        bt_clean = re.sub(r"\s*\(.*?\)", "", bt).strip()

        is_home_team = any(w in bt_clean for w in home_n.split() if len(w) > 3)
        is_away_team = any(w in bt_clean for w in away_n.split() if len(w) > 3)
        is_first_half = "1" in bt and "demie" in bt
        is_second_half = "2" in bt and "demie" in bt

        if is_home_team:
            expected_total = proj_h         # ligne équipe domicile
            sigma = 8.5
        elif is_away_team:
            expected_total = proj_a         # ligne équipe visiteuse
            sigma = 8.5
        elif is_first_half or is_second_half:
            expected_total = (proj_h + proj_a) / 2   # ~mi-temps
            sigma = 7.0
        else:
            expected_total = proj_h + proj_a          # plein match
            sigma = 13.5

        # Ajustement back-to-back
        if match_date:
            try:
                b2b = get_back_to_back_info(home_team, away_team, match_date)
                scale = 0.5 if (is_home_team or is_away_team or is_first_half or is_second_half) else 1.0
                if b2b["home_b2b"]: expected_total -= 2.5 * scale
                if b2b["away_b2b"]: expected_total -= 2.5 * scale
            except Exception:
                pass

        # Ajustement blessures
        try:
            inj = get_injury_advantage(home_team, away_team)
            inj_total = inj["home_injured"] + inj["away_injured"]
            scale = 0.5 if (is_home_team or is_away_team or is_first_half or is_second_half) else 1.0
            if inj_total >= 0.5:
                expected_total -= inj_total * 1.5 * scale
        except Exception:
            pass

        z = (line - expected_total) / sigma
        p_over = 1.0 - _normal_cdf(z)
        stat_prob = p_over if over else 1.0 - p_over
        stat_prob = max(0.05, min(0.95, stat_prob))

        return round(stat_w * stat_prob + math_w * math_prob, 4)

    return math_prob


def get_ou_projection(
    home_team: str,
    away_team: str,
    bet_type: str,
    selection: str,
    match_date: str | None = None,
) -> dict:
    """
    Retourne la projection Over/Under avec détails.
    {expected_total, line, gap, pace_h, pace_a, b2b_home, b2b_away, inj_total}
    gap > 0 → tendance Over, gap < 0 → tendance Under
    """
    result = {"expected_total": None, "line": None, "gap": None,
              "pace_h": None, "pace_a": None, "b2b_home": False, "b2b_away": False,
              "inj_total": 0.0}
    try:
        sel = selection.lower()
        btl = bet_type.lower()
        # Chercher la ligne dans sel, puis dans bet_type
        m = re.search(r"(\d+[.,]\d+|\d+)", sel) or re.search(r"(\d+[.,]\d+)", btl)
        if not m:
            return result
        line = float(m.group(1).replace(",", "."))
        result["line"] = line

        home = get_team_stats(home_team)
        away = get_team_stats(away_team)
        if not home or not away:
            return result

        pts_h = home.get("ptsPG", 112.0)
        pts_a = away.get("ptsPG", 112.0)
        opp_h = home.get("oppPtsPG", 112.0)
        opp_a = away.get("oppPtsPG", 112.0)

        pace_h = home.get("pace")
        pace_a = away.get("pace")
        ortg_h = home.get("ortg")
        drtg_h = home.get("drtg")
        ortg_a = away.get("ortg")
        drtg_a = away.get("drtg")

        if pace_h and pace_a and ortg_h and drtg_h and ortg_a and drtg_a:
            game_pace = (pace_h + pace_a) / 2
            proj_h = (ortg_h + drtg_a) / 2 * game_pace / 100
            proj_a = (ortg_a + drtg_h) / 2 * game_pace / 100
            result["pace_h"] = round(pace_h, 1)
            result["pace_a"] = round(pace_a, 1)
        else:
            proj_h = (pts_h + opp_a) / 2
            proj_a = (pts_a + opp_h) / 2

        # Détecter type de pari pour choisir la bonne projection
        home_n  = re.sub(r"\s*\(.*?\)", "", home_team).lower().strip()
        away_n  = re.sub(r"\s*\(.*?\)", "", away_team).lower().strip()
        bt_clean = re.sub(r"\s*\(.*?\)", "", btl).strip()
        is_home_team  = any(w in bt_clean for w in home_n.split() if len(w) > 3)
        is_away_team  = any(w in bt_clean for w in away_n.split() if len(w) > 3)
        is_half = ("1" in btl and "demie" in btl) or ("2" in btl and "demie" in btl)

        if is_home_team:
            expected = proj_h
            result["bet_scope"] = "home_team"
        elif is_away_team:
            expected = proj_a
            result["bet_scope"] = "away_team"
        elif is_half:
            expected = (proj_h + proj_a) / 2
            result["bet_scope"] = "half"
        else:
            expected = proj_h + proj_a
            result["bet_scope"] = "full_game"

        scale = 0.5 if (is_home_team or is_away_team or is_half) else 1.0

        if match_date:
            b2b = get_back_to_back_info(home_team, away_team, match_date)
            result["b2b_home"] = b2b.get("home_b2b", False)
            result["b2b_away"] = b2b.get("away_b2b", False)
            if result["b2b_home"]: expected -= 2.5 * scale
            if result["b2b_away"]: expected -= 2.5 * scale

        inj = get_injury_advantage(home_team, away_team)
        inj_total = inj["home_injured"] + inj["away_injured"]
        result["inj_total"] = round(inj_total, 2)
        if inj_total >= 0.5:
            expected -= inj_total * 1.5 * scale

        result["expected_total"] = round(expected, 1)
        result["gap"] = round(expected - line, 1)
    except Exception:
        pass
    return result


def _normal_cdf(z: float) -> float:
    """Approximation de la CDF de la loi normale standard."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


# ─── Explication textuelle ────────────────────────────────────────────────────

def build_reason(
    home_team: str,
    away_team: str,
    bet_type: str,
    selection: str,
    match_date: str | None = None,
) -> str:
    """
    Retourne une explication textuelle pour les paris NBA Excellent.
    """
    bt  = bet_type.lower()
    sel = selection.lower()

    home = get_team_stats(home_team)
    away = get_team_stats(away_team)
    if not home or not away:
        return ""

    parts: list[str] = []

    # ── Gagnant ───────────────────────────────────────────────────────────────
    if any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        home_n = re.sub(r"\s*\(.*?\)", "", home_team.lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)

        sel_team = home if home_sel else away
        opp_team = away if home_sel else home
        sel_name = (home_team if home_sel else away_team).split(" (")[0]
        opp_name = (away_team if home_sel else home_team).split(" (")[0]

        wp = round(sel_team["winPct"] * 100, 1)
        parts.append(f"{sel_name} {wp}% vict")

        # Streak
        if sel_team.get("streakCount", 0) >= 3:
            code  = sel_team.get("streakCode", "")
            count = sel_team["streakCount"]
            verb  = "victoires" if code == "W" else "défaites"
            parts.append(f"{count} {verb} de suite")

        # Forme L10
        sel_l10 = sel_team["l10Wins"]
        opp_l10 = opp_team["l10Wins"]
        if sel_l10 != opp_l10:
            parts.append(f"L10 : {sel_l10}-{10-sel_l10} vs {opp_l10}-{10-opp_l10}")

        # Points/match
        pts_s = round(sel_team.get("ptsPG", 0), 1)
        pts_o = round(opp_team.get("ptsPG", 0), 1)
        if pts_s and pts_o:
            parts.append(f"pts/match : {pts_s} vs {pts_o}")

        # Avantage domicile
        if home_sel:
            parts.append(f"avantage domicile")

    # ── Total de points ───────────────────────────────────────────────────────
    elif any(k in bt for k in ("total", "points", "plus/moins")):
        pts_h = round(home.get("ptsPG", 0), 1)
        pts_a = round(away.get("ptsPG", 0), 1)
        opp_h = round(home.get("oppPtsPG", 0), 1)
        opp_a = round(away.get("oppPtsPG", 0), 1)

        home_name = home_team.split(" (")[0]
        away_name = away_team.split(" (")[0]

        # Utiliser get_ou_projection pour la projection consolidée
        proj = get_ou_projection(home_team, away_team, bet_type, sel, match_date)
        expected = proj.get("expected_total") or round((pts_h + opp_a) / 2 + (pts_a + opp_h) / 2, 1)
        gap      = proj.get("gap")
        line     = proj.get("line")

        if line and gap is not None:
            direction = "↑ Over" if gap > 0 else "↓ Under"
            parts.append(f"Projection {expected} pts ({direction} de {abs(gap):.1f} vs ligne {line})")
        else:
            parts.append(f"Total prévu : {expected} pts")

        parts.append(f"{home_name} {pts_h} · {away_name} {pts_a} pts/match")
        if opp_h and opp_a:
            parts.append(f"déf. : {opp_h} vs {opp_a} encaissés")

        # Pace (si disponible)
        pace_h = home.get("pace")
        pace_a = away.get("pace")
        if pace_h and pace_a:
            game_pace = round((pace_h + pace_a) / 2, 1)
            parts.append(f"pace : {game_pace:.0f} poss/match")

        # Signaux B2B et blessures
        if proj.get("b2b_home"):
            parts.append(f"⚠ {home_name} B2B")
        if proj.get("b2b_away"):
            parts.append(f"⚠ {away_name} B2B")
        inj = proj.get("inj_total", 0)
        if inj >= 1.0:
            parts.append(f"⚠ blessures ({inj:.1f}pts impact)")

    # ── Gagnant : signaux nets supplémentaires ────────────────────────────────
    if any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        # NetRating si disponible et significatif
        home_net = home.get("netRating", 0)
        away_net = away.get("netRating", 0)
        home_n2  = re.sub(r"\s*\(.*?\)", "", home_team.lower()).strip()
        home_sel = any(w in sel for w in home_n2.split() if len(w) > 2)
        sel_net  = home_net if home_sel else away_net
        opp_net  = away_net if home_sel else home_net
        net_diff = sel_net - opp_net
        if abs(net_diff) >= 3:
            sign = "+" if net_diff > 0 else ""
            parts.append(f"net rating {sign}{net_diff:.1f}")

        # Back-to-back
        if match_date:
            try:
                b2b = get_back_to_back_info(home_team, away_team, match_date)
                h_n = home_team.split(" (")[0]
                a_n = away_team.split(" (")[0]
                if b2b["home_b2b"]:
                    parts.append(f"⚠ {h_n} B2B")
                if b2b["away_b2b"]:
                    parts.append(f"⚠ {a_n} B2B")
            except Exception:
                pass

    return " · ".join(parts)
