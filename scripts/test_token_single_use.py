"""Test if validate POST consumes the token (single-use)."""
import sys
sys.path.insert(0, "/root/hft-trading-algo")
from vietnam_algo.corporate_actions import (
    fetch_vietstock_session,
    fetch_vietstock_events_page_with_total,
    validate_vietstock_session,
)

print("=== Test A: Two POSTs without validate (both should work) ===")
session = fetch_vietstock_session(timeout=15.0)
try:
    rows, _ = fetch_vietstock_events_page_with_total("2026-09-02", "2026-09-09", 1, 100, session=session, timeout=15)
    print(f"  POST 1: {len(rows)} rows")
except Exception as e:
    print(f"  POST 1 FAIL: {e}")
try:
    rows, _ = fetch_vietstock_events_page_with_total("2026-09-02", "2026-09-09", 1, 100, session=session, timeout=15)
    print(f"  POST 2: {len(rows)} rows")
except Exception as e:
    print(f"  POST 2 FAIL: {e}")

print()
print("=== Test B: validate POST then real POST (does validate consume?) ===")
session2 = fetch_vietstock_session(timeout=15.0)
ok = validate_vietstock_session(session2, timeout=15)
print(f"  validate OK = {ok}")
try:
    rows, _ = fetch_vietstock_events_page_with_total("2026-09-02", "2026-09-09", 1, 100, session=session2, timeout=15)
    print(f"  real POST: {len(rows)} rows")
except Exception as e:
    print(f"  real POST FAIL: {e}")

print()
print("=== Test C: validate then validate again (does token re-issue?) ===")
session3 = fetch_vietstock_session(timeout=15.0)
ok1 = validate_vietstock_session(session3, timeout=15)
ok2 = validate_vietstock_session(session3, timeout=15)
print(f"  validate #1: {ok1}")
print(f"  validate #2: {ok2}")
try:
    rows, _ = fetch_vietstock_events_page_with_total("2026-09-02", "2026-09-09", 1, 100, session=session3, timeout=15)
    print(f"  real POST after 2 validates: {len(rows)} rows")
except Exception as e:
    print(f"  real POST after 2 validates FAIL: {e}")
