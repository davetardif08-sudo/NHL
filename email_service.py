"""
email_service.py — Service d'envoi des mises NHL par courriel via SendGrid.

Usage:
    from email_service import send_betting_summary, schedule_daily_email

    # Envoi manuel / test
    result = send_betting_summary(picks)

    # Démarrer le planificateur (5h PM ET, tourne en arrière-plan)
    schedule_daily_email(get_picks_fn)
"""

import os
import threading
import time
from datetime import datetime, timezone, timedelta

# ── Chargement .env ──────────────────────────────────────────────────────────
_ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
if os.path.isfile(_ENV_PATH):
    with open(_ENV_PATH, encoding="utf-8") as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY", "")
TO_EMAIL         = os.getenv("TO_EMAIL", "")
FROM_EMAIL       = os.getenv("FROM_EMAIL", "")

# Heure d'envoi automatique (fuseau ET)
SEND_HOUR_ET = 17   # 17h00 = 5:00 PM ET
SEND_MINUTE_ET = 0

# ET = UTC-5 (hiver) ou UTC-4 (heure d'été)
# Calcul automatique : mars→novembre = heure d'été (UTC-4), sinon UTC-5
def _et_now() -> datetime:
    utc_now = datetime.now(timezone.utc)
    month = utc_now.month
    is_dst = 3 <= month <= 11   # approximation DST
    offset = timedelta(hours=-4 if is_dst else -5)
    return (utc_now + offset).replace(tzinfo=timezone(offset))


# ── HTML / texte ─────────────────────────────────────────────────────────────

def _fmt_gain(mise: float, odds: float) -> str:
    """Calcule et formate le gain potentiel."""
    gain = round(mise * odds, 2)
    return f"{gain:.2f}$"

def _fmt_money(val: float) -> str:
    return f"{val:.2f}$".replace(".", ",")

