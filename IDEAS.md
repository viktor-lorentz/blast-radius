# Ranked experiment backlog (re-read after any context compaction)

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
