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

Status: coverage, semantic diff, a dialect-module load map, plain KEYWORDS narrowing, clean side-effect test narrowing, redundant assignment elimination, repeated statements whose import fingerprint does not change, version compares that a 3-tuple cannot distinguish, and numeric-escape values narrowed to tests that write the escape. test_execute_tpcds is dropped when its DuckDB SQL and optimized tree match, and also when only a few executed queries differ and those frames still match DuckDB. The patch must stay out of the executor. test_max_nodes is dropped when a depth-100 prefix of its statement has the same node count as the pristine parser and the limit error still names the limit. A renamed keyword, SQL function, or clause name keeps only the tests that spell it. The short runner is off unless BR_RUN=1.

Libraries: the standard library, plus DuckDB, pandas, and numpy so the TPC-DS frames can be checked.
`make sample` builds the image and scores the 30 sample commits.

License: MIT.
