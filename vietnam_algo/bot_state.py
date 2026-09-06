from __future__ import annotations

from dataclasses import asdict, dataclass
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
    tradingToken: str = ""


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
        tradingToken=str(payload.get("tradingToken", "")),
    )


def save_state(state: AutoBotState, data_dir: Path = DEFAULT_DATA_DIR, strategy_name: str = "") -> Path:
    path = state_path(data_dir, strategy_name)
    write_json(path, asdict(state))
    return path
