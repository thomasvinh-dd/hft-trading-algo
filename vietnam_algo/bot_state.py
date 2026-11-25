from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .data import DEFAULT_DATA_DIR, read_json, write_json


@dataclass
class AutoBotState:
    tokenDate: str | None = None
    tokenExpiry: str | None = None  # ISO8601 UTC; parsed from JWT `exp`
    orderSubmitDate: str | None = None
    eodDate: str | None = None
    corporateActionDate: str | None = None
    marketHolidaySyncDate: str | None = None
    lastHeartbeatDate: str | None = None
    startOfDayDate: str | None = None
    tradingToken: str = ""
    # Date (YYYY-MM-DD) we last sent a CA-sync-failure Telegram alert.
    # Empty string means no failure alert has been sent. Resets after a
    # successful sync (we clear it in save_state when sync succeeds).
    caAlertSentDate: str = ""
    last_ca_sync_error: str = ""  # most recent error message, for status display
    # Once-per-day suppression for the "market data stale" alert
    # (sentinel too old / sample latest bar too old). Reset by
    # ``runner.py`` when the next heartbeat / cycle confirms freshness.
    dataStaleAlertSentDate: str = ""
    # Once-per-day suppression for the "fetch error during market data
    # sync" alert (>5% of symbols failing). Reset by ``runner.py`` on
    # the next successful sync run.
    dataFetchErrorAlertSentDate: str = ""
    lastDataSyncErrorSummary: str = ""  # e.g. "3/1535 symbols failed" for status


def state_path(data_dir: Path = DEFAULT_DATA_DIR, strategy_name: str = "") -> Path:
    """Path to the auto-bot state file.

    When ``strategy_name`` is non-empty, the file is namespaced
    (e.g. ``auto_bot_state.momentum.json``) so multiple strategies can
    run side-by-side without clobbering each other.
    """
    name = f"auto_bot_state.{strategy_name}.json" if strategy_name else "auto_bot_state.json"
    return data_dir / "live" / name


def load_state(data_dir: Path = DEFAULT_DATA_DIR, strategy_name: str = "") -> AutoBotState:
    path = state_path(data_dir, strategy_name)
    if not path.exists():
        return AutoBotState()
    payload = read_json(path)
    return AutoBotState(
        tokenDate=payload.get("tokenDate"),
        tokenExpiry=payload.get("tokenExpiry"),
        orderSubmitDate=payload.get("orderSubmitDate"),
        eodDate=payload.get("eodDate"),
        corporateActionDate=payload.get("corporateActionDate"),
        marketHolidaySyncDate=payload.get("marketHolidaySyncDate"),
        lastHeartbeatDate=payload.get("lastHeartbeatDate"),
        startOfDayDate=payload.get("startOfDayDate"),
        tradingToken=str(payload.get("tradingToken", "")),
        caAlertSentDate=str(payload.get("caAlertSentDate") or ""),
        last_ca_sync_error=str(payload.get("last_ca_sync_error") or ""),
        dataStaleAlertSentDate=str(payload.get("dataStaleAlertSentDate") or ""),
        dataFetchErrorAlertSentDate=str(payload.get("dataFetchErrorAlertSentDate") or ""),
        lastDataSyncErrorSummary=str(payload.get("lastDataSyncErrorSummary") or ""),
    )


def save_state(state: AutoBotState, data_dir: Path = DEFAULT_DATA_DIR, strategy_name: str = "") -> Path:
    path = state_path(data_dir, strategy_name)
    write_json(path, asdict(state))
    return path
