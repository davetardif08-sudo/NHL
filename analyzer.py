"""
Moteur d'analyse des cotes de paris.

Méthodes d'analyse :
- Probabilité implicite : 1 / cote
- Marge de la maison (vig/overround)
- Valeur espérée (EV) estimée
- Score de valeur basé sur l'équilibre des cotes
"""

from dataclasses import dataclass, field
from typing import Optional
from scraper import Match, BetGroup, Selection


def _get_stats_module(sport: str):
    """Retourne le module de stats approprié selon le sport."""
    if sport == "basketball":
        import nba_stats
        return nba_stats
    else:
        import nhl_stats
        return nhl_stats


# ─── Résultats d'analyse ──────────────────────────────────────────────────────

@dataclass
class AnalyzedSelection:
    selection: Selection
    implied_prob: float         # Probabilité implicite brute (1/cote)
    fair_prob: float            # Probabilité corrigée de la marge
    edge: float                 # Avantage estimé (fair_prob - implied_prob_vs_max)
    value_score: float          # Score de valeur (0-100)
    recommendation: str         # "Excellent", "Bon", "Neutre", "Éviter"


@dataclass
class AnalyzedBetGroup:
    bet_group: BetGroup
    house_margin: float                         # Marge de la maison en %
    selections: list[AnalyzedSelection] = field(default_factory=list)
    best_value: Optional[AnalyzedSelection] = None


@dataclass
class AnalyzedMatch:
    match: Match
    analyzed_groups: list[AnalyzedBetGroup] = field(default_factory=list)
    top_picks: list[AnalyzedSelection] = field(default_factory=list)
    overall_score: float = 0.0  # Score global du match pour le ranking


@dataclass
class BettingOpportunity:
    """Un paris recommandé avec son contexte complet."""
    match: Match
    bet_type: str
    selection_label: str
    odds: float
    prediction_id: str
    value_score: float
    recommendation: str
    house_margin: float
    fair_prob: float
    implied_prob: float
    sport: str
    league: str
    math_prob: float = 0.0   # probabilité mathématique brute (avant ajustement stats)

    @property
    def display_match(self) -> str:
        return f"{self.match.away_team} @ {self.match.home_team}"

    @property
    def display_date(self) -> str:
        return f"{self.match.date} {self.match.time}"


# ─── Seuils de recommandation ─────────────────────────────────────────────────

THRESHOLDS = {
    "house_margin_low": 5.0,       # Marge < 5% = bonne situation
    "house_margin_medium": 8.0,    # Marge < 8% = acceptable
    "value_excellent": 70,
    "value_good": 50,
    "value_neutral": 30,
    "min_odds": 1.20,              # Cotes trop basses = peu d'intérêt
    "max_odds": 15.0,              # Cotes trop hautes = trop risqué
}


# ─── Moteur d'analyse ─────────────────────────────────────────────────────────

