from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyConfig:
    initialCash: float = 1_000_000_000
    maxPositions: int = 5
    bearMaxPositions: int = 2
    boardLotSize: int = 100
    sellSettlementDays: int = 3
    rebalanceDays: int = 5
    return3mDays: int = 63
    return6mDays: int = 126
    volatilityDays: int = 20
    smaFastDays: int = 50
    smaSlowDays: int = 100
    breakoutLookbackDays: int = 55
    breakoutVolumeDays: int = 20
    breakoutVolumeMultiplier: float = 1.5
    breakoutMaxRank: int = 20
    breakoutScoreBonus: float = 1.0
    maxBreakoutEntriesPerWeek: int = 2
    maxSectorPositions: int = 2
    maxSma50ExtensionPercent: float = 25.0
    marketSmaDays: int = 100
    marketBearSmaDays: int = 200
    marketExitRegime: int = 1
    neutralTrailingStopPercent: float = 7.0
    bearTrailingStopPercent: float = 5.0
    bullExposurePercent: float = 100.0
    neutralExposurePercent: float = 50.0
    bearExposurePercent: float = 20.0
    breakoutExitSmaDays: int = 20
    exitRank: int = 20
    stopLossPercent: float = 8.0
    trailingStopPercent: float = 10.0
    feeRate: float = 0.0015
    slippageRate: float = 0.001
