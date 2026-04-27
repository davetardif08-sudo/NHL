import sys, json
sys.path.insert(0, 'C:/Users/DaveTardif/Documents/Claude/miseojeu-analyzer')
from datetime import date, timedelta
from predictions import _load

preds = _load()
yesterday = (date.today() - timedelta(days=1)).isoformat()
today = date.today().isoformat()

yest_nba = [p for p in preds if p.get('date') == yesterday and p.get('sport') == 'basketball' and p.get('outcome') in ('win','loss')]
tonight_nba = [p for p in preds if p.get('date') == today and p.get('sport') == 'basketball']

print(f"=== NBA HIER ({yesterday}) : {len(yest_nba)} résolus ===")
wins = sum(1 for p in yest_nba if p['outcome'] == 'win')
print(f"Taux global: {wins}/{len(yest_nba)} = {round(wins/len(yest_nba)*100)}%")

# Par type de pari
bt_stats = {}
for p in yest_nba:
    bt = p.get('bet_type','').lower()
    if any(k in bt for k in ('écart','ecart','handicap')):   cat = 'Écart de points'
    elif any(k in bt for k in ('gagnant','2 issues')):       cat = 'Gagnant'
    elif 'demie' in bt:                                       cat = '1re demie'
    elif 'double' in bt:                                      cat = 'Double chance'
    elif any(k in bt for k in ('total','points','plus/moins')): cat = 'Total'
    else:                                                     cat = 'Autre'
    bt_stats.setdefault(cat, {'w':0,'t':0})
    bt_stats[cat]['t'] += 1
    if p['outcome'] == 'win': bt_stats[cat]['w'] += 1

print("\nPar type de pari:")
for cat, s in sorted(bt_stats.items(), key=lambda x: -x[1]['t']):
    wr = round(s['w']/s['t']*100)
    print(f"  {cat:<22} {s['w']}/{s['t']} = {wr}%")

# Par tranche de cotes
print("\nPar tranche de cotes:")
for label, lo, hi in [('<1.50',0,1.5),('1.50-1.70',1.5,1.7),('1.70-1.90',1.7,1.9),('1.90-2.20',1.9,2.2),('2.20+',2.2,99)]:
    grp = [p for p in yest_nba if lo <= float(p.get('odds') or 0) < hi]
    if not grp: continue
    w = sum(1 for p in grp if p['outcome'] == 'win')
    avg_o = sum(float(p.get('odds') or 0) for p in grp) / len(grp)
    seuil = round(100/avg_o) if avg_o else 0
    wr = round(w/len(grp)*100)
    diff = wr - seuil
    print(f"  {label:<12} {w}/{len(grp)} = {wr}%  (seuil {seuil}%  diff {'+' if diff>=0 else ''}{diff}%)")

# Signaux
print("\nSignaux (Excellent uniquement):")
exc = [p for p in yest_nba if 'Excellent' in (p.get('recommendation') or '')]
sig_stats = {}
for p in exc:
    for sig, val in (p.get('signals') or {}).items():
        if sig == 'is_home' or not isinstance(val, bool) or not val: continue
        sig_stats.setdefault(sig, {'w':0,'t':0})
        sig_stats[sig]['t'] += 1
        if p['outcome'] == 'win': sig_stats[sig]['w'] += 1
for sig, s in sorted(sig_stats.items(), key=lambda x: -x[1]['w']/max(x[1]['t'],1)):
    if s['t'] < 2: continue
    wr = round(s['w']/s['t']*100)
    print(f"  {sig:<25} {s['w']}/{s['t']} = {wr}%")

# Analyse ce soir
print(f"\n=== NBA CE SOIR ({today}) : {len(tonight_nba)} prédictions ===")
by_type = {}
for p in tonight_nba:
    bt = p.get('bet_type','').lower()
    if any(k in bt for k in ('écart','ecart')): cat = 'Écart de points'
    elif any(k in bt for k in ('gagnant','2 issues')): cat = 'Gagnant'
    elif 'demie' in bt: cat = '1re demie'
    elif 'double' in bt: cat = 'Double chance'
    elif any(k in bt for k in ('total','points','plus/moins')): cat = 'Total'
    else: cat = 'Autre'
    by_type.setdefault(cat, 0)
    by_type[cat] += 1
print("Répartition ce soir:")
for cat, n in sorted(by_type.items(), key=lambda x: -x[1]):
    print(f"  {cat:<22} {n} paris")

exc_tonight = [p for p in tonight_nba if 'Excellent' in (p.get('recommendation') or '')]
print(f"\nExcellent ce soir: {len(exc_tonight)}")
by_type_exc = {}
for p in exc_tonight:
    bt = p.get('bet_type','').lower()
    if any(k in bt for k in ('écart','ecart')): cat = 'Écart de points'
    elif any(k in bt for k in ('gagnant','2 issues')): cat = 'Gagnant'
    elif 'demie' in bt: cat = '1re demie'
    elif 'double' in bt: cat = 'Double chance'
    elif any(k in bt for k in ('total','points','plus/moins')): cat = 'Total'
    else: cat = 'Autre'
    by_type_exc.setdefault(cat, 0)
    by_type_exc[cat] += 1
print("Répartition Excellent ce soir:")
for cat, n in sorted(by_type_exc.items(), key=lambda x: -x[1]):
    print(f"  {cat:<22} {n} paris")
