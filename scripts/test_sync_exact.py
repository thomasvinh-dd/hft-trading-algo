"""Test sync with the EXACT same args as standalone."""
import sys
sys.path.insert(0, "/root/hft-trading-algo")
from pathlib import Path
from vietnam_algo.corporate_actions import sync_corporate_actions_incremental

# Use the same args as my standalone success
try:
    r = sync_corporate_actions_incremental(
        Path("/root/hft-trading-algo/vietnam_algo/data"),
        request_verification_token="",
        cookie="",
        to_date="2026-09-09",
        page_size=100,
        timeout=30.0,
        max_attempts=3,
        initial_backoff_seconds=10.0,
    )
    print(f"OK: fetched={r.get('fetched')}, added={r.get('added')}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
