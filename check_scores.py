import sys
sys.path.insert(0, 'C:/Users/DaveTardif/Documents/Claude/miseojeu-analyzer')
import importlib
import nba_stats, nhl_stats, predictions
importlib.reload(nba_stats)
importlib.reload(nhl_stats)
importlib.reload(predictions)
from predictions import update_outcomes, update_nba_outcomes
import json

with open('C:/Users/DaveTardif/Documents/Claude/miseojeu-analyzer/predictions.json', encoding='utf-8') as f:
    before = json.load(f)
null_before = len([p for p in before if p.get('date') == '2026-03-17' and not p.get('outcome')])
print("Sans outcome avant:", null_before)

n1 = update_outcomes()
n2 = update_nba_outcomes()
print("NHL mis a jour: %d" % n1)
print("NBA mis a jour: %d" % n2)

with open('C:/Users/DaveTardif/Documents/Claude/miseojeu-analyzer/predictions.json', encoding='utf-8') as f:
    after = json.load(f)
null_after = len([p for p in after if p.get('date') == '2026-03-17' and not p.get('outcome')])
print("Sans outcome apres:", null_after, "(resolu %d)" % (null_before - null_after))

remaining = [p for p in after if p.get('date') == '2026-03-17' and not p.get('outcome')]
if remaining:
    print("Encore sans outcome (%d):" % len(remaining))
    for p in remaining:
        print("  [%s] %s | %s | sel=%s" % (p.get('sport','?'), p.get('home_team',''), p.get('bet_type',''), p.get('selection','')))
