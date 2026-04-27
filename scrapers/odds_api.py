"""
The Odds API Wrapper - Fetch NHL odds from 8+ sportsbooks
Free tier: 500 requests/month
Covers: FanDuel, BetMGM, Caesars, Betano, Unibet, 888Sport, PlayOLG, PointsBet
"""

import requests
import os
from datetime import datetime
from typing import List, Dict, Optional

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Sportsbook source mapping (The Odds API keys → friendly names)
# Supports USA and Canadian regions
SPORTSBOOK_NAMES = {
    # USA Sportsbooks
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "draftkings": "DraftKings",
    "betrivers": "BetRivers",
    "caesars": "Caesars",
    "pointsbet": "PointsBet",
    # Canadian variants
    "fanduel_ca": "FanDuel",
    "betmgm_ca": "BetMGM",
    "caesars_ca": "Caesars",
    "betano_ca": "Betano",
    "unibet_ca": "Unibet",
    "888sport_ca": "888Sport",
    "playolg_ca": "PlayOLG",
    "pointsbetca": "PointsBet",
    # International
    "unibet": "Unibet",
    "888sport": "888Sport",
    "playolg": "PlayOLG",
    "betano": "Betano",
}


def fetch_odds_api_nhl() -> List[Dict]:
    """
    Fetch NHL odds from The Odds API for multiple sportsbooks.

    Returns:
        List of odds dicts: {
            "away": str,
            "home": str,
            "selection": str,          # Team name for h2h, "Over/Under X.X" for totals
            "odds": float,             # Decimal odds
            "bet_type": str,           # "h2h", "spreads", "totals"
            "source": str,             # Sportsbook name
            "date": str,               # ISO date YYYY-MM-DD
        }
    """
    if not ODDS_API_KEY:
        print("[WARNING] ODDS_API_KEY not set. Skipping The Odds API fetch.")
        return []

    try:
        response = requests.get(
            f"{ODDS_API_BASE}/sports/icehockey_nhl/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",  # USA (includes FanDuel, BetMGM, DraftKings, etc.)
                "markets": "h2h,spreads,totals",  # Moneyline, spreads, over/unders
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            },
            timeout=30
        )
        response.raise_for_status()
        data = response.json()

        picks = []
        today_str = str(datetime.now().date())

        # The Odds API returns a list of games directly
        games = data if isinstance(data, list) else data.get("games", [])

        for game in games:
            away_team = game.get("away_team", "")
            home_team = game.get("home_team", "")

            # Iterate through all bookmakers in this game
            for bookmaker in game.get("bookmakers", []):
                source_key = bookmaker.get("key", "").lower()

                # Only include known sportsbooks
                if source_key not in SPORTSBOOK_NAMES:
                    continue

                source_name = SPORTSBOOK_NAMES[source_key]

                # Iterate through all markets for this bookmaker
                for market in bookmaker.get("markets", []):
                    market_key = market.get("key", "")

                    for outcome in market.get("outcomes", []):
                        odds_value = outcome.get("price", 0)

                        if odds_value <= 0:
                            continue

                        # For spreads/totals, include the point spread in selection name
                        selection_name = outcome.get("name", "")
                        point = outcome.get("point")
                        if point is not None:
                            selection_name = f"{selection_name} {point:+.1f}"

                        picks.append({
                            "away": away_team,
                            "home": home_team,
                            "selection": selection_name,
                            "odds": odds_value,
                            "bet_type": market_key,  # "h2h", "spreads", "totals"
                            "source": source_name,
                            "date": today_str,
                        })

        return picks

    except requests.exceptions.RequestException as e:
        print(f"[ERROR] The Odds API error: {e}")
        return []
    except Exception as e:
        print(f"[ERROR] Unexpected error in fetch_odds_api_nhl: {e}")
        return []


def test_odds_api():
    """Quick test to verify The Odds API is working."""
    print("Testing The Odds API connection...")
    picks = fetch_odds_api_nhl()
    if picks:
        print(f"✅ Retrieved {len(picks)} odds from {len(set(p['source'] for p in picks))} sportsbooks")
        # Show first few picks as sample
        for pick in picks[:3]:
            print(f"  {pick['away']} @ {pick['home']}: {pick['selection']} @ {pick['odds']} ({pick['source']})")
    else:
        print("⚠️  No odds retrieved. Check API key and rate limits.")


if __name__ == "__main__":
    test_odds_api()
