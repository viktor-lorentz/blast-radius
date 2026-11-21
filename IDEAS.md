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