def _build_email(picks: list, date_str: str, sgp_proposals: list | None = None) -> tuple[str, str, str]:
    """
    Construit (subject, body_text, body_html) à partir de la liste de picks.
    Chaque pick doit avoir : match, selection, bet_type, odds, mise, time (optionnel).
    Affiche TOUS les picks : ceux avec mise en premier (section "Paris"), puis les
    autres en section "Surveillance", et enfin les Combos Même Match.
    """
    subject = f"🏒 Prédictions NHL du {date_str}"

    # Séparer paris avec mise vs surveillance
    picks_mise  = [p for p in picks if p.get("mise") and float(p.get("mise") or 0) > 0]
    picks_watch = [p for p in picks if not (p.get("mise") and float(p.get("mise") or 0) > 0)]

    # ── Calculs globaux ──────────────────────────────────────────────────────
    total_mise  = sum(float(p.get("mise") or 0) for p in picks_mise)
    total_gain  = sum(float(p.get("mise") or 0) * float(p.get("odds") or 1) for p in picks_mise)

    # ── Helpers texte brut ───────────────────────────────────────────────────
    def _pick_short_name(match):
        mparts = match.split(" @ ")
        away = mparts[0].split("(")[-1].rstrip(")") if "(" in mparts[0] else mparts[0]
        home = (mparts[1].split("(")[-1].rstrip(")") if "(" in mparts[1] else mparts[1]) if len(mparts) > 1 else ""
        return f"{away} @ {home}"

    # ── Texte brut ───────────────────────────────────────────────────────────
    lines_txt = [
        f"Bonne soirée de hockey ! Voici les prédictions pour ce soir ({date_str}) :\n",
    ]

    if picks_mise:
        lines_txt.append(f"{'═'*50}")
        lines_txt.append(f"  💵 PARIS DU SOIR ({len(picks_mise)} mises)")
        lines_txt.append(f"{'═'*50}")
        for p in picks_mise:
            match     = p.get("match", "?")
            selection = p.get("selection", "?")
            bet_type  = p.get("bet_type", "")
            odds      = float(p.get("odds") or 1)
            mise      = float(p.get("mise") or 0)
            heure     = p.get("time", "")
            gain      = round(mise * odds, 2)
            header    = _pick_short_name(match)
            if heure:
                header += f"  —  {heure}"
            lines_txt += [
                f"\n{header}",
                f"  ✅ Pick     : {selection}  ({bet_type})",
                f"  📈 Cote     : {odds:.2f}",
                f"  💵 Mise     : {_fmt_money(mise)}  →  Gain potentiel : {_fmt_money(gain)}",
            ]
        lines_txt += [
            f"\n{'─'*50}",
            f"Total misé ce soir     : {_fmt_money(total_mise)}",
            f"Gain potentiel total   : {_fmt_money(total_gain)}",
        ]

    if picks_watch:
        lines_txt.append(f"\n{'═'*50}")
        lines_txt.append(f"  👁  EN SURVEILLANCE ({len(picks_watch)} picks)")
        lines_txt.append(f"{'═'*50}")
        for p in picks_watch:
            match     = p.get("match", "?")
            selection = p.get("selection", "?")
            bet_type  = p.get("bet_type", "")
            odds      = float(p.get("odds") or 1)
            heure     = p.get("time", "")
            header    = _pick_short_name(match)
            if heure:
                header += f"  —  {heure}"
            lines_txt += [
                f"\n{header}",
                f"  👁  Pick     : {selection}  ({bet_type})",
                f"  📈 Cote     : {odds:.2f}",
            ]

    lines_txt.append(f"\n— DaveBet NHL")
    body_text = "\n".join(lines_txt)

    # ── HTML helpers ─────────────────────────────────────────────────────────
    def _pick_row_html(p, surveillance=False):
        match     = p.get("match", "?")
        selection = p.get("selection", "?")
        bet_type  = p.get("bet_type", "")
        odds      = float(p.get("odds") or 1)
        mise      = float(p.get("mise") or 0) if not surveillance else 0
        heure     = p.get("time", "")
        gain      = round(mise * odds, 2) if not surveillance else 0
        champion  = p.get("champion", False)

        mparts = match.split(" @ ")
        away   = mparts[0].split("(")[-1].rstrip(")") if "(" in mparts[0] else mparts[0]
        home   = (mparts[1].split("(")[-1].rstrip(")") if "(" in mparts[1] else mparts[1]) if len(mparts) > 1 else ""
        badge  = ' <span style="background:#f59e0b;color:#000;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:700;">🏆 CHAMPION</span>' if champion else ""

        row_bg  = "background:rgba(56,189,248,0.04);" if not surveillance else "background:rgba(255,255,255,0.02);"
        name_color = "#38bdf8" if not surveillance else "#64748b"
        odds_color = "#a3e635" if not surveillance else "#64748b"

        if surveillance:
            mise_cell = '<td style="padding:10px 8px;border-bottom:1px solid #1e293b;text-align:center;color:#475569;font-style:italic;">—</td>'
            gain_cell = '<td style="padding:10px 8px;border-bottom:1px solid #1e293b;text-align:center;color:#475569;font-style:italic;">—</td>'
        else:
            mise_cell = f'<td style="padding:10px 8px;border-bottom:1px solid #2d3748;text-align:center;color:#e2e8f0;">{_fmt_money(mise)}</td>'
            gain_cell = f'<td style="padding:10px 8px;border-bottom:1px solid #2d3748;text-align:center;color:#f59e0b;font-weight:700;">{_fmt_money(gain)}</td>'

        border_color = "#2d3748" if not surveillance else "#1e293b"
        sel_color    = "#e2e8f0" if not surveillance else "#94a3b8"

        return f"""
        <tr style="{row_bg}">
          <td style="padding:10px 8px;border-bottom:1px solid {border_color};">
            <strong style="color:{name_color};font-size:13px;">{away} @ {home}</strong>{badge}
            {'<br><span style="font-size:11px;color:#718096;">⏰ ' + heure + '</span>' if heure else ''}
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid {border_color};color:{sel_color};font-size:13px;">
            {selection}<br>
            <span style="font-size:11px;color:#718096;">{bet_type}</span>
          </td>
          <td style="padding:10px 8px;border-bottom:1px solid {border_color};text-align:center;color:{odds_color};font-weight:700;">
            {odds:.2f}
          </td>
          {mise_cell}
          {gain_cell}
        </tr>"""

    # ── Section paris avec mise ───────────────────────────────────────────────
    section_mise_html = ""
    if picks_mise:
        rows = "".join(_pick_row_html(p, surveillance=False) for p in picks_mise)
        section_mise_html = f"""
    <!-- Section Paris -->
    <tr>
      <td style="padding:20px 32px 4px;">
        <p style="margin:0 0 8px;color:#38bdf8;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;">
          💵 Paris du soir — {len(picks_mise)} mise{'s' if len(picks_mise) > 1 else ''}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <thead>
            <tr style="background:#0f172a;">
              <th style="padding:8px 8px;text-align:left;color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #334155;">Match</th>
              <th style="padding:8px 8px;text-align:left;color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #334155;">Sélection</th>
              <th style="padding:8px 8px;text-align:center;color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #334155;">Cote</th>
              <th style="padding:8px 8px;text-align:center;color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #334155;">Mise</th>
              <th style="padding:8px 8px;text-align:center;color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid #334155;">Gain potentiel</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </td>
    </tr>
    <!-- Totaux -->
    <tr>
      <td style="padding:8px 32px 20px;">
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#0f172a;border-radius:8px;padding:14px;">
          <tr>
            <td style="color:#94a3b8;font-size:13px;padding:3px 14px;">Total misé ce soir</td>
            <td style="color:#e2e8f0;font-size:14px;font-weight:700;text-align:right;padding:3px 14px;">{_fmt_money(total_mise)}</td>
          </tr>
          <tr>
            <td style="color:#94a3b8;font-size:13px;padding:3px 14px;">Gain potentiel total</td>
            <td style="color:#f59e0b;font-size:14px;font-weight:700;text-align:right;padding:3px 14px;">{_fmt_money(total_gain)}</td>
          </tr>
        </table>
      </td>
    </tr>"""

    # ── Section surveillance ──────────────────────────────────────────────────
    section_watch_html = ""
    if picks_watch:
        rows = "".join(_pick_row_html(p, surveillance=True) for p in picks_watch)
        section_watch_html = f"""
    <!-- Séparateur -->
    <tr>
      <td style="padding:0 32px;">
        <hr style="border:none;border-top:1px solid #1e3a5f;margin:0;">
      </td>
    </tr>
    <!-- Section Surveillance -->
    <tr>
      <td style="padding:16px 32px 4px;">
        <p style="margin:0 0 8px;color:#64748b;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;">
          👁 En surveillance — {len(picks_watch)} prédiction{'s' if len(picks_watch) > 1 else ''}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <thead>
            <tr style="background:#0d1526;">
              <th style="padding:7px 8px;text-align:left;color:#3d5068;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e293b;">Match</th>
              <th style="padding:7px 8px;text-align:left;color:#3d5068;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e293b;">Sélection</th>
              <th style="padding:7px 8px;text-align:center;color:#3d5068;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e293b;">Cote</th>
              <th style="padding:7px 8px;text-align:center;color:#3d5068;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e293b;">Mise</th>
              <th style="padding:7px 8px;text-align:center;color:#3d5068;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #1e293b;">Gain potentiel</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </td>
    </tr>"""

    # ── Section combos SGP ───────────────────────────────────────────────────
    combos = sgp_proposals or []
    section_sgp_html = ""
    if combos:
        combo_rows = ""
        for c in combos:
            short  = c.get("short_match") or c.get("match", "?")
            label  = c.get("label", "?")
            ctype  = c.get("combo_type", "")
            codds  = float(c.get("combined_odds") or 1.0)
            cmise  = c.get("mise")
            cgain  = round(float(cmise) * codds, 2) if cmise else None
            picks2 = c.get("picks") or []

            # Détail des deux picks
            pick_details = ""
            for pp in picks2:
                pp_sel  = pp.get("selection") or ""
                pp_bt   = pp.get("bet_type") or ""
                pp_odds = float(pp.get("odds") or 1.0)
                pick_details += f'<div style="font-size:11px;color:#94a3b8;margin-top:2px;">↳ {pp_sel} <span style="color:#64748b;">({pp_bt})</span> @ <span style="color:#a3e635;">{pp_odds:.2f}</span></div>'

            if cmise:
                mise_cell = f'<td style="padding:10px 8px;border-bottom:1px solid #2d3748;text-align:center;color:#e2e8f0;">{_fmt_money(float(cmise))}</td>'
                gain_cell = f'<td style="padding:10px 8px;border-bottom:1px solid #2d3748;text-align:center;color:#f59e0b;font-weight:700;">{_fmt_money(cgain)}</td>'
            else:
                mise_cell = '<td style="padding:10px 8px;border-bottom:1px solid #1e293b;text-align:center;color:#475569;font-style:italic;">—</td>'
                gain_cell = '<td style="padding:10px 8px;border-bottom:1px solid #1e293b;text-align:center;color:#475569;font-style:italic;">—</td>'

            combo_rows += f"""
            <tr style="background:rgba(168,85,247,0.04);">
              <td style="padding:10px 8px;border-bottom:1px solid #2d3748;">
                <strong style="color:#c084fc;font-size:13px;">{short}</strong>
                <div style="font-size:11px;color:#7c3aed;margin-top:1px;">{ctype}</div>
                {pick_details}
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #2d3748;color:#e2e8f0;font-size:13px;">
                {label}
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #2d3748;text-align:center;color:#a3e635;font-weight:700;">
                {codds:.2f}
              </td>
              {mise_cell}
              {gain_cell}
            </tr>"""

        section_sgp_html = f"""
    <!-- Séparateur SGP -->
    <tr>
      <td style="padding:0 32px;">
        <hr style="border:none;border-top:1px solid #2d1f4e;margin:0;">
      </td>
    </tr>
    <!-- Section Combos -->
    <tr>
      <td style="padding:16px 32px 4px;">
        <p style="margin:0 0 8px;color:#c084fc;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;">
          🎯 Combos Même Match — {len(combos)} proposition{'s' if len(combos) > 1 else ''}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
          <thead>
            <tr style="background:#120d1f;">
              <th style="padding:7px 8px;text-align:left;color:#4c1d7a;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2d1f4e;">Match</th>
              <th style="padding:7px 8px;text-align:left;color:#4c1d7a;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2d1f4e;">Sélections</th>
              <th style="padding:7px 8px;text-align:center;color:#4c1d7a;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2d1f4e;">Cote combo</th>
              <th style="padding:7px 8px;text-align:center;color:#4c1d7a;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2d1f4e;">Mise</th>
              <th style="padding:7px 8px;text-align:center;color:#4c1d7a;font-size:10px;text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid #2d1f4e;">Gain potentiel</th>
            </tr>
          </thead>
          <tbody>{combo_rows}</tbody>
        </table>
      </td>
    </tr>"""

    # Intro : compter les combos avec mise aussi
    n_combos_mise = sum(1 for c in combos if c.get("mise") and float(c.get("mise") or 0) > 0)
    intro_combos = f' · <strong style="color:#c084fc;">{len(combos)} combo{"s" if len(combos) > 1 else ""}</strong>' if combos else ""

    # Texte brut — section combos
    if combos:
        lines_txt.insert(-1, f"\n{'═'*50}")
        lines_txt.insert(-1, f"  🎯 COMBOS MÊME MATCH ({len(combos)} propositions)")
        lines_txt.insert(-1, f"{'═'*50}")
        for c in combos:
            short  = c.get("short_match") or c.get("match", "?")
            label  = c.get("label", "?")
            codds  = float(c.get("combined_odds") or 1.0)
            cmise  = c.get("mise")
            lines_txt.insert(-1, f"\n{short}")
            lines_txt.insert(-1, f"  🎯 Combo    : {label}")
            lines_txt.insert(-1, f"  📈 Cote     : {codds:.2f}")
            if cmise:
                cgain = round(float(cmise) * codds, 2)
                lines_txt.insert(-1, f"  💵 Mise     : {_fmt_money(float(cmise))}  →  Gain potentiel : {_fmt_money(cgain)}")

    body_html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>{subject}</title></head>
