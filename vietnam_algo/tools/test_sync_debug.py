import os
os.environ["VIETSTOCK_DEBUG"] = "1"
import sys
sys.path.insert(0, "/root/hft-trading-algo")
from pathlib import Path
from datetime import datetime
from vietnam_algo.libs.corporate_actions import sync_corporate_actions_incremental

try:
    r = sync_corporate_actions_incremental(
        Path("/root/hft-trading-algo/vietnam_algo/data"),
        request_verification_token="",
        cookie="",
        to_date=datetime.now().date().isoformat(),
        max_attempts=2,
        initial_backoff_seconds=2.0,
    )
    print(f"OK: fetched={r.get('fetched')}, added={r.get('added')}")
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {e}")
