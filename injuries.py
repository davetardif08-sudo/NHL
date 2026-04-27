"""
Blessures et alignements NHL depuis DailyFaceoff.com.

Logique :
  - Scrape pageProps.combinations depuis la page Next.js (SSG, pas de JS requis)
  - Identifie les joueurs blessés avec injuryStatus != null
  - Ignore les blessures de longue date (> ~10 matchs manqués ≈ 2 semaines)
  - Classe les joueurs par importance selon PPG et position
  - Retourne un impact sur win_prob et λ_goals
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CACHE_FILE = Path(__file__).parent / ".injuries_cache.json"
CACHE_TTL  = 4 * 3600   # 4 heures (les alignements changent chaque jour)

# Cache mémoire — évite de relire le fichier JSON à chaque appel
_mem_cache: dict | None = None

# Seuil de matchs manqués ≈ 2 semaines (NHL joue ~3-4 matchs/semaine)
MAX_GAMES_MISSED = 10

# Impact sur win_prob et λ selon le tier du joueur
_IMPACT = {
    "goalie_starter":  {"win_prob": -0.08, "lambda_against": +0.35},
    "forward_tier1":   {"win_prob": -0.05, "lambda_for":     -0.25},  # top-3 (PPG > 0.60)
    "forward_tier2":   {"win_prob": -0.02, "lambda_for":     -0.12},  # top-4-6 (PPG > 0.35)
    "defense_tier1":   {"win_prob": -0.03, "lambda_against": +0.15},  # top-2 D
}

# Slug DailyFaceoff par abréviation NHL
_TEAM_SLUGS: dict[str, str] = {
    "MTL": "montreal-canadiens",
    "TOR": "toronto-maple-leafs",
    "BOS": "boston-bruins",
    "NYR": "new-york-rangers",
    "NYI": "new-york-islanders",
    "PHI": "philadelphia-flyers",
    "PIT": "pittsburgh-penguins",
    "WSH": "washington-capitals",
    "CAR": "carolina-hurricanes",
    "NJD": "new-jersey-devils",
    "CBJ": "columbus-blue-jackets",
    "TBL": "tampa-bay-lightning",
    "FLA": "florida-panthers",
    "DET": "detroit-red-wings",
    "OTT": "ottawa-senators",
    "BUF": "buffalo-sabres",
    "CHI": "chicago-blackhawks",
    "NSH": "nashville-predators",
    "STL": "st-louis-blues",
    "WPG": "winnipeg-jets",
    "MIN": "minnesota-wild",
    "COL": "colorado-avalanche",
    "DAL": "dallas-stars",
    "CGY": "calgary-flames",
    "EDM": "edmonton-oilers",
    "VAN": "vancouver-canucks",
    "SEA": "seattle-kraken",
    "VGK": "vegas-golden-knights",
    "ANA": "anaheim-ducks",
    "LAK": "los-angeles-kings",
    "SJS": "san-jose-sharks",
    "UTA": "utah-hockey-club",
}


# ─── Cache ────────────────────────────────────────────────────────────────────

def _load_cache() -> dict:
    global _mem_cache
    if _mem_cache is not None:
        return _mem_cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                _mem_cache = json.load(f)
                return _mem_cache
        except Exception:
            pass
    _mem_cache = {}
    return _mem_cache


def _save_cache(data: dict) -> None:
    global _mem_cache
    _mem_cache = data
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ─── Scraping DailyFaceoff ────────────────────────────────────────────────────

def _fetch_combinations(abbrev: str) -> dict:
    """Fetch pageProps.combinations from DailyFaceoff for one team."""
    slug = _TEAM_SLUGS.get(abbrev)
    if not slug:
        return {}
    url = f"https://www.dailyfaceoff.com/teams/{slug}/line-combinations/"
    try:
        import requests
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=12,
        )
        resp.raise_for_status()
        m = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            resp.text,
            re.S,
        )
        if not m:
            return {}
        data = json.loads(m.group(1))
        return (data.get("props", {})
                    .get("pageProps", {})
                    .get("combinations", {}))
    except Exception:
        return {}


# ─── Analyse des blessures ────────────────────────────────────────────────────

def _player_tier(player: dict, team_players: list[dict]) -> str | None:
    """
    Détermine le tier d'un joueur (goalie_starter, forward_tier1/2, defense_tier1).
    Retourne None si joueur peu important.
    """
    pos  = (player.get("positionIdentifier") or "").upper()
    szn  = player.get("season") or {}
    gp   = szn.get("gamesPlayed") or 1

    # Joueurs blessés : positionIdentifier = "IR1", "IR5" etc.
    # Inférer la position depuis les stats de saison.
    if pos.startswith("IR") or pos == "":
        pts    = szn.get("points") or 0
        goals  = szn.get("goals")  or 0
        blocks = szn.get("blocks") or 0
        ppg    = pts / gp if gp > 0 else 0
        # Gardien : aucun point ni but ni block (gardiens n'ont pas ces stats ici)
        if pts == 0 and goals == 0 and blocks == 0 and gp <= 40:
            pos = "G"
        elif ppg > 0.10 or goals >= blocks:
            pos = "F"   # Attaquant
        else:
            pos = "D"   # Défenseur

    # ── Gardien ───────────────────────────────────────────────────────────────
    if pos == "G":
        # Gardien partant = plus de matchs joués parmi les gardiens
        goalies = [p for p in team_players
                   if (p.get("positionIdentifier") or "").upper() == "G"
                   and not p.get("injuryStatus")]
        if not goalies:
            return "goalie_starter"
        max_gp = max((p.get("season") or {}).get("gamesPlayed", 0) for p in goalies)
        if gp >= max_gp * 0.60:   # joue au moins 60% des matchs des gardiens sains
            return "goalie_starter"
        return None

    # ── Attaquants ────────────────────────────────────────────────────────────
    if pos in ("C", "LW", "RW", "F"):
        pts   = szn.get("points") or 0
        ppg   = pts / gp if gp > 0 else 0
        if ppg > 0.60:
            return "forward_tier1"
        if ppg > 0.35:
            return "forward_tier2"
        return None   # bottom-6 : impact négligeable

    # ── Défenseurs ────────────────────────────────────────────────────────────
    if pos == "D":
        pts = szn.get("points") or 0
        ppg = pts / gp if gp > 0 else 0
        if ppg > 0.35:            # top-2 D offensif
            return "defense_tier1"
        # Top-2 par TOI approximé par classement dans l'équipe
        d_players = sorted(
            [p for p in team_players
             if (p.get("positionIdentifier") or "").upper() == "D"],
            key=lambda p: (p.get("season") or {}).get("points", 0),
            reverse=True,
        )
        if d_players and player.get("playerId") == d_players[0].get("playerId"):
            return "defense_tier1"

    return None


def _is_recent(player: dict, team_gp: int) -> bool:
    """
    Vrai si la blessure est récente (≤ MAX_GAMES_MISSED matchs manqués).
    DTD = toujours récent.
    """
    status = (player.get("injuryStatus") or "").lower()
    if status == "dtd":
        return True
    gp_player = (player.get("season") or {}).get("gamesPlayed") or 0
    games_missed = team_gp - gp_player
    return 0 < games_missed <= MAX_GAMES_MISSED


def get_team_injuries(team_name: str) -> list[dict]:
    """
    Retourne la liste des blessures récentes significatives pour une équipe.

    Chaque entrée : {name, status, tier, impact}
    """
    from nhl_stats import _match_abbrev
    abbrev = _match_abbrev(team_name)
    if not abbrev:
        return []

    cache = _load_cache()
    ts    = cache.get("_ts", {})
    teams = cache.get("teams", {})

    # Rafraîchir si périmé
    if abbrev not in teams or time.time() - ts.get(abbrev, 0) > CACHE_TTL:
        combos = _fetch_combinations(abbrev)
        if combos:
            teams[abbrev] = combos
            ts[abbrev]    = time.time()
            _save_cache({"_ts": ts, "teams": teams})

    combos = teams.get(abbrev, {})
    if not combos:
        return []

    players  = combos.get("players") or []
    team_gp  = max(
        ((p.get("season") or {}).get("gamesPlayed") or 0) for p in players
    ) if players else 82

    result = []
    for p in players:
        status = (p.get("injuryStatus") or "").lower()
        if not status:
            continue
        group = (p.get("groupIdentifier") or "").lower()
        if status == "out" and group != "ir":   # "out" sans slot IR = prêt LNH/AHL
            continue
        if not _is_recent(p, team_gp):
            continue
        tier = _player_tier(p, players)
        if not tier:
            continue

        name   = f"{(p.get('firstName') or {}).get('default', '')} {(p.get('lastName') or {}).get('default', '')}".strip()
        # Si firstName/lastName sont des strings (pas des dicts)
        if not name:
            fn = p.get("firstName") or ""
            ln = p.get("lastName")  or ""
            name = f"{fn if isinstance(fn, str) else ''} {ln if isinstance(ln, str) else ''}".strip()
        if not name:
            name = p.get("name") or "Joueur"

        result.append({
            "name":   name,
            "status": status.upper(),
            "tier":   tier,
            "impact": _IMPACT.get(tier, {}),
        })

    return result


# ─── Impact agrégé ────────────────────────────────────────────────────────────

def get_injury_impact(team_name: str) -> dict:
    """
    Retourne l'impact agrégé des blessures récentes d'une équipe :
      win_prob_delta   : ajustement négatif sur la probabilité de victoire
      lambda_for_delta : ajustement sur les buts marqués
      lambda_against_delta : ajustement sur les buts encaissés
      notes : liste de textes pour build_reason()
    """
    injuries = get_team_injuries(team_name)
    if not injuries:
        return {"win_prob_delta": 0.0, "lambda_for_delta": 0.0,
                "lambda_against_delta": 0.0, "notes": []}

    wp_delta  = 0.0
    lf_delta  = 0.0
    la_delta  = 0.0
    notes     = []

    for inj in injuries:
        imp = inj["impact"]
        wp_delta += imp.get("win_prob", 0.0)
        lf_delta += imp.get("lambda_for", 0.0)
        la_delta += imp.get("lambda_against", 0.0)
        tier_label = {
            "goalie_starter": "gardien partant",
            "forward_tier1":  "attaquant top-3",
            "forward_tier2":  "attaquant top-6",
            "defense_tier1":  "défenseur #1",
        }.get(inj["tier"], "")
        notes.append(f"⚠ {inj['name']} absent ({inj['status']}, {tier_label})")

    # Plafonner pour éviter des ajustements extrêmes
    wp_delta = max(-0.20, wp_delta)
    lf_delta = max(-0.60, lf_delta)
    la_delta = min(+0.50, la_delta)

    return {
        "win_prob_delta":       round(wp_delta, 4),
        "lambda_for_delta":     round(lf_delta, 4),
        "lambda_against_delta": round(la_delta, 4),
        "notes":                notes,
    }


def get_hot_players(team_name: str, top_n: int = 3) -> list[dict]:
    """
    Retourne les joueurs en feu (forme récente élevée) pour une équipe.

    Critères :
      - Seulement attaquants (C, LW, RW) ayant joué au moins 3 des 5 derniers matchs
      - last5 PPG >= 1.0  (très chaud)
      - OU last5 PPG >= 0.6 ET >= 1.5× leur moyenne saisonnière
    Retourne au max top_n joueurs triés par PPG last5 décroissant.
    """
    from nhl_stats import _match_abbrev
    abbrev = _match_abbrev(team_name)
    if not abbrev:
        return []

    # Utiliser le cache existant (même source que les blessures)
    cache = _load_cache()
    combos = cache.get("teams", {}).get(abbrev, {})
    if not combos:
        combos = _fetch_combinations(abbrev)
        if combos:
            teams = cache.get("teams", {})
            teams[abbrev] = combos
            ts = cache.get("_ts", {})
            ts[abbrev] = time.time()
            _save_cache({"_ts": ts, "teams": teams})
    if not combos:
        return []

    players = combos.get("players") or []
    result = []

    for p in players:
        if p.get("injuryStatus"):          # blessé → pas "en feu"
            continue
        pos = (p.get("positionIdentifier") or "").lower()
        if pos not in ("c", "lw", "rw", "f"):
            continue                        # seulement attaquants

        l5  = p.get("last5")  or {}
        szn = p.get("season") or {}

        gp_l5  = l5.get("gamesPlayed") or 0
        pts_l5 = l5.get("points")      or 0
        if gp_l5 < 3:
            continue                        # pas assez de données récentes

        ppg_l5  = pts_l5 / gp_l5
        gp_szn  = szn.get("gamesPlayed") or 1
        ppg_szn = (szn.get("points") or 0) / gp_szn

        hot = (
            ppg_l5 >= 1.0                              # très chaud
            or (ppg_l5 >= 0.6 and ppg_szn > 0 and ppg_l5 >= ppg_szn * 1.5)
        )
        if not hot:
            continue

        name = p.get("name") or ""
        if not name:
            continue

        result.append({
            "name":     name,
            "pts_l5":   pts_l5,
            "gp_l5":    gp_l5,
            "goals_l5": l5.get("goals")   or 0,
            "ppg_l5":   round(ppg_l5, 2),
        })

    result.sort(key=lambda x: x["ppg_l5"], reverse=True)
    return result[:top_n]


def prefetch_injuries(team_names: list[str], max_workers: int = 6) -> None:
    """
    Pré-charge les blessures de toutes les équipes en parallèle.
    À appeler avant l'analyse principale pour éviter les requêtes séquentielles.
    """
    stale = []
    try:
        from nhl_stats import _match_abbrev
        cache = _load_cache()
        ts    = cache.get("_ts", {})
        for name in team_names:
            abbrev = _match_abbrev(name)
            if abbrev and (abbrev not in cache.get("teams", {})
                           or time.time() - ts.get(abbrev, 0) > CACHE_TTL):
                stale.append(name)
    except Exception:
        stale = team_names

    if not stale:
        return

    def _fetch_one(name):
        try:
            get_team_injuries(name)
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        list(as_completed([ex.submit(_fetch_one, n) for n in stale]))
