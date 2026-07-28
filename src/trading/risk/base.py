from __future__ import annotations

from typing import Protocol

from trading.domain.contracts import PortfolioDecision, RiskDecision


class RiskEngine(Protocol):
    def evaluate(self, decision: PortfolioDecision) -> RiskDecision: ...

