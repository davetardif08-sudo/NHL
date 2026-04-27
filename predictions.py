"""
Suivi des prédictions passées pour calibrer les pourcentages futurs.

Flux :
  1. record_opportunity()     — sauvegarde chaque paris recommandé avec ses signaux
  2. update_outcomes()        — interroge api-web.nhle.com pour les résultats
  3. compute_calibration()    — compare prédit vs réel, calcule correction + poids
  4. get_feature_weights()    — retourne les poids optimaux appris de l'historique

Poids appris :
  - stat_vs_math   : quelle part accorder aux stats NHL vs probabilité mathématique
  - intra_stat     : quel poids donner à chaque statistique dans team_strength_score()
                     (win%, PP%, PK%, Corsi, tirs, mise en jeu)
"""

import copy
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PREDICTIONS_FILE = Path(__file__).parent / "predictions.json"
NHL_SCORE_URL    = "https://api-web.nhle.com/v1/score/{date}"
NBA_SCORE_URL    = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={date}"

# N'activer la correction que si on a au moins N résultats
MIN_OUTCOMES_FOR_CORRECTION = 20
MIN_SIGNAL_SAMPLES          = 8   # occurrences min avant d'utiliser un signal

# Mapping signal → clé de poids dans team_strength_score()
_SIGNAL_TO_WEIGHT_KEY = {
    "win_pct":          "base_score",
    "home_road_record": "base_score",
    "recent_form":      "base_score",
    "pp_pct":           "ppPct",
    "pk_pct":           "pkPct",
    "corsi":            "satPct",
    "shots_for":        "sfPG",
    "faceoff":          "foPct",
    "goals_for":        "gfPG",      # buts marqués/match → clé propre
    "goals_against":    "gaPG",      # buts encaissés/match → clé propre
    "jet_lag":          "jetLag",    # décalage horaire → clé propre (intermittent)
    "injury_net":       "injuryNet", # avantage net de blessures → clé propre
    "hot_player":       "hotPlayer", # avantage joueurs en feu → clé propre
    "goalie":           "_goalie",   # spécial : influence goalie_lambda_factor
    "math_odds":        "_math",     # spécial : influence stat_vs_math
}

# Poids par défaut (identiques à advanced_stats.py)
_DEFAULT_WEIGHTS = {
    "stat_vs_math":        0.55,
    "goalie_lambda_factor": 1.0,   # 1.0 = ajustement actuel, 0 = ignorer, 2 = doubler
    "intra_stat": {
        "base_score": 0.34,  # win%, record dom/route, forme récente
        "ppPct":      0.13,
        "pkPct":      0.10,
        "satPct":     0.13,
        "sfPG":       0.07,
        "foPct":      0.04,
        "gfPG":       0.08,  # buts marqués/match
        "gaPG":       0.08,  # buts encaissés/match
        "jetLag":     0.03,  # décalage horaire (intermittent → poids faible par défaut)
        "injuryNet":  0.05,  # avantage net de blessures (intermittent)
        "hotPlayer":  0.04,  # avantage joueurs en feu (intermittent)
    },
}

# Cache module-level pour éviter de relire le fichier à chaque pari
_fw_cache: dict | None = None
_fw_cache_ts: float    = 0.0
_FW_TTL                = 300.0   # 5 min

# Mapping signal NBA → clé de poids
_NBA_SIGNAL_TO_WEIGHT_KEY: dict[str, str] = {
    "win_pct":          "base_score",
    "home_road_record": "base_score",
    "recent_form":      "base_score",
    "home_win_pct":     "home_road",
    "road_win_pct":     "home_road",
    "points_for":       "pts_for",
    "points_against":   "pts_against",
    "net_rating":       "net_rating",
    "streak":           "streak",
    "seed_rank":        "seed_rank",
    "rest_advantage":   "rest",
    "injury_net":       "injury",
    "math_odds":        "_math",
}

# Poids NBA par défaut
_NBA_DEFAULT_WEIGHTS: dict = {
    "stat_vs_math":  0.55,
    "intra_stat": {
        "base_score":   0.22,
        "home_road":    0.10,
        "pts_for":      0.13,
        "pts_against":  0.13,
        "net_rating":   0.13,
        "streak":       0.08,
        "seed_rank":    0.08,
        "rest":         0.06,
        "injury":       0.07,
    },
}

# Cache NBA feature weights
_nba_fw_cache: dict | None = None
_nba_fw_ts: float = 0.0

# Correction de calibration NBA injectée depuis les snapshots (par app.py)
# Format: {"factor": float, "stat_vs_math_before": float, "stat_vs_math_after": float, "bins": [...]}
# _UNSET = pas encore calculé. None = calculé, aucune sur-estimation trouvée.
_NBA_CAL_UNSET = object()
_nba_cal_correction: object = _NBA_CAL_UNSET


def set_nba_calibration_correction(correction: dict | None) -> None:
    """Appelé depuis app.py après calcul de la correction depuis les snapshots.
    Invalide le cache des poids pour que la correction soit appliquée immédiatement.
    """
    global _nba_cal_correction, _nba_fw_cache, _nba_fw_ts
    _nba_cal_correction = correction
    _nba_fw_cache = None   # forcer recalcul
    _nba_fw_ts = 0.0


def get_nba_calibration_correction() -> object:
    """Retourne _NBA_CAL_UNSET si pas encore calculé, None si calculé sans sur-estimation,
    ou un dict avec la correction."""
    return _nba_cal_correction


def is_nba_calibration_set() -> bool:
    return _nba_cal_correction is not _NBA_CAL_UNSET


