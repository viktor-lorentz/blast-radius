"""Build-time: every sqlglot code object test_generator_memory enters at the pinned
tree, imports included (module bodies, class bodies, functions), for blast.genmem.

    python tools/genmem_map.py SRC_ROOT OUT.json
"""
import json, os, sys, time, unittest
src = os.path.abspath(sys.argv[1]); out = os.path.abspath(sys.argv[2])
sys.path.insert(0, src); os.chdir(src)
M = sys.monitoring; TOOL = 4
M.use_tool_id(TOOL, "genmem")
pre = src + "/sqlglot/"
seen = set()
def on_start(code, off):
    f = code.co_filename
    if f.startswith(pre):
        seen.add("%s:%d" % (f[len(src) + 1:], code.co_firstlineno))
    return M.DISABLE
M.register_callback(TOOL, M.events.PY_START, on_start)
M.set_events(TOOL, M.events.PY_START)
t0 = time.process_time()
import tests.test_generator as tg
suite = unittest.TestSuite([tg.TestGenerator("test_generator_memory")])
res = unittest.TextTestRunner(stream=open(os.devnull, "w")).run(suite)
M.set_events(TOOL, 0)
if not res.wasSuccessful():
    sys.exit("test_generator_memory fails at the pinned tree")
loaded = sorted({m.__file__[len(src) + 1:] for m in list(sys.modules.values())
                 if getattr(m, "__file__", None) and m.__file__.startswith(pre)})
json.dump({"entered": sorted(seen), "loaded": loaded}, open(out, "w"))
print("genmem entered", len(seen), "loaded", len(loaded), "cpu %.1f" % (time.process_time() - t0), file=sys.stderr)
