from __future__ import annotations

from typing import Protocol

from trading.domain.contracts import PortfolioDecision, StrategyForecast


class PortfolioEngine(Protocol):
    def decide(
        self,
        *,
        arm_id: str,
        forecasts: list[StrategyForecast],
        previous_weights: dict[str, float],
    ) -> PortfolioDecision: ...

