# blast-radius

Entry for the RWX "Blast Radius" track of the Builders Buildathon: given a patch to
[sqlglot](https://github.com/tobymao/sqlglot) (pinned commit `eb11aec4`), choose which
tests to run and how to split them across up to 8 workers.

## Approach
Maps derived from the pinned sqlglot commit (coverage, imports, fixtures) pick a
candidate set. There is no table from a patch, or from a kind of edit, to the tests
it breaks. When that set is cheap, the program runs those tests and drops the ones
that pass. A crash, a timeout, or a fixture file edited on disk keeps the candidate set.

- `blast/patch.py`: strict unified-diff parser and applier (anything odd -> run everything).
- `blast/split.py`: picks the worker count k (1..8) that minimises busiest-load + 2k.
- `selector.py`: AST-level classification of each patch.

Status: v0 (AST no-op detection + optimal split). Coverage maps are in progress.

Libraries: Python standard library only at run time.
`make sample` builds the image and scores the 30 sample commits.

License: MIT.