<body style="margin:0;padding:0;background:#0f172a;font-family:'Segoe UI',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:660px;margin:0 auto;background:#1e293b;border-radius:12px;overflow:hidden;">

    <!-- Header -->
    <tr>
      <td style="background:linear-gradient(135deg,#0f172a,#1e3a5f);padding:28px 32px;text-align:center;">
        <div style="font-size:28px;">🏒</div>
        <h1 style="margin:8px 0 4px;color:#38bdf8;font-size:22px;font-weight:800;letter-spacing:.5px;">
          DaveBet NHL
        </h1>
        <p style="margin:0;color:#94a3b8;font-size:13px;">Prédictions du soir — {date_str}</p>
      </td>
    </tr>

    <!-- Intro -->
    <tr>
      <td style="padding:20px 32px 4px;">
        <p style="margin:0;color:#cbd5e1;font-size:14px;line-height:1.6;">
          Bonne soirée de hockey ! <strong style="color:#38bdf8;">{len(picks_mise)} mise{'s' if len(picks_mise) > 1 else ''}</strong> ce soir
          {f'· <strong style="color:#64748b;">{len(picks_watch)} en surveillance</strong>' if picks_watch else ''}{intro_combos}
        </p>
      </td>
    </tr>

    {section_mise_html}
    {section_watch_html}
    {section_sgp_html}

    <!-- Footer -->
    <tr>
      <td style="background:#0f172a;padding:16px 32px;text-align:center;margin-top:8px;">
        <p style="margin:0;color:#475569;font-size:11px;">
          Généré automatiquement par DaveBet · {date_str}
        </p>
      </td>
    </tr>

  </table>
