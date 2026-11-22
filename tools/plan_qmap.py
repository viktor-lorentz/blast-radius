"""Build-time: per-query function map of test_execute_tpcds at the pinned tree, for
blast.plans. Query i (every tpc-ds pair, file order) records each sqlglot function
entered by what the test does for it: parse_one, the pickle round trip to the pool,
and for executed queries find_tables, execute (optimize included), the result's
pickle round trip, rows/columns and the DuckDB transpile. Tag 'S' is setUpClass
(Table build for every TPC table, pickled as pool initargs); boot is import time.

    python tools/plan_qmap.py SRC_ROOT OUT.json
"""
import ast, csv, json, os, pickle, sys, time
src = os.path.abspath(sys.argv[1]); out = os.path.abspath(sys.argv[2])
sys.path.insert(0, src); os.chdir(src)
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
from sqlglot.executor.table import Table
from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs
from tests.test_executor import open_file
import sqlglot.dialects.duckdb; from sqlglot.dialects.dialect import Dialect; Dialect.get_or_raise("duckdb")
M.set_events(TOOL, 0)
boot = sorted("%s:%d" % k for k in seen)
def window():
    seen.clear(); M.restart_events(); M.set_events(TOOL, M.events.PY_START)
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
funcs = {}
def flush(tag):
    M.set_events(TOOL, 0)
    for k in seen:
        funcs.setdefault("%s:%d" % k, []).append(tag)
t0 = time.process_time()
window()
for t, c in TPCH_SCHEMA.items(): build(FIXTURES_DIR + "/optimizer/tpc-h/", t, c, {})
for t, c in TPCDS_SCHEMA.items(): build(FIXTURES_DIR + "/optimizer/tpc-ds/", t, c, tables)
pickle.loads(pickle.dumps(tables))  # initargs to the spawn workers
flush("S")
qi = -1; execd = []
for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
    qi += 1
    window()
    e = pickle.loads(pickle.dumps(parse_one(sql)))
    if meta.get("execute"):
        execd.append(qi)
        tb = {t.name: tables[t.name] for t in find_tables(e)}
        res = ex.execute(e, schema=TPCDS_SCHEMA, tables=tb)
        res = pickle.loads(pickle.dumps(res))
        list(res.rows); res.columns
        transpile(sql, write="duckdb")
    flush(qi)
json.dump({"n": qi + 1, "exec": execd, "boot": boot, "funcs": funcs}, open(out, "w"))
print("queries", qi + 1, "exec", len(execd), "funcs", len(funcs), "boot", len(boot), "cpu %.1f" % (time.process_time() - t0), file=sys.stderr)
