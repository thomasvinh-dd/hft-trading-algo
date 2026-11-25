"""Pytest configuration for the ``vietnam_algo`` test suite.

The critical job here is to STOP THE TESTS FROM WRITING TO THE
PRODUCTION POSTGRES DATABASE.

``libs.order_ledger.save_ledger`` mirrors every write into the
``order_ledger`` table unless ``HFT_MIRROR_TO_DB=0``. The guard is
opt-IN (``os.environ.get("HFT_MIRROR_TO_DB", "1")``), so running the
test suite without that variable makes every test that calls
``save_ledger`` UPSERT its fixture rows straight into the live
trading database.

The observed damage (2026-10-29): the production ``order_ledger``
table carried rows for future dates (``run_id='2026-10-30'``) and
fixture symbols (``ZZZ``, ``AAA``, ``ABW``, ``MST``) that no live
order ever produced — the signature of test pollution, not trading
activity. Those rows can also mislead the live read path
(``libs.db_sync.read_order_ledger`` prefers Postgres over the JSON
ledger), so the mirror must reflect real orders only.

Setting the variable here — at conftest import time, which happens
before any test module is imported — closes the hole for every test
under ``vietnam_algo/`` (both ``vietnam_algo/tests/`` and the
top-level ``vietnam_algo/test_*.py`` modules).
"""

from __future__ import annotations

import os

# Keep the test suite out of the production Postgres mirror.
os.environ["HFT_MIRROR_TO_DB"] = "0"