</body>
</html>"""

    return subject, body_text, body_html


# ── Envoi via SendGrid ────────────────────────────────────────────────────────

def send_betting_summary(picks: list, date_str: str | None = None, sgp_proposals: list | None = None) -> dict:
    """
    Envoie le courriel de résumé des mises via SendGrid.

    Args:
        picks         : liste de picks (dicts avec mise, odds, match, selection, etc.)
        date_str      : date à afficher (ex. "25 mars 2026"), défaut = aujourd'hui
        sgp_proposals : propositions de combos Même Match (optionnel)

    Returns:
        dict avec 'ok' (bool), 'status_code', 'message'
    """
    if not SENDGRID_API_KEY:
        return {"ok": False, "message": "SENDGRID_API_KEY non configurée dans .env"}
    if not TO_EMAIL:
        return {"ok": False, "message": "TO_EMAIL non configurée dans .env"}
    if not FROM_EMAIL:
        return {"ok": False, "message": "FROM_EMAIL non configurée dans .env"}

    # Filtrer seulement les prédictions Excellent ("Excellent ***" ou champion)
    # On inclut TOUS ces picks — ceux avec mise seront affichés comme paris,
    # les autres comme "en surveillance".
    picks_all = [
        p for p in picks
        if "Excellent" in (p.get("recommendation") or "") or p.get("champion")
    ]
    # Fallback : si aucun filtre ne correspond (ancienne structure), prendre tous les picks
    if not picks_all:
        picks_all = picks

    if not picks_all:
        return {"ok": False, "message": "Aucune prédiction ce soir — courriel non envoyé"}

    if not date_str:
        now_et    = _et_now()
        MOIS_FR   = ["janvier","février","mars","avril","mai","juin",
                     "juillet","août","septembre","octobre","novembre","décembre"]
        date_str  = f"{now_et.day} {MOIS_FR[now_et.month-1]} {now_et.year}"

    subject, body_text, body_html = _build_email(picks_all, date_str, sgp_proposals=sgp_proposals)

    try:
        import urllib.request, urllib.error, json as _json

        payload = {
            "personalizations": [{"to": [{"email": TO_EMAIL}]}],
            "from":             {"email": FROM_EMAIL, "name": "DaveBet NHL"},
            "subject":          subject,
            "content": [
                {"type": "text/plain", "value": body_text},
                {"type": "text/html",  "value": body_html},
            ],
        }
        data = _json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data    = data,
            headers = {
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type":  "application/json",
            },
            method  = "POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
        return {"ok": status in (200, 202), "status_code": status,
                "message": f"Courriel envoyé à {TO_EMAIL} (HTTP {status})"}

    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def send_real_bets_summary(sessions: list, balance_info: dict | None = None) -> dict:
    """
    Envoie un courriel récapitulatif des mises réelles (toutes les sessions).
    sessions : liste de sessions enrichies (avec picks.outcome, picks.net, etc.)
    balance_info : {"latest": float, "first": float} ou None
    """
    if not SENDGRID_API_KEY:
        return {"ok": False, "message": "SENDGRID_API_KEY non configurée dans .env"}
    if not TO_EMAIL or not FROM_EMAIL:
        return {"ok": False, "message": "TO_EMAIL / FROM_EMAIL non configurés dans .env"}
    if not sessions:
        return {"ok": False, "message": "Aucune session à envoyer"}

    now_et   = _et_now()
    MOIS_FR  = ["janvier","février","mars","avril","mai","juin",
                "juillet","août","septembre","octobre","novembre","décembre"]
    date_str = f"{now_et.day} {MOIS_FR[now_et.month-1]} {now_et.year}"

    latest_bal = (balance_info or {}).get("latest")
    first_bal  = (balance_info or {}).get("first")
    net_cum    = round(latest_bal - first_bal, 2) if latest_bal is not None and first_bal is not None else None

    all_picks   = [p for s in sessions for p in (s.get("picks") or [])]
    resolved    = [p for p in all_picks if p.get("outcome") in ("win", "loss")]
    total_wins  = sum(1 for p in resolved if p.get("outcome") == "win")
    win_rate    = round(total_wins / len(resolved) * 100, 1) if resolved else 0.0
    total_mise  = sum(float(p.get("mise_reelle") or 0) for p in all_picks)
    total_net   = sum(float(p.get("net") or 0) for p in resolved)

    # ── Construire les lignes HTML par session ────────────────────────────────
    rows_html = ""
    for s in sessions:
        picks = s.get("picks") or []
        s_resolved = [p for p in picks if p.get("outcome") in ("win", "loss")]
        s_wins = sum(1 for p in s_resolved if p.get("outcome") == "win")
        s_net  = s.get("net_total", 0) or 0
        net_color = "#3fb950" if s_net >= 0 else "#f85149"

        pick_rows = ""
        for p in picks:
            oc   = p.get("outcome", "")
            net  = p.get("net")
            mise = float(p.get("mise_reelle") or 0)
            odds = float(p.get("odds") or 1)
            fp   = p.get("fair_prob")
            fp_str = f"{float(fp):.1f}%" if fp else "—"
            if oc == "win":
                oc_cell = f'<td style="padding:7px 8px;color:#3fb950;font-weight:700;">✅ +{float(net):.2f}$</td>'
            elif oc == "loss":
                oc_cell = f'<td style="padding:7px 8px;color:#f85149;font-weight:700;">❌ −{abs(float(net)):.2f}$</td>'
            else:
                oc_cell = '<td style="padding:7px 8px;color:#64748b;">⏳ En cours</td>'
            pick_rows += f"""<tr style="border-top:1px solid #1e293b;">
              <td style="padding:7px 8px;color:#e2e8f0;font-size:12px;">{p.get("match","?")}</td>
              <td style="padding:7px 8px;color:#94a3b8;font-size:11px;">{p.get("selection","?")}</td>
              <td style="padding:7px 8px;text-align:center;color:#a3e635;font-weight:700;">{odds:.2f}</td>
              <td style="padding:7px 8px;text-align:center;color:#38bdf8;">{mise:.2f}$</td>
              <td style="padding:7px 8px;text-align:center;color:#94a3b8;font-size:11px;">{fp_str}</td>
              {oc_cell}
            </tr>"""

        rows_html += f"""
    <tr><td style="padding:4px 32px 0;">
      <div style="background:#0d1117;border:1px solid #21262d;border-radius:8px;margin-bottom:10px;">
        <div style="padding:10px 14px;border-bottom:1px solid #21262d;display:flex;justify-content:space-between;align-items:center;">
          <span style="color:#c9d1d9;font-weight:700;font-size:13px;">📅 {s.get("date","?")} à {s.get("time","?")}</span>
          <span style="font-weight:700;font-size:13px;color:{net_color};">{'+' if s_net >= 0 else ''}{s_net:.2f}$ &nbsp;({s_wins}/{len(s_resolved)})</span>
        </div>
        <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:12px;">
          <thead><tr style="background:#161b22;">
            <th style="padding:6px 8px;text-align:left;color:#4a5568;font-size:10px;text-transform:uppercase;">Match</th>
            <th style="padding:6px 8px;text-align:left;color:#4a5568;font-size:10px;text-transform:uppercase;">Sélection</th>
            <th style="padding:6px 8px;text-align:center;color:#4a5568;font-size:10px;text-transform:uppercase;">Cote</th>
            <th style="padding:6px 8px;text-align:center;color:#4a5568;font-size:10px;text-transform:uppercase;">Mise</th>
            <th style="padding:6px 8px;text-align:center;color:#4a5568;font-size:10px;text-transform:uppercase;">% prédit</th>
            <th style="padding:6px 8px;color:#4a5568;font-size:10px;text-transform:uppercase;">Résultat</th>
          </tr></thead>
          <tbody>{pick_rows}</tbody>
        </table>
      </div>
    </td></tr>"""

    # ── Résumé global ─────────────────────────────────────────────────────────
    bal_line = (f"<tr><td align='center' style='padding:4px 0;color:#94a3b8;font-size:13px;'>"
                f"Solde actuel : <strong style='color:#38bdf8;'>{latest_bal:.2f}$</strong>"
                f"&nbsp;·&nbsp;Net cumulatif : <strong style='color:{'#3fb950' if (net_cum or 0) >= 0 else '#f85149'};'>"
                f"{'+' if (net_cum or 0) >= 0 else ''}{net_cum:.2f}$</strong></td></tr>"
                ) if latest_bal is not None else ""

    body_html = f"""<!DOCTYPE html><html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0d1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 8px;">
