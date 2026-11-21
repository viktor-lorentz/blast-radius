"""Assemble an evaluation dir from corpus patches + downloaded truth shards.
   python3 tools/mkeval.py CORPUS_DIR TRUTH_DIR OUT_DIR"""
import glob, json, os, shutil, sys
corpus, truth, out = sys.argv[1:4]
T = json.load(open("sample/tests.json"))["tests"]
num = {t["id"]: t["number"] for t in T}
kinds = json.load(open(os.path.join(corpus, "kinds.json"))) if os.path.exists(os.path.join(corpus, "kinds.json")) else {}
res = {}
for f in glob.glob(os.path.join(truth, "*", "truth-*.json")) + glob.glob(os.path.join(truth, "truth-*.json")):
    res.update(json.load(open(f)))
shutil.rmtree(out, ignore_errors=True)
os.makedirs(os.path.join(out, "commits"))
shutil.copy("sample/tests.json", os.path.join(out, "tests.json"))
ans = {}
for cid, failing in res.items():
    src = os.path.join(corpus, "commits", cid + ".patch")
    if not os.path.exists(src):
        continue
    shutil.copy(src, os.path.join(out, "commits"))
    ans[cid] = {"failing": sorted(num[i] for i in failing if i in num), "kind": kinds.get(cid, "?")}
json.dump({"commits": ans}, open(os.path.join(out, "answers.json"), "w"))
print(f"{len(ans)} commits, {sum(1 for a in ans.values() if not a['failing'])} with no failing test")
