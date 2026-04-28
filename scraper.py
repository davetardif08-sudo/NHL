"""
Scraper pour miseojeuplus.espacejeux.com
Utilise l'API REST publique du site pour obtenir les cotes en temps reel.

Architecture :
  1. La page d'accueil contient des JSON-LD schema.org/SportsEvent avec les event IDs
  2. L'API content-service retourne les marches et cotes pour chaque evenement
  3. Endpoint : content.mojp-sgdigital-jel.com/content-service/api/v1/q/events-by-ids
  4. Sports supportés : hockey (NHL) et basketball (NBA)
"""

import asyncio
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import async_playwright, Page


# --- Structures de donnees ---------------------------------------------------

@dataclass
class Selection:
    label: str
    odds: float
    prediction_id: str  # ID du outcome (ex: "324967530")


@dataclass
class BetGroup:
    bet_type: str
    selections: list[Selection] = field(default_factory=list)


@dataclass
class Match:
    sport: str
    league: str
    home_team: str
    away_team: str
    date: str       # YYYY-MM-DD en heure locale (Montreal)
    time: str       # HH:MM en heure locale
    event_id: str   # ID de l'evenement
    event_url: str = ""  # URL complète vers la page du match sur Mise-O-Jeu
    bet_groups: list[BetGroup] = field(default_factory=list)


# --- Configuration -----------------------------------------------------------

BASE_SITE    = "https://miseojeuplus.espacejeux.com/sports/fr/"
API_BASE     = "https://content.mojp-sgdigital-jel.com/content-service/api/v1/q"
API_PARAMS   = (
    "includeChildMarkets=true"
    "&includeCollections=true"
    "&includePriorityCollectionChildMarkets=true"
    "&includePriceHistory=false"
    "&includeCommentary=false"
    "&includeIncidents=false"
    "&includeRace=false"
    "&includeMedia=false"
    "&includePools=false"
    "&includeNonFixedOdds=false"
    "&lang=fr-CA"
    "&channel=I"
)

# Types de paris a inclure (on garde les paris sur l'equipe/match, pas les joueurs)
MARKET_GROUPS_WANTED = {
    "MATCH_RESULT_WIN_DRAW_WIN",        # Gagnant 2 issues
    "WIN_DRAW_WIN",
    "MATCH_WINNER",
    "MATCH_RESULT",
    "WINNER_2_WAY",
    "TOTAL_GOALS",                      # Total de buts
    "GOALS_OVER_UNDER",
    "OVER_UNDER",
    "MATCH_HANDICAP_2_WAY",             # Ecart
    "HANDICAP_2_WAY",
    "DOUBLE_CHANCE",                    # Double chance
    "BOTH_TEAMS_TO_SCORE",              # Les 2 equipes marquent
    "RESULT_TOTAL_GOALS",
    # Noms partiels - verifie si le nom contient ces mots
}

MARKET_NAME_KEYWORDS_WANTED = [
    "gagnant",
    "victoire",
    "total de buts",
    "total de points",    # NBA
    "plus/moins",
    "double chance",
    "les 2",
    "2 issues",
    "3 issues",
    "ecart",
    "pointage",           # NBA
]

MARKET_NAME_KEYWORDS_EXCLUDE = [
    "1re periode",
    "1\u00e8re p\u00e9riode",
    "2e periode",
    "2\u00e8me p\u00e9riode",
    "3e periode",
    "3\u00e8me p\u00e9riode",
    "1er quart",          # NBA quarters
    "2e quart",
    "3e quart",
    "4e quart",
    "1re mi",             # NBA halves
    "2e mi",
    "marquera",
    "lancers",
    "tirs",
    "premier",
    "paires",
    "impair",
    "avantage",
    "barrage",
    "prolongation",
    "marge",
    "quand",
    "points de",          # NBA player props
    "rebonds",
    "passes",
    "triple",
]


# --- Conversion UTC -> heure Montreal ----------------------------------------

def _utc_to_local(utc_str: str) -> tuple[str, str]:
    """
    Convertit une date UTC ISO en date/heure locale (UTC-4 ete, UTC-5 hiver).
    Retourne (YYYY-MM-DD, HH:MM).
    """
    try:
        dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        # Offset simplifie : UTC-5 (hiver) / UTC-4 (ete)
        # Mars = heure ete (2eme dimanche mars -> fin mars), donc UTC-4
        from datetime import timedelta
        local = dt - timedelta(hours=4)
        return local.strftime('%Y-%m-%d'), local.strftime('%H:%M')
    except Exception:
        return utc_str[:10], ""


# --- Filtrage des marches ----------------------------------------------------