<table width="600" cellpadding="0" cellspacing="0" style="background:#161b22;border:1px solid #21262d;border-radius:12px;overflow:hidden;">
  <!-- En-tête -->
  <tr><td style="padding:20px 32px;background:linear-gradient(135deg,#1e3a5f,#0d2137);">
    <div style="font-size:22px;font-weight:800;color:#ffffff;">💰 DaveBet — Bilan Mises Réelles</div>
    <div style="font-size:13px;color:#94a3b8;margin-top:4px;">Rapport du {date_str}</div>
  </td></tr>
  <!-- Stats globales -->
  <tr><td style="padding:16px 32px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td align="center" style="padding:10px;background:#0d1117;border:1px solid #21262d;border-radius:8px;margin:4px;">
        <div style="font-size:22px;font-weight:800;color:#e2e8f0;">{len(sessions)}</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Sessions</div>
      </td>
      <td width="8"></td>
      <td align="center" style="padding:10px;background:#0d1117;border:1px solid #21262d;border-radius:8px;">
        <div style="font-size:22px;font-weight:800;color:#38bdf8;">{total_mise:.2f}$</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Total misé</div>
      </td>
      <td width="8"></td>
      <td align="center" style="padding:10px;background:#0d1117;border:1px solid #21262d;border-radius:8px;">
        <div style="font-size:22px;font-weight:800;color:#a3e635;">{win_rate}%</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Win rate ({total_wins}/{len(resolved)})</div>
      </td>
      <td width="8"></td>
      <td align="center" style="padding:10px;background:#0d1117;border:1px solid #21262d;border-radius:8px;">
        <div style="font-size:22px;font-weight:800;color:{'#3fb950' if total_net >= 0 else '#f85149'};">{'+' if total_net >= 0 else ''}{total_net:.2f}$</div>
        <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Net sessions</div>
      </td>
    </tr></table>
    {bal_line if bal_line else ""}
  </td></tr>
  <!-- Sessions -->
  {rows_html}
  <!-- Footer -->
  <tr><td style="padding:16px 32px;border-top:1px solid #21262d;text-align:center;color:#4a5568;font-size:11px;">
    DaveBet NHL · Rapport généré le {date_str}
  </td></tr>
