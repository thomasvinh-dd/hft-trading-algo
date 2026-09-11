"""Test the actual production flow: fetch_vietstock_session + fetch_vietstock_events."""
import sys
sys.path.insert(0, "/root/hft-trading-algo")
import json
from datetime import date
from vietnam_algo.corporate_actions import (
    fetch_vietstock_session,
    fetch_vietstock_events_page_with_total,
)

print("Step 1: Get Vietstock session...")
session = fetch_vietstock_session(timeout=15.0)
print(f"  session.token: {session.token[:30]}...")
print(f"  opener.handlers: {[(type(h).__name__, getattr(h, 'cookiejar', None) is not None) for h in session.opener.handlers]}")

print()
print("Step 2: GET events page 1 (today only, 2026-09-09)...")
try:
    rows, total = fetch_vietstock_events_page_with_total(
        from_date="2026-09-09",
        to_date="2026-09-09",
        page=1,
        page_size=10,
        session=session,
        timeout=15.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
    if rows:
        print(f"  first: {rows[0].symbol if hasattr(rows[0], 'symbol') else rows[0]}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
