import sys, json
sys.path.insert(0, 'C:/Users/DaveTardif/Documents/Claude/miseojeu-analyzer')

with open('C:/Users/DaveTardif/Documents/Claude/miseojeu-analyzer/predictions.json', encoding='utf-8') as f:
    data = json.load(f)

yest = [p for p in data if p.get('date') == '2026-03-17' and p.get('outcome') in ('win', 'loss')]
print(f"Total prédictions résolues hier: {len(yest)}")

# Par sport
for sport in ['hockey', 'basketball']:
    preds = [p for p in yest if p.get('sport') == sport]
    if not preds:
        continue
    wins = sum(1 for p in preds if p['outcome'] == 'win')
    print(f"\n=== {sport.upper()} ===")
    print(f"  Total: {len(preds)}  Gagnés: {wins}  ({round(wins/len(preds)*100)}%)")

    # Par type de pari
    bt_stats = {}
    for p in preds:
        bt = p.get('bet_type', '').lower()
        # Catégoriser
        if any(k in bt for k in ('gagnant', '2 issues', '3 issues', 'victoire')):
            cat = 'Gagnant'
        elif any(k in bt for k in ('écart', 'ecart', 'handicap')):
            cat = 'Écart de points'
        elif any(k in bt for k in ('total', 'plus/moins', 'buts', 'points')) and 'demie' not in bt and '1re' not in bt:
            cat = 'Total'
        elif any(k in bt for k in ('2 équipes', 'les 2', 'marquer')):
            cat = 'Les 2 équipes marquent'
        elif 'demie' in bt or 'half' in bt:
            cat = '1re demie'
        elif 'double' in bt:
            cat = 'Double chance'
        else:
            cat = 'Autre'
        if cat not in bt_stats:
            bt_stats[cat] = {'wins': 0, 'total': 0, 'odds_sum': 0}
        bt_stats[cat]['total'] += 1
        bt_stats[cat]['odds_sum'] += float(p.get('odds') or 0)
        if p['outcome'] == 'win':
            bt_stats[cat]['wins'] += 1

    print("  Par type de pari:")
    for cat, s in sorted(bt_stats.items(), key=lambda x: -x[1]['total']):
        wr = round(s['wins']/s['total']*100)
        avg_odds = round(s['odds_sum']/s['total'], 2)
        seuil = round(100/avg_odds) if avg_odds > 0 else 0
        diff = wr - seuil
        sign = '+' if diff >= 0 else ''
        print(f"    {cat:<30} {s['wins']}/{s['total']} = {wr}%  (seuil {seuil}%  diff {sign}{diff}%)")

    # Par tranche de cotes
    print("  Par tranche de cotes:")
    ranges = [('<1.50', 0, 1.50), ('1.50-1.70', 1.50, 1.70), ('1.70-1.90', 1.70, 1.90), ('1.90-2.20', 1.90, 2.20), ('2.20+', 2.20, 99)]
    for label, lo, hi in ranges:
        grp = [p for p in preds if lo <= float(p.get('odds') or 0) < hi]
        if not grp:
            continue
        w = sum(1 for p in grp if p['outcome'] == 'win')
        avg_o = sum(float(p.get('odds') or 0) for p in grp) / len(grp)
        seuil = round(100/avg_o) if avg_o > 0 else 0
        wr = round(w/len(grp)*100)
        diff = wr - seuil
        sign = '+' if diff >= 0 else ''
        print(f"    {label:<12} {w}/{len(grp)} = {wr}%  (seuil {seuil}%  diff {sign}{diff}%)")

    # Signaux les plus/moins fiables hier
    print("  Signaux prédictifs hier (Excellent uniquement):")
    excellent = [p for p in preds if float(p.get('fair_prob') or 0) >= 0.60 or float(p.get('odds') or 0) <= 1.80]
    sig_stats = {}
    for p in excellent:
        for sig, val in (p.get('signals') or {}).items():
            if sig == 'is_home' or not isinstance(val, bool) or not val:
                continue
            if sig not in sig_stats:
                sig_stats[sig] = {'wins': 0, 'total': 0}
            sig_stats[sig]['total'] += 1
            if p['outcome'] == 'win':
                sig_stats[sig]['wins'] += 1
    for sig, s in sorted(sig_stats.items(), key=lambda x: -x[1]['wins']/max(x[1]['total'],1)):
        if s['total'] < 2:
            continue
        wr = round(s['wins']/s['total']*100)
        print(f"    {sig:<25} {s['wins']}/{s['total']} = {wr}%")

print("\n=== RÉSUMÉ GLOBAL ===")
wins_all = sum(1 for p in yest if p['outcome'] == 'win')
print(f"Taux global: {wins_all}/{len(yest)} = {round(wins_all/len(yest)*100)}%")
avg_odds_all = sum(float(p.get('odds') or 0) for p in yest) / len(yest)
seuil_all = round(100/avg_odds_all)
print(f"Cotes moyennes: {round(avg_odds_all,2)}  Seuil implicite: {seuil_all}%")
roi = round((wins_all * avg_odds_all - len(yest)) / len(yest) * 100, 1)
print(f"ROI simulé (mise égale): {roi}%")
