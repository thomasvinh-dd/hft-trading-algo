"""Test the production flow with page_size=100 (which sync uses)."""
import sys
sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.libs.corporate_actions import (
    fetch_vietstock_session,
    fetch_vietstock_events_page_with_total,
)

print("== Test page_size=100 (production) ==")
session = fetch_vietstock_session(timeout=15.0)
print(f"  session token: {session.token[:20]}...")

# Try page_size=100, range spanning 2026-08-18 → 2026-09-09
try:
    rows, total = fetch_vietstock_events_page_with_total(
        "2026-08-18",
        "2026-09-09",
        1,
        100,           # production page_size
        session=session,
        timeout=15.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")

print()
print("== Test page_size=10, same range ==")
try:
    session2 = fetch_vietstock_session(timeout=15.0)
    rows, total = fetch_vietstock_events_page_with_total(
        "2026-08-18",
        "2026-09-09",
        1,
        10,            # smaller
        session=session2,
        timeout=15.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")

print()
print("== Test page_size=50 ==")
try:
    session3 = fetch_vietstock_session(timeout=15.0)
    rows, total = fetch_vietstock_events_page_with_total(
        "2026-08-18",
        "2026-09-09",
        1,
        50,
        session=session3,
        timeout=15.0,
    )
    print(f"  OK: rows={len(rows)}, total={total}")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
