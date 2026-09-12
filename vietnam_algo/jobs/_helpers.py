"""Internal helpers for ``vietnam_algo.jobs.common_sync``.

These were lifted out of the bot's own EOD pipeline so the cron job
can re-use them without dragging the bot's argparse layer into cron.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from vietnam_algo.libs.corporate_actions import sync_corporate_actions_incremental


def run_ca_incremental(
    index_path: Path,
    existing: list[dict] | None = None,
) -> list[dict]:
    """Run the CA incremental sync and append/refresh today's events.

    ``index_path`` is the directory where daily CA JSON files live.
    ``existing`` is the current in-memory event list (so we can
    de-duplicate without re-reading all files).

    The vendored ``sync_corporate_actions_incremental`` returns the
    merged in-memory list and persists new entries via its own
    callbacks. We return the post-sync list so the caller can summarise.
    """
    merged: list[dict] = list(existing or [])
    seen = {(e.get("symbol"), e.get("eventDate"), e.get("type")) for e in merged}

    def _on_new(event: dict) -> None:
        key = (event.get("symbol"), event.get("eventDate"), event.get("type"))
        if key in seen:
            return
        merged.append(event)
        seen.add(key)

    try:
        synced = sync_corporate_actions_incremental(
            index_path,
            from_date=None,
            on_event=_on_new,
        )
    except Exception:
        return merged
    return synced or merged
