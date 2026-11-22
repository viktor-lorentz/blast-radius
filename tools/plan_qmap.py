"""Build-time: per-query, per-phase function map of test_execute_tpcds at the pinned
tree, plus optimizer snapshots, for blast.plans.

Each sqlglot function gets the tags of the windows that entered it (sys.monitoring
PY_START, re-armed per window): 'S' setUpClass (Table build for every TPC table,
pickled as pool initargs); for query i (every tpc-ds pair, file order) 'P<i>'
parse_one + the pickle round trip; and for executed queries 'R<i>' the DuckDB
transpile, 'Opre<i>' optimize() outside its rules (prologue, loop, epilogue),
'O<r>_<i>' optimizer rule r, 'U<i>' unpickling the stored snapshots, 'X<i>'
find_tables + execute + the result's pickle round trip and rows/columns. Boot is
import time plus one render per generator class (_build_dispatch runs once).

Snapshots: for each rule r in SNAP_RULES, the pickled tree optimize() holds just
before rule r, per executed query; kept only when optimize(snapshot, rules=RULES[r:])
reproduces the full optimize digest.

    python tools/plan_qmap.py SRC_ROOT OUT.json     (writes OUT.json and tpcds_snap_<r>.pkl beside it)
"""
import ast, csv, functools, inspect, json, os, pickle, sys, time
src = os.path.abspath(sys.argv[1]); out = os.path.abspath(sys.argv[2])
sys.path.insert(0, src); os.chdir(src)
sys.path.insert(1, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
M = sys.monitoring; TOOL = 3
M.use_tool_id(TOOL, "qmap")
pre = src + "/sqlglot/"
seen = set()
def on_start(code, off):
    f = code.co_filename
    if f.startswith(pre):
        seen.add((f[len(src) + 1:], code.co_firstlineno))
    return M.DISABLE
M.register_callback(TOOL, M.events.PY_START, on_start)
M.set_events(TOOL, M.events.PY_START)
from sqlglot import find_tables, parse_one, transpile
import sqlglot.executor as ex
import sqlglot.optimizer
from sqlglot.optimizer import optimize
from sqlglot.optimizer.optimizer import RULES
from sqlglot.executor.table import Table
from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
from tests.test_executor import open_file
import sqlglot.dialects.duckdb; from sqlglot.dialects.dialect import Dialect; Dialect.get_or_raise("duckdb")
Dialect.get_or_raise("duckdb").generator(); Dialect().generator()  # per-class dispatch tables
M.set_events(TOOL, 0)
boot = sorted("%s:%d" % k for k in seen)
from blast.plans import _digest  # after boot: not sqlglot code

SNAP_RULES = (2, 4, 5, 8, 11, 12, 13)
def window():
    seen.clear(); M.restart_events(); M.set_events(TOOL, M.events.PY_START)
funcs = {}
def flush(tag):
    M.set_events(TOOL, 0)
    for k in seen:
        funcs.setdefault("%s:%d" % k, []).append(tag)

cur = {"q": None}
def wrap(ri, rule):
    @functools.wraps(rule)
    def w(*a, **k):
        q = cur["q"]
        flush("Opre%d" % q); window()
        try:
            return rule(*a, **k)
        finally:
            flush("O%d_%d" % (ri, q)); window()
    w.__signature__ = inspect.signature(rule)
    return w
WRAPPED = [wrap(ri, r) for ri, r in enumerate(RULES)]

tables = {}
def build(d, t, cols, store):
    r = csv.reader(open_file(f"{d}{t}.csv.gz"), delimiter="|"); next(r)
    rows, ct = [], []
    for row in r:
        if not ct:
            for v in row:
                try: ct.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError): ct.append(str)
        rows.append(tuple(None if (t2 is not str and v == "") else t2(v) for t2, v in zip(ct, row)))
    store[t] = Table(columns=cols, rows=rows)
t0 = time.process_time()
window()
for t, c in TPCH_SCHEMA.items(): build(FIXTURES_DIR + "/optimizer/tpc-h/", t, c, {})
for t, c in TPCDS_SCHEMA.items(): build(FIXTURES_DIR + "/optimizer/tpc-ds/", t, c, tables)
pickle.loads(pickle.dumps(tables))  # initargs to the spawn workers
flush("S")
qi = -1; execd = []; snaps = {r: [] for r in SNAP_RULES}; kept = 0
for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    qi += 1
    window()
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    flush("P%d" % qi)
    if not meta.get("execute"):
        continue
    execd.append(qi)
    window(); transpile(sql, write="duckdb"); flush("R%d" % qi)
    tree = parse_one(sql)
    cur["q"] = qi; window()
    full = optimize(tree, TPCDS_SCHEMA, rules=WRAPPED, leave_tables_isolated=True)
    flush("Opre%d" % qi)
    want = _digest(full)
    for r in SNAP_RULES:
        blob = pickle.dumps(optimize(parse_one(sql), TPCDS_SCHEMA, rules=RULES[:r], leave_tables_isolated=True))
        window(); snap = pickle.loads(blob); flush("U%d" % qi)
        ok = _digest(optimize(snap, TPCDS_SCHEMA, rules=RULES[r:], leave_tables_isolated=True)) == want
        snaps[r].append(blob if ok else None); kept += ok
    window()
    tb = {t.name: tables[t.name] for t in find_tables(e)}
    res = ex.execute(e, schema=TPCDS_SCHEMA, tables=tb)
    res = pickle.loads(pickle.dumps(res))
    list(res.rows); res.columns
    flush("X%d" % qi)
for r, blobs in snaps.items():
    with open(os.path.join(os.path.dirname(out), "tpcds_snap_%d.pkl" % r), "wb") as fh:
        pickle.dump(blobs, fh, protocol=pickle.HIGHEST_PROTOCOL)
json.dump({"n": qi + 1, "exec": execd, "boot": boot, "funcs": funcs, "snap_rules": list(SNAP_RULES),
           "rules": [r.__name__ for r in RULES]}, open(out, "w"))
print("queries", qi + 1, "exec", len(execd), "funcs", len(funcs), "boot", len(boot),
      "snapshots kept %d/%d" % (kept, len(execd) * len(SNAP_RULES)), "cpu %.1f" % (time.process_time() - t0), file=sys.stderr)