def _should_include_market(name: str, group_code: str) -> bool:
    """Retourne True si ce type de pari doit etre affiche."""
    name_lower = name.lower()
    # Exclure paris sur les periodes isolees et paris de joueurs
    for kw in MARKET_NAME_KEYWORDS_EXCLUDE:
        if kw in name_lower:
            return False
    # Inclure si le groupCode correspond
    if group_code in MARKET_GROUPS_WANTED:
        return True
    # Inclure si le nom contient un mot-cle voulu
    for kw in MARKET_NAME_KEYWORDS_WANTED:
        if kw in name_lower:
            return True
    return False


# --- Parsing API -------------------------------------------------------------

def _parse_event(data: dict) -> Optional[Match]:
    """Convertit un dict d'evenement API en objet Match."""
    if not data.get('displayed') or not data.get('active'):
        return None

    event_id = str(data.get('id', ''))
    name     = data.get('name', '')
    start    = data.get('startTime', '')

    # Teams
    teams = data.get('teams', [])
    home_team = away_team = ""
    for t in teams:
        if t.get('side') == 'HOME':
            home_team = t['name']
        elif t.get('side') == 'AWAY':
            away_team = t['name']

    # Si pas de teams, essayer depuis le nom
    if not home_team or not away_team:
        m = re.match(r'^(.+?)\s+[aà@]\s+(.+)$', name, re.IGNORECASE)
        if m:
            away_team, home_team = m.group(1).strip(), m.group(2).strip()
        else:
            return None

    # Nettoyer les noms (enlever les accents corrompus)
    home_team = home_team.replace('\ufffd', 'e').replace('\u00e9', 'e').replace('\u00e8', 'e')
    away_team = away_team.replace('\ufffd', 'e').replace('\u00e9', 'e').replace('\u00e8', 'e')

    date_str, time_str = _utc_to_local(start)

    # Ligue
    type_info = data.get('type', {})
    league    = type_info.get('name', 'NHL')

    # Déterminer le sport depuis la ligue
    league_upper = league.upper()
    if any(k in league_upper for k in ("NBA", "BASKETBALL", "BBALL")):
        sport = "basketball"
    else:
        sport = "hockey"

    match = Match(
        sport=sport,
        league=league,
        home_team=home_team,
        away_team=away_team,
        date=date_str,
        time=time_str,
        event_id=event_id,
    )

    # Marches et cotes
    seen_group_codes = set()
    for market in data.get('markets', []):
        if not market.get('displayed') or not market.get('active'):
            continue

        market_name  = market.get('name', '')
        group_code   = market.get('groupCode', '')

        if not _should_include_market(market_name, group_code):
            continue

        # Dedupliquer : garder seulement le premier marche par groupCode
        if group_code and group_code in seen_group_codes:
            continue
        if group_code:
            seen_group_codes.add(group_code)

        outcomes = market.get('outcomes', [])
        if not outcomes:
            continue

        grp = BetGroup(bet_type=market_name)

        for outcome in outcomes:
            if not outcome.get('displayed') or not outcome.get('active'):
                continue
            prices = outcome.get('prices', [])
            if not prices:
                continue
            dec = prices[0].get('decimal')
            if not dec or float(dec) <= 1.0:
                continue

            sel_name = outcome.get('name', '')
            sel_name = sel_name.replace('\ufffd', 'e').replace('\u00e9', 'e').replace('\u00e8', 'e')

            # Pour les paris Plus/Moins : ajouter la valeur de ligne si le nom est tronqué
            if sel_name in ('Plus de', 'Moins de', 'Over', 'Under'):
                line = (outcome.get('line') or outcome.get('points')
                        or outcome.get('attr') or market.get('line')
                        or market.get('attr') or '')
                if line:
                    sel_name = f"{sel_name} {line}"

            grp.selections.append(Selection(
                label=sel_name,
                odds=float(dec),
                prediction_id=str(outcome.get('id', '')),
            ))

        if len(grp.selections) >= 2:
            match.bet_groups.append(grp)

    return match if match.bet_groups else None


# --- Fetch API events via requests (sans navigateur) -------------------------

import requests as _requests_mod

_API_HEADERS = {
    "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":      "application/json",
    "Referer":     "https://miseojeuplus.espacejeux.com/",
    "Origin":      "https://miseojeuplus.espacejeux.com",
}

# Session partagée pour réutiliser les connexions TCP entre les events parallèles
_API_SESSION = _requests_mod.Session()
_API_SESSION.headers.update(_API_HEADERS)


