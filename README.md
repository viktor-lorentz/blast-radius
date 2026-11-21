# blast-radius

Entry for the RWX "Blast Radius" track of the Builders Buildathon: given a patch to
[sqlglot](https://github.com/tobymao/sqlglot) (pinned commit `eb11aec4`), choose which
tests to run and how to split them across up to 8 workers.

## Approach
Maps derived from the pinned sqlglot commit (coverage, imports, fixtures) pick a
candidate set. There is no table from a patch, or from a kind of edit, to the tests
it breaks. A docstring edit does not select tests: the scored suite does not run doctests.
An unparsable leaf dialect, parser, or generator module selects the tests that load
that module (the load map is imports, fixture headers, and covered loader lines).

- `blast/patch.py`: strict unified-diff parser and applier (anything odd -> run everything).
- `blast/split.py`: picks the worker count k (1..8) that minimises busiest-load + 2k.
- `selector.py`: AST-level classification of each patch.

Status: coverage, semantic diff, a dialect-module load map, plain KEYWORDS narrowing, clean side-effect test narrowing, redundant assignment elimination, and repeated statements whose import fingerprint does not change. The in-container runner is off.

Libraries: Python standard library only at run time.
`make sample` builds the image and scores the 30 sample commits.

License: MIT.
