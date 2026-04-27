"""
Anomaly Detection - Compare odds from multiple sportsbooks and identify mispricing.
Finds outliers where odds deviate >10% from market average.
"""

from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional
from collections import defaultdict


@dataclass
class Anomaly:
    """Represents a detected pricing anomaly (mispricing)."""

    away: str
    home: str
    selection: str                 # e.g., "Team A", "Over 5.5", "Team B -1.5"
    bet_type: str                  # "h2h", "spreads", "totals"
    sportsbook_outlier: str        # Which sportsbook has the outlier odds
    outlier_odds: float            # The mispriced odds
    outlier_prob: float            # Implied probability (100 / odds)
    market_avg_odds: float         # Average odds from all sources
    market_avg_prob: float         # Average implied probability
    deviation_pct: float           # Percentage deviation from market average
    arbitrage_roi: float           # ROI if both sides of arb are covered
    severity: str                  # "low" (<15%), "medium" (15-25%), "high" (>25%)
    timestamp: str                 # ISO date YYYY-MM-DD

    def to_dict(self) -> Dict:
        """Convert to JSON-serializable dict."""
        return {
            "match": f"{self.away} @ {self.home}",
            "selection": self.selection,
            "bet_type": self.bet_type,
            "outlier": {
                "sportsbook": self.sportsbook_outlier,
                "odds": round(self.outlier_odds, 2)
            },
            "market_avg": round(self.market_avg_odds, 2),
            "deviation_pct": round(self.deviation_pct, 1),
            "severity": self.severity,
            "arbitrage_roi": round(self.arbitrage_roi * 100, 1) if self.arbitrage_roi > 0 else 0,
            "timestamp": self.timestamp,
        }


def detect_anomalies(all_odds: List[Dict], min_deviation_pct: float = 10.0) -> List[Anomaly]:
    """
    Detect pricing anomalies by comparing odds across sportsbooks.

    Args:
        all_odds: List of odds dicts from all sources
        min_deviation_pct: Minimum % deviation to flag as anomaly (default 10%)

    Returns:
        List of Anomaly objects for detected misprices
    """

    anomalies = []

    # Group odds by (away, home, selection, bet_type)
    groups: Dict[Tuple, List[Dict]] = defaultdict(list)

    for odd in all_odds:
        key = (odd["away"], odd["home"], odd["selection"], odd["bet_type"])
        groups[key].append(odd)

    # Analyze each group for anomalies
    for (away, home, selection, bet_type), odds_list in groups.items():

        # Need at least 2 sources to detect anomalies
        if len(odds_list) < 2:
            continue

        # Calculate market average odds (simple arithmetic mean)
        odds_values = [o["odds"] for o in odds_list if o["odds"] > 0]

        if not odds_values:
            continue

        market_avg = sum(odds_values) / len(odds_values)
        market_prob = 100.0 / market_avg if market_avg > 0 else 0

        # Find outliers for each sportsbook in this group
        for odd in odds_list:
            odds_val = odd["odds"]

            if odds_val <= 0:
                continue

            # Calculate deviation percentage
            deviation_pct = abs(odds_val - market_avg) / market_avg * 100

            # Flag if deviation exceeds minimum threshold
            if deviation_pct > min_deviation_pct:

                outlier_prob = 100.0 / odds_val if odds_val > 0 else 0

                # Calculate potential arbitrage ROI
                # For h2h/moneyline: check if there's an opposite bet available at good odds
                arb_roi = calculate_arbitrage_roi(
                    odds_list,
                    odd,
                    odds_val,
                    outlier_prob
                )

                # Determine severity based on deviation
                if deviation_pct > 25:
                    severity = "high"
                elif deviation_pct > 15:
                    severity = "medium"
                else:
                    severity = "low"

                anomalies.append(Anomaly(
                    away=away,
                    home=home,
                    selection=selection,
                    bet_type=bet_type,
                    sportsbook_outlier=odd["source"],
                    outlier_odds=odds_val,
                    outlier_prob=round(outlier_prob, 1),
                    market_avg_odds=round(market_avg, 2),
                    market_avg_prob=round(market_prob, 1),
                    deviation_pct=deviation_pct,
                    arbitrage_roi=arb_roi,
                    severity=severity,
                    timestamp=odd["date"]
                ))

    return anomalies


