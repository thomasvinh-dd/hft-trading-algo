"""Test if 3-week range with pageSize=100 works."""
import sys
sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.corporate_actions import (
    fetch_vietstock_session,
    fetch_vietstock_events_page_with_total,
)

print("Test 1: 3-week range, pageSize=100 (production)")
session = fetch_vietstock_session(timeout=15.0)
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

print()
print("Test 2: 3-week range, pageSize=100 (fresh session)")
session2 = fetch_vietstock_session(timeout=15.0)
try:
    rows, total = fetch_vietstock_events_page_with_total(
        "2026-08-18", "2026-09-09",
        1, 100,
        session=session2,
        timeout=30.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")

print()
print("Test 3: 7-day range, pageSize=100")
session3 = fetch_vietstock_session(timeout=15.0)
try:
    rows, total = fetch_vietstock_events_page_with_total(
        "2026-09-02", "2026-09-09",
        1, 100,
        session=session3,
        timeout=30.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")

print()
print("Test 4: 3-week range, pageSize=10 (smaller)")
session4 = fetch_vietstock_session(timeout=15.0)
try:
    rows, total = fetch_vietstock_events_page_with_total(
        "2026-08-18", "2026-09-09",
        1, 10,
        session=session4,
        timeout=30.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
