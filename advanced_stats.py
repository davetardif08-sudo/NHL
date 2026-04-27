"""
Stats avancées des équipes NHL depuis l'API publique api.nhle.com/stats/rest.

Sources :
  1. /team/summary   → PP%, PK%, faceoff%, shots for/against per game
  2. /team/realtime  → satPct (Corsi%), giveaways, takeaways, hits
  3. Evolving Hockey → CF%, xGF% (via Playwright, mis en cache)

Cache fichier JSON — TTL 12 heures.
"""

import json
import time
from pathlib import Path

CACHE_FILE = Path(__file__).parent / ".advanced_stats_cache.json"
CACHE_TTL  = 12 * 3600   # 12 heures

# Cache mémoire — évite de relire le fichier JSON à chaque appel
_mem_cache: dict | None = None

_NHL_REST_BASE = "https://api.nhle.com/stats/rest/en/team"
_ENDPOINTS = ["summary", "realtime", "powerplay", "penaltykill"]

# Plages typiques NHL pour normalisation 0-1
_RANGES = {
    "ppPct":     (12.0, 27.0),
    "pkPct":     (70.0, 90.0),
    "satPct":    (43.0, 57.0),
    "sfPG":      (24.0, 38.0),
    "saPG":      (24.0, 38.0),
    "foPct":     (44.0, 56.0),
    "xgfPct":    (35.0, 65.0),   # Evolving Hockey xGF%
    "cfPct":     (43.0, 57.0),   # Evolving Hockey CF%
    "pimPerGame": (2.0, 6.0),    # Pénalités (minutes) par match
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


def _cache_fresh(cache: dict) -> bool:
    return time.time() - cache.get("_ts", 0) < CACHE_TTL


# ─── Saison courante ──────────────────────────────────────────────────────────

def _current_season() -> int:
    from datetime import date
    d = date.today()
    if d.month >= 10:
        return d.year * 10000 + (d.year + 1)
    return (d.year - 1) * 10000 + d.year


# ─── Récupération NHL REST ────────────────────────────────────────────────────

def _qs(season: int) -> str:
    import urllib.parse
    sort = urllib.parse.quote('[{"property":"seasonId","direction":"DESC"}]')
    expr = urllib.parse.quote(f"gameTypeId=2 and seasonId={season}")
    return f"?isAggregate=false&isGame=false&sort={sort}&start=0&limit=40&cayenneExp={expr}"


def _fetch_nhl_rest() -> dict[str, dict]:
    """Retourne un dict abbrev → stats depuis les endpoints NHL REST."""
    import requests
    from nhl_stats import _match_abbrev

    season = _current_season()
    merged: dict[str, dict] = {}

    for endpoint in _ENDPOINTS:
        url = f"{_NHL_REST_BASE}/{endpoint}{_qs(season)}"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            for team in resp.json().get("data", []):
                name   = team.get("teamFullName", "")
                abbrev = _match_abbrev(name)
                if not abbrev:
                    continue
                if abbrev not in merged:
                    merged[abbrev] = {}
                merged[abbrev].update(_extract_fields(endpoint, team))
        except Exception:
            continue

    return merged


def _to_pct(v) -> float | None:
    """Convertit une valeur 0-1 en pourcentage. Laisse inchangé si déjà > 1."""
    if v is None:
        return None
    return round(v * 100, 2) if v <= 1.0 else round(float(v), 2)


def _extract_fields(endpoint: str, team: dict) -> dict:
    """Extrait les champs pertinents selon le endpoint."""
    if endpoint == "summary":
        return {
            k: v for k, v in {
                "ppPct":      _to_pct(team.get("powerPlayPct")),
                "pkPct":      _to_pct(team.get("penaltyKillPct")),
                "foPct":      _to_pct(team.get("faceoffWinPct")),
                "sfPG":       team.get("shotsForPerGame"),
                "saPG":       team.get("shotsAgainstPerGame"),
                "pimPerGame": team.get("penaltiesPerGame") or team.get("pimPerGame"),
            }.items() if v is not None
        }
    if endpoint == "realtime":
        return {
            k: v for k, v in {
                "satPct":    _to_pct(team.get("satPct")),
                "giveaways": team.get("giveaways"),
                "takeaways": team.get("takeaways"),
                "hits":      team.get("hits"),
            }.items() if v is not None
        }
    if endpoint == "powerplay":
        return {
            k: v for k, v in {
                "ppPct":   _to_pct(team.get("powerPlayPct")),
                "ppPerGP": team.get("ppOpportunitiesPerGame"),
            }.items() if v is not None
        }
    if endpoint == "penaltykill":
        return {
            k: v for k, v in {
                "pkPct":   _to_pct(team.get("penaltyKillPct")),
                "pkTOIPG": team.get("pkTimeOnIcePerGame"),
            }.items() if v is not None
        }
    return {}


# ─── Evolving Hockey (Playwright) ─────────────────────────────────────────────

async def _fetch_evolving_hockey_async() -> dict[str, dict]:
    """
    Scrape les stats CF% et xGF% depuis evolving-hockey.com via Playwright.
    Retourne dict abbrev → {cfPct, xgfPct} ou {} si inaccessible.
    """
    from playwright.async_api import async_playwright
    from nhl_stats import _match_abbrev
    import re

    result = {}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page()

            # La page Shiny charge les données automatiquement
            await page.goto(
                "https://evolving-hockey.com/stats/team_standard/",
                wait_until="networkidle",
                timeout=30000,
            )
            # Attendre que le tableau DataTables soit prêt
            await page.wait_for_selector("table", timeout=20000)

            # Lire les en-têtes pour trouver CF% et xGF%
            headers = await page.eval_on_selector_all(
                "thead th",
                "els => els.map(e => e.innerText.trim())"
            )
            cf_idx  = next((i for i, h in enumerate(headers) if "CF%" in h), None)
            xgf_idx = next((i for i, h in enumerate(headers) if "xGF%" in h), None)
            team_idx = next((i for i, h in enumerate(headers) if "Team" in h), 0)

            rows = await page.query_selector_all("tbody tr")
            for row in rows:
                cells = await row.query_selector_all("td")
                texts = [await c.inner_text() for c in cells]
                if not texts:
                    continue
                team_name = texts[team_idx] if team_idx < len(texts) else ""
                abbrev    = _match_abbrev(team_name)
                if not abbrev:
                    continue
                entry: dict = {}
                if cf_idx is not None and cf_idx < len(texts):
                    try:
                        entry["cfPct"] = float(texts[cf_idx].replace("%", "").strip())
                    except ValueError:
                        pass
                if xgf_idx is not None and xgf_idx < len(texts):
                    try:
                        entry["xgfPct"] = float(texts[xgf_idx].replace("%", "").strip())
                    except ValueError:
                        pass
                if entry:
                    result[abbrev] = entry

            await browser.close()
    except Exception:
        pass

    return result


def _try_fetch_evolving_hockey() -> dict[str, dict]:
    """Lance le scrape Evolving Hockey de façon synchrone."""
    try:
        import asyncio
        return asyncio.run(_fetch_evolving_hockey_async())
    except Exception:
        return {}


# ─── Point d'entrée public ────────────────────────────────────────────────────

def refresh_advanced_stats(force: bool = False) -> bool:
    """
    Rafraîchit le cache si nécessaire.
    Retourne True si une mise à jour a été effectuée.
    """
    cache = _load_cache()
    if not force and _cache_fresh(cache):
        return False

    teams = _fetch_nhl_rest()

    # Evolving Hockey (optionnel — peut prendre quelques secondes)
    eh = _try_fetch_evolving_hockey()
    for abbrev, stats in eh.items():
        if abbrev in teams:
            teams[abbrev].update(stats)
        else:
            teams[abbrev] = stats

    _save_cache({"_ts": time.time(), "teams": teams})
    return True


def get_advanced_stats(team_name: str) -> dict:
    """
    Retourne les stats avancées d'une équipe.
    Rafraîchit le cache si périmé.
    """
    from nhl_stats import _match_abbrev

    cache = _load_cache()
    if not _cache_fresh(cache):
        refresh_advanced_stats()
        cache = _load_cache()

    abbrev = _match_abbrev(team_name)
    if not abbrev:
        return {}
    return cache.get("teams", {}).get(abbrev, {})


# ─── Normalisation pour score de force ────────────────────────────────────────

def normalize(value: float, stat_key: str) -> float:
    """Normalise une stat dans [0, 1] selon les plages NHL typiques."""
    lo, hi = _RANGES.get(stat_key, (0.0, 100.0))
    if hi == lo:
        return 0.5
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


_DEFAULT_INTRA_WEIGHTS = {
    "base_score": 0.40,
    "ppPct":      0.15,
    "pkPct":      0.12,
    "satPct":     0.15,   # couvre aussi xgfPct et cfPct
    "sfPG":       0.08,
    "foPct":      0.05,
}


def team_strength_score(
    win_stats: dict,
    adv: dict,
    home: bool,
    weights: dict | None = None,
) -> float:
    """
    Calcule un score de force 0-1 combinant stats de base et avancées.

    win_stats : dict retourné par nhl_stats.get_team_stats()
    adv       : dict retourné par get_advanced_stats()
    home      : True si l'équipe joue à domicile
    weights   : poids appris via predictions.get_feature_weights()['intra_stat']
                Si None, utilise les poids par défaut (_DEFAULT_INTRA_WEIGHTS).
    """
    w = {**_DEFAULT_INTRA_WEIGHTS, **(weights or {})}
    parts = []

    # ── Stats de base (win% saison, domicile/route, forme L10) ───────────────
    base_win   = (win_stats["homeWinPct" if home else "roadWinPct"] / 100 * 0.60
                  + win_stats["winPct"] / 100 * 0.40)
    l10_rate   = win_stats["l10Wins"] / max(win_stats["l10GP"], 1)
    form_adj   = (l10_rate - win_stats["winPct"] / 100) * 0.15
    base_score = max(0.05, min(0.95, base_win + form_adj))
    parts.append((base_score, w["base_score"]))

    # ── PP / PK ───────────────────────────────────────────────────────────────
    if "ppPct" in adv:
        parts.append((normalize(adv["ppPct"], "ppPct"), w["ppPct"]))
    if "pkPct" in adv:
        parts.append((normalize(adv["pkPct"], "pkPct"), w["pkPct"]))

    # ── Possession / Corsi / xGF% ─────────────────────────────────────────────
    if "xgfPct" in adv:
        parts.append((normalize(adv["xgfPct"], "xgfPct"), w["satPct"]))
    elif "satPct" in adv:
        parts.append((normalize(adv["satPct"], "satPct"), w["satPct"]))
    elif "cfPct" in adv:
        parts.append((normalize(adv["cfPct"], "cfPct"), w["satPct"]))

    # ── Tirs ──────────────────────────────────────────────────────────────────
    if "sfPG" in adv:
        parts.append((normalize(adv["sfPG"], "sfPG"), w["sfPG"]))

    # ── Mise en jeu ───────────────────────────────────────────────────────────
    if "foPct" in adv:
        parts.append((normalize(adv["foPct"], "foPct"), w["foPct"]))

    # ── Moyenne pondérée ──────────────────────────────────────────────────────
    total_w = sum(ww for _, ww in parts)
    if total_w == 0:
        return base_score
    return sum(s * ww for s, ww in parts) / total_w
