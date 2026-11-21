# Ranked experiment backlog (re-read after any context compaction)

1. **Import-time semantic diff** (code written: blast/semantic.py, blast/fingerprint.py, tools/fp_baseline.py; selector wired).
   TODO: download new maps (attrs.json, names.json) from the latest build-maps run; add to the Dockerfile:
   `RUN python -m compileall -q /opt/sqlglot && python /app/tools/fp_baseline.py /opt/sqlglot /app/fp` (COPY tools/fp_baseline.py).
   Increase seeds for the "dropped" check (Databricks.text_type is noise). Expected: 4 sample commits drop from ~37 s to ~0-3 s each (about -140 s).
2. **Validate on corpus c1** (400 mutants, ground truth from the corpus-truth workflow run 44852782678). Must show 0 misses. Generate a held-out c2 (seed 2) and never tune on it.
3. Equivalent-mutant savings: `is`<->`==` against None/enum members pull in tpcds (21 s). Adopt only if the corpus shows ~0 failures for that pattern.
4. CPU: diff only the changed top-level statements instead of whole-file ast.parse (parser.py is large).
5. Possibly unseen kinds: new/deleted/renamed files currently fall back to ALL (safe).