def _fetch_one_event(event_id: str, url_map: dict) -> list[Match]:
    """Récupère les cotes d'un événement via requests (HTTP simple, sans Playwright)."""
    api_url = f"{API_BASE}/events-by-ids?eventIds={event_id}&{API_PARAMS}"
    try:
        resp = _API_SESSION.get(api_url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        events_list = data.get("data", {}).get("events", [])
        result = []
        for ev in events_list:
            match = _parse_event(ev)
            if match:
                match.event_url = url_map.get(match.event_id, "")
                print(f"     {match.away_team} @ {match.home_team} - {len(match.bet_groups)} marchés")
                result.append(match)
        return result
    except Exception as e:
        print(f"    [!] Erreur event {event_id}: {e}")
        return []


def _fetch_events_parallel(event_ids: list[str], url_map: dict,
                            max_workers: int = 8) -> list[Match]:
    """Récupère tous les événements en parallèle via ThreadPoolExecutor."""
    matches: list[Match] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one_event, eid, url_map): eid
                   for eid in event_ids}
        for future in as_completed(futures):
            try:
                matches.extend(future.result())
            except Exception:
                pass
    return matches


# --- Scraper principal -------------------------------------------------------

class MiseOJeuScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless

    @staticmethod
    def _launch_kwargs(headless: bool) -> dict:
        """Retourne les kwargs pour pw.chromium.launch()."""
        return {
            "headless": headless,
            # Requis pour Docker/Linux root : sans ces flags, Chromium rend
            # une page partielle (~275K au lieu de 2.4MB)
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        }

    async def scrape_all(self, sports: list | None = None) -> list[Match]:
        """
        Scrape les événements Mise-O-Jeu.

        sports : liste de sports à récupérer, ex. ["hockey"] ou ["basketball"].
                 None = tous les sports (hockey + NBA).
        """
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(**self._launch_kwargs(self.headless))
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="fr-CA",
                viewport={"width": 1280, "height": 900},
            )

            # Etape 1 : obtenir la liste des event IDs
            # networkidle requis pour que le React SPA charge tous les liens de matchs
            print("  >> Chargement de la page principale...")
            page = await context.new_page()
            try:
                await page.goto(BASE_SITE, wait_until='networkidle', timeout=30000)
            except Exception:
                await asyncio.sleep(2)

            html    = await page.content()

            # Extraire les cookies de session et les injecter dans la session HTTP
            # → permet d'utiliser requests (sans Playwright) pour les events individuels
            cookies = await context.cookies()
            for ck in cookies:
                _API_SESSION.cookies.set(ck["name"], ck["value"], domain=ck.get("domain", ""))

            await page.close()

            event_data = self._extract_all_event_ids(html)

            # Si peu de matchs hockey trouvés sur la page principale, charger aussi
            # la page de compétition NHL (playoffs apparaissent sous /competition/574/)
            nhl_found = sum(1 for _, _, sp in event_data if sp == "hockey")
            if nhl_found < 3:
                print("  >> Peu de matchs NHL — chargement page playoffs...")
                NHL_COMP_URL = "https://miseojeuplus.espacejeux.com/sports/fr/sports/competition/574/hockey/amerique-du-nord/nhl/matches"
                page2 = await context.new_page()
                try:
                    await page2.goto(NHL_COMP_URL, wait_until='networkidle', timeout=30000)
                except Exception:
                    await asyncio.sleep(2)
                html2 = await page2.content()
                await page2.close()
                extra = self._extract_all_event_ids(html2)
                seen_ids = {eid for eid, _, _ in event_data}
                for item in extra:
                    if item[0] not in seen_ids:
                        event_data.append(item)
                        seen_ids.add(item[0])
                nhl_now = sum(1 for _, _, sp in event_data if sp == "hockey")
                print(f"  >> Après playoffs: {nhl_now} NHL")

            # Filtrer par sport si demandé (garder "unknown" pour les nouveaux IDs courts)
            if sports:
                event_data = [(eid, url, sp) for eid, url, sp in event_data if sp in sports or sp == "unknown"]

            nhl_count = sum(1 for _, _, sp in event_data if sp == "hockey")
            nba_count = sum(1 for _, _, sp in event_data if sp == "basketball")
            unknown_count = sum(1 for _, _, sp in event_data if sp == "unknown")
            sport_label = "/".join(sports) if sports else "tous"
            print(f"     {nhl_count} NHL + {nba_count} NBA + {unknown_count} a verifier ({sport_label})")

            if not event_data:
                await browser.close()
                return []

            # Construire un mapping event_id → url
            url_map = {eid: url for eid, url, _ in event_data}
            event_ids = [eid for eid, _, _ in event_data]

            # Etape 2 : récupérer les cotes via HTTP direct avec les cookies de session
            # Beaucoup plus rapide que d'ouvrir un onglet Playwright par event
            await browser.close()
            matches = _fetch_events_parallel(event_ids, url_map, max_workers=10)

            # Fallback Playwright si les cookies n'ont pas suffi (anti-bot renforcé)
            if not matches:
                print("  >> Fallback Playwright pour les events...")
                browser2 = await pw.chromium.launch(**self._launch_kwargs(self.headless))
                context2 = await browser2.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    locale="fr-CA",
                )
                tasks = [self._fetch_event_async(context2, eid, url_map) for eid in event_ids]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                await browser2.close()
                for r in results:
                    if isinstance(r, list):
                        matches.extend(r)

            return matches

    async def _fetch_event_async(self, context, event_id: str, url_map: dict) -> list[Match]:
        """Récupère les cotes d'un événement via une page Playwright (async, parallèle)."""
        api_url = f"{API_BASE}/events-by-ids?eventIds={event_id}&{API_PARAMS}"
        page = await context.new_page()
        try:
            await page.goto(api_url, wait_until='domcontentloaded', timeout=20000)
            raw = await page.content()
            m = re.search(r'<pre[^>]*>(.*?)</pre>', raw, re.DOTALL)
            json_str = m.group(1) if m else raw
            if not json_str.strip().startswith('{'):
                m2 = re.search(r'(\{.*\})', raw, re.DOTALL)
                if m2:
                    json_str = m2.group(1)
            data = json.loads(json_str)
            events_list = data.get('data', {}).get('events', [])
            result = []
            for ev in events_list:
                match = _parse_event(ev)
                if match:
                    match.event_url = url_map.get(match.event_id, "")
                    print(f"     {match.away_team} @ {match.home_team} - {len(match.bet_groups)} marchés")
                    result.append(match)
            return result
        except Exception as e:
            print(f"    [!] Erreur event {event_id}: {e}")
            return []
        finally:
            await page.close()

    def _extract_all_event_ids(self, html: str) -> list[tuple[str, str, str]]:
        """
        Extrait les IDs, URLs et sports de tous les evenements (NHL + NBA).
        Retourne une liste de tuples (event_id, event_url, sport).

        Supporte deux formats d'URL:
        - Ancien: /sports/fr/en-jeux/evenement/ID/hockey/amerique-du-nord/nhl/nom
        - Nouveau: /sports/fr/sportif/evenement/ID  (sport detecte via contexte HTML)
        """
        seen   = set()
        result = []

        # Cherche les chemins relatifs ET absolus, segments en-jeux, sportif ou sports
        base = "https://miseojeuplus.espacejeux.com"
        patterns = [
            # Format long (local/Québec) : URL inclut le sport dans le chemin
            (r'href="(/sports/fr/(?:en-jeux|sportif|sports)/evenement/(\d+)/hockey/amerique-du-nord/nhl/[^"\'<>\s]*)"',
             "hockey"),
            (r'href="(/sports/fr/(?:en-jeux|sportif|sports)/evenement/(\d+)/basketball/amerique-du-nord/nba/[^"\'<>\s]*)"',
             "basketball"),
            # Format court (Fly.io/Ontario) : URL sans suffixe sport — sport déterminé plus tard
            (r'href="(/sports/fr/(?:en-jeux|sportif|sports)/evenement/(\d+))(?:["\'\s])',
             "unknown"),
        ]
        total_hrefs = html.count('href="')
        print(f"  >> HTML: {len(html)} chars, {total_hrefs} hrefs totaux")
        for pattern, sport in patterns:
            for path, eid in re.findall(pattern, html):
                if eid not in seen:
                    seen.add(eid)
                    result.append((eid, base + path.rstrip('/'), sport))

        # Debug: si aucun match trouvé, afficher des exemples de hrefs pour diagnostiquer
        if not result:
            # Cherche tout href contenant un ID d'événement numérique
            broad = re.findall(r'href="(/[^"\'<>\s]*evenement/\d+[^"\'<>\s]*)"', html)[:8]
            if broad:
                print(f"  >> DEBUG evenements trouves (format inconnu): {broad[:4]}")
            else:
                sample_hrefs = re.findall(r'href="(/sports/[^"\'<>\s]{10,80})"', html)[:5]
                print(f"  >> DEBUG hrefs /sports/ sample: {sample_hrefs}")
                # Aussi chercher dans un format anglais possible
                en_hrefs = re.findall(r'href="(/[^"\'<>\s]*/event/\d+[^"\'<>\s]*)"', html)[:5]
                if en_hrefs:
                    print(f"  >> DEBUG hrefs /event/ (anglais?): {en_hrefs}")

        return result

    # Alias pour compatibilité ascendante
    def _extract_nhl_event_ids(self, html: str) -> list[tuple[str, str]]:
        return [(eid, url) for eid, url, sport in self._extract_all_event_ids(html)
                if sport == "hockey"]


def scrape_all_sync(headless: bool = True,
                    sports: list | None = None) -> list[Match]:
    scraper = MiseOJeuScraper(headless=headless)
    return asyncio.run(scraper.scrape_all(sports=sports))