</table></td></tr></table></body></html>"""

    body_text = f"DaveBet — Bilan Mises Réelles — {date_str}\n\n"
    body_text += f"Sessions : {len(sessions)} | Total misé : {total_mise:.2f}$ | Win rate : {win_rate}% ({total_wins}/{len(resolved)}) | Net : {'+' if total_net >= 0 else ''}{total_net:.2f}$\n"
    if net_cum is not None:
        body_text += f"Solde actuel : {latest_bal:.2f}$ | Net cumulatif : {'+' if net_cum >= 0 else ''}{net_cum:.2f}$\n"
    body_text += "\n"
    for s in sessions:
        picks = s.get("picks") or []
        body_text += f"\n{s.get('date','?')} à {s.get('time','?')} — Net: {'+' if (s.get('net_total') or 0) >= 0 else ''}{s.get('net_total', 0):.2f}$\n"
        for p in picks:
            oc   = p.get("outcome", "?")
            icon = "✅" if oc == "win" else ("❌" if oc == "loss" else "⏳")
            body_text += f"  {icon} {p.get('match','?')} — {p.get('selection','?')} @ {float(p.get('odds',1)):.2f} ({float(p.get('mise_reelle',0)):.2f}$)\n"

    subject = f"💰 Bilan Mises Réelles — DaveBet NHL ({date_str})"

    try:
        import urllib.request, urllib.error, json as _json
        payload = {
            "personalizations": [{"to": [{"email": TO_EMAIL}]}],
            "from":   {"email": FROM_EMAIL, "name": "DaveBet NHL"},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": body_text},
                {"type": "text/html",  "value": body_html},
            ],
        }
        data = _json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data    = data,
            headers = {"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            method  = "POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
        return {"ok": status in (200, 202), "status_code": status,
                "message": f"Courriel envoyé à {TO_EMAIL} (HTTP {status})"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


# ── Planificateur quotidien ───────────────────────────────────────────────────

_scheduler_started = False
_snapshot_scheduler_started = False

def schedule_daily_email(get_picks_fn, get_sgp_fn=None, hour: int = SEND_HOUR_ET, minute: int = SEND_MINUTE_ET):
    """
    Lance un thread de fond qui déclenche send_betting_summary() chaque jour
    à l'heure spécifiée (fuseau ET).

    Args:
        get_picks_fn : callable sans argument → retourne la liste de picks hockey
        get_sgp_fn   : callable sans argument → retourne la liste de combos (optionnel)
        hour / minute : heure ET de l'envoi (défaut 17h00)
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        print(f"[email] Planificateur démarré — envoi quotidien à {hour:02d}h{minute:02d} ET")
        last_sent_date = None

        while True:
            now_et = _et_now()
            today  = now_et.date().isoformat()

            if (now_et.hour == hour and now_et.minute == minute
                    and last_sent_date != today):
                try:
                    picks  = get_picks_fn() or []
                    sgps   = get_sgp_fn() if get_sgp_fn else []
                    MOIS_FR = ["janvier","février","mars","avril","mai","juin",
                               "juillet","août","septembre","octobre","novembre","décembre"]
                    ds = f"{now_et.day} {MOIS_FR[now_et.month-1]} {now_et.year}"
                    result = send_betting_summary(picks, ds, sgp_proposals=sgps)
                    print(f"[email] {result['message']}")
                    last_sent_date = today
                except Exception as exc:
                    print(f"[email] Erreur planificateur : {exc}")

            time.sleep(30)   # vérifie toutes les 30 secondes

    t = threading.Thread(target=_loop, daemon=True, name="email-scheduler")
    t.start()


