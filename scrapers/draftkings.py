"""
DraftKings Scraper - Fetch NHL odds from DraftKings using the community library.
"""

from typing import List, Dict, Optional

try:
    import draftkings as dk
except ImportError:
    dk = None

from datetime import datetime


def fetch_draftkings_nhl() -> List[Dict]:
    """
    Fetch NHL odds from DraftKings using draftkings library.

    Returns:
        List of odds dicts: {
            "away": str,
            "home": str,
            "selection": str,
            "odds": float,
            "bet_type": str,
            "source": "DraftKings",
            "date": str,
        }
    """

    if dk is None:
        print("[WARNING] draftkings library not installed. Run: pip install draftkings")
        return []

    try:
        picks = []
        today_str = str(datetime.now().date())

        # Get all sports
        sports = dk.get_sports()

        # Find ice hockey / NHL
        nhl = None
        for sport in sports:
            if "hockey" in sport.name.lower() or "nhl" in sport.name.lower():
                nhl = sport
                break

        if not nhl:
            print("[WARNING] Could not find NHL/Hockey sport in DraftKings API")
            return []

        # Get all competitions (games) for today
        competitions = nhl.competitions if nhl.competitions else []

        for comp in competitions:
            try:
                away_team = comp.away_team if hasattr(comp, "away_team") else ""
                home_team = comp.home_team if hasattr(comp, "home_team") else ""

                if not away_team or not home_team:
                    continue

                # Moneyline bets (win/loss for each team)
                # Check if competition has contenders
                contenders = comp.contenders if hasattr(comp, "contenders") else []

                for contender in contenders:
                    try:
                        contender_name = contender.name if hasattr(contender, "name") else ""
                        props = contender.props if hasattr(contender, "props") else []

                        for prop in props:
                            try:
                                odds_val = prop.odds if hasattr(prop, "odds") else None
                                prop_type = prop.type if hasattr(prop, "type") else "h2h"

                                if odds_val and odds_val > 0:
                                    picks.append({
                                        "away": away_team,
                                        "home": home_team,
                                        "selection": contender_name,
                                        "odds": float(odds_val),
                                        "bet_type": prop_type,
                                        "source": "DraftKings",
                                        "date": today_str,
                                    })
                            except (AttributeError, ValueError, TypeError):
                                continue

                    except (AttributeError, TypeError):
                        continue

            except (AttributeError, TypeError):
                continue

        return picks

    except Exception as e:
        print(f"[ERROR] DraftKings error: {e}")
        return []


def test_draftkings():
    """Quick test to verify DraftKings scraper is working."""

    print("Testing DraftKings connection...")

    if dk is None:
        print("[WARNING] draftkings library not installed")
        print("   Install with: pip install draftkings")
        return

    picks = fetch_draftkings_nhl()

    if picks:
        print(f"[OK] Retrieved {len(picks)} odds from DraftKings")
        # Show first few picks as sample
        for pick in picks[:3]:
            print(f"  {pick['away']} @ {pick['home']}: {pick['selection']} @ {pick['odds']}")
    else:
        print("[WARNING] No odds retrieved from DraftKings")


if __name__ == "__main__":
    test_draftkings()
