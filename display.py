"""
Interface d'affichage en ligne de commande.
Utilise Rich pour un rendu colore et structure.
"""
import sys
import io

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from analyzer import AnalyzedMatch, BettingOpportunity, OddsAnalyzer
from scraper import Match

# Force UTF-8 sur Windows pour eviter les erreurs d'encodage
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

console = Console(highlight=False, width=180)


# --- Couleurs par recommandation ---------------------------------------------

REC_STYLE = {
    "Excellent ***": "bold green",
    "Bon **":        "green",
    "Neutre *":      "yellow",
    "Eviter":        "dim red",
}

SPORT_LABEL = {
    "hockey":   "HOC",
    "football": "FOO",
}


def _odds_color(odds: float) -> str:
    if odds <= 1.50:
        return "cyan"
    elif odds <= 2.50:
        return "bright_white"
    elif odds <= 4.00:
        return "yellow"
    else:
        return "bright_yellow"


def _margin_color(margin: float) -> str:
    if margin < 5:
        return "green"
    elif margin < 8:
        return "yellow"
    else:
        return "red"


def _score_bar(score: float, width: int = 10) -> str:
    """Barre de progression ASCII simple."""
    filled = int(score / 100 * width)
    return "#" * filled + "-" * (width - filled)


# --- Affichage principal -----------------------------------------------------

def print_header():
    console.print()
    console.print(Panel.fit(
        "[bold blue]Mise-O-Jeu[/bold blue] [white]-- Analyseur de Paris Sportifs[/white]\n"
        "[dim]Hockey [HOC]  |  Football [FOO]  |  miseojeu.lotoquebec.com[/dim]",
        border_style="blue",
        padding=(1, 4),
    ))
    console.print()


def print_top_opportunities(
    opportunities: list[BettingOpportunity],
    title: str = "Meilleures Opportunites",
    sport_filter: str = None,
):
    """Affiche le tableau des meilleures opportunites."""
    if not opportunities:
        console.print(Panel(
            "[yellow]Aucune opportunite trouvee.[/yellow]\n"
            "[dim]Verifiez votre connexion ou reessayez plus tard.[/dim]",
            title=title,
            border_style="yellow",
        ))
        return

    table = Table(
        title=title,
        box=box.ROUNDED,
        border_style="blue",
        header_style="bold cyan",
        show_lines=True,
        padding=(0, 1),
    )

    table.add_column("#",             style="dim",        width=3,  justify="right")
    table.add_column("Match",         style="white",      min_width=24)
    table.add_column("Ligue",         style="dim cyan",   width=8)
    table.add_column("Type de pari",  style="white",      min_width=20)
    table.add_column("Selection",     style="bold",       min_width=15)
    table.add_column("Cote",          justify="center",   width=6)
    table.add_column("Marge",         justify="center",   width=7)
    table.add_column("Score",         justify="center",   width=13)
    table.add_column("Avis",          justify="center",   width=14)
    table.add_column("ID Pred.",      style="dim",        width=7)

    for i, opp in enumerate(opportunities, 1):
        rec_style  = REC_STYLE.get(opp.recommendation, "white")
        odds_style = _odds_color(opp.odds)
        marg_style = _margin_color(opp.house_margin)
        bar        = _score_bar(opp.value_score)
        sport_lbl  = SPORT_LABEL.get(opp.sport, "???")

        match_str  = f"{sport_lbl} {opp.display_match}"
        date_str   = opp.display_date
        bet_type   = opp.bet_type[:27] + ("..." if len(opp.bet_type) > 27 else "")
        sel_label  = opp.selection_label[:20]

        match_cell = Text()
        match_cell.append(match_str, style="white")
        match_cell.append(f"\n{date_str}", style="dim")

        table.add_row(
            str(i),
            match_cell,
            opp.league,
            bet_type,
            sel_label,
            f"[{odds_style}]{opp.odds:.2f}[/{odds_style}]",
            f"[{marg_style}]{opp.house_margin:.1f}%[/{marg_style}]",
            f"[dim]{bar}[/dim] [bold]{opp.value_score:.0f}[/bold]",
            f"[{rec_style}]{opp.recommendation}[/{rec_style}]",
            opp.prediction_id or "--",
        )

    console.print(table)
    console.print()


def print_match_detail(am: AnalyzedMatch):
    """Affiche le detail d'un match analyse."""
    match = am.match
    sport_lbl = SPORT_LABEL.get(match.sport, "???")
    title    = f"[{sport_lbl}] {match.away_team} @ {match.home_team}  [{match.league}]"
    subtitle = f"[dim]{match.date} {match.time}[/dim]"

    content = Text()
    content.append(subtitle + "\n\n")

    for ag in am.analyzed_groups:
        if not ag.selections:
            continue

        content.append(f"  {ag.bet_group.bet_type}\n", style="bold cyan")
        content.append("  Marge maison: ", style="dim")
        content.append(f"{ag.house_margin:.1f}%\n", style=_margin_color(ag.house_margin))
        content.append("\n")

        for sel in sorted(ag.selections, key=lambda x: x.value_score, reverse=True):
            rec_style = REC_STYLE.get(sel.recommendation, "white")
            bar = _score_bar(sel.value_score, width=8)
            content.append(
                f"    [{bar}] {sel.value_score:.0f}  "
                f"{sel.selection.label:<18}  "
                f"Cote: {sel.selection.odds:.2f}  "
                f"({sel.fair_prob*100:.1f}% juste)  ",
                style="white",
            )
            content.append(f"{sel.recommendation}\n", style=rec_style)

        content.append("\n")

    console.print(Panel(content, title=title, border_style="blue"))


def print_summary(
    hockey_opps: list[BettingOpportunity],
    football_opps: list[BettingOpportunity],
    total_matches: int,
):
    """Affiche un resume de la session."""
    excellent_h = sum(1 for o in hockey_opps   if "Excellent" in o.recommendation)
    excellent_f = sum(1 for o in football_opps if "Excellent" in o.recommendation)

    text = (
        f"[bold]Matchs analyses :[/bold] {total_matches}    "
        f"[bold]Hockey :[/bold] {len(hockey_opps)} paris  (dont {excellent_h} Excellent)    "
        f"[bold]Football :[/bold] {len(football_opps)} paris  (dont {excellent_f} Excellent)"
    )
    console.print(Panel(text, title="Resume", border_style="green"))
    console.print()


def print_legend():
    """Affiche la legende des scores et recommandations."""
    legend = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    legend.add_column(style="bold")
    legend.add_column()

    legend.add_row("[bold green]Excellent ***[/bold green]",
                   "Score >= 70, faible marge maison -- Paris fortement recommande")
    legend.add_row("[green]Bon **[/green]",
                   "Score 50-69 -- Paris interessant a considerer")
    legend.add_row("[yellow]Neutre *[/yellow]",
                   "Score 30-49 -- Ni bon ni mauvais")
    legend.add_row("[dim red]Eviter[/dim red]",
                   "Score < 30 ou forte marge -- Non recommande")
    legend.add_row("[cyan]Marge maison[/cyan]",
                   "< 5% vert (ideal) | 5-8% jaune | > 8% rouge")
    legend.add_row("[white]Cote optimale[/white]",
                   "Zone 1.50 - 3.50 : meilleur rapport risque/rendement")

    console.print(Panel(legend, title="Legende", border_style="dim", padding=(0, 1)))
