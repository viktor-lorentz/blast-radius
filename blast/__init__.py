"""Install the TPC-DS frame check in front of the plan check."""
from __future__ import annotations

import os


def _install() -> None:
    from blast import plans

    if getattr(plans.narrow_tpcds, "_heavy", False):
        return
    from blast import heavy

    original = plans.narrow_tpcds

    def narrow_tpcds(*args, **kwargs):
        # BR_HEAVY=0 keeps the plan check and does not run DuckDB.
        if os.environ.get("BR_HEAVY", "1") == "0":
            return original(*args, **kwargs)
        return heavy.narrow(*args, **kwargs)

    narrow_tpcds._heavy = True
    plans.narrow_tpcds = narrow_tpcds


_install()
