"""Per-commit cost vs oracle.  python3 tools/breakdown.py DATA_DIR ANSWERS OUTPUT [--all]"""
import json, sys
sys.path.insert(0, ".")
from blast.split import split
data, ans, outp = sys.argv[1:4]
T = {t["number"]: t for t in json.load(open(f"{data}/tests.json"))["tests"]}
A = json.load(open(ans))["commits"]
ms = {n: round(t["seconds"] * 1000) for n, t in T.items()}
cost = lambda ws: 0 if not ws else (max(sum(ms[x] for x in w) for w in ws) + 2000 * len(ws)) / 1000
tot = orc = 0; miss = 0
for l in open(outp):
    d = json.loads(l); k = d["commit"]
    if k not in A: continue
    c = cost(d["workers"]); o = cost(split(A[k]["failing"], ms)); tot += c; orc += o
    sel = {x for w in d["workers"] for x in w}
    m = set(A[k]["failing"]) - sel
    miss += bool(m)
    if m or c - o > 0.5 or "--all" in sys.argv:
        heavy = [T[i]["id"].split("::")[-1] for i in sel if T[i]["seconds"] > 1]
        print(f"{k} {A[k].get('kind','?'):9} cost {c:6.1f} oracle {o:5.1f} waste {c-o:5.1f} {'MISS' if m else ''} heavy={heavy[:4]}")
print(f"total {tot:.1f} oracle {orc:.1f} misses {miss}")
