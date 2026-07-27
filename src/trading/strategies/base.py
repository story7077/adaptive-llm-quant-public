from __future__ import annotations

from typing import Protocol

from trading.domain.contracts import FeatureSnapshot, StrategyForecast
from trading.strategies.models import StrategyForecastContext


class Strategy(Protocol):
    strategy_id: str

    def forecast(
        self,
        feature: FeatureSnapshot,
        *,
        context: StrategyForecastContext,
    ) -> StrategyForecast: ...
