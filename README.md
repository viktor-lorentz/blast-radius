# blast-radius

Entry for the RWX "Blast Radius" track of the Builders Buildathon: given a patch to
[sqlglot](https://github.com/tobymao/sqlglot) (pinned commit `eb11aec4`), choose which
tests to run and how to split them across up to 8 workers.

## Approach
Static analysis only, no pytest at decision time, no per-patch answer tables.
Maps are derived from the pinned sqlglot commit (coverage, imports, fixtures).

- `blast/patch.py`: strict unified-diff parser and applier (anything odd -> run everything).
- `blast/split.py`: picks the worker count k (1..8) that minimises busiest-load + 2k.
- `selector.py`: AST-level classification of each patch.

Status: v0 (AST no-op detection + optimal split). Coverage maps are in progress.

Libraries: Python standard library only at run time.
`make sample` builds the image and scores the 30 sample commits.

License: MIT.
