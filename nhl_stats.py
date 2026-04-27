"""
Récupère les stats des équipes NHL depuis l'API publique de nhl.com.
Utilisé pour enrichir les explications des paris recommandés.
"""

import requests
from functools import lru_cache

STANDINGS_URL = "https://api-web.nhle.com/v1/standings/now"

# Session partagée pour réutiliser les connexions TCP/SSL
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

# ── Détection playoffs + score de série via NHL API ──────────────────────────
_GAME_TYPE_CACHE: dict = {}   # date_str → {(away_ab, home_ab): gameType}
_SERIES_CACHE:    dict = {}   # date_str → {(away_ab, home_ab): (away_wins, home_wins)}

def _get_game_type(home_team: str, away_team: str, date_str: str | None) -> str:
    """Retourne 'playoff' ou 'regular' pour un match donné."""
    if not date_str:
        return "regular"
    if date_str not in _GAME_TYPE_CACHE:
        try:
            resp = _SESSION.get(
                f"https://api-web.nhle.com/v1/score/{date_str}",
                timeout=5,
            )
            if resp.ok:
                games = resp.json().get("games", [])
                type_map   = {}
                series_map = {}
                for g in games:
                    away_ab = g.get("awayTeam", {}).get("abbrev", "")
                    home_ab = g.get("homeTeam", {}).get("abbrev", "")
                    key = (away_ab, home_ab)
                    type_map[key] = g.get("gameType", 2)
                    # seriesSummary présent dans les parties playoff
                    ss = g.get("seriesSummary", {}) or {}
                    series_map[key] = (
                        ss.get("awayWins", 0) or 0,
                        ss.get("homeWins", 0) or 0,
                    )
                _GAME_TYPE_CACHE[date_str] = type_map
                _SERIES_CACHE[date_str]    = series_map
            else:
                _GAME_TYPE_CACHE[date_str] = {}
                _SERIES_CACHE[date_str]    = {}
        except Exception:
            _GAME_TYPE_CACHE[date_str] = {}
            _SERIES_CACHE[date_str]    = {}

    away_ab = (_match_abbrev(away_team) or "").upper()
    home_ab = (_match_abbrev(home_team) or "").upper()
    game_type = _GAME_TYPE_CACHE.get(date_str, {}).get((away_ab, home_ab), 2)
    return "playoff" if game_type == 3 else "regular"


def _get_series_score(home_team: str, away_team: str, date_str: str | None) -> tuple[int, int]:
    """
    Retourne (away_wins, home_wins) dans la série avant le match du jour.
    Appeler _get_game_type d'abord pour peupler le cache.
    """
    if not date_str:
        return (0, 0)
    # S'assurer que le cache est peuplé
    _get_game_type(home_team, away_team, date_str)
    away_ab = (_match_abbrev(away_team) or "").upper()
    home_ab = (_match_abbrev(home_team) or "").upper()
    return _SERIES_CACHE.get(date_str, {}).get((away_ab, home_ab), (0, 0))

_TEAM_KEYWORDS: dict[str, str] = {
    "montreal":    "MTL", "montréal":  "MTL", "canadiens": "MTL",
    "toronto":     "TOR", "maple leafs": "TOR",
    "boston":      "BOS", "bruins":    "BOS",
    "ny rangers":  "NYR", "new york rangers": "NYR", "rangers": "NYR",
    "ny islanders":"NYI", "islanders": "NYI",
    "philadelphia":"PHI", "flyers":    "PHI", "philadelphie":"PHI",
    "pittsburgh":  "PIT", "penguins":  "PIT",
    "washington":  "WSH", "capitals":  "WSH",
    "carolina":    "CAR", "hurricanes":"CAR", "caroline":"CAR",
    "new jersey":  "NJD", "devils":    "NJD",
    "columbus":    "CBJ", "blue jackets":"CBJ",
    "tampa bay":   "TBL", "lightning": "TBL",
    "florida":     "FLA", "panthers":  "FLA", "floride":"FLA",
    "detroit":     "DET", "red wings": "DET",
    "ottawa":      "OTT", "senators":  "OTT",
    "buffalo":     "BUF", "sabres":    "BUF",
    "chicago":     "CHI", "blackhawks":"CHI",
    "nashville":   "NSH", "predators": "NSH",
    "st. louis":   "STL", "st louis":  "STL", "blues": "STL", "saint-louis":"STL", "saint louis":"STL",
    "winnipeg":    "WPG", "jets":      "WPG",
    "minnesota":   "MIN", "wild":      "MIN",
    "colorado":    "COL", "avalanche": "COL",
    "dallas":      "DAL", "stars":     "DAL",
    "calgary":     "CGY", "flames":    "CGY",
    "edmonton":    "EDM", "oilers":    "EDM",
    "vancouver":   "VAN", "canucks":   "VAN",
    "seattle":     "SEA", "kraken":    "SEA",
    "vegas":       "VGK", "golden knights": "VGK",
    "anaheim":     "ANA", "ducks":     "ANA",
    "los angeles": "LAK", "kings":     "LAK",
    "san jose":    "SJS", "sharks":    "SJS",
    "utah":        "UTA",
}


def _normalize(name: str) -> str:
    return name.lower().strip()


def _word_in(keyword: str, text: str) -> bool:
    """Vérifie si keyword apparaît comme mot entier dans text."""
    import re as _re
    return bool(_re.search(r'\b' + _re.escape(keyword) + r'\b', text))


def _match_abbrev(team_name: str) -> str | None:
    import re
    n = _normalize(team_name)
    # 1re passe : tester le nom complet (avec parenthèses) — priorité au nom d'équipe
    #   ex: "New York (Islanders)" → "islanders" matche → NYI (pas NYR)
    for keyword, abbrev in _TEAM_KEYWORDS.items():
        if _word_in(keyword, n) or _word_in(n, keyword):
            return abbrev
    # 2e passe : enlever le suffixe entre parenthèses et réessayer
    #   ex: "Anaheim (Ducks)" → "anaheim" → ANA
    n_stripped = re.sub(r'\s*\(.*?\)', '', n).strip()
    if n_stripped != n:
        for keyword, abbrev in _TEAM_KEYWORDS.items():
            if _word_in(keyword, n_stripped) or _word_in(n_stripped, keyword):
                return abbrev
    return None


