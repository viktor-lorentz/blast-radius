# Ranked experiment backlog (re-read after any context compaction)

## STATE (2026-11-21 ~03:45 PST) — runner submitted, waiting on the server
BB-AD82E23D (e6080ac) scored 185.3. sim 166.345 (was 172.712), CPU 19.0 (was 2.9), 0 misses.
The runner is -EV on the server: 6.4s of simulated time for 16s of CPU. It is OFF by default
(BR_RUN=0). Do not submit another build with the runner on unless a run costs less CPU than
the simulated time it deletes. Best remains v2c 175.6. Do not pin 185.3.
Slot frees 4:41 AM PST (2026-11-21 12:41 UTC). Next submit should be the docstring fix with
the runner off. Leader was team 67 at 130.8.

Next commit on top, not yet submitted: a docstring the scored suite never runs no longer
falls back to every test. tests/test_docs.py is absent from tests.json. Local, with the runner:
c1 2963.1 (was 3036.7), c2 2136.0 (was 2172.9), c4 2195.5, sample 108.0, all 0 misses.
That is two full-suite commits deleted on c1 (73.6s) and one on c2 (36.8s). Submit this sha
next, but only if BB-AD82E23D did not miss. If it missed, drop the runner and keep this fix.

## STATE (2026-11-21 ~03:40 PST) — runner, local numbers
Safe runner is local, 0 misses. It runs a selected set only when that is cheaper than its
scheduled cost, and only when every edited file is Python under sqlglot/ or a test file the
selection already imports. Fixture edits and any other non-Python file keep the static set.
A crash, timeout, or unittest error keeps the test.

Measured (python 3.13, .cache/fp): sample 108.0 (was 115.0) oracle 94.4 misses 0.
c1 3036.7 (static 3138.2) oracle 1211.6 misses 0.
c2 2172.9 (static 2215.2) oracle 986.7 misses 0.
c4 2195.5 (static 2271.0) oracle 1071.7 misses 0.
c5 6.6 (static 12.7) oracle 4.2 misses 0.
Do not shrink a commit whose patch touches a fixture or a test module we do not import:
that false-passed 19 c1 commits (the unpatched test passed). Best server score is still
v2c 175.6 (sim 172.712). Leader on the board was team 67 at 130.8, #2 1212 at 136.1.

Next, in order, only if the new corpora stay at 0 misses:
1. Read this submission's server sim. If sim does not fall, the hidden set has few all-pass
   worker-fee commits; do not pile more CPU on the same idea.
2. Install duckdb and pandas in the image so executor/optimizer tests can actually run.
   5y72 (2.5s) and the rest of 2qn5 stay selected today because those imports error.
3. Spend the 90s run budget on generator_memory first (native ~0.7s vs recorded 6.7s),
   then other heavy tests, and stop with time left. Never turn a fail-rate into a
   kind-of-edit → test list.
4. ALL-narrowing for a syntax error in one dialect module is still unsafe (c3). Do not ship it
   unless a new corpus of syntax commits stays at 0 misses.

Submit path: real Brave, profile ~/.buildathon-brave, port 9222, tools/brave_debug.sh,
tools/submit_brave.py. The hand-in fields are input[name=repo] and input[name=commit].
One submit per hour. Never force-push.



State (2026-11-20 ~21:30 ET): v1 submitted (7a83571, tag best-submitted; placed 59th).
v2 = import-time semantic diff: sample 165.2 s, 0 misses, amd64 CPU 1.2 s/30 commits.
Corpus c1 (dev, 400 mutants) found 5 misses -> fixed in the map builder (cache isolation, class-valued attrs,
docstring->code). Held-out corpus c2 (300, seed 2): truth running, NEVER tune on it.

Gate for any submission: sample 0 misses + c1 0 misses + c2 0 misses + bench workflow (amd64, real flags) green.

1. [in progress] Verify the fixes: rebuild maps -> re-evaluate c1 (all 400) -> c2 -> submit v2.
2. CPU: import-time commits touching core modules (closure ~177 modules) cost ~1 s each (c1: 58 s/400).
   Idea: long-lived helper that imports pristine sqlglot once, then for each commit fork() and
   importlib.reload() the dependency closure in topological order; compare against fingerprints taken in the
   same parent (also removes hash-seed noise). Fallback to a fresh import on any error.