def calculate_arbitrage_roi(
    odds_list: List[Dict],
    current_odd: Dict,
    current_odds_val: float,
    current_prob: float
) -> float:
    """
    Calculate arbitrage ROI if both sides of a bet are available.

    For moneyline (h2h): If one team is overpriced, check if the other team
    is underpriced enough to create arbitrage.

    Returns:
        ROI as decimal (0.05 = 5%), or 0 if no arbitrage opportunity
    """

    # Only apply to moneyline (h2h) bets for now
    if current_odd.get("bet_type") != "h2h":
        return 0

    # Look for opposite selection at favorable odds
    for other_odd in odds_list:
        if other_odd["source"] == current_odd["source"]:
            continue  # Skip same sportsbook

        other_odds_val = other_odd["odds"]
        if other_odds_val <= 0:
            continue

        other_prob = 100.0 / other_odds_val

        # Check if combined probability is < 100% (arbitrage opportunity)
        combined_prob = (current_prob + other_prob) / 100.0

        if combined_prob < 1.0:
            # Calculate ROI: (1 / combined_prob) - 1
            roi = (1.0 / combined_prob) - 1.0
            return max(roi, 0)  # Return highest ROI found

    return 0


def filter_anomalies(
    anomalies: List[Anomaly],
    severity: Optional[str] = None,
    only_arbitrage: bool = False,
    sportsbook: Optional[str] = None
) -> List[Anomaly]:
    """
    Filter anomalies by severity, arbitrage opportunity, or sportsbook.

    Args:
        anomalies: List of detected anomalies
        severity: Filter by "low", "medium", "high" (None = all)
        only_arbitrage: Only return anomalies with arbitrage ROI > 0
        sportsbook: Only return anomalies from specific sportsbook

    Returns:
        Filtered list of anomalies
    """

    result = anomalies

    if severity:
        result = [a for a in result if a.severity == severity]

    if only_arbitrage:
        result = [a for a in result if a.arbitrage_roi > 0]

    if sportsbook:
        result = [a for a in result if a.sportsbook_outlier == sportsbook]

    return result


def test_compare_odds():
    """Quick test of anomaly detection."""

    # Mock odds data
    test_odds = [
        {
            "away": "Montreal",
            "home": "Toronto",
            "selection": "Montreal",
            "odds": 2.05,
            "bet_type": "h2h",
            "source": "Mise-o-Jeu",
            "date": "2026-04-27",
        },
        {
            "away": "Montreal",
            "home": "Toronto",
            "selection": "Montreal",
            "odds": 1.85,  # 10% lower = potential anomaly if others avg higher
            "bet_type": "h2h",
            "source": "DraftKings",
            "date": "2026-04-27",
        },
        {
            "away": "Montreal",
            "home": "Toronto",
            "selection": "Montreal",
            "odds": 2.00,
            "bet_type": "h2h",
            "source": "FanDuel",
            "date": "2026-04-27",
        },
        {
            "away": "Montreal",
            "home": "Toronto",
            "selection": "Toronto",
            "odds": 1.78,
            "bet_type": "h2h",
            "source": "Mise-o-Jeu",
            "date": "2026-04-27",
        },
    ]

    print("Testing anomaly detection...")
    anomalies = detect_anomalies(test_odds, min_deviation_pct=10)

    if anomalies:
        print(f"[OK] Detected {len(anomalies)} anomalies")
        for anom in anomalies:
            print(f"  {anom.match}: {anom.selection} @ {anom.outlier_odds} "
                  f"({anom.sportsbook_outlier}) — {anom.deviation_pct:.1f}% deviation, "
                  f"severity: {anom.severity}")
    else:
        print("[WARNING] No anomalies detected in test data")


if __name__ == "__main__":
    test_compare_odds()
