from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .data import DEFAULT_DATA_DIR, read_json, write_json, utc_now


@dataclass
class PendingRebuy:
    """A post-CA rebuy queued by a forced exit in the live engine.

    Mirrors the in-memory ``pending_corporate_action_rebuys`` dict used by
    ``backtest.py``. The fields are deliberately serialisable: ``dataclasses``
    + ``read_json``/``write_json`` is enough for the live bot because the
    total number of pending rebays is small (<30 across a 5-year horizon).
    """

    symbol: str
    exitDate: str                       # trading date on which the force-sell fired
    exitPrice: float                    # exit price (close)
    targetValue: float                  # cash reserved for the post-CA rebuy (VND)
    exDate: str                         # ex-date of the underlying CA event
    hasHighRisk: bool = False           # the exit was triggered by a high-risk event
    sector: str = "unknown"
    queuedAt: str = ""                  # ISO timestamp; useful for auditing
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PendingRebuy":
        return cls(
            symbol=str(payload["symbol"]),
            exitDate=str(payload["exitDate"]),
            exitPrice=float(payload["exitPrice"]),
            targetValue=float(payload["targetValue"]),
            exDate=str(payload["exDate"]),
            hasHighRisk=bool(payload.get("hasHighRisk", False)),
            sector=str(payload.get("sector", "unknown")),
            queuedAt=str(payload.get("queuedAt", "")),
            note=str(payload.get("note", "")),
        )


@dataclass
class ReservedCash:
    """Cash reserved against a pending rebuy so the next ``generate_live_orders``
    cycle doesn't spend it on a fresh entry.

    Mirrors the in-memory ``reserved_corporate_action_cash`` dict used by
    ``backtest.py``.

    Attributes
    ----------
    symbol:           ticker the reserve is tied to
    amount:           VND reserved
    exDate:           ex-date of the underlying CA
    expiryIndex:      absolute trading-date index after which the reserve is
                      automatically released (matches the backtest's
                      ``expiry_index = index + ReserveExpiryDays``)
    sector:           sector tag (used by sector-cap accounting)
    reservedAt:       ISO timestamp; useful for auditing
    """

    symbol: str
    amount: float
    exDate: str
    expiryIndex: int
    sector: str = "unknown"
    reservedAt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReservedCash":
        return cls(
            symbol=str(payload["symbol"]),
            amount=float(payload["amount"]),
            exDate=str(payload["exDate"]),
            expiryIndex=int(payload["expiryIndex"]),
            sector=str(payload.get("sector", "unknown")),
            reservedAt=str(payload.get("reservedAt", "")),
        )


@dataclass
class CaRebuyState:
    """Persistent in-process state for the live CA-rebuy queue.

    This is the live-equivalent of the backtest's local
    ``pending_corporate_action_rebuys`` + ``reserved_corporate_action_cash``
    dicts. The bot's daily cycle loads this on every iteration, mutates it
    (queue/expire/fire/drop), and writes it back. Keeping it on disk means
    reboots don't drop queued rebays.
    """

    pendingRebuys: list[PendingRebuy] = field(default_factory=list)
    reservedCash: list[ReservedCash] = field(default_factory=list)
    lastUpdated: str = ""

    # ------------------------------------------------------------------ I/O

    @staticmethod
    def state_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
        return data_dir / "live" / "ca_rebuy_state.json"

    @classmethod
    def load(cls, data_dir: Path = DEFAULT_DATA_DIR) -> "CaRebuyState":
        path = cls.state_path(data_dir)
        if not path.exists():
            return cls()
        payload = read_json(path) or {}
        return cls.from_dict(payload)

    def save(self, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
        self.lastUpdated = utc_now()
        path = self.state_path(data_dir)
        write_json(path, self.to_dict())
        return path

    # ---------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, Any]:
        return {
            "pendingRebuys": [p.to_dict() for p in self.pendingRebuys],
            "reservedCash": [r.to_dict() for r in self.reservedCash],
            "lastUpdated": self.lastUpdated,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CaRebuyState":
        pending = [PendingRebuy.from_dict(p) for p in payload.get("pendingRebuys", []) or []]
        reserved = [ReservedCash.from_dict(r) for r in payload.get("reservedCash", []) or []]
        return cls(
            pendingRebuys=pending,
            reservedCash=reserved,
            lastUpdated=str(payload.get("lastUpdated", "")),
        )

    # ------------------------------------------------------- mutators

    def enqueue(
        self,
        symbol: str,
        exit_date: str,
        exit_price: float,
        target_value: float,
        ex_date: str,
        has_high_risk: bool,
        sector: str = "unknown",
        expiry_index: int = 0,
        note: str = "",
    ) -> None:
        """Queue a new rebuy. If a pending record for the symbol already
        exists, refresh it (last writer wins). Reserves the cash too."""
        existing_idx = next(
            (i for i, p in enumerate(self.pendingRebuys) if p.symbol == symbol),
            None,
        )
        new_pending = PendingRebuy(
            symbol=symbol,
            exitDate=exit_date,
            exitPrice=exit_price,
            targetValue=target_value,
            exDate=ex_date,
            hasHighRisk=has_high_risk,
            sector=sector,
            queuedAt=utc_now(),
            note=note,
        )
        if existing_idx is not None:
            self.pendingRebuys[existing_idx] = new_pending
        else:
            self.pendingRebuys.append(new_pending)

        # Reserve the cash only when explicitly requested (expiry_index >= 0).
        if target_value > 0 and expiry_index > 0:
            existing_reserve = next(
                (i for i, r in enumerate(self.reservedCash) if r.symbol == symbol),
                None,
            )
            new_reserve = ReservedCash(
                symbol=symbol,
                amount=target_value,
                exDate=ex_date,
                expiryIndex=expiry_index,
                sector=sector,
                reservedAt=utc_now(),
            )
            if existing_reserve is not None:
                self.reservedCash[existing_reserve] = new_reserve
            else:
                self.reservedCash.append(new_reserve)

    def drop(self, symbol: str) -> None:
        """Remove both the pending rebuy and the cash reserve for ``symbol``."""
        self.pendingRebuys = [p for p in self.pendingRebuys if p.symbol != symbol]
        self.reservedCash = [r for r in self.reservedCash if r.symbol != symbol]

    def drop_expired(self, current_trading_index: int) -> list[str]:
        """Drop any pending rebuy whose reserve expired. Returns the dropped symbols."""
        expired_symbols: list[str] = []
        new_reserved: list[ReservedCash] = []
        for r in self.reservedCash:
            if current_trading_index > r.expiryIndex:
                expired_symbols.append(r.symbol)
            else:
                new_reserved.append(r)
        self.reservedCash = new_reserved
        keep = {r.symbol for r in self.reservedCash}
        self.pendingRebuys = [p for p in self.pendingRebuys if p.symbol in keep]
        return expired_symbols

    # ------------------------------------------------------ queries

    def pending_for(self, symbol: str) -> PendingRebuy | None:
        return next((p for p in self.pendingRebuys if p.symbol == symbol), None)

    def reserved_total(self) -> float:
        return sum(r.amount for r in self.reservedCash)

    def reserved_sector_counts(self, sector_of: dict[str, str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.reservedCash:
            sym_sector = sector_of.get(r.symbol, r.sector)
            counts[sym_sector] = counts.get(sym_sector, 0) + 1
        return counts
