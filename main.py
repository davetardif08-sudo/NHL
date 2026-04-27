"""
Mise-O-Jeu Analyzer -- Point d'entree principal.

Usage :
    python main.py                    # Analyse complete hockey + football
    python main.py --hockey           # Hockey seulement
    python main.py --football         # Football seulement
    python main.py --top 15           # Afficher le top 15 (defaut: 10)
    python main.py --detail           # Afficher le detail par match
    python main.py --visible          # Ouvrir le navigateur (debug)
    python main.py --demo             # Mode demo sans scraper (donnees exemple)
"""

import argparse
import sys
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

console = Console()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Analyseur de paris sportifs — Mise-O-Jeu (Loto-Québec)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--hockey",   action="store_true", help="Analyser hockey seulement")
    parser.add_argument("--football", action="store_true", help="Analyser football seulement")
    parser.add_argument("--top",      type=int, default=10, help="Nombre de paris à afficher (défaut: 10)")
    parser.add_argument("--detail",   action="store_true", help="Afficher le détail par match")
    parser.add_argument("--visible",  action="store_true", help="Navigateur visible (mode debug)")
    parser.add_argument("--demo",     action="store_true", help="Mode démo avec données d'exemple")
    return parser.parse_args()


# ─── Données de démo ──────────────────────────────────────────────────────────

