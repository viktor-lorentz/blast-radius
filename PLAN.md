# Final push: Sat 5:45 PM PT to Sun 8:00 AM PT

## Goal
Win the FINAL. The final re-scores the commit we Pick on about 1.5x as many unseen commits,
including kinds of change the sample does not have. The practice board carries no prize.
Practice board at 5:15 PM PT: 67 103.5, 1212 114.6, @GMUNVIS 115.0, RoboM 116.7, Steve WK 120.2.
We are at 172.4 (sim 167.914, CPU ~4.5, 0 misses).

## What we know
- The practice batch is fixed. Each submission reports sim (to the ms), CPU and misses for each of its 3 runs.
  We get 2 submissions an hour, so about 28 slots remain. A practice miss costs nothing toward the prize,
  so use every slot as an experiment.
- Sim has stayed at exactly 167.914 across 10 submissions. Rules tuned on the local corpora do not touch
  whatever the hidden batch wastes. Stop guessing and measure with ablations (Phase 1).
- Cost structure: running everything costs ~36.8 s (k=4, test_max_nodes alone is 28.8 s).
  test_execute_tpcds is 21.5 s, test_generator_memory 6.7 s, test_execute_tpch 3.9 s, test_optimized_tpch 3.5 s.
  Almost all of our waste is a heavy test that was selected and then passed.
- c1+c2+c4: test_execute_tpcds was selected 142 times and failed 31 times (22%). Every time it failed,
  other selected tests failed too. That makes it a canary candidate.
- Break-even: dropping a test that saves S seconds pays off if P(it fails) < S/300.
  For 27 s that means under 9%.
- We need first place, not second. Take calibrated risk. The bar is 0 misses on the practice batch plus
  a low miss rate on the corpora, not "provably 0".

## Phase 1, now to ~8 PM: ablations on the practice board (LEAD)
Each ablation is a general policy on its own branch, never main, and is submitted as-is:
- A1: HEAD, but test_max_nodes is removed from every selection that is not a full-suite (ALL) fallback.
- A2: HEAD, but test_execute_tpcds is removed from every selection that is not an ALL fallback.
- A3: HEAD, but every ALL fallback except a core-module import failure becomes coverage + importers of the edited files.
- A4: the combination of whichever of A1-A3 cut sim without a miss.
Read the change in sim and the misses after each one. That tells us which heavy test or fallback holds the
hidden waste, and whether it ever fails there. Post a one-line verdict per ablation at the top of IDEAS.md.

## Phase 2, ~8 PM to 4 AM: make the winning ablation safe
- HEAVY: canary runner. Before dropping a heavy test, run in the container the cheap selected tests that
  co-fail with it in the corpora. Use one parent process that forks per commit, a patched copy of sqlglot in /tmp,
  and unittest directly, never pytest, with a CPU cap. Drop the heavy test only when its canaries pass,
  combined with the existing plan and max_nodes checks. Validate on sample and c1-c5 for misses and sim+CPU.
  Server CPU is about 1.5-2x local CPU.
- EQUIV: cheaper handling for the final's new kinds, where each full-suite fallback costs 36.8 s.
  (1) Semantic no-ops as AST normalizations: annotation-only edits in modules with
  `from __future__ import annotations`, local-variable renames (alpha-equivalence), import reordering,
  reformatting, `pass`/parentheses/trailing commas.
  (2) Narrow the remaining ALL paths: new, deleted and renamed files (importers of the old path, nothing for an
  unimported new file), config files (pyproject outside pytest sections, setup.py, MANIFEST.in, requirements),
  unknown non-Python files.
  (3) Make the patch parser tolerant of real `git diff` output: no newline at EOF, CRLF, mode lines,
  binary, quoted paths, `index` lines, rename/copy headers.
  Build corpus c6 of these shapes, with truth from the bench GitHub workflow.
- CORPUS (optional, Sonnet): build c7, organizer-like commits that mirror the sample's mix
  (code 53%, fixture 27%, multi 10%, comment 10%). Use realistic edits, not mutants. Get truth from the workflow,
  then post a per-kind waste table (tools/breakdown.py) and hand the biggest classes to HEAVY and EQUIV.

## Phase 3, from 4 AM: freeze and pick (LEAD)
By 7:30 AM PT, click Pick on the commit with the lowest practice score that has 0 practice misses and
at most 1 miss per 400 corpus commits (roughly a 10% chance of one miss in the final).
Before Phase 3 it does not matter which commit is auto-entered.

## Rules
- Only LEAD submits, merges to main, and edits results.tsv. Everyone else works in their own git worktree
  (`git worktree add .wt/<name> -b <name>`, inside the repo so the overnight Stop hook and permissions apply; `.wt/` is git-excluded) and hands LEAD a pushed commit plus numbers in an
  "## HANDOFF" section at the top of IDEAS.md.
- Every submitted commit is pushed and builds (bench workflow green). Never force-push.
- Keep server CPU at or under ~6 s in total.
- No table from a patch, or a kind of edit, to tests. Maps come from the pinned commit only. Ablations are general policies.
- The Pro plan's usage is shared by everyone. Send logs to files and tail/grep them. No subagents. /clear between phases.
- LEAD push-notifies the user with every score.