@lru_cache(maxsize=1)
def _fetch_standings() -> dict[str, dict]:
    try:
        resp = _SESSION.get(STANDINGS_URL, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return {}

    result = {}
    for team in data.get("standings", []):
        abbrev = team.get("teamAbbrev", {})
        if isinstance(abbrev, dict):
            abbrev = abbrev.get("default", "")
        gp      = team.get("gamesPlayed", 1) or 1
        home_gp = team.get("homeGamesPlayed", 1) or 1
        road_gp = team.get("roadGamesPlayed", 1) or 1
        l10gp   = team.get("l10GamesPlayed", 10) or 10
        result[abbrev] = {
            "name":        (team.get("teamCommonName") or {}).get("default", abbrev),
            "wins":        team.get("wins", 0),
            "losses":      team.get("losses", 0),
            "otLosses":    team.get("otLosses", 0),
            "gp":          gp,
            "winPct":      round(team.get("wins", 0) / gp * 100),
            "homeWins":    team.get("homeWins", 0),
            "homeGP":      home_gp,
            "homeWinPct":  round(team.get("homeWins", 0) / home_gp * 100),
            "roadWins":    team.get("roadWins", 0),
            "roadGP":      road_gp,
            "roadWinPct":  round(team.get("roadWins", 0) / road_gp * 100),
            "streakCode":  team.get("streakCode", ""),
            "streakCount": team.get("streakCount", 0),
            "l10Wins":     team.get("l10Wins", 0),
            "l10Losses":   team.get("l10Losses", 0),
            "l10OtLosses": team.get("l10OtLosses", 0),
            "l10GP":       l10gp,
            "gfPG":        round(team.get("goalFor", 0) / gp, 2),
            "gaPG":        round(team.get("goalAgainst", 0) / gp, 2),
            # ── Splits domicile / extérieur (utilisés pour Total de buts) ──
            "homeGfPG":    round(team.get("homeGoalsFor", 0) / home_gp, 2),
            "homeGaPG":    round(team.get("homeGoalsAgainst", 0) / home_gp, 2),
            "roadGfPG":    round(team.get("roadGoalsFor", 0) / road_gp, 2),
            "roadGaPG":    round(team.get("roadGoalsAgainst", 0) / road_gp, 2),
            "homeGpRaw":   home_gp,
            "roadGpRaw":   road_gp,
            "points":      team.get("points", 0),
            "leagueSeq":   team.get("leagueSequence", 99),
        }
    return result


def get_team_stats(team_name: str) -> dict | None:
    abbrev = _match_abbrev(team_name)
    if not abbrev:
        return None
    return _fetch_standings().get(abbrev)


def _streak_text(t: dict) -> str:
    code  = t.get("streakCode", "")
    count = t.get("streakCount", 0)
    if not code or not count:
        return ""
    if code == "W":
        return f"série de {count}V" if count >= 2 else ""
    if code == "L":
        return f"série de {count}D" if count >= 2 else ""
    return ""


def get_adjusted_prob(
    home_team: str, away_team: str, bet_type: str,
    selection: str, math_prob: float,
    match_date: str | None = None,
) -> float:
    """
    Retourne une probabilité ajustée (0-1) en blendant :
      - 55% probabilité basée sur les stats réelles NHL
      - 45% probabilité mathématique (fair_prob des cotes)

    Marchés couverts :
      - Gagnant du match (2 ou 3 issues) : win% domicile/route + ajustement L10
      - Total de buts : distribution de Poisson sur λ = attaque vs défense
    Pour les autres marchés : retourne math_prob inchangé.
    """
    import re
    import math as _math

    home = get_team_stats(home_team)
    away = get_team_stats(away_team)
    bt  = bet_type.lower()
    sel = selection.lower()

    # ── Détection playoffs ────────────────────────────────────────────────────
    _is_playoff = _get_game_type(home_team, away_team, match_date) == "playoff"

    # Paramètres modulés selon le contexte
    _home_base_w  = 0.63  if _is_playoff else 0.60   # avantage glace plus fort en séries
    _l10_factor   = 0.05  if _is_playoff else 0.15   # L10 moins prédictif en séries
    _stat_default = 0.60  if _is_playoff else 0.55   # stats comptent plus vs marché
    _apply_b2b    = not _is_playoff                   # back-to-back rarissime en séries
    _scoring_k    = 0.90  if _is_playoff else 1.00   # ~10% moins de buts en séries (conservateur)
    _apply_jetlag = not _is_playoff                   # même ville pour 2 matchs en séries
    _apply_goaldiff = not _is_playoff                 # diff buts saison régulière = bruit en séries

    # Score de la série en cours (momentum)
    _series_away_w, _series_home_w = (0, 0)
    if _is_playoff:
        _series_away_w, _series_home_w = _get_series_score(home_team, away_team, match_date)

    # ── Gagnant du match ──────────────────────────────────────────────────────
    if any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        if not home or not away:
            return math_prob

        home_name_n = re.sub(r'\s*\(.*?\)', '', _normalize(home_team)).strip()
        away_name_n = re.sub(r'\s*\(.*?\)', '', _normalize(away_team)).strip()

        home_sel = any(w in sel for w in home_name_n.split() if len(w) > 2)
        away_sel = any(w in sel for w in away_name_n.split() if len(w) > 2)
        if not home_sel and not away_sel:
            return math_prob

        # Proba de base : poids domicile/route adapté à l'avantage réel de chaque équipe
        # Ex: équipe +20% meilleure à domicile → on pèse davantage son record domicile
        home_delta   = (home['homeWinPct'] - home['winPct']) / 100
        away_delta   = (away['roadWinPct'] - away['winPct']) / 100
        home_local_w = max(0.45, min(0.75, _home_base_w + home_delta * 0.5))
        away_local_w = max(0.45, min(0.75, _home_base_w + away_delta * 0.5))
        home_base    = home['homeWinPct'] / 100 * home_local_w + home['winPct'] / 100 * (1 - home_local_w)
        away_base    = away['roadWinPct'] / 100 * away_local_w + away['winPct'] / 100 * (1 - away_local_w)

        # Ajustement forme récente (L10 vs moyenne saison)
        # OTL = défaite en prolongation : équipe compétitive → compte 0.4 victoire
        home_l10_otl  = max(0, home['l10GP'] - home['l10Wins'] - home['l10Losses'])
        away_l10_otl  = max(0, away['l10GP'] - away['l10Wins'] - away['l10Losses'])
        home_l10_rate = (home['l10Wins'] + 0.4 * home_l10_otl) / max(home['l10GP'], 1)
        away_l10_rate = (away['l10Wins'] + 0.4 * away_l10_otl) / max(away['l10GP'], 1)
        home_form_adj = (home_l10_rate - home['winPct'] / 100) * _l10_factor
        away_form_adj = (away_l10_rate - away['winPct'] / 100) * _l10_factor

        home_stat = max(0.05, min(0.95, home_base + home_form_adj))
        away_stat = max(0.05, min(0.95, away_base + away_form_adj))

        # Normaliser pour que home + away = 1
        total = home_stat + away_stat
        if total <= 0:
            return math_prob
        home_norm = home_stat / total
        away_norm = away_stat / total

        stat_prob = home_norm if home_sel else away_norm

        # ── Charger les poids appris de l'historique des prédictions ─────────
        try:
            from predictions import get_feature_weights
            _fw      = get_feature_weights()
            _stat_w  = _fw.get("stat_vs_math", _stat_default)
            _intra_w = _fw.get("intra_stat", {})
        except Exception:
            _stat_w  = 0.55
            _intra_w = {}

        # ── Affiner avec les stats avancées (PP%, Corsi/satPct, xGF%) ────────
        try:
            from advanced_stats import get_advanced_stats, team_strength_score
            home_adv = get_advanced_stats(home_team)
            away_adv = get_advanced_stats(away_team)
            h_score  = team_strength_score(home, home_adv, home=True,  weights=_intra_w)
            a_score  = team_strength_score(away, away_adv, home=False, weights=_intra_w)
            total_s  = h_score + a_score
            if total_s > 0:
                stat_prob = (h_score if home_sel else a_score) / total_s
        except Exception:
            pass   # conserve stat_prob de base si stats avancées indisponibles

        blended = _stat_w * stat_prob + (1.0 - _stat_w) * math_prob

        # ── Ajustement blessures ──────────────────────────────────────────────
        try:
            from injuries import get_injury_impact
            home_inj = get_injury_impact(home_team)
            away_inj = get_injury_impact(away_team)
            if home_sel:
                blended += home_inj["win_prob_delta"]
                blended -= away_inj["win_prob_delta"]   # blessures adversaires = avantage
            else:
                blended += away_inj["win_prob_delta"]
                blended -= home_inj["win_prob_delta"]
            blended = max(0.05, min(0.95, blended))
        except Exception:
            pass

        # ── Ajustement back-to-back (saison régulière seulement) ──────────────
        if _apply_b2b:
            try:
                from extra_stats import get_schedule_context
                home_ctx = get_schedule_context(home_team, match_date)
                away_ctx = get_schedule_context(away_team, match_date)
                if home_sel:
                    if home_ctx.get("is_back_to_back"):
                        blended -= 0.04
                    if away_ctx.get("is_back_to_back"):
                        blended += 0.03
                else:
                    if away_ctx.get("is_back_to_back"):
                        blended -= 0.04
                    if home_ctx.get("is_back_to_back"):
                        blended += 0.03
                blended = max(0.05, min(0.95, blended))
            except Exception:
                pass

        # ── Momentum de série (playoffs seulement) ───────────────────────────
        # Équipe menant 2-0 : +5% | menant 1-0 : +2.5% | égalité : 0
        # Équipe en retard 0-1 : -2.5% | en retard 0-2 : -5%
        if _is_playoff and (_series_home_w > 0 or _series_away_w > 0):
            try:
                _sel_wins = _series_home_w if home_sel else _series_away_w
                _opp_wins = _series_away_w if home_sel else _series_home_w
                _momentum = (_sel_wins - _opp_wins) * 0.025   # ±2.5% par victoire d'avance
                _momentum = max(-0.05, min(0.05, _momentum))  # cap ±5%
                blended   = max(0.05, min(0.95, blended + _momentum))
            except Exception:
                pass

        # ── Ajustement décalage horaire (saison régulière seulement) ──────────
        # En séries : les équipes jouent dans les mêmes villes, pas de jet lag
        if _apply_jetlag:
            try:
                from extra_stats import get_timezone_diff
                tz_diff = get_timezone_diff(away_team, home_team)
                if tz_diff <= -2:
                    penalty = 0.015 + 0.010 * max(0, -tz_diff - 2)
                    if home_sel:
                        blended += penalty
                    else:
                        blended -= penalty
                    blended = max(0.05, min(0.95, blended))
            except Exception:
                pass

        # ── Ajustement PDO (régression vers la moyenne) ───────────────────────
        # PDO = shooting% + save%. >102 = chanceux → régression à la baisse.
        try:
            from advanced_stats import get_advanced_stats as _gad_pdo
            _sel_s   = home if home_sel else away
            _sel_adv = _gad_pdo(home_team if home_sel else away_team)
            _sfPG = _sel_adv.get("sfPG")
            _saPG = _sel_adv.get("saPG")
            if _sel_s and _sfPG and _saPG and _sfPG > 0 and _saPG > 0:
                _shoot = _sel_s["gfPG"] / _sfPG
                _save  = 1 - _sel_s["gaPG"] / _saPG
                _pdo   = (_shoot + _save) * 100
                # Correction max ±4% : PDO 104 → -4%, PDO 96 → +4%
                _pdo_adj = max(-0.04, min(0.04, (100.0 - _pdo) / 100.0 * 0.20))
                blended  = max(0.05, min(0.95, blended + _pdo_adj))
        except Exception:
            pass

        # ── Ajustement discipline × jeu de puissance adverse ──────────────────
        # Si l'équipe sélectionnée prend beaucoup de pénalités ET que l'adversaire
        # a un fort PP, sa probabilité de victoire baisse (et inversement).
        try:
            from advanced_stats import get_advanced_stats as _gad_disc
            _sel_adv_d = _gad_disc(home_team if home_sel else away_team)
            _opp_adv_d = _gad_disc(away_team if home_sel else home_team)
            _pim_sel   = _sel_adv_d.get("pimPerGame") or 0
            _pp_opp    = (_opp_adv_d.get("ppPct") or 0) / 100
            _pim_opp   = _opp_adv_d.get("pimPerGame") or 0
            _pp_sel    = (_sel_adv_d.get("ppPct") or 0) / 100
            if _pim_sel > 0 and _pp_opp > 0 and _pim_opp > 0 and _pp_sel > 0:
                _pressure_sel = (_pim_sel / 2) * _pp_opp   # PP goals contre la sélection
                _pressure_opp = (_pim_opp / 2) * _pp_sel   # PP goals contre l'adversaire
                _disc_adj = (_pressure_opp - _pressure_sel) * 0.03
                blended   = max(0.05, min(0.95, blended + _disc_adj))
        except Exception:
            pass

        # ── Ajustement différentiel de buts récent (saison régulière seulement) ─
        # En séries : le jeu devient défensif, le diff de buts saison est du bruit
        if _apply_goaldiff:
            try:
                from extra_stats import get_recent_goals_stats as _grgs_ml
                _hr_ml = _grgs_ml(home_team, n=10, game_date=match_date)
                _ar_ml = _grgs_ml(away_team, n=10, game_date=match_date)
                _h_diff = _hr_ml.get("goal_diff_recent", 0)
                _a_diff = _ar_ml.get("goal_diff_recent", 0)
                _diff_h = max(-0.03, min(0.03, _h_diff * 0.015))
                _diff_a = max(-0.03, min(0.03, _a_diff * 0.015))
                if home_sel:
                    blended = max(0.05, min(0.95, blended + _diff_h - _diff_a))
                else:
                    blended = max(0.05, min(0.95, blended + _diff_a - _diff_h))
            except Exception:
                pass

        return round(_apply_bias(blended), 4)

    # ── Total de buts ─────────────────────────────────────────────────────────
    elif any(k in bt for k in ("total", "buts", "plus/moins", "over", "under")):
        if not home or not away:
            return math_prob

        # ── Forme récente + splits domicile/route : λ contextualisé ─────────
        # Priorité de blend (si tout dispo) :
        #   50% forme L10 + 30% split venue (home/road) + 20% saison globale
        # Si L10 manquant    : 60% split venue + 40% saison globale
        # Si split insuffisant (< 5 matchs) : fallback L10 60% + saison 40%
        _home_gf = home['gfPG']
        _home_ga = home['gaPG']
        _away_gf = away['gfPG']
        _away_ga = away['gaPG']

        # Récupérer les splits venue (le home team joue à domicile, l'away sur la route)
        _home_split_gf = home.get('homeGfPG')
        _home_split_ga = home.get('homeGaPG')
        _away_split_gf = away.get('roadGfPG')
        _away_split_ga = away.get('roadGaPG')
        _home_split_gp = home.get('homeGpRaw', 0) or 0
        _away_split_gp = away.get('roadGpRaw', 0) or 0
        # Minimum de matchs pour considérer le split fiable (début de saison)
        _MIN_SPLIT_GP = 5
        _home_split_ok = _home_split_gf is not None and _home_split_gp >= _MIN_SPLIT_GP
        _away_split_ok = _away_split_gf is not None and _away_split_gp >= _MIN_SPLIT_GP

        try:
            from extra_stats import get_recent_goals_stats
            _hr = get_recent_goals_stats(home_team, n=10, game_date=match_date)
            _ar = get_recent_goals_stats(away_team, n=10, game_date=match_date)

            # ── Équipe à domicile ──
            if _hr and _home_split_ok:
                _home_gf = round(0.50 * _hr["gfPG_recent"] + 0.30 * _home_split_gf + 0.20 * home['gfPG'], 3)
                _home_ga = round(0.50 * _hr["gaPG_recent"] + 0.30 * _home_split_ga + 0.20 * home['gaPG'], 3)
            elif _hr:
                _home_gf = round(0.60 * _hr["gfPG_recent"] + 0.40 * home['gfPG'], 3)
                _home_ga = round(0.60 * _hr["gaPG_recent"] + 0.40 * home['gaPG'], 3)
            elif _home_split_ok:
                _home_gf = round(0.60 * _home_split_gf + 0.40 * home['gfPG'], 3)
                _home_ga = round(0.60 * _home_split_ga + 0.40 * home['gaPG'], 3)

            # ── Équipe visiteuse ──
            if _ar and _away_split_ok:
                _away_gf = round(0.50 * _ar["gfPG_recent"] + 0.30 * _away_split_gf + 0.20 * away['gfPG'], 3)
                _away_ga = round(0.50 * _ar["gaPG_recent"] + 0.30 * _away_split_ga + 0.20 * away['gaPG'], 3)
            elif _ar:
                _away_gf = round(0.60 * _ar["gfPG_recent"] + 0.40 * away['gfPG'], 3)
                _away_ga = round(0.60 * _ar["gaPG_recent"] + 0.40 * away['gaPG'], 3)
            elif _away_split_ok:
                _away_gf = round(0.60 * _away_split_gf + 0.40 * away['gfPG'], 3)
                _away_ga = round(0.60 * _away_split_ga + 0.40 * away['gaPG'], 3)
        except Exception:
            # Fallback : au moins appliquer les splits si disponibles
            try:
                if _home_split_ok:
                    _home_gf = round(0.60 * _home_split_gf + 0.40 * home['gfPG'], 3)
                    _home_ga = round(0.60 * _home_split_ga + 0.40 * home['gaPG'], 3)
                if _away_split_ok:
                    _away_gf = round(0.60 * _away_split_gf + 0.40 * away['gfPG'], 3)
                    _away_ga = round(0.60 * _away_split_ga + 0.40 * away['gaPG'], 3)
            except Exception:
                pass

        # λ enrichi : intégrer les shots/game des stats avancées si disponibles
        try:
            from advanced_stats import get_advanced_stats
            home_adv = get_advanced_stats(home_team)
            away_adv = get_advanced_stats(away_team)
            # Blender gfPG avec shots × save% adversaire pour λ plus précis
            sfPG_home = home_adv.get("sfPG")
            sfPG_away = away_adv.get("sfPG")
            saPG_home = home_adv.get("saPG")
            saPG_away = away_adv.get("saPG")
            if sfPG_home and sfPG_away and saPG_home and saPG_away:
                # save% implicite calculé avec gaPG blendé (forme récente)
                sv_home = max(0.80, min(0.96, 1 - _home_ga / saPG_home))
                sv_away = max(0.80, min(0.96, 1 - _away_ga / saPG_away))
                # λ basé sur les tirs plutôt que les buts marqués
                lambda_home = sfPG_home * (1 - sv_away)
                lambda_away = sfPG_away * (1 - sv_home)
            else:
                raise ValueError("stats incomplètes")
        except Exception:
            # Fallback sur buts blendés (forme récente + saison)
            lambda_home = (_home_gf + _away_ga) / 2
            lambda_away = (_away_gf + _home_ga) / 2
        # ── Ajustement gardiens ────────────────────────────────────────────────
        try:
            from extra_stats import get_starting_goalie
            hg   = get_starting_goalie(home_team)
            ag_g = get_starting_goalie(away_team)
            NHL_AVG_SV = 91.5
            try:
                from predictions import get_feature_weights
                _gf = get_feature_weights().get("goalie_lambda_factor", 1.0)
            except Exception:
                _gf = 1.0
            def _gadj(sv_pct, factor):
                raw = NHL_AVG_SV / max(sv_pct, 80)
                return 1.0 + (raw - 1.0) * factor
            # Appliquer indépendamment — pas besoin que les deux soient trouvés
            # Poids plus fort si gardien confirmé (Daily Faceoff) vs estimé
            if ag_g:
                sv_ag  = ag_g.get("svPctRecent") or ag_g.get("svPct") or NHL_AVG_SV
                # Gardien confirmé → facteur plein ; non confirmé → 60% du facteur
                _conf_factor = _gf if ag_g.get("confirmed") else _gf * 0.6
                lambda_home = max(0.5, lambda_home * _gadj(sv_ag, _conf_factor))
            if hg:
                sv_hg  = hg.get("svPctRecent") or hg.get("svPct") or NHL_AVG_SV
                _conf_factor = _gf if hg.get("confirmed") else _gf * 0.6
                lambda_away = max(0.5, lambda_away * _gadj(sv_hg, _conf_factor))
        except Exception:
            pass

        # ── Ajustement blessures sur λ ────────────────────────────────────────
        try:
            from injuries import get_injury_impact
            home_inj = get_injury_impact(home_team)
            away_inj = get_injury_impact(away_team)
            lambda_home = max(0.5, lambda_home + home_inj["lambda_for_delta"] - away_inj["lambda_against_delta"])
            lambda_away = max(0.5, lambda_away + away_inj["lambda_for_delta"] - home_inj["lambda_against_delta"])
        except Exception:
            pass

        # ── Ajustement PDO sur λ (shooting/save% chanceux → régression) ──────
        try:
            from advanced_stats import get_advanced_stats as _gad_pdo_t
            for _tn, _ts, _is_home in [(home_team, home, True), (away_team, away, False)]:
                _adv_t = _gad_pdo_t(_tn)
                _sfPG_t = _adv_t.get("sfPG")
                _saPG_t = _adv_t.get("saPG")
                if _ts and _sfPG_t and _saPG_t and _sfPG_t > 0 and _saPG_t > 0:
                    _shoot_t = _ts["gfPG"] / _sfPG_t
                    _save_t  = 1 - _ts["gaPG"] / _saPG_t
                    _pdo_t   = (_shoot_t + _save_t) * 100
                    _pdo_mult = max(0.93, min(1.07, 1.0 + (100.0 - _pdo_t) / 1000.0))
                    if _is_home:
                        lambda_home = max(0.5, lambda_home * _pdo_mult)
                    else:
                        lambda_away = max(0.5, lambda_away * _pdo_mult)
        except Exception:
            pass

        # ── Ajustement discipline × PP adverse sur λ ──────────────────────────
        try:
            from advanced_stats import get_advanced_stats as _gad_disc_t
            _hadv_t = _gad_disc_t(home_team)
            _aadv_t = _gad_disc_t(away_team)
            _pim_h = (_hadv_t.get("pimPerGame") or 0) / 2   # minutes → opportunités (~2min/pén)
            _pim_a = (_aadv_t.get("pimPerGame") or 0) / 2
            _pp_h  = (_hadv_t.get("ppPct") or 0) / 100
            _pp_a  = (_aadv_t.get("ppPct") or 0) / 100
            _NHL_AVG = 1.75 * 0.20   # 1.75 pén/match × 20% PP = 0.35 PP goals/match
            if _pim_h > 0 and _pp_a > 0:
                lambda_away = max(0.5, lambda_away + (_pim_h * _pp_a - _NHL_AVG) * 0.25)
            if _pim_a > 0 and _pp_h > 0:
                lambda_home = max(0.5, lambda_home + (_pim_a * _pp_h - _NHL_AVG) * 0.25)
        except Exception:
            pass

        # ── Ajustement back-to-back sur λ (saison régulière seulement) ──────────
        if _apply_b2b:
            try:
                from extra_stats import get_schedule_context
                home_ctx = get_schedule_context(home_team, match_date)
                away_ctx = get_schedule_context(away_team, match_date)
                if home_ctx.get("is_back_to_back"):
                    lambda_home = max(0.5, lambda_home * 0.92)
                if away_ctx.get("is_back_to_back"):
                    lambda_away = max(0.5, lambda_away * 0.92)
            except Exception:
                pass

        # ── Ajustement playoff : jeu défensif → ~7% moins de buts ───────────────
        lambda_home = lambda_home * _scoring_k
        lambda_away = lambda_away * _scoring_k

        lambda_total = lambda_home + lambda_away

        m = re.search(r'(\d+[.,]\d+|\d+)', sel)
        if not m:
            return math_prob
        line = float(m.group(1).replace(',', '.'))

        # P(total >= ceil(line+1)) = P(over) via Poisson
        k_min = int(line) + 1
        prob_under = sum(
            _math.exp(-lambda_total) * (lambda_total ** k) / _math.factorial(k)
            for k in range(k_min)
        )
        stat_over = max(0.05, min(0.95, 1.0 - prob_under))
        stat_prob = stat_over if ("plus" in sel or "over" in sel) else (1.0 - stat_over)

        # ── Calibration par seuil ─────────────────────────────────────────────
        # Pour les seuils élevés (≥6.0), le modèle Poisson est moins fiable
        # (λ incertain, événement rare) → on donne plus de poids au marché
        if line >= 6.0:
            blend_stat = 0.40   # 40% stats / 60% marché
        elif line >= 5.0:
            blend_stat = 0.48   # 48% stats / 52% marché
        else:
            blend_stat = 0.55   # défaut : 55% stats / 45% marché

        blended = blend_stat * stat_prob + (1.0 - blend_stat) * math_prob
        return round(_apply_bias(blended), 4)

    # ── Les 2 équipes marquent (par période) ──────────────────────────────────
    elif any(k in bt for k in ("2 équipes", "les 2", "both")):
        if not home or not away:
            return math_prob
        import re as _re_btts
        pm = _re_btts.search(r'(\d)[eè]r?e?\s*p[eé]riode', bt)
        if pm:
            try:
                from extra_stats import get_period_scoring_probs
                period  = int(pm.group(1))
                pp      = get_period_scoring_probs(
                    home["gfPG"], away["gfPG"], home["gaPG"], away["gaPG"]
                )
                stat_p = pp.get(f"btts_p{period}", math_prob)
                if "oui" in sel or "marquent" in sel:
                    blended = 0.55 * stat_p + 0.45 * math_prob
                else:
                    blended = 0.55 * (1 - stat_p) + 0.45 * math_prob
                return round(_apply_bias(blended), 4)
            except Exception:
                pass
        return math_prob

    return math_prob


def _apply_bias(prob: float) -> float:
    """
    Applique le facteur de correction issu de l'historique des prédictions.
    N'est actif que si on a accumulé suffisamment de résultats.
    """
    try:
        from predictions import compute_calibration
        cal = compute_calibration()
        if cal.get("correction_active"):
            bias = cal["bias_factor"]
            return max(0.05, min(0.95, prob * bias))
    except Exception:
        pass
    return max(0.05, min(0.95, prob))


def build_reason(
    home_team: str,
    away_team: str,
    bet_type: str,
    selection: str,
    match_date: str | None = None,
) -> str:
    """
    Génère une explication riche basée sur les stats réelles NHL.
    Retourne une liste de faits séparés par ' · '.
    """
    home = get_team_stats(home_team)
    away = get_team_stats(away_team)

    if not home and not away:
        return ""

    bt  = bet_type.lower()
    sel = selection.lower()
    parts = []

    # ── Props joueur (ex: "Matvei Michkov Total de points 0.5") ──────────────
    import re as _re_pp
    _player_prop_keywords = ("total de points", "passes", "rebonds", "aides",
                             "buts marqués", "tirs", "mises en échec")
    if ('(' not in bet_type
            and _re_pp.match(r'^[A-ZÀ-Ü][a-zà-ü\'-]+ [A-ZÀ-Ü]', bet_type)
            and any(k in bt for k in _player_prop_keywords)):
        # Extraire le nom du joueur (2 premiers mots)
        tokens = bet_type.split()
        player_name = f"{tokens[0]} {tokens[1]}" if len(tokens) >= 2 else tokens[0]
        if home and away:
            combined = round(home["gfPG"] + away["gfPG"], 1)
            parts.append(f"Match à rythme {combined} buts attendus ({home['name']} {home['gfPG']} + {away['name']} {away['gfPG']})")
            parts.append(f"Défenses : {home['name']} concède {home['gaPG']}/match · {away['name']} concède {away['gaPG']}/match")
            if "total de points" in bt or "aides" in bt:
                # Plus de contexte scoring pour les props sur points/passes
                try:
                    from advanced_stats import get_advanced_stats
                    ha = get_advanced_stats(home_team)
                    aa = get_advanced_stats(away_team)
                    if ha.get("sfPG") and aa.get("sfPG"):
                        parts.append(f"Tirs : {home['name']} {ha['sfPG']:.1f}/match · {away['name']} {aa['sfPG']:.1f}/match")
                except Exception:
                    pass
        elif home:
            parts.append(f"Match : {home['name']} {home['gfPG']} buts/match · concède {home['gaPG']}/match")
        elif away:
            parts.append(f"Match : {away['name']} {away['gfPG']} buts/match · concède {away['gaPG']}/match")
        if "moins" in sel or "under" in sel:
            if home and away:
                avg_ga = round((home["gaPG"] + away["gaPG"]) / 2, 2)
                if avg_ga <= 2.8:
                    parts.append("Contexte défensif favorable à l'Under")
        elif "plus" in sel or "over" in sel:
            if home and away and round(home["gfPG"] + away["gfPG"], 1) >= 6.0:
                parts.append("Rencontre à tendance offensive")
        return " · ".join(parts)

    # ── Paris sur le gagnant ─────────────────────────────────────────────────
    if any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        # Déterminer quelle équipe est sélectionnée
        home_name_n = _normalize(home_team) if home else ""
        away_name_n = _normalize(away_team) if away else ""
        # Enlever parenthèses pour comparaison
        import re
        home_name_n = re.sub(r'\s*\(.*?\)', '', home_name_n).strip()
        away_name_n = re.sub(r'\s*\(.*?\)', '', away_name_n).strip()

        home_sel = home and any(w in sel for w in home_name_n.split())
        away_sel = away and any(w in sel for w in away_name_n.split())

        # Stats avancées (PP%, Corsi/xGF) pour les deux équipes
        try:
            from advanced_stats import get_advanced_stats
            home_adv = get_advanced_stats(home_team)
            away_adv = get_advanced_stats(away_team)
        except Exception:
            home_adv = away_adv = {}

        if home_sel and home:
            parts.append(f"{home['name']} : {home['homeWinPct']}% victoires à domicile ({home['homeWins']}/{home['homeGP']})")
            parts.append(f"Saison : {home['wins']}V-{home['losses']}D ({home['winPct']}%)")
            parts.append(f"10 derniers : {home['l10Wins']}V-{home['l10Losses']}D")
            streak = _streak_text(home)
            if streak:
                parts.append(streak)
            # Stats avancées domicile
            if home_adv.get("ppPct"):
                pp = home_adv["ppPct"]
                pk = home_adv.get("pkPct", 0)
                parts.append(f"PP {pp:.1f}% · PK {pk:.1f}%" if pk else f"PP {pp:.1f}%")
            if home_adv.get("xgfPct"):
                parts.append(f"xGF% {home_adv['xgfPct']:.1f}% (qualité des tirs)")
            elif home_adv.get("satPct"):
                parts.append(f"Possession (Corsi) {home_adv['satPct']:.1f}%")
            if away:
                parts.append(f"Adversaire {away['name']} : {away['roadWinPct']}% sur la route")
                if away_adv.get("ppPct"):
                    parts.append(f"PP adverse {away_adv['ppPct']:.1f}%")

        elif away_sel and away:
            parts.append(f"{away['name']} : {away['roadWinPct']}% victoires sur la route ({away['roadWins']}/{away['roadGP']})")
            parts.append(f"Saison : {away['wins']}V-{away['losses']}D ({away['winPct']}%)")
            parts.append(f"10 derniers : {away['l10Wins']}V-{away['l10Losses']}D")
            streak = _streak_text(away)
            if streak:
                parts.append(streak)
            # Stats avancées visiteur
            if away_adv.get("ppPct"):
                pp = away_adv["ppPct"]
                pk = away_adv.get("pkPct", 0)
                parts.append(f"PP {pp:.1f}% · PK {pk:.1f}%" if pk else f"PP {pp:.1f}%")
            if away_adv.get("xgfPct"):
                parts.append(f"xGF% {away_adv['xgfPct']:.1f}% (qualité des tirs)")
            elif away_adv.get("satPct"):
                parts.append(f"Possession (Corsi) {away_adv['satPct']:.1f}%")
            if home:
                parts.append(f"Adversaire {home['name']} : {home['homeWinPct']}% à domicile")
                if home_adv.get("ppPct"):
                    parts.append(f"PP adverse {home_adv['ppPct']:.1f}%")

        else:
            # Sélection non identifiée → comparer les deux
            if home and away:
                parts.append(f"{home['name']} : {home['winPct']}% saison, {home['homeWinPct']}% domicile")
                parts.append(f"{away['name']} : {away['winPct']}% saison, {away['roadWinPct']}% route")
                if home["l10Wins"] != away["l10Wins"]:
                    better = home if home["l10Wins"] > away["l10Wins"] else away
                    parts.append(f"{better['name']} en meilleure forme ({better['l10Wins']}/10)")

        # ── PDO · Discipline ─────────────────────────────────────────────────
        try:
            from advanced_stats import get_advanced_stats as _gad_br
            _sel_s_br   = home if home_sel else away
            _sel_adv_br = _gad_br(home_team if home_sel else away_team)
            _opp_adv_br = _gad_br(away_team if home_sel else home_team)
            # PDO
            _sfPG_br = _sel_adv_br.get("sfPG")
            _saPG_br = _sel_adv_br.get("saPG")
            if _sel_s_br and _sfPG_br and _saPG_br and _sfPG_br > 0 and _saPG_br > 0:
                _shoot_br = _sel_s_br["gfPG"] / _sfPG_br
                _save_br  = 1 - _sel_s_br["gaPG"] / _saPG_br
                _pdo_br   = round((_shoot_br + _save_br) * 100, 1)
                if _pdo_br > 102:
                    parts.append(f"PDO {_pdo_br} (chanceux — régression attendue)")
                elif _pdo_br < 98:
                    parts.append(f"PDO {_pdo_br} (malchanceux — amélioration attendue)")
            # Discipline vs PP adverse
            _pim_br  = _sel_adv_br.get("pimPerGame")
            _pp_opp_br = _opp_adv_br.get("ppPct")
            if _pim_br and _pp_opp_br:
                _pp_goals_br = round((_pim_br / 2) * (_pp_opp_br / 100), 2)
                _NHL_AVG_BR  = 0.35
                if _pim_br > 4.5:
                    parts.append(f"Discipline fragile : {_pim_br:.1f} pén·min/match · PP adverse {_pp_opp_br:.1f}%")
                elif _pim_br < 2.5:
                    parts.append(f"Bonne discipline : {_pim_br:.1f} pén·min/match")
        except Exception:
            pass

        # ── Gardiens · Back-to-back · Face-à-face ────────────────────────────
        try:
            from extra_stats import get_starting_goalie, get_schedule_context, get_h2h_stats
            hg   = get_starting_goalie(home_team)
            ag_g = get_starting_goalie(away_team)
            if hg:
                hn    = home["name"] if home else home_team
                conf  = " ✓" if hg.get("confirmed") else f" ({hg.get('status','?')})"
                sv_r  = hg.get("svPctRecent")
                sv_s  = hg.get("svPct")
                sv_str = (f"{sv_r}% SV L10 (saison {sv_s}%)"
                          if sv_r and sv_r != sv_s else f"{sv_s}% SV")
                gaa_r = hg.get("gaaRecent") or hg.get("gaa")
                parts.append(f"Gardien {hn} : {hg['name']}{conf} · {sv_str} · {gaa_r} MPM")
            if ag_g:
                an    = away["name"] if away else away_team
                conf  = " ✓" if ag_g.get("confirmed") else f" ({ag_g.get('status','?')})"
                sv_r  = ag_g.get("svPctRecent")
                sv_s  = ag_g.get("svPct")
                sv_str = (f"{sv_r}% SV L10 (saison {sv_s}%)"
                          if sv_r and sv_r != sv_s else f"{sv_s}% SV")
                gaa_r = ag_g.get("gaaRecent") or ag_g.get("gaa")
                parts.append(f"Gardien {an} : {ag_g['name']}{conf} · {sv_str} · {gaa_r} MPM")
            home_ctx = get_schedule_context(home_team, match_date)
            away_ctx = get_schedule_context(away_team, match_date)
            if home_ctx.get("is_back_to_back"):
                hn = home["name"] if home else home_team
                parts.append(f"⚠ {hn} joue en back-to-back ({home_ctx['days_rest']}j de repos)")
            if away_ctx.get("is_back_to_back"):
                an = away["name"] if away else away_team
                parts.append(f"⚠ {an} joue en back-to-back ({away_ctx['days_rest']}j de repos)")
            # Décalage horaire
            try:
                from extra_stats import get_timezone_diff
                tz_diff = get_timezone_diff(away_team, home_team)
                if tz_diff <= -2:
                    an = away["name"] if away else away_team
                    parts.append(f"✈ {an} : voyage de {-tz_diff}h vers l'est (jet lag −{1.5 + max(0, -tz_diff-2):.1f}%)")
            except Exception:
                pass
            h2h = get_h2h_stats(home_team, away_team, match_date)
            if h2h.get("games", 0) >= 2:
                ha = _match_abbrev(home_team) or home_team
                aa = _match_abbrev(away_team) or away_team
                parts.append(
                    f"Face-à-face ({h2h['games']}j) : {ha} {h2h['home_wins']}V–{aa} {h2h['away_wins']}V"
                    f" · {h2h['avg_total_goals']} buts/match moy."
                )
        except Exception:
            pass

    # ── Paris total de buts ───────────────────────────────────────────────────
    elif any(k in bt for k in ("total", "buts", "plus/moins", "over", "under")):
        if home and away:
            combined = round(home["gfPG"] + away["gfPG"], 1)
            parts.append(f"Moy. buts/match : {home['name']} {home['gfPG']}m + {away['name']} {away['gfPG']}m = {combined}")
            parts.append(f"Défenses : {home['name']} concède {home['gaPG']}/match, {away['name']} concède {away['gaPG']}/match")
            # Tirs par match si disponibles
            try:
                from advanced_stats import get_advanced_stats
                ha = get_advanced_stats(home_team)
                aa = get_advanced_stats(away_team)
                if ha.get("sfPG") and aa.get("sfPG"):
                    parts.append(f"Tirs : {home['name']} {ha['sfPG']:.1f}/match, {away['name']} {aa['sfPG']:.1f}/match")
            except Exception:
                pass
            if "plus" in sel or "over" in sel:
                if combined >= 6.5:
                    parts.append("Tendance offensives élevée cette saison")
                elif combined >= 5.5:
                    parts.append("Rythme de buts modéré-élevé")
            elif "moins" in sel or "under" in sel:
                avg_ga = round((home["gaPG"] + away["gaPG"]) / 2, 2)
                if avg_ga <= 2.8:
                    parts.append("Deux bonnes défenses cette saison")
                else:
                    parts.append("Défenses moyennes — match serré possible")
            # Gardiens
            try:
                from extra_stats import get_starting_goalie
                hg   = get_starting_goalie(home_team)
                ag_g = get_starting_goalie(away_team)
                if hg:
                    conf = " ✓" if hg.get("confirmed") else f" ({hg.get('status','?')})"
                    parts.append(f"Gardien {home['name']} : {hg['name']}{conf} · {hg['svPct']}% SV · {hg['gaa']} MPM")
                if ag_g:
                    conf = " ✓" if ag_g.get("confirmed") else f" ({ag_g.get('status','?')})"
                    parts.append(f"Gardien {away['name']} : {ag_g['name']}{conf} · {ag_g['svPct']}% SV · {ag_g['gaa']} MPM")
            except Exception:
                pass

    # ── Les 2 équipes marquent ────────────────────────────────────────────────
    elif any(k in bt for k in ("2 équipes", "les 2", "both")):
        if home and away:
            parts.append(f"{home['name']} marque {home['gfPG']} buts/match en moyenne")
            parts.append(f"{away['name']} marque {away['gfPG']} buts/match en moyenne")
            if "oui" in sel or "marquent" in sel:
                if home["gfPG"] >= 2.8 and away["gfPG"] >= 2.8:
                    parts.append("Les deux attaques sont actives")
            elif "non" in sel or "ne marque" in sel:
                if home["gaPG"] <= 2.6 or away["gaPG"] <= 2.6:
                    parts.append("Au moins une solide défense")
            # Probabilités par période (Poisson)
            try:
                import re as _re_p
                pm = _re_p.search(r'(\d)[eè]r?e?\s*p[eé]riode', bt)
                if pm:
                    from extra_stats import get_period_scoring_probs
                    period = int(pm.group(1))
                    pp     = get_period_scoring_probs(
                        home["gfPG"], away["gfPG"], home["gaPG"], away["gaPG"]
                    )
                    hp     = pp.get(f"home_score_p{period}", 0)
                    ap     = pp.get(f"away_score_p{period}", 0)
                    btts_p = pp.get(f"btts_p{period}", 0)
                    parts.append(
                        f"Période {period} (Poisson) — P(marquer) : "
                        f"{home['name']} {hp*100:.0f}% · {away['name']} {ap*100:.0f}% "
                        f"→ P(les 2 marquent) {btts_p*100:.0f}%"
                    )
            except Exception:
                pass

    return " · ".join(parts)