3. Heavy-test waste: equivalent mutants covered by test_execute_tpcds (21 s) cost ~25 s each (2haz, 66ks).
   Only act if c1 data shows P(tpcds fails | covered and mutated) < 7%. Otherwise keep it.
4. Startup CPU: index.json and the per-file cov JSONs -> marshal/pickle; tests.json parse is unavoidable.
5. Broad attribute reads (4d63 NUMERIC_ESCAPES -> 9 s): a finer read granularity (dict key level?) is probably
   not worth the risk.
6. [done v2d: is/== equivalence for None+Enum] ALL fallbacks = module import failures (syntax errors / import-time exceptions): 13/360 on c1, ~20 s waste each.
   A safe narrowing needs a per-test "modules loaded" map built WITHOUT the pre-import (lazy dialect loading),
   plus collection-time imports in test files and function-level imports. Risky; only if time allows.
7. Heavy tests: test_execute_tpcds is included 57x and fails 6x on c1 (10.5% > 7% break-even): keep.

## STATE (usage limit hit ~20:00 PST Nov 20)
- Submitted: v2c 85d129a = 175.6 s server (BB-8F41C77F), 0 miss. v1 449.3.
- Local HEAD (pushed): v2d = is/== equivalence for None+Enum. sample 115.0, c1 0 miss, c2 0 miss, c5 (120 is/== swaps) 0 miss.
- TODO before submitting v2d (slot frees 8:20 PM PST): c4 eval is INVALID (ran empty). Re-run:
  rm -rf .cache/truth_c4; gh run download 44860307402 -D .cache/truth_c4; python3 tools/mkeval.py corpus/c4 .cache/truth_c4 .cache/eval_c4; run selector, tools/breakdown.py. Need 0 misses.
- Then check bench.yml green for HEAD, submit via Chrome (new tab), tag submitted-v2d (no force push), log results.tsv, push-notify.
- v2e 3c4a227 submitted ~02:16 PST Nov 21; next slot ~03:16 PST. Read server score from the page, log it, push-notify.
- RESULT v2e BB-EA69003C: 176.0 s (172.712 sim, 3.25 cpu) vs v2c 175.6 (172.712 sim, 2.93 cpu). Server batch sim is IDENTICAL, so
  local-sample gains (is/== : 165.2->115.0) do NOT transfer; server set has other waste. best-submitted stays v2c. Next slot ~03:16 PST.
  Leaders: 142.9, 146.3, 151.8, 153.7. Our waste on server set is ~unknown; sim 172.7 vs oracle likely ~100-130.
  Ideas: cut CPU (2.9 s is ~2% now); investigate which heavy tests dominate waste (tpcds 21 s inclusion at 10% fail rate is break-even-ish).
- ANALYSIS (c1+c2+c4, 142 commits selecting test_execute_tpcds): fails in 31 (22%); whenever it fails other tests fail too.
  By selected-set size: <20 tests: 0/13 fail (too few samples: 95% upper bound ~23% >> 7% break-even); 20-60: 7/17; 60-200: 8/43; >200: 16/69.
  CONCLUSION: dropping tpcds is NOT provably safe -> keep (0-miss rule). Remaining gain needs a real per-line predictor; not done.
  Final entry = v2c (BB-8F41C77F, 175.6 s). Only submit a new commit if it beats 175.6 locally on the SERVER-like metric AND 0 miss on c1/c2/c4/c5.
- ALL-fallback study (c3: 80 syntax/import-error mutants; maps pass 3 traces lazily imported dialect/generator/parser modules per test):
  53/80 are eager/core modules (true failing set ~ everything: ALL is right). Of 27 lazy modules, trace is exact for 19, misses for 8
  (big dialects hive/snowflake/presto: 600+ tests short); file-level widening still misses 6 (and widens to ~ALL). Unsafe -> NOT deployed.
  ALL costs ~36.8 s sim (k=4, test_max_nodes 28.8 s alone). Server gap to ~136 s is plausibly ONE ALL-fallback commit; no safe cheaper rule found.
  maps/ in repo unchanged (pass 3 code is tooling only; new builds also emit imports.json).