# ─── Persistance ──────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not PREDICTIONS_FILE.exists():
        return []
    try:
        with open(PREDICTIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(preds: list[dict]) -> None:
    """Sauvegarde preds dans predictions.json avec deux protections :
    1. Les picks RÉSOLUS (outcome != None) ne sont JAMAIS effacés.
    2. Si la nouvelle liste réduit le total de >20%, on fusionne les
       anciens picks non-résolus absents pour éviter les pertes accidentelles.
    """
    if PREDICTIONS_FILE.exists():
        try:
            existing = json.loads(PREDICTIONS_FILE.read_text(encoding="utf-8"))
            new_keys = {p["key"] for p in preds}

            # Protection 1 : toujours conserver les picks résolus absents de la nouvelle liste
            resolved_orphans = [p for p in existing
                                if p.get("outcome") is not None and p["key"] not in new_keys]
            if resolved_orphans:
                preds = preds + resolved_orphans
                new_keys = {p["key"] for p in preds}

            # Protection 2 : anti-écrasement 20% (non-résolus)
            if len(existing) > 10 and len(preds) < len(existing) * 0.8:
                import shutil, datetime as _dt
                bak = PREDICTIONS_FILE.with_suffix(
                    f".bak_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                )
                shutil.copy2(PREDICTIONS_FILE, bak)
                unresolved_orphans = [p for p in existing
                                      if p.get("outcome") is None and p["key"] not in new_keys]
                preds = preds + unresolved_orphans
        except Exception:
            pass
    with open(PREDICTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(preds, f, indent=2, ensure_ascii=False)


# ─── Backfill depuis les snapshots ────────────────────────────────────────────

def backfill_from_snapshots() -> dict:
    """
    Importe dans predictions.json tous les picks des fichiers snapshot passés
    qui n'y sont pas encore.  Idempotent : un pick déjà présent (même clé) est ignoré.
    Retourne {"added": N, "dates": [...]} pour le reporting.
    """
    import re as _re
    snapshots_dir = PREDICTIONS_FILE.parent / "snapshots"
    if not snapshots_dir.exists():
        return {"added": 0, "dates": []}

    today = date.today().isoformat()
    preds = _load()
    existing_keys = {p["key"] for p in preds}
    added = 0
    dates_done: list[str] = []

    # Détection du sport par surnom entre parenthèses
    def _detect_sport(team_name: str) -> str:
        try:
            from nhl_stats import _match_abbrev as nhl_ab
            from nba_stats import _match_abbrev as nba_ab
            m = _re.search(r'\(([^)]+)\)', team_name or "")
            if m:
                nick = m.group(1)
                if nhl_ab(nick) and not nba_ab(nick):
                    return "hockey"
                if nba_ab(nick) and not nhl_ab(nick):
                    return "basketball"
        except Exception:
            pass
        return "hockey"  # défaut : NHL pour nos snapshots

    for snap_file in sorted(snapshots_dir.glob("20*.json")):
        snap_date = snap_file.stem           # "2026-03-21"
        if snap_date >= today:
            continue                         # jamais traiter aujourd'hui ni futur

        try:
            with open(snap_file, encoding="utf-8-sig") as f:
                snap = json.load(f)
        except Exception:
            continue

        snap_added = 0
        for pick in snap.get("picks", []):
            # Récupérer home/away — peut être vide dans les vieux snapshots
            home = (pick.get("home_team") or "").strip()
            away = (pick.get("away_team") or "").strip()
            if not home or not away:
                # Parser depuis le champ match "Away @ Home"
                parts = (pick.get("match") or "").split(" @ ")
                if len(parts) == 2:
                    away, home = parts[0].strip(), parts[1].strip()
                else:
                    continue

            bet_type  = (pick.get("bet_type") or "").strip()
            selection = (pick.get("selection") or "").strip()
            if not bet_type or not selection:
                continue

            key = "|".join([
                snap_date,
                home.lower(),
                away.lower(),
                bet_type.lower(),
                selection.lower(),
            ])
            if key in existing_keys:
                continue

            # fair_prob est en % dans les snapshots, en 0-1 dans predictions.json
            fp_raw = float(pick.get("fair_prob") or 0)
            fp     = round(fp_raw / 100, 4) if fp_raw > 1 else round(fp_raw, 4)

            new_pred = {
                "key":            key,
                "date":           snap_date,
                "time":           pick.get("time"),
                "home_team":      home,
                "away_team":      away,
                "bet_type":       bet_type,
                "selection":      selection,
                "odds":           pick.get("odds"),
                "fair_prob":      fp,
                "value_score":    float(pick.get("value_score") or 0),
                "p_math":         fp,
                "signals":        pick.get("signals") or {},
                "recommendation": pick.get("recommendation"),
                "sport":          _detect_sport(home),
                "champion":       bool(pick.get("champion")),
                "mise":           pick.get("mise"),
                "outcome":        None,
                "saved_at":       snap.get("time") or snap_date,
                "locked_at":      None,
            }
            preds.append(new_pred)
            existing_keys.add(key)
            snap_added += 1
            added += 1

        if snap_added:
            dates_done.append(snap_date)

    if added:
        # Écriture directe (bypass de la protection de shrinkage — on ne réduit pas)
        with open(PREDICTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(preds, f, indent=2, ensure_ascii=False)
        print(f"  [backfill] {added} picks importés depuis snapshots : {dates_done}")

    return {"added": added, "dates": dates_done}


# ─── Enregistrement ───────────────────────────────────────────────────────────

def _extract_nba_signals(opp) -> dict:
    """Signaux booléens pour les paris NBA Gagnant."""
    try:
        import re
        from nba_stats import get_team_stats

        home_team = opp.match.home_team
        away_team = opp.match.away_team
        sel       = opp.selection_label.lower()

        home = get_team_stats(home_team)
        away = get_team_stats(away_team)
        if not home or not away:
            return {}

        home_n   = re.sub(r'\s*\(.*?\)', '', home_team.lower()).strip()
        away_n   = re.sub(r'\s*\(.*?\)', '', away_team.lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        if not home_sel:
            away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
            if not away_sel:
                return {}

        sel_team = home if home_sel else away
        opp_team = away if home_sel else home

        sel_l10 = sel_team["l10Wins"] / max(sel_team.get("l10GP", 10), 1)
        opp_l10 = opp_team["l10Wins"] / max(opp_team.get("l10GP", 10), 1)

        sel_net = sel_team.get("netRating", sel_team.get("ptsPG", 0) - sel_team.get("oppPtsPG", 0))
        opp_net = opp_team.get("netRating", opp_team.get("ptsPG", 0) - opp_team.get("oppPtsPG", 0))

        signals: dict = {
            "is_home":          home_sel,
            "win_pct":          sel_team["winPct"] > opp_team["winPct"],
            "home_road_record": (
                sel_team["homeWinPct"] > opp_team["roadWinPct"] if home_sel
                else sel_team["roadWinPct"] > opp_team["homeWinPct"]
            ),
            "home_win_pct":     sel_team["homeWinPct"] > 0.5 if home_sel else sel_team["roadWinPct"] > 0.5,
            "road_win_pct":     opp_team["roadWinPct"] < 0.5 if home_sel else opp_team["homeWinPct"] < 0.5,
            "recent_form":      sel_l10 > opp_l10,
            "points_for":       sel_team.get("ptsPG", 0) > opp_team.get("ptsPG", 0),
            "points_against":   sel_team.get("oppPtsPG", 999) < opp_team.get("oppPtsPG", 999),
            "net_rating":       sel_net > opp_net,
            "streak":           (
                sel_team.get("streakCode") == "W" and opp_team.get("streakCode") == "L"
            ) or (
                sel_team.get("streakCode") == "W"
                and sel_team.get("streakCount", 0) > opp_team.get("streakCount", 0)
            ),
            "seed_rank":        sel_team.get("leagueSeq", 99) < opp_team.get("leagueSeq", 99),
        }

        # Probabilité mathématique brute
        p_math = getattr(opp, "math_prob", opp.fair_prob)
        signals["math_odds"] = p_math > 0.5

        # Back-to-back (conditionnel — ajouté seulement si pertinent)
        try:
            from nba_stats import get_back_to_back_info
            match_date = getattr(opp.match, "date", None) or getattr(opp.match, "game_date", None)
            if match_date:
                b2b = get_back_to_back_info(home_team, away_team, str(match_date)[:10])
                if b2b["rest_adv"] != 0.0:
                    # True si l'équipe sélectionnée bénéficie de l'avantage repos
                    signals["rest_advantage"] = (b2b["rest_adv"] > 0) == home_sel
        except Exception:
            pass

        # Blessures (conditionnel — ajouté seulement si déséquilibre significatif)
        try:
            from nba_stats import get_injury_advantage
            inj = get_injury_advantage(home_team, away_team)
            if abs(inj["injury_adv"]) >= 0.25:
                # True si l'équipe sélectionnée est moins blessée
                signals["injury_net"] = (inj["injury_adv"] > 0) == home_sel
        except Exception:
            pass

        return signals
    except Exception:
        return {}


def _extract_signals(opp) -> dict:
    """
    Extrait des signaux booléens pour un pari sur le gagnant du match.
    Signal = True si l'indicateur prédit que l'équipe sélectionnée va gagner.
    Retourne {} pour les marchés non-gagnant (total de buts, etc.).
    """
    bt = opp.bet_type.lower()
    if not any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        return {}

    # Détection NBA vs Hockey
    sport = getattr(opp.match, 'sport', None)
    if sport == 'basketball':
        return _extract_nba_signals(opp)

    try:
        import re
        from nhl_stats import get_team_stats

        home_team = opp.match.home_team
        away_team = opp.match.away_team
        sel       = opp.selection_label.lower()

        home = get_team_stats(home_team)
        away = get_team_stats(away_team)
        if not home or not away:
            return {}

        home_n   = re.sub(r'\s*\(.*?\)', '', home_team.lower()).strip()
        away_n   = re.sub(r'\s*\(.*?\)', '', away_team.lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        if not home_sel:
            away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
            if not away_sel:
                return {}

        sel_team = home if home_sel else away
        opp_team = away if home_sel else home
        sel_name = home_team if home_sel else away_team
        opp_name = away_team if home_sel else home_team

        sel_l10 = sel_team["l10Wins"] / max(sel_team["l10GP"], 1)
        opp_l10 = opp_team["l10Wins"] / max(opp_team["l10GP"], 1)

        signals: dict = {
            "is_home":          home_sel,           # indicateur contextuel, pas prédicteur
            "win_pct":          sel_team["winPct"] > opp_team["winPct"],
            "home_road_record": (
                sel_team["homeWinPct"] > opp_team["roadWinPct"] if home_sel
                else sel_team["roadWinPct"] > opp_team["homeWinPct"]
            ),
            "recent_form":      sel_l10 > opp_l10,
            "goals_for":        sel_team["gfPG"] > opp_team["gfPG"],
            "goals_against":    sel_team["gaPG"] < opp_team["gaPG"],  # < = meilleure défense
        }

        # Stats avancées (PP%, PK%, Corsi, tirs, mise en jeu)
        try:
            from advanced_stats import get_advanced_stats
            sel_adv = get_advanced_stats(sel_name)
            opp_adv = get_advanced_stats(opp_name)

            if sel_adv.get("ppPct") and opp_adv.get("ppPct"):
                signals["pp_pct"] = sel_adv["ppPct"] > opp_adv["ppPct"]
            if sel_adv.get("pkPct") and opp_adv.get("pkPct"):
                signals["pk_pct"] = sel_adv["pkPct"] > opp_adv["pkPct"]
            # Corsi : préférer xGF% > satPct > cfPct
            c_sel = sel_adv.get("xgfPct") or sel_adv.get("satPct") or sel_adv.get("cfPct")
            c_opp = opp_adv.get("xgfPct") or opp_adv.get("satPct") or opp_adv.get("cfPct")
            if c_sel and c_opp:
                signals["corsi"] = c_sel > c_opp
            if sel_adv.get("sfPG") and opp_adv.get("sfPG"):
                signals["shots_for"] = sel_adv["sfPG"] > opp_adv["sfPG"]
            if sel_adv.get("foPct") and opp_adv.get("foPct"):
                signals["faceoff"] = sel_adv["foPct"] > opp_adv["foPct"]
        except Exception:
            pass

        # Décalage horaire : l'équipe adverse souffre-t-elle du jet lag (voyage est)?
        try:
            from extra_stats import get_timezone_diff
            tz_diff = get_timezone_diff(away_team, home_team)
            # tz_diff <= -2 : away_team a voyagé ≥2 fuseaux vers l'est → pénalisée
            if tz_diff <= -2:
                signals["jet_lag"] = home_sel     # True si on mise sur l'équipe locale (qui bénéficie)
            elif tz_diff >= 2:
                signals["jet_lag"] = not home_sel # True si on mise sur l'équipe visiteuse (voyage ouest = neutre)
        except Exception:
            pass

        # Gardien partant : le gardien de l'équipe sélectionnée est-il meilleur?
        try:
            from extra_stats import get_starting_goalie
            hg   = get_starting_goalie(home_team)
            ag_g = get_starting_goalie(away_team)
            sel_goalie = hg   if home_sel else ag_g
            opp_goalie = ag_g if home_sel else hg
            if (sel_goalie and opp_goalie
                    and sel_goalie.get("svPct") and opp_goalie.get("svPct")):
                signals["goalie"] = sel_goalie["svPct"] > opp_goalie["svPct"]
        except Exception:
            pass

        # Avantage net de blessures : uniquement les joueurs clés
        # (goalie_starter, forward_tier1, defense_tier1 — exclut forward_tier2)
        try:
            from injuries import get_team_injuries
            _KEY_TIERS = {"goalie_starter", "forward_tier1", "defense_tier1"}
            def _key_injury_delta(team_name: str) -> float:
                injs = get_team_injuries(team_name)
                delta = 0.0
                for inj in injs:
                    if inj.get("tier") in _KEY_TIERS:
                        delta += inj.get("impact", {}).get("win_prob", 0.0)
                return delta
            sel_delta = _key_injury_delta(sel_name)
            opp_delta = _key_injury_delta(opp_name)
            if sel_delta != 0.0 or opp_delta != 0.0:
                signals["injury_net"] = sel_delta > opp_delta   # moins pénalisé = True
        except Exception:
            pass

        # Avantage joueurs en feu : notre équipe a-t-elle plus de joueurs chauds ?
        try:
            from injuries import get_hot_players
            sel_hot = len(get_hot_players(sel_name))
            opp_hot = len(get_hot_players(opp_name))
            if sel_hot > 0 or opp_hot > 0:
                signals["hot_player"] = sel_hot > opp_hot
        except Exception:
            pass

        # Probabilité mathématique brute (marché favorise-t-il cette sélection?)
        p_math = getattr(opp, "math_prob", opp.fair_prob)
        signals["math_odds"] = p_math > 0.5

        return signals
    except Exception:
        return {}


def _compute_feature_accuracies(preds: list[dict],
                                 min_samples: int = MIN_SIGNAL_SAMPLES) -> dict[str, dict]:
    """
    Pour chaque signal booléen, calcule son taux de réussite.
    N'utilise que les prédictions Excellentes avec résultat connu.
    Ne comptabilise que les cas où le signal "vote" pour la sélection (signal=True).
    Retourne seulement les signaux avec au moins min_samples occurrences.
    """
    done = [p for p in preds
            if p.get("outcome") in ("win", "loss")
            and p.get("signals")
            and _is_excellent(p)]

    stats: dict[str, dict] = {}
    for pred in done:
        outcome_win = pred["outcome"] == "win"
        for sig_name, sig_value in pred["signals"].items():
            if sig_name == "is_home" or not isinstance(sig_value, bool):
                continue
            if not sig_value:
                continue  # ne compter que quand le signal dit "cette équipe va gagner"
            if sig_name not in stats:
                stats[sig_name] = {"total": 0, "correct": 0}
            stats[sig_name]["total"] += 1
            if outcome_win:
                stats[sig_name]["correct"] += 1

    result = {}
    for name, s in stats.items():
        if s["total"] < min_samples:
            continue
        acc = s["correct"] / s["total"]
        # weight_contribution : 0 si aléatoire (50%), 1.0 si parfait (100%)
        contrib = round(max(0.0, (acc - 0.5) * 2), 4)
        result[name] = {
            "n":                   s["total"],
            "total":               s["total"],
            "correct":             s["correct"],
            "accuracy":            round(acc * 100, 1),
            "weight_contribution": contrib,
        }
    return result


def get_feature_weights() -> dict:
    """
    Retourne les poids optimaux appris de l'historique des prédictions :
      - stat_vs_math  : part des stats NHL (vs probabilité mathématique des cotes)
      - intra_stat    : poids de chaque stat dans team_strength_score()

    N'est actif qu'après MIN_OUTCOMES_FOR_CORRECTION prédictions avec signaux.
    Retourne les poids par défaut sinon.
    Met en cache le résultat 5 minutes pour éviter de relire le fichier constamment.
    """
    import copy
    global _fw_cache, _fw_cache_ts

    if _fw_cache is not None and time.time() - _fw_cache_ts < _FW_TTL:
        return _fw_cache

    weights = copy.deepcopy(_DEFAULT_WEIGHTS)

    try:
        preds          = _load()
        # Seulement les Excellentes avec résultat et signaux
        with_signals   = [p for p in preds
                          if p.get("outcome") in ("win", "loss")
                          and p.get("signals")
                          and _is_excellent(p)]
        if len(with_signals) < MIN_OUTCOMES_FOR_CORRECTION:
            _fw_cache    = weights
            _fw_cache_ts = time.time()
            return weights

        accs = _compute_feature_accuracies(preds)
        if not accs:
            _fw_cache    = weights
            _fw_cache_ts = time.time()
            return weights

        # ── Poids intra-stat ──────────────────────────────────────────────────
        # Regrouper les contributions par catégorie de stat
        cat_contribs: dict[str, list[float]] = {}
        for sig_name, data in accs.items():
            cat = _SIGNAL_TO_WEIGHT_KEY.get(sig_name)
            if cat and cat != "_math":
                cat_contribs.setdefault(cat, []).append(data["weight_contribution"])

        if cat_contribs:
            new_intra = dict(weights["intra_stat"])
            for cat, contribs in cat_contribs.items():
                avg   = sum(contribs) / len(contribs)
                prior = _DEFAULT_WEIGHTS["intra_stat"].get(cat, 0.10)
                # 60% appris + 40% prior pour éviter l'over-fitting
                new_intra[cat] = max(0.01, round(0.60 * avg + 0.40 * prior, 4))
            # Normaliser pour que la somme = 1
            total = sum(new_intra.values())
            if total > 0:
                new_intra = {k: round(v / total, 4) for k, v in new_intra.items()}
            weights["intra_stat"] = new_intra

        # ── Facteur gardien λ ────────────────────────────────────────────────
        # weight_contribution va de 0.0 (signal = bruit) à 1.0 (signal = parfait)
        # On le mappe en facteur lambda : 0.0 → ignorer, 1.0 → comportement actuel,
        # 2.0 → doubler l'ajustement. Base 40% prior (1.0) + 60% appris.
        goalie_data = accs.get("goalie")
        if goalie_data:
            learned = goalie_data["weight_contribution"] * 2.0   # 0→0, 0.5→1, 1→2
            weights["goalie_lambda_factor"] = round(
                max(0.0, min(2.5, 0.60 * learned + 0.40 * 1.0)), 3
            )
        else:
            weights["goalie_lambda_factor"] = 1.0  # comportement par défaut

        # ── Blend stat vs math ────────────────────────────────────────────────
        # Si les stats sont plus prédictives que le marché → augmenter poids stat
        math_data    = accs.get("math_odds")
        stat_signals = [n for n in accs if n not in ("math_odds",)]
        if math_data and stat_signals:
            math_contrib = math_data["weight_contribution"]
            avg_stat     = sum(accs[n]["weight_contribution"] for n in stat_signals) / len(stat_signals)
            # Si stat_contrib > math_contrib, on monte le poids stat
            # base: 0.55 stat / 0.45 math · plage: [0.30 – 0.75]
            adj    = (avg_stat - math_contrib) * 0.30
            stat_w = max(0.30, min(0.75, round(0.55 + adj, 3)))
            weights["stat_vs_math"] = stat_w

    except Exception:
        pass

    _fw_cache    = weights
    _fw_cache_ts = time.time()
    return weights


def get_nba_feature_weights() -> dict:
    """
    Retourne les poids optimaux appris de l'historique des prédictions NBA :
      - stat_vs_math  : part des stats NBA (vs probabilité mathématique des cotes)
      - intra_stat    : poids de chaque stat dans le calcul NBA

    N'est actif qu'après MIN_OUTCOMES_FOR_CORRECTION prédictions NBA avec signaux.
    Retourne les poids par défaut sinon.
    Met en cache le résultat 5 minutes.
    """
    global _nba_fw_cache, _nba_fw_ts
    if _nba_fw_cache is not None and time.time() - _nba_fw_ts < _FW_TTL:
        return _nba_fw_cache

    weights = copy.deepcopy(_NBA_DEFAULT_WEIGHTS)
    try:
        # Clés présentes uniquement dans les signaux NBA (pas NHL)
        _NBA_ONLY_KEYS = {"points_for", "points_against", "net_rating", "streak", "seed_rank",
                         "home_win_pct", "road_win_pct", "rest_advantage", "injury_net"}
        preds = _load()
        nba_excellent = [p for p in preds
                         if p.get("sport") == "basketball"
                         and p.get("outcome") in ("win", "loss")
                         and _NBA_ONLY_KEYS & set((p.get("signals") or {}).keys())
                         and _is_excellent(p)]

        # Calculer les précisions par signal (pour affichage) même sous le seuil
        # min_samples=3 pour l'affichage, MIN_SIGNAL_SAMPLES pour les poids réels
        accs_display = _compute_feature_accuracies(nba_excellent, min_samples=3) if nba_excellent else {}
        weights["feature_accuracies"] = accs_display

        if len(nba_excellent) < MIN_OUTCOMES_FOR_CORRECTION:
            if isinstance(_nba_cal_correction, dict):
                try:
                    stat_w_adj = float(_nba_cal_correction.get("stat_vs_math_after", weights["stat_vs_math"]))
                    weights["stat_vs_math"] = round(max(0.30, min(0.75, stat_w_adj)), 3)
                    weights["_cal_correction"] = _nba_cal_correction
                except Exception:
                    pass
            _nba_fw_cache = weights
            _nba_fw_ts = time.time()
            return weights

        # Recalculer avec seuil strict pour l'ajustement des poids
        accs = _compute_feature_accuracies(nba_excellent)

        # intra_stat weights
        cat_contribs: dict[str, list[float]] = {}
        for sig, data in accs.items():
            key = _NBA_SIGNAL_TO_WEIGHT_KEY.get(sig)
            if not key or key.startswith("_"):
                continue
            cat_contribs.setdefault(key, []).append(data["weight_contribution"])

        if cat_contribs:
            raw = {k: sum(v)/len(v) for k, v in cat_contribs.items()}
            total = sum(raw.values()) or 1.0
            learned = {k: v / total for k, v in raw.items()}
            intra = {}
            for k, default in _NBA_DEFAULT_WEIGHTS["intra_stat"].items():
                l = learned.get(k, default)
                intra[k] = round(max(0.0, 0.60 * l + 0.40 * default), 3)
            total2 = sum(intra.values()) or 1.0
            weights["intra_stat"] = {k: round(v / total2, 3) for k, v in intra.items()}

        # stat_vs_math — base : contribution stats vs math_odds
        math_data = accs.get("math_odds")
        stat_keys = [s for s in accs if s != "math_odds" and not s.startswith("_") and s != "is_home"]
        stat_w = weights["stat_vs_math"]
        if stat_keys and math_data:
            avg_stat_contrib = sum(accs[s]["weight_contribution"] for s in stat_keys) / len(stat_keys)
            math_contrib = math_data["weight_contribution"]
            adj = (avg_stat_contrib - math_contrib) * 0.30
            stat_w = round(max(0.30, min(0.75, 0.55 + adj)), 3)
            weights["stat_vs_math"] = stat_w

        # Correction calibration : injectée depuis les snapshots via set_nba_calibration_correction()
        # (les snapshots NBA sont lus par app.py, pas accessibles ici)
        if isinstance(_nba_cal_correction, dict):
            try:
                stat_w_adj = float(_nba_cal_correction.get("stat_vs_math_after", stat_w))
                stat_w_adj = round(max(0.30, min(0.75, stat_w_adj)), 3)
                weights["stat_vs_math"] = stat_w_adj
                weights["_cal_correction"] = _nba_cal_correction
            except Exception:
                pass

    except Exception:
        pass

    _nba_fw_cache = weights
    _nba_fw_ts = time.time()
    return weights


def record_opportunity(opp, first_match_time: str = "23:59") -> bool:
    """
    Sauvegarde un BettingOpportunity dans l'historique.
    Inclut les signaux de features pour l'apprentissage des poids.
    Retourne True si ajouté ou mis à jour, False si ignoré.
    N'enregistre que les avis Excellent et Bon.

    first_match_time : heure HH:MM du premier match de la journée (ex: "19:00").
                       Avant cette heure → les cotes sont mises à jour à chaque refresh.
                       Après cette heure → les prédictions sont verrouillées ; aucune
                       modification n'est possible. Ceci garantit que le lendemain on
                       analyse la dernière version vue AVANT le début des matchs.
    """
    if "Neutre" in opp.recommendation or "Eviter" in opp.recommendation:
        return False

    key = "|".join([
        opp.match.date or "",
        (opp.match.home_team or "").lower(),
        (opp.match.away_team or "").lower(),
        opp.bet_type.lower(),
        opp.selection_label.lower(),
    ])

    now_time = datetime.now().strftime("%H:%M")
    locked   = now_time >= first_match_time   # fenêtre de pari fermée

    preds = _load()
    existing_idx = next(
        (i for i, p in enumerate(preds) if p["key"] == key),
        None,
    )

    if existing_idx is not None:
        if locked:
            # Les matchs ont commencé : ne pas écraser la version pré-match
            return False
        # Avant le premier match : mettre à jour les cotes (peuvent avoir bougé)
        preds[existing_idx].update({
            "odds":           opp.odds,
            "fair_prob":      round(opp.fair_prob, 4),
            "value_score":    round(getattr(opp, "value_score", 0) or 0, 1),
            "p_math":         round(getattr(opp, "math_prob", opp.fair_prob), 4),
            "recommendation": opp.recommendation,
            "signals":        _extract_signals(opp),
            "saved_at":       datetime.now().isoformat(),
            "locked_at":      first_match_time,
        })
        _save(preds)
        return True

    # Nouvelle prédiction
    preds.append({
        "key":            key,
        "date":           opp.match.date,
        "time":           opp.match.time,
        "home_team":      opp.match.home_team,
        "away_team":      opp.match.away_team,
        "bet_type":       opp.bet_type,
        "selection":      opp.selection_label,
        "odds":           opp.odds,
        "fair_prob":      round(opp.fair_prob, 4),
        "value_score":    round(getattr(opp, "value_score", 0) or 0, 1),
        "p_math":         round(getattr(opp, "math_prob", opp.fair_prob), 4),
        "signals":        _extract_signals(opp),
        "recommendation": opp.recommendation,
        "sport":          getattr(opp, "sport", None) or getattr(opp.match, "sport", None),
        "outcome":        None,   # "win" | "loss" | null
        "saved_at":       datetime.now().isoformat(),
        "locked_at":      first_match_time,
    })
    _save(preds)
    return True


def record_opportunities_batch(opps: list, first_match_time: str = "23:59") -> int:
    """
    Version batch de record_opportunity : charge predictions.json une seule fois,
    met à jour tous les paris en mémoire, puis sauvegarde une seule fois.
    Retourne le nombre de prédictions ajoutées ou mises à jour.
    """
    now_time = datetime.now().strftime("%H:%M")
    locked   = now_time >= first_match_time

    preds    = _load()
    idx_map  = {p["key"]: i for i, p in enumerate(preds)}
    changed  = 0

    for opp in opps:
        if "Neutre" in opp.recommendation or "Eviter" in opp.recommendation:
            continue
        key = "|".join([
            opp.match.date or "",
            (opp.match.home_team or "").lower(),
            (opp.match.away_team or "").lower(),
            opp.bet_type.lower(),
            opp.selection_label.lower(),
        ])
        if key in idx_map:
            if locked:
                continue
            preds[idx_map[key]].update({
                "odds":           opp.odds,
                "fair_prob":      round(opp.fair_prob, 4),
                "value_score":    round(getattr(opp, "value_score", 0) or 0, 1),
                "p_math":         round(getattr(opp, "math_prob", opp.fair_prob), 4),
                "recommendation": opp.recommendation,
                "signals":        _extract_signals(opp),
                "saved_at":       datetime.now().isoformat(),
                "locked_at":      first_match_time,
            })
            changed += 1
        else:
            new_pred = {
                "key":            key,
                "date":           opp.match.date,
                "time":           opp.match.time,
                "home_team":      opp.match.home_team,
                "away_team":      opp.match.away_team,
                "bet_type":       opp.bet_type,
                "selection":      opp.selection_label,
                "odds":           opp.odds,
                "fair_prob":      round(opp.fair_prob, 4),
                "value_score":    round(getattr(opp, "value_score", 0) or 0, 1),
                "p_math":         round(getattr(opp, "math_prob", opp.fair_prob), 4),
                "signals":        _extract_signals(opp),
                "recommendation": opp.recommendation,
                "sport":          getattr(opp, "sport", None) or getattr(opp.match, "sport", None),
                "outcome":        None,
                "saved_at":       datetime.now().isoformat(),
                "locked_at":      first_match_time,
            }
            preds.append(new_pred)
            idx_map[key] = len(preds) - 1
            changed += 1

    if changed:
        _save(preds)
    return changed


def update_champion_flags(champion_keys: set, all_today_keys: set,
                          mise_by_key: dict | None = None) -> None:
    """
    Persiste le flag champion et la mise Kelly pour les prédictions d'aujourd'hui.

    champion_keys  : clés des Champions.
    all_today_keys : toutes les clés d'aujourd'hui.
    mise_by_key    : dict {clé → montant_mise} (None = ne pas toucher les mises).
    """
    if not all_today_keys:
        return
    preds = _load()
    changed = False
    for p in preds:
        if p["key"] in all_today_keys:
            new_val = p["key"] in champion_keys
            if p.get("champion") != new_val:
                p["champion"] = new_val
                changed = True
            if mise_by_key is not None:
                new_mise = mise_by_key.get(p["key"])  # None si non sélectionné
                if p.get("mise") != new_mise:
                    p["mise"] = new_mise
                    changed = True
    if changed:
        _save(preds)


def _compute_nba_signals_from_dict(p: dict) -> dict:
    """
    Génère les signaux NBA directement depuis un dict de prédiction JSON.
    Utilisé pour rétro-remplir les prédictions sauvegardées sans signaux.
    """
    try:
        import re
        from nba_stats import get_team_stats

        home_team = p.get("home_team", "")
        away_team = p.get("away_team", "")
        sel       = (p.get("selection") or "").lower()

        home = get_team_stats(home_team)
        away = get_team_stats(away_team)
        if not home or not away:
            return {}

        home_n   = re.sub(r'\s*\(.*?\)', '', home_team.lower()).strip()
        away_n   = re.sub(r'\s*\(.*?\)', '', away_team.lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        if not home_sel:
            away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
            if not away_sel:
                return {}

        sel_team = home if home_sel else away
        opp_team = away if home_sel else home

        sel_l10 = sel_team["l10Wins"] / max(sel_team.get("l10GP", 10), 1)
        opp_l10 = opp_team["l10Wins"] / max(opp_team.get("l10GP", 10), 1)

        sel_net = sel_team.get("netRating", sel_team.get("ptsPG", 0) - sel_team.get("oppPtsPG", 0))
        opp_net = opp_team.get("netRating", opp_team.get("ptsPG", 0) - opp_team.get("oppPtsPG", 0))

        signals: dict = {
            "is_home":          home_sel,
            "win_pct":          sel_team["winPct"] > opp_team["winPct"],
            "home_road_record": (
                sel_team["homeWinPct"] > opp_team["roadWinPct"] if home_sel
                else sel_team["roadWinPct"] > opp_team["homeWinPct"]
            ),
            "recent_form":      sel_l10 > opp_l10,
            "points_for":       sel_team.get("ptsPG", 0) > opp_team.get("ptsPG", 0),
            "points_against":   sel_team.get("oppPtsPG", 999) < opp_team.get("oppPtsPG", 999),
            "net_rating":       sel_net > opp_net,
            "streak":           (
                sel_team.get("streakCode") == "W" and opp_team.get("streakCode") == "L"
            ) or (
                sel_team.get("streakCode") == "W"
                and sel_team.get("streakCount", 0) > opp_team.get("streakCount", 0)
            ),
            "seed_rank":        sel_team.get("leagueSeq", 99) < opp_team.get("leagueSeq", 99),
        }

        p_math = float(p.get("p_math") or p.get("fair_prob") or 0)
        signals["math_odds"] = p_math > 0.5

        return signals
    except Exception:
        return {}


def backfill_nba_signals() -> int:
    """
    Rétro-remplit les signaux NBA pour les prédictions 'Gagnant' sans signaux NBA valides.
    Retourne le nombre de prédictions mises à jour.
    """
    preds = _load()
    updated = 0
    nba_signal_keys = {"win_pct", "home_road_record", "recent_form", "points_for",
                       "points_against", "net_rating", "streak", "seed_rank", "math_odds"}
    for p in preds:
        if p.get("sport") != "basketball":
            continue
        bt = (p.get("bet_type") or "").lower()
        if not any(k in bt for k in ("gagnant", "2 issues", "victoire")):
            continue
        existing = p.get("signals") or {}
        # Ne remplacer que si les signaux sont absents ou contiennent des clés NHL
        has_nba_signals = bool(existing) and bool(nba_signal_keys & set(existing.keys()))
        if has_nba_signals:
            continue
        new_sig = _compute_nba_signals_from_dict(p)
        if new_sig:
            p["signals"] = new_sig
            updated += 1
    if updated:
        _save(preds)
    return updated


# ─── Mise à jour des résultats ─────────────────────────────────────────────────

def _repair_missing_sport(preds: list[dict]) -> int:
    """
    Assigne ou corrige le champ 'sport' de toutes les prédictions.

    Stratégie de désambiguïsation (villes partagées ex: Toronto, Minnesota, Chicago) :
      1. Extraire le surnom entre parenthèses — ex: "(Wild)" vs "(Timberwolves)"
         Le surnom est UNIQUE entre les deux ligues, aucun faux positif possible.
      2. Fallback sur le nom de ville seulement si les deux ligues ne matchent pas
         ou si une seule matche sans ambiguïté.
    Retourne le nombre de prédictions ajoutées ou corrigées.
    """
    import re as _re
    from nhl_stats import _match_abbrev as nhl_abbrev
    from nba_stats import _match_abbrev as nba_abbrev

    fixed = 0
    for p in preds:
        home = p.get("home_team", "")
        if not home:
            continue

        # 1. Surnom entre parenthèses (non ambigu entre les ligues)
        m = _re.search(r'\(([^)]+)\)', home)
        correct_sport = None
        if m:
            nickname = m.group(1)
            nba_nick = nba_abbrev(nickname)
            nhl_nick = nhl_abbrev(nickname)
            if nba_nick and not nhl_nick:
                correct_sport = "basketball"
            elif nhl_nick and not nba_nick:
                correct_sport = "hockey"

        # 2. Fallback : nom complet si surnom ambigu ou absent
        if correct_sport is None:
            nba_full = nba_abbrev(home)
            nhl_full = nhl_abbrev(home)
            if nba_full and not nhl_full:
                correct_sport = "basketball"
            elif nhl_full and not nba_full:
                correct_sport = "hockey"

        if correct_sport and p.get("sport") != correct_sport:
            p["sport"] = correct_sport
            fixed += 1
    return fixed


def update_outcomes() -> int:
    """
    Pour chaque prédiction passée sans résultat, interroge l'API NHL
    et détermine si le pari était gagnant ou perdant.
    Retourne le nombre de prédictions mises à jour.
    """
    preds  = _load()
    today  = date.today().isoformat()

    # Corriger les prédictions sans sport (rétrocompatibilité)
    repaired = _repair_missing_sport(preds)

    # Dates passées avec des résultats manquants (hockey uniquement)
    dates  = {p["date"] for p in preds
              if p["outcome"] is None and p["date"] and p["date"] < today
              and p.get("sport") in (None, "hockey")}

    updated = 0
    for game_date in sorted(dates):
        games = _fetch_scores(game_date)
        if not games:
            continue
        for p in preds:
            if p["date"] != game_date or p["outcome"] is not None:
                continue
            if p.get("sport") == "basketball":
                continue  # géré par update_nba_outcomes
            outcome = _determine_outcome(p, games)
            if outcome:
                p["outcome"] = outcome
                updated += 1

    if updated or repaired:
        _save(preds)
    return updated


def _fetch_scores(date_str: str) -> list[dict]:
    try:
        import requests
        resp = requests.get(NHL_SCORE_URL.format(date=date_str), timeout=8)
        resp.raise_for_status()
        return resp.json().get("games", [])
    except Exception:
        return []


def _determine_outcome(pred: dict, games: list[dict]) -> str | None:
    from nhl_stats import _match_abbrev
    home_abbrev = _match_abbrev(pred["home_team"])
    away_abbrev = _match_abbrev(pred["away_team"])
    if not home_abbrev or not away_abbrev:
        return None

    game = next(
        (g for g in games
         if g.get("homeTeam", {}).get("abbrev") == home_abbrev
         and g.get("awayTeam", {}).get("abbrev") == away_abbrev
         and g.get("gameState") in ("FINAL", "OFF")),
        None,
    )
    if not game:
        return None

    home_score  = game["homeTeam"].get("score", 0) or 0
    away_score  = game["awayTeam"].get("score", 0) or 0
    total_goals = home_score + away_score

    bt  = pred["bet_type"].lower()
    sel = pred["selection"].lower()

    # Helper : compte les buts d'une période donnée depuis le tableau goals[]
    goals_arr = game.get("goals", [])

    def _period_goals(period_n: int, team_abbrev: str | None = None) -> int:
        count = 0
        for g in goals_arr:
            # Le champ period peut être un entier ou {"number": n}
            p = g.get("period")
            if isinstance(p, dict):
                p = p.get("number")
            if p != period_n:
                continue
            if team_abbrev:
                ta = g.get("teamAbbrev")
                if isinstance(ta, dict):
                    ta = ta.get("default")
                if ta != team_abbrev:
                    continue
            count += 1
        return count

    # ── Gagnant du match ──────────────────────────────────────────────────────
    if any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues", "3 issues")):
        home_n = re.sub(r'\s*\(.*?\)', '', pred["home_team"].lower()).strip()
        away_n = re.sub(r'\s*\(.*?\)', '', pred["away_team"].lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
        # Résultat nul (match nul en règlement, i.e. prolongation ou TB)
        if "nul" in sel or "draw" in sel or "tie" in sel:
            # Nul = les deux équipes à égalité après 60 min (OT/SO possible)
            # On vérifie si les buts après la 3e période sont égaux
            h3 = sum(_period_goals(p) for p in (1, 2, 3) if True)
            # Simplification : si le jeu a nécessité OT/SO, la 3e était égale
            went_ot = game.get("periodDescriptor", {}).get("number", 3) > 3 \
                      or game.get("gameOutcome", {}).get("lastPeriodType", "REG") != "REG"
            reg_home = sum(_period_goals(p, home_abbrev) for p in (1, 2, 3))
            reg_away = sum(_period_goals(p, away_abbrev) for p in (1, 2, 3))
            if goals_arr:
                tied_after_reg = reg_home == reg_away
            else:
                # Pas de données goals : si le jeu est allé en OT, c'était égal
                tied_after_reg = went_ot
            return "win" if tied_after_reg else "loss"
        if home_sel:
            return "win" if home_score > away_score else "loss"
        if away_sel:
            return "win" if away_score > home_score else "loss"

    # ── Total de buts ─────────────────────────────────────────────────────────
    elif any(k in bt for k in ("total", "buts", "plus/moins")):
        # La valeur peut être dans la sélection OU dans le type de pari
        m = re.search(r'(\d+[.,]\d+|\d+)', sel)
        if not m:
            m = re.search(r'(\d+[.,]\d+|\d+)', bt)
        if not m:
            return None
        line = float(m.group(1).replace(',', '.'))

        over = "plus" in sel or "over" in sel
        under = "moins" in sel or "under" in sel
        if not over and not under:
            return None

        # Détecter une période spécifique : "2e période", "3e période", "1re période"
        period_m = re.search(r'(\d)[eè]?r?e?\s*p[eé]riode', bt)

        # Détecter un total d'équipe : "[Équipe] Total de buts ..."
        # Le format est "[Nom équipe] Total de buts plus/moins X"
        team_abbrev_total = None
        if not period_m:
            for abbrev, name_parts in [(home_abbrev, pred["home_team"]),
                                       (away_abbrev, pred["away_team"])]:
                name_clean = re.sub(r'\s*\(.*?\)', '', name_parts).lower().strip()
                if any(w in bt for w in name_clean.split() if len(w) > 2):
                    team_abbrev_total = abbrev
                    break

        if period_m:
            period_n = int(period_m.group(1))
            if goals_arr:
                g_total = _period_goals(period_n)
                return "win" if (over and g_total > line) or (under and g_total < line) else "loss"
            # Pas de détail par période disponible → impossible à résoudre
            return None
        elif team_abbrev_total:
            team_score = home_score if team_abbrev_total == home_abbrev else away_score
            return "win" if (over and team_score > line) or (under and team_score < line) else "loss"
        else:
            # Total du match complet
            return "win" if (over and total_goals > line) or (under and total_goals < line) else "loss"

    # ── Les 2 équipes marquent ────────────────────────────────────────────────
    elif any(k in bt for k in ("2 équipes", "les 2", "both")):
        both = home_score > 0 and away_score > 0
        if "oui" in sel or "marquent" in sel:
            return "win" if both else "loss"
        if "non" in sel or "ne marque" in sel:
            return "win" if not both else "loss"

    return None


# ─── Résultats NBA ────────────────────────────────────────────────────────────

def update_nba_outcomes() -> int:
    """
    Pour chaque prédiction NBA passée sans résultat, interroge l'API ESPN
    et détermine si le pari était gagnant ou perdant.
    Retourne le nombre de prédictions mises à jour.
    """
    preds = _load()
    today = date.today().isoformat()

    # Corriger les sports mal classifiés avant de filtrer
    repaired = _repair_missing_sport(preds)

    # Après réparation, le champ sport est fiable
    def _is_nba(p):
        return p.get("sport") == "basketball"

    dates = {p["date"] for p in preds
             if p["outcome"] is None and p["date"] and p["date"] < today
             and _is_nba(p)}

    updated = 0
    for game_date in sorted(dates):
        games = _fetch_nba_scores(game_date)
        if not games:
            continue
        for p in preds:
            if p["date"] != game_date or p["outcome"] is not None:
                continue
            if not _is_nba(p):
                continue
            outcome = _determine_nba_outcome(p, games)
            if outcome:
                p["outcome"] = outcome
                updated += 1

    if updated:
        _save(preds)

    # Rétro-remplir les signaux NBA manquants ou incorrects
    try:
        backfill_nba_signals()
    except Exception:
        pass

    return updated


def _fetch_nba_scores(date_str: str) -> list[dict]:
    """Retourne les scores NBA depuis l'API ESPN pour une date donnée."""
    try:
        import requests
        date_compact = date_str.replace("-", "")  # YYYYMMDD
        url = NBA_SCORE_URL.format(date=date_compact)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        events = resp.json().get("events", [])
        games = []
        for ev in events:
            comp = ev.get("competitions", [{}])[0]
            competitors = comp.get("competitors", [])
            status_desc = comp.get("status", {}).get("type", {}).get("description", "")
            if not any(c.get("homeAway") == "home" for c in competitors):
                continue
            home = next(c for c in competitors if c.get("homeAway") == "home")
            away = next(c for c in competitors if c.get("homeAway") == "away")
            games.append({
                "home_abbrev":      home["team"]["abbreviation"],
                "away_abbrev":      away["team"]["abbreviation"],
                "home_score":       int(home.get("score") or 0),
                "away_score":       int(away.get("score") or 0),
                "status":           status_desc,
                "home_linescores":  [ls["value"] for ls in home.get("linescores", [])],
                "away_linescores":  [ls["value"] for ls in away.get("linescores", [])],
            })
        return games
    except Exception:
        return []


def _determine_nba_outcome(pred: dict, games: list[dict]) -> str | None:
    """Détermine le résultat d'un pari NBA en comparant avec les scores ESPN."""
    from nba_stats import _match_abbrev as nba_abbrev

    home_abbrev = nba_abbrev(pred["home_team"])
    away_abbrev = nba_abbrev(pred["away_team"])
    if not home_abbrev or not away_abbrev:
        return None

    game = next(
        (g for g in games
         if g["home_abbrev"] == home_abbrev
         and g["away_abbrev"] == away_abbrev
         and g["status"] in ("Final", "Final/OT")),
        None,
    )
    if not game:
        return None

    home_score = game["home_score"]
    away_score = game["away_score"]
    total_pts  = home_score + away_score

    bt  = pred["bet_type"].lower()
    sel = pred["selection"].lower()

    # ── Écart de points (spread) — vérifié EN PREMIER avant "total/points" ────
    # "Écart de points -3.5" contient "points" mais c'est un pari écart, pas total
    if any(k in bt for k in ("écart", "ecart", "handicap", "spread")):
        m = re.search(r'([+-]?\d+[.,]?\d*)', bt)
        if not m:
            return None
        spread = float(m.group(1).replace(',', '.'))
        home_n = re.sub(r"\s*\(.*?\)", "", pred["home_team"].lower()).strip()
        away_n = re.sub(r"\s*\(.*?\)", "", pred["away_team"].lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
        if home_sel:
            return "win" if (home_score + spread) > away_score else "loss"
        if away_sel:
            return "win" if (away_score + spread) > home_score else "loss"
        return None

    # ── Double chance ─────────────────────────────────────────────────────────
    # En basketball il n'y a pas de nul : "Team X ou Nul" = "Team X gagne"
    elif "double chance" in bt or "double" in bt:
        home_n = re.sub(r"\s*\(.*?\)", "", pred["home_team"].lower()).strip()
        away_n = re.sub(r"\s*\(.*?\)", "", pred["away_team"].lower()).strip()
        home_in_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        away_in_sel = any(w in sel for w in away_n.split() if len(w) > 2)
        if home_in_sel:
            return "win" if home_score >= away_score else "loss"
        if away_in_sel:
            return "win" if away_score >= home_score else "loss"
        return None

    # ── Gagnant du match ──────────────────────────────────────────────────────
    elif any(k in bt for k in ("gagnant", "victoire", "winner", "2 issues")):
        home_n = re.sub(r"\s*\(.*?\)", "", pred["home_team"].lower()).strip()
        away_n = re.sub(r"\s*\(.*?\)", "", pred["away_team"].lower()).strip()
        home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
        away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
        if home_sel:
            return "win" if home_score > away_score else "loss"
        if away_sel:
            return "win" if away_score > home_score else "loss"

    # ── Total de points ───────────────────────────────────────────────────────
    elif any(k in bt for k in ("total", "points", "plus/moins")):
        m = re.search(r"(\d+[.,]\d+|\d+)", sel)
        if not m:
            m = re.search(r"(\d+[.,]\d+|\d+)", bt)
        if not m:
            return None
        line = float(m.group(1).replace(",", "."))

        over  = "plus" in sel or "over" in sel
        under = "moins" in sel or "under" in sel
        if not over and not under:
            return None

        # Détecter un quart spécifique : "1er quart", "2e quart", etc.
        quarter_m = re.search(r"(\d)[eè]?r?e?\s*quart", bt)

        # Détecter un total d'équipe
        team_abbrev_total = None
        if not quarter_m:
            for abbrev, name_parts in [(home_abbrev, pred["home_team"]),
                                       (away_abbrev, pred["away_team"])]:
                name_clean = re.sub(r"\s*\(.*?\)", "", name_parts).lower().strip()
                if any(w in bt for w in name_clean.split() if len(w) > 2):
                    team_abbrev_total = abbrev
                    break

        if quarter_m:
            q = int(quarter_m.group(1)) - 1   # index 0-based
            h_ls = game["home_linescores"]
            a_ls = game["away_linescores"]
            if q >= len(h_ls) or q >= len(a_ls):
                return None
            q_total = h_ls[q] + a_ls[q]
            return "win" if (over and q_total > line) or (under and q_total < line) else "loss"
        elif team_abbrev_total:
            team_score = home_score if team_abbrev_total == home_abbrev else away_score
            return "win" if (over and team_score > line) or (under and team_score < line) else "loss"
        else:
            return "win" if (over and total_pts > line) or (under and total_pts < line) else "loss"

    # ── 1re demie ─────────────────────────────────────────────────────────────
    elif "demie" in bt or "half" in bt:
        h_ls = game.get("home_linescores", [])
        a_ls = game.get("away_linescores", [])
        if len(h_ls) < 2 or len(a_ls) < 2:
            return None
        h_half = float(h_ls[0]) + float(h_ls[1])
        a_half = float(a_ls[0]) + float(a_ls[1])
        if any(k in bt for k in ("gagnant", "winner")):
            home_n = re.sub(r"\s*\(.*?\)", "", pred["home_team"].lower()).strip()
            away_n = re.sub(r"\s*\(.*?\)", "", pred["away_team"].lower()).strip()
            home_sel = any(w in sel for w in home_n.split() if len(w) > 2)
            away_sel = any(w in sel for w in away_n.split() if len(w) > 2)
            if home_sel:
                return "win" if h_half > a_half else "loss"
            if away_sel:
                return "win" if a_half > h_half else "loss"
        elif any(k in bt for k in ("total", "points", "plus/moins")):
            m = re.search(r"(\d+[.,]\d+|\d+)", sel)
            if not m:
                m = re.search(r"(\d+[.,]\d+|\d+)", bt)
            if not m:
                return None
            line = float(m.group(1).replace(",", "."))
            over  = "plus" in sel or "over" in sel
            under = "moins" in sel or "under" in sel
            half_total = h_half + a_half
            if over:
                return "win" if half_total > line else "loss"
            if under:
                return "win" if half_total < line else "loss"

    return None


# ─── Zones rentables ──────────────────────────────────────────────────────────

_ODDS_RANGES = [
    ("<1.50",     0.0,  1.50),
    ("1.50-1.70", 1.50, 1.70),
    ("1.70-1.90", 1.70, 1.90),
    ("1.90-2.20", 1.90, 2.20),
    ("2.20+",     2.20, 99.0),
]

_profitable_cache: list | None = None
_profitable_ts: float = 0.0
_PROFITABLE_TTL = 300.0  # 5 min

# Plages de cotes utilisées par défaut quand l'historique est insuffisant
_DEFAULT_PROFITABLE_RANGES: list[tuple[float, float, float]] = [
    (1.70, 1.90, 0.0),
    (1.90, 2.20, 0.0),
]


def get_profitable_odds_ranges(min_samples: int = 5) -> list[tuple[float, float, float]]:
    """
    Retourne les zones de cotes historiquement rentables (ROI > 0) parmi les
    prédictions Excellentes résolues, triées par ROI décroissant.
    Chaque entrée : (lo, hi, roi).
    Mis en cache 5 minutes.
    """
    global _profitable_cache, _profitable_ts
    if _profitable_cache is not None and time.time() - _profitable_ts < _PROFITABLE_TTL:
        return _profitable_cache

    preds = [p for p in _load()
             if p.get("outcome") in ("win", "loss")
             and _is_excellent(p)
             and p.get("odds")
             and p.get("mise") is not None]   # seulement les picks réellement distribués

    result = []
    for _, lo, hi in _ODDS_RANGES:
        group = [p for p in preds if lo <= float(p["odds"]) < hi]
        if len(group) < min_samples:
            continue
        wins     = sum(1 for p in group if p["outcome"] == "win")
        wr       = wins / len(group)
        avg_odds = sum(float(p["odds"]) for p in group) / len(group)
        roi      = (wr * avg_odds - 1) * 100
        if roi > 0:
            result.append((lo, hi, round(roi, 1)))

    result.sort(key=lambda x: -x[2])   # tri ROI décroissant
    # Fallback : utiliser les plages par défaut si pas assez d'historique
    if not result:
        result = _DEFAULT_PROFITABLE_RANGES
    _profitable_cache = result
    _profitable_ts    = time.time()
    return result


# ─── Classification type de pari ──────────────────────────────────────────────

def classify_bet_type(bt: str, home: str = "", away: str = "") -> str:
    """Catégorise un type de pari pour l'analyse historique et le ranking."""
    bt_l = bt.lower()
    # Props joueur : pas de parenthèses, commence par Prénom Nom
    if ('(' not in bt
            and re.match(r'^[A-ZÀ-Ü][a-zà-ü\'-]+ [A-ZÀ-Ü]', bt)
            and any(k in bt_l for k in ("total de points", "total de buts",
                                         "buts marqués", "passes", "aides",
                                         "tirs", "mises en échec"))):
        return "Props joueur"
    if "3 issues" in bt_l:
        return "Gagnant (3 issues)"
    if any(k in bt_l for k in ("2 issues", "gagnant", "victoire", "winner")):
        if any(k in bt_l for k in ("période", "quart", "demie", "half")):
            return "Gagnant (période / quart)"
        return "Gagnant (2 issues)"
    if "total de buts" in bt_l or (
            "total" in bt_l and "buts" in bt_l and "points" not in bt_l):
        home_n = re.sub(r'\s*\(.*?\)', '', home.lower()).strip()
        away_n = re.sub(r'\s*\(.*?\)', '', away.lower()).strip()
        team_in_bt = any(
            len(w) > 2 and w in bt_l
            for w in (home_n + " " + away_n).split()
        )
        return "Total de buts (équipe)" if team_in_bt else "Total de buts (match)"
    if "total de points" in bt_l or ("total" in bt_l and "points" in bt_l):
        home_n = re.sub(r'\s*\(.*?\)', '', home.lower()).strip()
        away_n = re.sub(r'\s*\(.*?\)', '', away.lower()).strip()
        team_in_bt = any(
            len(w) > 2 and w in bt_l
            for w in (home_n + " " + away_n).split()
        )
        if "quart" in bt_l or "demie" in bt_l:
            return "Total de points (quart/demie)"
        return "Total de points (équipe)" if team_in_bt else "Total de points (match)"
    if any(k in bt_l for k in ("les 2", "both", "2 équipes", "2 equipes")):
        return "Les 2 équipes marquent"
    if any(k in bt_l for k in ("double chance", "chance double", "double")):
        return "Double chance"
    if any(k in bt_l for k in ("prolongation", "barrage", "overtime", "shootout")):
        return "Prolongation / Barrage"
    if any(k in bt_l for k in ("écart", "ecart", "handicap", "spread")):
        return "Écart de points"
    if any(k in bt_l for k in ("période", "quart", "demie", "half")):
        return "Période / Quart"
    if "plus/moins" in bt_l or "over/under" in bt_l:
        return "Total de buts (match)"
    return "Autre"


_bt_mult_cache: dict | None = None
_bt_mult_ts: float = 0.0
_BT_MULT_TTL = 300.0   # 5 min


def get_bet_type_multipliers(sport: str | None = None,
                             min_samples: int = 5) -> dict[str, float]:
    """
    Retourne un multiplicateur de confiance par catégorie de pari,
    basé sur le taux de réussite historique (prédictions Excellentes résolues).

    Multiplier > 1.0 → catégorie performante (prioriser dans le classement)
    Multiplier < 1.0 → catégorie faible (rétrograder)
    Multiplier = 1.0 → données insuffisantes

    Formule : mult = 1 + (win_rate% - 50%) / 100  · clampé [0.80 – 1.20]
    """
    global _bt_mult_cache, _bt_mult_ts
    if _bt_mult_cache is not None and time.time() - _bt_mult_ts < _BT_MULT_TTL:
        return _bt_mult_cache

    from nhl_stats import _match_abbrev as nhl_abbrev

    def _sport_match(p):
        if sport is None:
            return True
        if sport == "basketball":
            return p.get("sport") == "basketball"
        if sport == "hockey":
            if p.get("sport") is not None:
                return p.get("sport") == "hockey"
            return nhl_abbrev(p.get("home_team", "")) is not None
        return True

    preds = [p for p in _load()
             if p["outcome"] in ("win", "loss")
             and _is_excellent(p)
             and _sport_match(p)]

    cat_stats: dict[str, dict] = {}
    for p in preds:
        cat = classify_bet_type(
            p.get("bet_type") or "",
            p.get("home_team") or "",
            p.get("away_team") or "",
        )
        if cat not in cat_stats:
            cat_stats[cat] = {"wins": 0, "total": 0}
        cat_stats[cat]["total"] += 1
        if p["outcome"] == "win":
            cat_stats[cat]["wins"] += 1

    result: dict[str, float] = {}
    for cat, v in cat_stats.items():
        if v["total"] < min_samples:
            result[cat] = 1.0
            continue
        wr   = v["wins"] / v["total"] * 100   # ex: 62.5
        mult = 1.0 + (wr - 50.0) / 100.0     # ±10% par ±10pp
        result[cat] = round(max(0.80, min(1.20, mult)), 4)

    _bt_mult_cache = result
    _bt_mult_ts = time.time()
    return result


# ─── Calibration ──────────────────────────────────────────────────────────────

def _is_excellent(p: dict) -> bool:
    """Vrai si la prédiction était jugée Excellente."""
    return "Excellent" in (p.get("recommendation") or "")


_SIM_BET = 2.0   # Montant simulé par pari (en dollars)


def _sim_financials(pred_list: list[dict]) -> dict:
    """Simule un pari de 2$ fixe sur chaque prédiction Excellente résolue."""
    n = len(pred_list)
    profit = 0.0
    for p in pred_list:
        if p["outcome"] == "win":
            profit += _SIM_BET * (float(p.get("odds") or 0) - 1.0)
        else:
            profit -= _SIM_BET
    return {
        "bets":   n,
        "spent":  round(n * _SIM_BET, 2),
        "profit": round(profit, 2),
    }


def get_bias_factor(min_samples: int = 20) -> float:
    """
    Retourne le facteur de correction de biais fair_prob vs résultats réels.
    1.0 si pas assez de données (< min_samples prédictions résolues).
    """
    try:
        preds = [p for p in _load()
                 if p.get("outcome") in ("win", "loss")
                 and _is_excellent(p)
                 and p.get("sport") != "basketball"]
        if len(preds) < min_samples:
            return 1.0
        wins     = sum(1 for p in preds if p["outcome"] == "win")
        avg_pred = sum(p["fair_prob"] for p in preds) / len(preds)
        if avg_pred <= 0:
            return 1.0
        # Correction partielle : confiance proportionnelle à l'échantillon (pleine à 50+)
        raw_factor  = wins / len(preds) / avg_pred
        raw_factor  = max(0.60, min(1.40, raw_factor))
        confidence  = min(len(preds) / 50.0, 1.0)
        return round(1.0 + (raw_factor - 1.0) * confidence, 4)
    except Exception:
        return 1.0


def _sim_kelly(pred_list: list[dict], budget: float = 10.0, min_bet: float = 0.5,
               min_bets: int = 1, max_bets: int = 7,
               min_kelly: float = 0.03, bias_factor: float | None = None) -> dict:
    """
    Simule la distribution Kelly de budget$ sur 1-7 prédictions Excellentes résolues.
    - min_kelly : seuil minimum de ½ Kelly pour sélectionner un pari (défaut 3%)
    - bias_factor : correction fair_prob (1.0 = pas de correction)
    """
    resolved = [p for p in pred_list if p.get("outcome") in ("win", "loss")]
    if not resolved:
        return {"bets": 0, "spent": 0.0, "profit": 0.0}

    bf = bias_factor if bias_factor is not None else get_bias_factor()

    def hk(p: dict) -> float:
        prob = float(p.get("fair_prob") or 0)           # 0-1 dans predictions.json
        prob = min(prob * bf, 0.95)                     # correction biais calibration
        odds = float(p.get("odds") or 0)
        if odds <= 1 or prob <= 0:
            return 0.0
        b = odds - 1
        return max(0.0, (prob * b - (1 - prob)) / b / 2)

    with_hk = [(p, hk(p)) for p in resolved]

    # Seulement les paris avec Kelly >= min_kelly — sans traitement spécial pour les champions
    selected = sorted([(p, k) for p, k in with_hk if k >= min_kelly], key=lambda x: -x[1])
    selected = selected[:max_bets]
    if not selected:
        return {"bets": 0, "spent": 0.0, "profit": 0.0}

    # Distribuer budget proportionnellement (pas de minimum artificiel)
    total_hk = sum(k for _, k in selected)
    amounts  = [k / total_hk * budget for _, k in selected]
    amounts  = [max(round(a * 2) / 2, min_bet) for a in amounts]
    tot      = sum(amounts)
    amounts  = [round(a / tot * budget * 2) / 2 for a in amounts]
    amounts  = [max(a, min_bet) for a in amounts]
    diff     = round((budget - sum(amounts)) * 2) / 2
    if diff != 0 and amounts:
        mx = amounts.index(max(amounts))
        amounts[mx] = round((amounts[mx] + diff) * 2) / 2

    # Calculer profit réel
    profit = 0.0
    for (p, _), amount in zip(selected, amounts):
        if p["outcome"] == "win":
            profit += amount * (float(p.get("odds") or 0) - 1.0)
        else:
            profit -= amount

    return {
        "bets":   len(selected),
        "spent":  round(sum(amounts), 2),
        "profit": round(profit, 2),
    }


def compute_calibration(sport: str | None = None) -> dict:
    """
    Calcule les stats de calibration depuis l'historique.
    N'utilise que les prédictions jugées Excellentes (les seules qui comptent).

    sport=None        → tous les sports (comportement par défaut, = hockey pour l'historique)
    sport="basketball"→ seulement les prédictions NBA (champ sport explicite requis)
    sport="hockey"    → seulement les prédictions NHL

    Retourne :
      count         — nombre de prédictions Excellentes avec résultat
      wins          — nombre de paris gagnés
      win_rate      — taux de réussite réel (%)
      avg_predicted — probabilité prédite moyenne (%)
      bias_factor   — correction à appliquer (réel / prédit)
      buckets       — détail par tranche de probabilité
    """
    from nhl_stats import _match_abbrev as nhl_abbrev

    def _sport_match(p):
        if sport is None:
            return True
        if sport == "basketball":
            return p.get("sport") == "basketball"
        if sport == "hockey":
            # Nouvelles prédictions avec champ sport explicite
            if p.get("sport") is not None:
                return p.get("sport") == "hockey"
            # Anciennes prédictions sans champ sport = hockey
            return nhl_abbrev(p.get("home_team", "")) is not None
        return True

    preds = [p for p in _load()
             if p["outcome"] in ("win", "loss") and _is_excellent(p) and _sport_match(p)]

    if not preds:
        _empty_sim = {"bets": 0, "spent": 0.0, "profit": 0.0}
        return {
            "count": 0, "wins": 0,
            "win_rate": None, "avg_predicted": None,
            "bias_factor": 1.0, "buckets": {},
            "correction_active": False,
            "feature_accuracies": {}, "feature_weights": {},
            "sim_yesterday": _empty_sim, "sim_total": _empty_sim,
        }

    wins          = sum(1 for p in preds if p["outcome"] == "win")
    win_rate      = wins / len(preds)
    avg_predicted = sum(p["fair_prob"] for p in preds) / len(preds)
    bias_factor   = (win_rate / avg_predicted) if avg_predicted > 0 else 1.0
    # Limiter le facteur pour éviter des corrections extrêmes
    bias_factor   = max(0.60, min(1.50, bias_factor))

    # Analyse par tranche de 10%
    buckets: dict[str, dict] = {}
    for p in preds:
        label = f"{int(p['fair_prob'] * 10) * 10}-{int(p['fair_prob'] * 10) * 10 + 10}%"
        if label not in buckets:
            buckets[label] = {"total": 0, "wins": 0}
        buckets[label]["total"] += 1
        if p["outcome"] == "win":
            buckets[label]["wins"] += 1

    buckets_out = {
        k: {
            "total":    v["total"],
            "wins":     v["wins"],
            "win_rate": round(v["wins"] / v["total"] * 100, 1),
        }
        for k, v in sorted(buckets.items())
    }

    # Signaux : seulement sur les Excellentes (filtrées par sport si besoin)
    excellent_preds     = [p for p in _load() if _is_excellent(p) and _sport_match(p)]
    feature_accuracies  = _compute_feature_accuracies(excellent_preds)
    feature_weights     = get_feature_weights()

    # Simulation financière (mise Kelly /10$ par journée)
    yesterday     = (date.today() - timedelta(days=1)).isoformat()
    sim_yesterday = _sim_kelly([p for p in preds if p.get("date") == yesterday])

    # sim_total : appliquer Kelly jour par jour (10$/jour) puis sommer
    from itertools import groupby as _gb
    _by_date: dict[str, list] = {}
    for p in preds:
        _by_date.setdefault(p.get("date") or "?", []).append(p)
    _st_profit, _st_spent, _st_bets = 0.0, 0.0, 0
    for _day_preds in _by_date.values():
        _ds = _sim_kelly(_day_preds)
        _st_profit += _ds["profit"]; _st_spent += _ds["spent"]; _st_bets += _ds["bets"]
    sim_total = {"bets": _st_bets, "spent": round(_st_spent, 2), "profit": round(_st_profit, 2)}

    # Précision jour par jour
    profitable_ranges_daily = get_profitable_odds_ranges(min_samples=5)

    def _is_champion_pred(p: dict) -> bool:
        """True si la prédiction était un Champion (flag persisté ou fallback cotes rentables)."""
        stored = p.get("champion")
        if stored is not None:
            return bool(stored)
        odds = float(p.get("odds") or 0)
        return any(lo <= odds < hi for lo, hi, _ in profitable_ranges_daily)

    daily: dict[str, dict] = {}
    for p in preds:
        d = p.get("date") or ""
        if not d:
            continue
        if d not in daily:
            daily[d] = {"wins": 0, "total": 0, "preds": [],
                        "champ_wins": 0, "champ_total": 0}
        daily[d]["total"] += 1
        daily[d]["preds"].append(p)
        if p["outcome"] == "win":
            daily[d]["wins"] += 1
        if _is_champion_pred(p):
            daily[d]["champ_total"] += 1
            if p["outcome"] == "win":
                daily[d]["champ_wins"] += 1
    daily_accuracy = []
    for d, v in sorted(daily.items()):
        sim     = _sim_financials(v["preds"])
        sim_k   = _sim_kelly(v["preds"])
        roi     = round(sim["profit"] / sim["spent"] * 100, 1) if sim["spent"] > 0 else 0.0
        roi_k   = round(sim_k["profit"] / sim_k["spent"] * 100, 1) if sim_k["spent"] > 0 else 0.0
        ct = v["champ_total"]
        cw = v["champ_wins"]
        daily_accuracy.append({
            "date":           d,
            "wins":           v["wins"],
            "total":          v["total"],
            "win_rate":       round(v["wins"] / v["total"] * 100, 1),
            "roi":            roi,
            "profit":         sim["profit"],
            "kelly_profit":   sim_k["profit"],
            "kelly_spent":    sim_k["spent"],
            "kelly_roi":      roi_k,
            "champ_wins":     cw,
            "champ_total":    ct,
            "champ_win_rate": round(cw / ct * 100, 1) if ct > 0 else None,
        })

    # Performance par tranche de cotes
    _odds_ranges = [
        ("<1.50",     0.0,  1.50),
        ("1.50-1.70", 1.50, 1.70),
        ("1.70-1.90", 1.70, 1.90),
        ("1.90-2.20", 1.90, 2.20),
        ("2.20+",     2.20, 99.0),
    ]
    odds_accuracy = []
    for label, lo, hi in _odds_ranges:
        group = [p for p in preds if lo <= float(p.get("odds") or 0) < hi]
        if not group:
            continue
        g_wins    = sum(1 for p in group if p["outcome"] == "win")
        g_wr      = g_wins / len(group)
        avg_odds  = sum(float(p.get("odds") or 0) for p in group) / len(group)
        roi       = round((g_wr * avg_odds - 1) * 100, 1)
        seuil     = round((1 / avg_odds) * 100, 1) if avg_odds > 0 else 0
        odds_accuracy.append({
            "range":    label,
            "total":    len(group),
            "wins":     g_wins,
            "win_rate": round(g_wr * 100, 1),
            "avg_odds": round(avg_odds, 2),
            "seuil":    seuil,
            "roi":      roi,
            "positive": g_wr * 100 > seuil,
        })

    # Corrélation value_score → réussite (uniquement les prédictions avec score sauvegardé)
    scored = [p for p in preds if p.get("value_score") is not None and p["value_score"] > 0]
    score_buckets: dict[str, dict] = {}
    for p in scored:
        vs = p["value_score"]
        if vs >= 100:  sk = "100+"
        elif vs >= 90: sk = "90-100"
        elif vs >= 80: sk = "80-90"
        elif vs >= 70: sk = "70-80"
        elif vs >= 60: sk = "60-70"
        else:          sk = "<60"
        if sk not in score_buckets:
            score_buckets[sk] = {"wins": 0, "total": 0}
        score_buckets[sk]["total"] += 1
        if p["outcome"] == "win":
            score_buckets[sk]["wins"] += 1
    score_accuracy = [
        {
            "range":    k,
            "total":    v["total"],
            "wins":     v["wins"],
            "win_rate": round(v["wins"] / v["total"] * 100, 1),
        }
        for k, v in sorted(score_buckets.items(),
                           key=lambda x: float(x[0].replace("+","").split("-")[0]),
                           reverse=True)
    ]

    # Réussite par équipe
    team_stats: dict[str, dict] = {}
    for p in preds:
        for team in (p.get("home_team", ""), p.get("away_team", "")):
            if not team:
                continue
            if team not in team_stats:
                team_stats[team] = {"wins": 0, "total": 0}
            team_stats[team]["total"] += 1
            if p["outcome"] == "win":
                team_stats[team]["wins"] += 1
    team_accuracy = sorted(
        [
            {
                "team":     team,
                "total":    v["total"],
                "wins":     v["wins"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1),
            }
            for team, v in team_stats.items()
            if v["total"] >= 2
        ],
        key=lambda x: (-x["wins"], -x["win_rate"]),
    )

    # Réussite par type de pari (Excellent + Bon — même sport que la calibration)
    all_resolved = [p for p in _load()
                    if p["outcome"] in ("win", "loss")
                    and ("Excellent" in (p.get("recommendation") or "")
                         or "Bon" in (p.get("recommendation") or ""))
                    and _sport_match(p)]
    bt_stats: dict[str, dict] = {}
    for p in all_resolved:
        bt_raw  = p.get("bet_type") or ""
        ht      = p.get("home_team") or ""
        at      = p.get("away_team") or ""
        cat     = classify_bet_type(bt_raw, ht, at)
        if cat not in bt_stats:
            bt_stats[cat] = {"wins": 0, "total": 0, "profit": 0.0}
        bt_stats[cat]["total"] += 1
        if p["outcome"] == "win":
            bt_stats[cat]["wins"] += 1
            bt_stats[cat]["profit"] += _SIM_BET * (float(p.get("odds") or 0) - 1.0)
        else:
            bt_stats[cat]["profit"] -= _SIM_BET
    bet_type_accuracy = sorted(
        [
            {
                "category": cat,
                "total":    v["total"],
                "wins":     v["wins"],
                "win_rate": round(v["wins"] / v["total"] * 100, 1),
                "roi":      round(v["profit"] / (v["total"] * _SIM_BET) * 100, 1),
            }
            for cat, v in bt_stats.items()
        ],
        key=lambda x: -x["total"],
    )

    # Réussite des Champions : Excellents dans les zones historiquement rentables
    champion_preds = [p for p in preds if _is_champion_pred(p)]
    if champion_preds:
        c_wins = sum(1 for p in champion_preds if p["outcome"] == "win")
        c_sim  = _sim_financials(champion_preds)
        champion_accuracy = {
            "total":    len(champion_preds),
            "wins":     c_wins,
            "win_rate": round(c_wins / len(champion_preds) * 100, 1),
            "roi":      round(c_sim["profit"] / c_sim["spent"] * 100, 1) if c_sim["spent"] > 0 else 0.0,
            "profit":   c_sim["profit"],
        }
    else:
        champion_accuracy = None

    return {
        "count":              len(preds),
        "wins":               wins,
        "win_rate":           round(win_rate * 100, 1),
        "avg_predicted":      round(avg_predicted * 100, 1),
        "bias_factor":        round(bias_factor, 3),
        "buckets":            buckets_out,
        "correction_active":  len(preds) >= MIN_OUTCOMES_FOR_CORRECTION,
        "feature_accuracies": feature_accuracies,
        "feature_weights":    feature_weights,
        "sim_yesterday":      sim_yesterday,
        "sim_total":          sim_total,
        "odds_accuracy":      odds_accuracy,
        "daily_accuracy":     daily_accuracy,
        "score_accuracy":     score_accuracy,
        "champion_accuracy":  champion_accuracy,
        "team_accuracy":      team_accuracy,
        "bet_type_accuracy":  bet_type_accuracy,
    }
