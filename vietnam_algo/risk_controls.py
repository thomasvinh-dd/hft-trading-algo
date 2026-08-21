from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiveRiskLimits:
    tradingEnabled: bool = False
    maxOrderNotional: float = 0.0
    maxDailyNotional: float = 0.0
    maxOrderCount: int = 0


def live_risk_limits_from_env() -> LiveRiskLimits:
    enabled = os.environ.get("TRADING_ENABLED", "0").strip() in {"1", "true", "True", "yes"}
    return LiveRiskLimits(
        tradingEnabled=enabled,
        maxOrderNotional=float(os.environ.get("MAX_ORDER_NOTIONAL", "0") or 0),
        maxDailyNotional=float(os.environ.get("MAX_DAILY_NOTIONAL", "0") or 0),
        maxOrderCount=int(float(os.environ.get("MAX_ORDER_COUNT", "0") or 0)),
    )


def validate_live_orders(orders: list[Any], limits: LiveRiskLimits) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    if not limits.tradingEnabled:
        violations.append({"type": "trading_disabled", "reason": "TRADING_ENABLED is not set to 1"})
    if limits.maxOrderCount > 0 and len(orders) > limits.maxOrderCount:
        violations.append({"type": "max_order_count", "count": len(orders), "limit": limits.maxOrderCount})
    total_notional = 0.0
    for order in orders:
        notional = float(order.quantity) * float(order.price)
        total_notional += notional
        if limits.maxOrderNotional > 0 and notional > limits.maxOrderNotional:
            violations.append(
                {
                    "type": "max_order_notional",
                    "symbol": order.symbol,
                    "side": order.side,
                    "notional": notional,
                    "limit": limits.maxOrderNotional,
                }
            )
    if limits.maxDailyNotional > 0 and total_notional > limits.maxDailyNotional:
        violations.append({"type": "max_daily_notional", "notional": total_notional, "limit": limits.maxDailyNotional})
    return violations
