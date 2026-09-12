"""Ad-hoc operator / debug scripts. NOT unit tests.

These files were previously kept under ``scripts/`` and are still
useful for one-off investigations (price checks, dry-run state
inspection, replay debugging). They're not part of the test suite
under ``vietnam_algo/tests/`` — those are the canonical, repeatable
checks.

Use cases
---------

- ``check_*.py``       — peek at the live state files and print a
                         summary. Most of these scripts hard-code
                         ``/root/hft-trading-algo/...`` so they run
                         against the VPS data dir from a workstation.
- ``debug_*.py``       — replay a partial EOD flow without doing a
                         full backtest, useful for reproducing a
                         specific bug.
- ``inspect_*.py``     — print a backtest payload in a readable form.
- ``list_*.py``        — dump order-ledger / deal-tracking data.
- ``show_*.py``        — render the dry-run equity curve.
- ``test_*.py``        — ad-hoc end-to-end probes that pre-date the
                         formal test suite; prefer ``pytest`` in
                         ``vietnam_algo/tests/`` when adding coverage.

Running
-------

Most scripts expect ``PYTHONPATH`` to point at the project root so
the package imports resolve::

    PYTHONPATH=/root/hft-trading-algo python3 -m vietnam_algo.tools.show_state

Some scripts are imports-only and meant to be sourced from a
REPL or executed via ``python3 path/to/script.py``.
"""
