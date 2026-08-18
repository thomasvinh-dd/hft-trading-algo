from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .data import DEFAULT_DATA_DIR, read_json, write_json


@dataclass
class AutoBotState:
    tokenDate: str | None = None
    orderSubmitDate: str | None = None
    eodDate: str | None = None
    corporateActionDate: str | None = None
    tradingToken: str = ""


def state_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir / "live" / "auto_bot_state.json"


def load_state(data_dir: Path = DEFAULT_DATA_DIR) -> AutoBotState:
    path = state_path(data_dir)
    if not path.exists():
        return AutoBotState()
    payload = read_json(path)
    return AutoBotState(
        tokenDate=payload.get("tokenDate"),
        orderSubmitDate=payload.get("orderSubmitDate"),
        eodDate=payload.get("eodDate"),
        corporateActionDate=payload.get("corporateActionDate"),
        tradingToken=str(payload.get("tradingToken", "")),
    )


def save_state(state: AutoBotState, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    path = state_path(data_dir)
    write_json(path, asdict(state))
    return path