class OddsAnalyzer:
    """Analyse les cotes et identifie les meilleures opportunités."""

    def analyze_matches(self, matches: list[Match]) -> list[AnalyzedMatch]:
        """Analyse une liste de matchs et retourne les résultats triés."""
        analyzed = []
        for match in matches:
            am = self._analyze_match(match)
            if am.top_picks:  # Ne garder que les matchs avec des options analysables
                analyzed.append(am)

        # Trier par score global décroissant
        analyzed.sort(key=lambda x: x.overall_score, reverse=True)
        return analyzed

    def _analyze_match(self, match: Match) -> AnalyzedMatch:
        am = AnalyzedMatch(match=match)

        for group in match.bet_groups:
            ag = self._analyze_group(group)
            am.analyzed_groups.append(ag)

            # Collecter les meilleures sélections
            for sel in ag.selections:
                if sel.value_score >= THRESHOLDS["value_neutral"]:
                    am.top_picks.append(sel)

        if am.top_picks:
            am.overall_score = sum(p.value_score for p in am.top_picks) / len(am.top_picks)
            am.top_picks.sort(key=lambda x: x.value_score, reverse=True)
            am.top_picks = am.top_picks[:3]  # Top 3 par match

        return am

    def _analyze_group(self, group: BetGroup) -> AnalyzedBetGroup:
        selections = [s for s in group.selections
                      if s.odds >= THRESHOLDS["min_odds"]]

        if not selections:
            return AnalyzedBetGroup(bet_group=group, house_margin=0.0)

        # Calcul de la marge de la maison
        implied_probs = [1.0 / s.odds for s in selections]
        total_implied = sum(implied_probs)
        house_margin = (total_implied - 1.0) * 100  # En pourcentage

        # Rejeter les marches malformes (marge negative = cotes incohérentes)
        if house_margin < 0:
            return AnalyzedBetGroup(bet_group=group, house_margin=0.0)

        # Probabilités corrigées (sans la marge)
        if total_implied > 0:
            fair_probs = [p / total_implied for p in implied_probs]
        else:
            fair_probs = [1.0 / len(selections)] * len(selections)

        ag = AnalyzedBetGroup(bet_group=group, house_margin=house_margin)

        for i, sel in enumerate(selections):
            implied_prob = implied_probs[i]
            fair_prob = fair_probs[i]

            # Score de valeur : combinaison de plusieurs facteurs
            value_score = self._compute_value_score(
                odds=sel.odds,
                implied_prob=implied_prob,
                fair_prob=fair_prob,
                house_margin=house_margin,
                n_selections=len(selections),
            )

            edge = fair_prob - implied_prob  # Positif = sous-coté

            recommendation = self._classify(value_score, house_margin)

            analyzed_sel = AnalyzedSelection(
                selection=sel,
                implied_prob=implied_prob,
                fair_prob=fair_prob,
                edge=edge,
                value_score=value_score,
                recommendation=recommendation,
            )
            ag.selections.append(analyzed_sel)

        if ag.selections:
            ag.best_value = max(ag.selections, key=lambda x: x.value_score)

        return ag

    def _compute_value_score(
        self,
        odds: float,
        implied_prob: float,
        fair_prob: float,
        house_margin: float,
        n_selections: int,
    ) -> float:
        """
        Calcule un score de valeur entre 0 et 100.

        Critères :
        1. Marge de la maison (plus basse = mieux)
        2. Équilibre des cotes (marché équilibré = plus fiable)
        3. Zone de cotes optimale (1.5 - 3.5 = meilleur rapport risque/gain)
        4. Rapport fair_prob / implied_prob
        """
        score = 50.0  # Score de base

        # 1. Bonus/malus selon la marge de la maison
        if house_margin < THRESHOLDS["house_margin_low"]:
            score += 20
        elif house_margin < THRESHOLDS["house_margin_medium"]:
            score += 10
        elif house_margin > 15:
            score -= 20
        elif house_margin > 10:
            score -= 10

        # 2. Zone de cotes optimale
        if 1.50 <= odds <= 3.50:
            score += 15
        elif 3.50 < odds <= 6.00:
            score += 5
        elif odds < 1.30:
            score -= 15  # Trop favoris
        elif odds > 8.0:
            score -= 10  # Trop risqué

        # 3. Marché à 2 issues (plus fiable)
        if n_selections == 2:
            score += 10
        elif n_selections == 3:
            score += 5
        elif n_selections > 6:
            score -= 10

        # 4. Rapport fair/implied (sélections "sous-cotées")
        ratio = fair_prob / max(implied_prob, 0.001)
        if ratio > 1.05:
            score += 10  # Légèrement sous-coté vs marché
        elif ratio < 0.95:
            score -= 10  # Sur-coté

        # Normaliser entre 0 et 100
        return max(0.0, min(100.0, score))

    def _classify(self, value_score: float, house_margin: float) -> str:
        """Classifie une selection en recommandation."""
        if value_score >= THRESHOLDS["value_excellent"] and house_margin < 8:
            return "Excellent ***"
        elif value_score >= THRESHOLDS["value_good"]:
            return "Bon **"
        elif value_score >= THRESHOLDS["value_neutral"]:
            return "Neutre *"
        else:
            return "Eviter"

    def get_top_opportunities(
        self,
        analyzed_matches: list[AnalyzedMatch],
        n: int = 10,
        sport_filter: Optional[str] = None,
        include_eviter: bool = False,
        raw_matches: list | None = None,
    ) -> list[BettingOpportunity]:
        """
        Extrait les N meilleures opportunites de paris.

        Filtres appliqués pour réduire les pertes corrélées :
          1. Exclure les marchés par période (trop volatils).
          2. Par match + catégorie de marché, garder la sélection la plus confiante
             seulement (élimine les paris contradictoires des deux côtés).
          3. Maximum 2 paris Excellent par match.
        """
        # Mots-clés indiquant un marché de période (ex: "2e période - Gagnant")
        _PERIOD_KW = ("1re période", "2e période", "3e période",
                      "1re p", "2e p", "3e p", "période")

        def _is_period_market(bt: str) -> bool:
            bt_l = bt.lower()
            return any(k in bt_l for k in _PERIOD_KW)

        def _market_cat(bt: str) -> str:
            bt_l = bt.lower()
            if any(k in bt_l for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
                return "winner"
            if any(k in bt_l for k in ("total", "buts", "plus/moins")):
                return "total"
            if any(k in bt_l for k in ("2 équipes", "les 2", "both")):
                return "btts"
            return bt_l  # catégorie brute pour les autres

        opportunities = []

        # En mode include_eviter, reconstruire les AnalyzedMatch sans le filtre top_picks
        if include_eviter and raw_matches:
            analyzed_matches = [self._analyze_match(m) for m in raw_matches
                                if not sport_filter or m.sport == sport_filter]

        for am in analyzed_matches:
            match = am.match
            if sport_filter and match.sport != sport_filter:
                continue

            for ag in am.analyzed_groups:
                # ── Correction 1 : ignorer les marchés par période ─────────────
                if _is_period_market(ag.bet_group.bet_type):
                    continue

                for sel in ag.selections:
                    if sel.recommendation == "Eviter" and not include_eviter:
                        continue
                    if sel.selection.odds < THRESHOLDS["min_odds"]:
                        continue
                    if sel.selection.odds > THRESHOLDS["max_odds"]:
                        continue

                    # Ajuster fair_prob avec les stats réelles (NHL ou NBA)
                    stats_mod = _get_stats_module(match.sport)
                    adjusted_fp = stats_mod.get_adjusted_prob(
                        home_team=match.home_team,
                        away_team=match.away_team,
                        bet_type=ag.bet_group.bet_type,
                        selection=sel.selection.label,
                        math_prob=sel.fair_prob,
                        match_date=match.date,
                    )

                    opp = BettingOpportunity(
                        match=match,
                        bet_type=ag.bet_group.bet_type,
                        selection_label=sel.selection.label,
                        odds=sel.selection.odds,
                        prediction_id=sel.selection.prediction_id,
                        value_score=sel.value_score,
                        recommendation=sel.recommendation,
                        house_margin=ag.house_margin,
                        fair_prob=adjusted_fp,
                        implied_prob=sel.implied_prob,
                        sport=match.sport,
                        league=match.league,
                        math_prob=sel.fair_prob,
                    )
                    opportunities.append(opp)

        # Garder seulement les matchs d'aujourd'hui et du passé (pas de demain)
        # En mode include_eviter, inclure aussi demain (matchs du soir décalés par UTC)
        from datetime import date as _date, timedelta as _td
        today = _date.today().isoformat()
        cutoff = (_date.today() + _td(days=1)).isoformat() if include_eviter else today
        opportunities = [o for o in opportunities
                         if (o.match.date or "9999-99-99") <= cutoff]

        # Charger les multiplicateurs historiques par type de pari
        try:
            from predictions import get_bet_type_multipliers, classify_bet_type
            _bt_mult = get_bet_type_multipliers(sport=sport_filter)
        except Exception:
            _bt_mult = {}
            def classify_bet_type(bt, h="", a=""):
                return ""

        def sort_key(o):
            d = o.match.date or "9999-99-99"
            priority = 0 if d == today else 2
            rec_priority = 0 if "Excellent" in o.recommendation else (1 if "Bon" in o.recommendation else 2)
            cat  = classify_bet_type(o.bet_type, o.match.home_team or "", o.match.away_team or "")
            mult = _bt_mult.get(cat, 1.0)
            return (priority, d, rec_priority, -(o.fair_prob * mult))

        opportunities.sort(key=sort_key)

        # ── Correction 2 : une seule sélection par match + catégorie de marché ─
        # Garde la plus confiante (fair_prob le plus élevé) — élimine les paris
        # contradictoires (ex: parier Colorado ET Pittsburgh sur le même match).
        seen_market: dict[tuple, BettingOpportunity] = {}
        deduped: list[BettingOpportunity] = []
        for opp in opportunities:
            match_key = (
                opp.match.date,
                (opp.match.home_team or "").lower(),
                (opp.match.away_team or "").lower(),
                _market_cat(opp.bet_type),
            )
            if match_key not in seen_market:
                seen_market[match_key] = opp
                deduped.append(opp)
            # Si déjà vu → l'entrée existante a déjà le fair_prob le plus élevé
            # (la liste est triée par -fair_prob avant cette étape)

        # ── Correction 3 : max 2 paris Excellent par match ────────────────────
        # + plafond de variété sur les Neutre uniquement (Excellent/Bon : pas de cap)
        match_excellent_count: dict[str, int] = {}
        neutre_type_count: dict[str, int] = {}
        neutre_cap = max(2, n // 6)
        final: list[BettingOpportunity] = []
        for opp in deduped:
            mk = f"{opp.match.date}|{(opp.match.home_team or '').lower()}|{(opp.match.away_team or '').lower()}"
            if "Excellent" in opp.recommendation:
                if match_excellent_count.get(mk, 0) >= 2:
                    continue
                match_excellent_count[mk] = match_excellent_count.get(mk, 0) + 1
            # Variété : limiter uniquement les Neutre par type de pari
            elif "Neutre" in opp.recommendation:
                cat = classify_bet_type(opp.bet_type, opp.match.home_team or "", opp.match.away_team or "")
                if neutre_type_count.get(cat, 0) >= neutre_cap:
                    continue
                neutre_type_count[cat] = neutre_type_count.get(cat, 0) + 1
            final.append(opp)

        return final[:n]
