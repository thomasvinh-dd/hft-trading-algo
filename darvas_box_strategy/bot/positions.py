"""Live position tracking for the Darvas Box Telegram bot.

The bot needs to remember which positions it opened so it can evaluate
the exit ladder (TIER / TRAIL / SL_SCALE / SL_HARD) on subsequent bars
and notify Telegram when an exit fires.

State is persisted to a JSON file (``darvas_box_strategy/data/positions.json``)
so it survives between cron runs.

State schema::

    {
      "version": 1,
      "updated_at": "2026-10-31T15:47:00",
      "positions": {
        "ORS_2026-08-12": {
          "ticker": "ORS",
          "entry_date": "2026-08-12",
          "entry_price": 15150.0,
          "shares": 9900,
          "remaining": 9900,
          "size_pct": 0.15,
          "sl_T": 13635.0,
          "sl_F": 12423.0,
          "tiers": [[1.063, 0.10], ...],
          "conviction": "low",
          "tier_idx": 0,
          "trailing_active": false,
          "trail_floor": 0.0,
          "peak_close": 15150.0,
          "last_close": 15150.0,
          "bars_held": 0,
          "cumulative_pnl": 0.0,
          "realized_pnl": 0.0,
          "notified": ["2026-08-20:TIER"],   # dedupe keys already sent
          "last_bar_date": "2026-08-12"
        }
      }
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("darvas.bot.positions")

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "darvas_box_strategy" / "data" / "positions.json"

STATE_VERSION = 1


@dataclass
class LivePosition:
    """A position the bot opened and is tracking for exit events."""

    ticker: str
    entry_date: str            # ISO date of the fill
    entry_price: float
    shares: int
    remaining: int
    size_pct: float = 0.0
    sl_T: float = 0.0
    sl_F: float = 0.0
    tiers: List[List[float]] = field(default_factory=list)
    conviction: str = "low"
    tier_idx: int = 0
    trailing_active: bool = False
    trail_floor: float = 0.0
    peak_close: float = 0.0
    last_close: float = 0.0
    bars_held: int = 0
    cumulative_pnl: float = 0.0
    realized_pnl: float = 0.0
    # Dedupe keys of exit events already sent to Telegram
    notified: List[str] = field(default_factory=list)
    # Date of the last bar we evaluated (so we don't re-evaluate)
    last_bar_date: str = ""

    @property
    def key(self) -> str:
        return f"{self.ticker}_{self.entry_date}"

    @property
    def sl_midpoint(self) -> float:
        if self.sl_T and self.sl_F:
            return (self.sl_F + self.sl_T) / 2.0
        return 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LivePosition":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def load_state(path: Path = STATE_PATH) -> Dict[str, LivePosition]:
    """Load persisted positions. Returns an empty dict on any error."""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("positions state unreadable (%s) — starting empty", exc)
        return {}
    positions: Dict[str, LivePosition] = {}
    for key, raw in (payload.get("positions") or {}).items():
        try:
            pos = LivePosition.from_dict(raw)
            positions[key] = pos
        except (TypeError, ValueError) as exc:
            log.warning("skipping bad position %s: %s", key, exc)
    log.info("loaded %d live position(s) from %s", len(positions), path)
    return positions


def save_state(
    positions: Dict[str, LivePosition], path: Path = STATE_PATH,
) -> None:
    """Persist positions to disk (atomic write)."""
    payload = {
        "version": STATE_VERSION,
        "updated_at": date.today().isoformat(),
        "positions": {k: v.to_dict() for k, v in positions.items()},
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.error("failed to save positions state to %s: %s", path, exc)
        raise


def position_from_signal(signal, shares: Optional[int] = None) -> LivePosition:
    """Build a LivePosition from a generated Signal."""
    entry_date = signal.entry_date
    if hasattr(entry_date, "isoformat"):
        entry_date = entry_date.isoformat()
    sh = int(shares if shares is not None else signal.shares)
    tiers = [[float(m), float(p)] for m, p in (signal.tiers or ())]
    return LivePosition(
        ticker=signal.ticker,
        entry_date=str(entry_date),
        entry_price=float(signal.entry_price),
        shares=sh,
        remaining=sh,
        size_pct=float(getattr(signal, "size_pct", 0.0) or 0.0),
        sl_T=float(signal.sl_T),
        sl_F=float(signal.sl_F),
        tiers=tiers,
        conviction=str(getattr(signal, "conviction", "low") or "low"),
        peak_close=float(signal.entry_price),
        last_close=float(signal.entry_price),
    )


def upsert_position(
    positions: Dict[str, LivePosition], pos: LivePosition,
) -> None:
    """Insert or replace a position, keyed by ticker+entry_date."""
    positions[pos.key] = pos


def remove_position(
    positions: Dict[str, LivePosition], key: str,
) -> Optional[LivePosition]:
    return positions.pop(key, None)


def already_notified(pos: LivePosition, key: str) -> bool:
    return key in pos.notified


def mark_notified(pos: LivePosition, key: str) -> None:
    if key not in pos.notified:
        pos.notified.append(key)


def find_position_for_ticker(
    positions: Dict[str, LivePosition], ticker: str,
) -> Optional[LivePosition]:
    """Return the most recent open position for a ticker (if any)."""
    candidates = [p for p in positions.values() if p.ticker == ticker.upper()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.entry_date)
    return candidates[-1]
