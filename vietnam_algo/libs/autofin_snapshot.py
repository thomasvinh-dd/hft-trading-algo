"""Snapshot autofin's raw API responses for every decision cycle.

Why this exists (2026-10-29, the VOS incident)
-----------------------------------------------
The bot entered a NEW position on VOS even though autofin's own
recommendation history for that deal had moved to "⚠️ Hạ tỷ trọng lần 2"
(reduce weight). To answer "was the mistake ours, or did autofin send us
bad data?" we needed the RAW payloads autofin returned at decision
time. We had none — the only evidence was our own cached/derived
state, which is exactly what was in question.

So: every decision cycle writes a gzipped snapshot of
  - every raw autofin HTTP response used for the decision
  - a human-readable ``decision_summary.json`` describing what the bot
    decided for each candidate and WHY
  - the resolved per-deal recommendation kind (our classification of
    autofin's text) so a disagreement between "what autofin said" and
    "how we read it" is visible after the fact.

Layout under ``<data_dir>/live/autofin_snapshots/``::

    2026-10-29/
        2026-10-29T09-01-12Z.json.gz      # full raw payload bundle
        2026-10-29T09-01-12Z.summary.json # decision summary (readable)

Retention is by day (default 7). Older day directories are pruned.

Everything here is best-effort: a snapshot failure must NEVER break a
trading cycle, so every write is wrapped and swallowed (with a printed
warning).
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SNAPSHOT_RETENTION_DAYS = 7


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _cycle_stamp(dt: datetime | None = None) -> str:
    """A filesystem-safe ISO stamp for a cycle.

    ``2026-10-29T09-01-12Z`` — colons are illegal on some filesystems
    (and annoying in shell commands), so we use dashes.
    """
    dt = dt or _now_utc()
    return dt.strftime("%Y-%m-%dT%H-%M-%SZ")


def snapshot_root(data_dir: Path | str) -> Path:
    """The directory that holds all autofin snapshots."""
    return Path(data_dir) / "live" / "autofin_snapshots"


def _retention_days() -> int:
    raw = os.environ.get("AUTOFIN_SNAPSHOT_RETENTION_DAYS", "").strip()
    if raw:
        try:
            return max(1, int(float(raw)))
        except ValueError:
            pass
    return DEFAULT_SNAPSHOT_RETENTION_DAYS


def prune_snapshots(data_dir: Path | str, keep_days: int | None = None) -> list[str]:
    """Delete snapshot day-directories older than the retention window.

    Returns the list of pruned day-directory names. Best-effort: a
    failure to remove a directory is ignored (it just means we keep
    more history than requested).
    """
    keep = keep_days if keep_days is not None else _retention_days()
    root = snapshot_root(data_dir)
    if not root.exists():
        return []
    cutoff = _now_utc().date().toordinal() - keep
    pruned: list[str] = []
    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            try:
                ordinal = datetime.strptime(child.name, "%Y-%m-%d").date().toordinal()
            except ValueError:
                continue  # not one of ours — leave it alone
            if ordinal < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                pruned.append(child.name)
    except Exception:
        pass
    return pruned


def write_snapshot(
    data_dir: Path | str,
    payloads: dict[str, Any],
    summary: dict[str, Any] | None = None,
    *,
    dt: datetime | None = None,
) -> Path | None:
    """Write one cycle's snapshot bundle.

    ``payloads`` maps a label to the RAW autofin payload for that
    endpoint (e.g. ``{"holding": [...], "suggest": [...]}``).
    ``summary`` is the decision summary written alongside as plain
    (uncompressed) JSON so it can be inspected without gunzipping.

    Returns the path of the raw bundle, or None if the write failed.
    """
    dt = dt or _now_utc()
    stamp = _cycle_stamp(dt)
    try:
        root = snapshot_root(data_dir)
        day_dir = root / dt.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)
        bundle = {
            "capturedAt": dt.isoformat(),
            "tradeDate": dt.strftime("%Y-%m-%d"),
            "payloads": payloads,
        }
        raw_path = day_dir / f"{stamp}.json.gz"
        with gzip.open(raw_path, "wt", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, indent=1, default=str)
        if summary is not None:
            sum_path = day_dir / f"{stamp}.summary.json"
            sum_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        # Pruning is housekeeping — a failure here must NOT lose the
        # snapshot we just wrote, so it is isolated from the write path.
        try:
            prune_snapshots(data_dir)
        except Exception as exc:
            print(f"[autofin_snapshot] prune failed: {exc}")
        return raw_path
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[autofin_snapshot] write failed: {type(exc).__name__}: {exc}")
        return None


def read_snapshot(path: Path | str) -> dict[str, Any]:
    """Read back a raw snapshot bundle (handles both .gz and plain .json)."""
    p = Path(path)
    if p.suffix == ".gz":
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(p.read_text(encoding="utf-8"))


def latest_snapshot_paths(data_dir: Path | str) -> tuple[Path | None, Path | None]:
    """Return ``(raw_bundle_path, summary_path)`` for the newest snapshot.

    Used by the operator tooling and by tests to find "the snapshot for
    the decision I'm asking about".
    """
    root = snapshot_root(data_dir)
    if not root.exists():
        return None, None
    raws: list[Path] = []
    sums: list[Path] = []
    try:
        for day_dir in sorted(root.iterdir(), reverse=True):
            if not day_dir.is_dir():
                continue
            raws.extend(sorted(day_dir.glob("*.json.gz"), reverse=True))
            sums.extend(sorted(day_dir.glob("*.summary.json"), reverse=True))
            if raws:
                break
    except Exception:
        pass
    return (raws[0] if raws else None, sums[0] if sums else None)