def generate_demo_data():
    """Génère des données d'exemple réalistes."""
    from scraper import Match, BetGroup, Selection

    demo_matches = [
        # ── Hockey ─────────────────────────────────────────────────────────
        Match(
            sport="hockey", league="NHL",
            home_team="Montréal", away_team="Toronto",
            date="2026-03-15", time="19:00", event_id="demo1",
            event_url="https://miseojeuplus.espacejeux.com/sports/fr/hockey/amerique-du-nord/nhl/",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues (prolongation incluse)",
                    selections=[
                        Selection(label="Montréal", odds=2.20, prediction_id="62707"),
                        Selection(label="Toronto",  odds=1.65, prediction_id="62708"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de buts – Plus/Moins",
                    selections=[
                        Selection(label="Plus de 5.5", odds=1.85, prediction_id="62709"),
                        Selection(label="Moins de 5.5", odds=1.95, prediction_id="62710"),
                    ]
                ),
                BetGroup(
                    bet_type="Gagnant du match – 3 issues",
                    selections=[
                        Selection(label="Montréal",   odds=2.55, prediction_id="62711"),
                        Selection(label="Nulle (AP)", odds=4.50, prediction_id="62712"),
                        Selection(label="Toronto",    odds=1.90, prediction_id="62713"),
                    ]
                ),
            ]
        ),
        Match(
            sport="hockey", league="NHL",
            home_team="Boston", away_team="New York Rangers",
            date="2026-03-15", time="19:30", event_id="demo2",
            event_url="https://miseojeuplus.espacejeux.com/sports/fr/hockey/amerique-du-nord/nhl/",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Boston",        odds=1.75, prediction_id="62720"),
                        Selection(label="NY Rangers",    odds=2.05, prediction_id="62721"),
                    ]
                ),
                BetGroup(
                    bet_type="1ère période – Gagnant",
                    selections=[
                        Selection(label="Boston gagne",   odds=2.10, prediction_id="62722"),
                        Selection(label="Nulle",          odds=2.30, prediction_id="62723"),
                        Selection(label="Rangers gagnent",odds=3.20, prediction_id="62724"),
                    ]
                ),
            ]
        ),
        Match(
            sport="hockey", league="NHL",
            home_team="Colorado", away_team="Vegas",
            date="2026-03-16", time="21:00", event_id="demo3",
            event_url="https://miseojeuplus.espacejeux.com/sports/fr/hockey/amerique-du-nord/nhl/",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Colorado", odds=1.90, prediction_id="62730"),
                        Selection(label="Vegas",    odds=1.90, prediction_id="62731"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de buts",
                    selections=[
                        Selection(label="Plus de 6.0",  odds=1.90, prediction_id="62732"),
                        Selection(label="Moins de 6.0", odds=1.90, prediction_id="62733"),
                    ]
                ),
            ]
        ),
        Match(
            sport="hockey", league="NHL",
            home_team="Tampa Bay", away_team="Florida",
            date="2026-03-16", time="19:00", event_id="demo4",
            event_url="https://miseojeuplus.espacejeux.com/sports/fr/hockey/amerique-du-nord/nhl/",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Tampa Bay", odds=1.55, prediction_id="62740"),
                        Selection(label="Florida",   odds=2.45, prediction_id="62741"),
                    ]
                ),
            ]
        ),
        Match(
            sport="hockey", league="NHL",
            home_team="Edmonton", away_team="Calgary",
            date="2026-03-17", time="21:00", event_id="demo5",
            event_url="https://miseojeuplus.espacejeux.com/sports/fr/hockey/amerique-du-nord/nhl/",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Edmonton", odds=1.80, prediction_id="62750"),
                        Selection(label="Calgary",  odds=2.00, prediction_id="62751"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de buts",
                    selections=[
                        Selection(label="Plus de 5.5",  odds=1.80, prediction_id="62752"),
                        Selection(label="Moins de 5.5", odds=2.00, prediction_id="62753"),
                    ]
                ),
            ]
        ),
        # ── Football ───────────────────────────────────────────────────────
        Match(
            sport="football", league="NFL",
            home_team="Kansas City Chiefs", away_team="Buffalo Bills",
            date="2026-03-20", time="20:00", event_id="demo6",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match",
                    selections=[
                        Selection(label="Kansas City",  odds=1.70, prediction_id="45481"),
                        Selection(label="Buffalo Bills", odds=2.15, prediction_id="45482"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de points – Plus/Moins 47.5",
                    selections=[
                        Selection(label="Plus de 47.5 pts",  odds=1.88, prediction_id="45483"),
                        Selection(label="Moins de 47.5 pts", odds=1.92, prediction_id="45484"),
                    ]
                ),
            ]
        ),
        Match(
            sport="football", league="NFL",
            home_team="Los Angeles Rams", away_team="San Francisco 49ers",
            date="2026-03-21", time="16:30", event_id="demo7",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match",
                    selections=[
                        Selection(label="LA Rams",     odds=2.10, prediction_id="45490"),
                        Selection(label="SF 49ers",    odds=1.72, prediction_id="45491"),
                    ]
                ),
            ]
        ),
        Match(
            sport="football", league="NFL",
            home_team="Detroit Lions", away_team="Dallas Cowboys",
            date="2026-03-22", time="13:00", event_id="demo8",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match",
                    selections=[
                        Selection(label="Detroit",  odds=1.85, prediction_id="45500"),
                        Selection(label="Dallas",   odds=1.95, prediction_id="45501"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de points – Plus/Moins 51.5",
                    selections=[
                        Selection(label="Plus de 51.5",  odds=1.90, prediction_id="45502"),
                        Selection(label="Moins de 51.5", odds=1.90, prediction_id="45503"),
                    ]
                ),
            ]
        ),
    ]
    return demo_matches


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    # Si ni --hockey ni --football, analyser les deux
    do_hockey   = args.hockey or (not args.hockey and not args.football)
    do_football = args.football or (not args.hockey and not args.football)

    from display import (
        print_header, print_top_opportunities,
        print_match_detail, print_summary, print_legend,
    )
    from analyzer import OddsAnalyzer
    from scraper import scrape_all_sync

    print_header()

    # ── Chargement des données ─────────────────────────────────────────────
    all_matches = []

    if args.demo:
        console.print("[bold yellow]Mode DÉMO[/bold yellow] — données d'exemple\n")
        all_matches = generate_demo_data()
    else:
        console.print("[bold]Scraping de miseojeu.lotoquebec.com…[/bold]\n")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Chargement des pages en cours…", total=None)
            try:
                all_matches = scrape_all_sync(headless=not args.visible)
            except Exception as e:
                progress.stop()
                console.print(f"\n[red]Erreur lors du scraping:[/red] {e}")
                console.print("[yellow]Astuce : Essayez --demo pour tester avec des données d'exemple[/yellow]")
                sys.exit(1)
            progress.update(task, description="Scraping terminé ✓")

    if not all_matches:
        console.print("[red]Aucun match trouvé.[/red]")
        console.print("[dim]Conseil : lancez avec --demo pour voir un exemple de l'analyse[/dim]")
        sys.exit(0)

    console.print(f"\n[green]{len(all_matches)} match(es) chargé(s)[/green]\n")

    # ── Analyse ───────────────────────────────────────────────────────────
    analyzer = OddsAnalyzer()
    analyzed_matches = analyzer.analyze_matches(all_matches)

    # ── Filtrage par sport ────────────────────────────────────────────────
    hockey_opps   = analyzer.get_top_opportunities(analyzed_matches, n=args.top, sport_filter="hockey")   if do_hockey   else []
    football_opps = analyzer.get_top_opportunities(analyzed_matches, n=args.top, sport_filter="football") if do_football else []
    all_opps      = hockey_opps + football_opps
    all_opps.sort(key=lambda x: x.value_score, reverse=True)

    # ── Affichage des résultats ───────────────────────────────────────────
    if do_hockey:
        print_top_opportunities(
            hockey_opps,
            title=f"Top {args.top} Paris — Hockey 🏒",
            sport_filter="hockey",
        )

    if do_football:
        print_top_opportunities(
            football_opps,
            title=f"Top {args.top} Paris — Football 🏈",
            sport_filter="football",
        )

    if do_hockey and do_football:
        best_overall = sorted(all_opps, key=lambda x: x.value_score, reverse=True)[:5]
        print_top_opportunities(
            best_overall,
            title="Top 5 — Meilleurs Paris Tous Sports",
        )

    # ── Détail des matchs ─────────────────────────────────────────────────
    if args.detail:
        console.print("\n[bold]Détail par match :[/bold]\n")
        for am in analyzed_matches:
            if am.top_picks:
                print_match_detail(am)

    # ── Résumé et légende ─────────────────────────────────────────────────
    print_summary(hockey_opps, football_opps, len(all_matches))
    print_legend()

    console.print("[dim]⚠  Jouer comporte des risques. Jouez de façon responsable.[/dim]\n")


if __name__ == "__main__":
    main()
