picks = [
    {'fair_prob':0.4263, 'odds':2.70, 'value_score':70},
    {'fair_prob':0.5995, 'odds':1.57, 'value_score':75},
    {'fair_prob':0.5165, 'odds':1.95, 'value_score':95},
    {'fair_prob':0.6156, 'odds':1.53, 'value_score':75},
    {'fair_prob':0.5481, 'odds':1.69, 'value_score':75},
    {'fair_prob':0.2747, 'odds':3.25, 'value_score':50},
    {'fair_prob':0.5744, 'odds':1.63, 'value_score':75},
]

print("=== avec /100 (code actuel) ===")
for p in picks:
    prob = float(p['fair_prob']) / 100
    b = p['odds'] - 1
    hk = max(0.0, (prob * b - (1 - prob)) / b / 2)
    print(f"  fair_prob={p['fair_prob']} -> prob={prob:.5f} -> hk={hk:.5f}")

print("\n=== sans /100 ===")
for p in picks:
    prob = float(p['fair_prob'])
    b = p['odds'] - 1
    hk = max(0.0, (prob * b - (1 - prob)) / b / 2)
    print(f"  fair_prob={p['fair_prob']} -> prob={prob:.5f} -> hk={hk:.5f}")

print("\n=== si fair_prob etait stocke en % (42.63) ===")
picks_pct = [
    {'fair_prob':42.63, 'odds':2.70, 'value_score':70},
    {'fair_prob':59.95, 'odds':1.57, 'value_score':75},
    {'fair_prob':51.65, 'odds':1.95, 'value_score':95},
    {'fair_prob':61.56, 'odds':1.53, 'value_score':75},
    {'fair_prob':54.81, 'odds':1.69, 'value_score':75},
    {'fair_prob':27.47, 'odds':3.25, 'value_score':50},
    {'fair_prob':57.44, 'odds':1.63, 'value_score':75},
]
budget = 10.0
min_bet = 0.5
for p in picks_pct:
    prob = float(p['fair_prob']) / 100
    b = p['odds'] - 1
    hk = max(0.0, (prob * b - (1 - prob)) / b / 2)
    p['_hk'] = hk
    print(f"  fair_prob={p['fair_prob']} -> prob={prob:.5f} -> hk={hk:.5f}")

selected = sorted([p for p in picks_pct if p['_hk'] > 0], key=lambda x: -x['_hk'])[:7]
if len(selected) < 3:
    remaining = sorted([p for p in picks_pct if p not in selected], key=lambda x: -(x.get('value_score') or 0))
    for p in remaining:
        if len(selected) >= 3: break
        selected.append(p)

total_hk = sum(p['_hk'] for p in selected)
if total_hk > 0:
    weights = [p['_hk'] for p in selected]
else:
    weights = [max(p.get('value_score') or 1, 0.01) for p in selected]
    total_hk = sum(weights)

amounts = [w / total_hk * budget for w in weights]
amounts = [max(round(a * 2) / 2, min_bet) for a in amounts]
tot = sum(amounts)
amounts = [round(a / tot * budget * 2) / 2 for a in amounts]
amounts = [max(a, min_bet) for a in amounts]
diff = round((budget - sum(amounts)) * 2) / 2
if diff != 0:
    mx = amounts.index(max(amounts))
    amounts[mx] = round((amounts[mx] + diff) * 2) / 2

print(f"\nMises calculees: {amounts}")
print(f"Total: {sum(amounts):.2f}$")