# ── Snapshot Dynamique (30 min avant le premier match NHL) ────────────────────

def schedule_dynamic_snapshots(get_picks_fn, get_sgp_fn=None):
    """
    Lance un thread de fond qui :
    1. Chaque matin à 8h ET, scrape les matchs NHL
    2. Trouve l'heure du premier match
    3. Déclenche le snapshot 30 min avant ce premier match

    Cela assure que les snapshots se prennent au bon moment, indépendamment
    de l'heure variable du premier match chaque jour.
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        print("[snapshot] Planificateur dynamique démarré — snapshot 30 min avant le premier match NHL")
        last_check_date = None
        first_game_time = None  # (hour, minute) du premier match
        last_snapshot_date = None
        first_run = True  # Flag pour scraper immédiatement au démarrage

        while True:
            now_et = _et_now()
            today = now_et.date().isoformat()

            # ── À 8h ET chaque matin, scraper les matchs pour trouver l'heure du premier
            # ── OU au premier démarrage si on est après 8h AM (rattrapage)
            should_scrape = (now_et.hour == 8 and now_et.minute == 0 and last_check_date != today) or (first_run and now_et.hour >= 8)
            if should_scrape:
                try:
                    print(f"[snapshot] Scrape des matchs du jour {today} pour trouver le premier...")
                    # Import tardif pour éviter les dépendances circulaires
                    from scraper import scrape_all_sync
                    matches = scrape_all_sync(headless=True, sports=["hockey"])

                    if matches:
                        # Trier par heure et trouver le premier
                        times = sorted(set(m.time for m in matches if m.time))
                        if times:
                            first_time_str = times[0]  # ex. "19:00"
                            try:
                                h, m = map(int, first_time_str.split(":"))
                                # Soustraire 30 minutes
                                snapshot_m = m - 30
                                snapshot_h = h
                                if snapshot_m < 0:
                                    snapshot_h -= 1
                                    snapshot_m += 60
                                first_game_time = (snapshot_h, snapshot_m)
                                print(f"[snapshot] Premier match: {first_time_str} ET → snapshot à {snapshot_h:02d}:{snapshot_m:02d} ET")
                            except ValueError:
                                print(f"[snapshot] Format d'heure invalide: {first_time_str}")
                        else:
                            print(f"[snapshot] Aucune heure trouvée dans les matchs")
                    else:
                        print(f"[snapshot] Aucun match NHL trouvé pour {today}")

                    last_check_date = today
                    first_run = False  # Ne plus faire le rattrapage après la première exécution
                except Exception as exc:
                    print(f"[snapshot] Erreur lors du scrape du matin: {exc}")
                    first_run = False  # Ne plus faire le rattrapage même en cas d'erreur

            # ── À l'heure calculée, déclencher le snapshot
            if first_game_time and now_et.hour == first_game_time[0] and now_et.minute == first_game_time[1]:
                if last_snapshot_date != today:
                    try:
                        print(f"[snapshot] Déclenchement du snapshot à {now_et.hour:02d}:{now_et.minute:02d} ET")
                        picks = get_picks_fn() or []
                        sgps = get_sgp_fn() if get_sgp_fn else []
                        MOIS_FR = ["janvier","février","mars","avril","mai","juin",
                                   "juillet","août","septembre","octobre","novembre","décembre"]
                        ds = f"{now_et.day} {MOIS_FR[now_et.month-1]} {now_et.year}"
                        result = send_betting_summary(picks, ds, sgp_proposals=sgps)
                        print(f"[snapshot] {result.get('message', 'Snapshot envoyé')}")
                        last_snapshot_date = today
                    except Exception as exc:
                        print(f"[snapshot] Erreur lors du snapshot: {exc}")

            time.sleep(30)  # vérifie toutes les 30 secondes

    t = threading.Thread(target=_loop, daemon=True, name="snapshot-scheduler")
    t.start()
