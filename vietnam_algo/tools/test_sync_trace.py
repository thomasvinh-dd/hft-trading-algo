"""Trace exactly what sync_corporate_actions_incremental does."""
import sys, traceback
sys.path.insert(0, "/root/hft-trading-algo")
from pathlib import Path
from datetime import datetime
from vietnam_algo.libs.corporate_actions import (
    fetch_vietstock_session,
    fetch_vietstock_events_page_with_total,
    validate_vietstock_session,
)

DATA_DIR = Path("/root/hft-trading-algo/vietnam_algo/data")
print("=== Manually replicating sync_corporate_actions_incremental's first attempt ===")
session = fetch_vietstock_session(timeout=30.0)
print(f"  session token: {session.token[:20]}...")

# Validate (probe with 7-day window)
ok = validate_vietstock_session(session, timeout=15.0)
print(f"  probe OK = {ok}")

if ok:
    print("\n=== Now try real fetch with same session: ===")
    try:
        rows, total = fetch_vietstock_events_page_with_total(
            "2026-08-18", "2026-09-09",
            1, 100,
            session=session,
            timeout=30.0,
        )
        print(f"  OK: rows={len(rows)}, total={total}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        traceback.print_exc()
