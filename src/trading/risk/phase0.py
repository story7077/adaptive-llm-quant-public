from __future__ import annotations

from datetime import datetime

from trading.domain.contracts import PortfolioDecision, RiskDecision
from trading.domain.hashing import stable_id


class Phase0RiskEngine:
    def evaluate(
        self,
        decision: PortfolioDecision,
        *,
        created_at: datetime,
    ) -> RiskDecision:
        weights = decision.target_weights_pre_risk
        reasons: list[str] = []
        if any(weight < -1e-12 for weight in weights.values()):
            reasons.append("LONG_ONLY_VIOLATION")
        if abs(sum(weights.values()) - 1.0) > 1e-8:
            reasons.append("WEIGHT_SUM_VIOLATION")
        if any(weight > 0.35 + 1e-12 for symbol, weight in weights.items() if symbol != "USD_CASH"):
            reasons.append("MAX_SINGLE_SYMBOL_VIOLATION")
        if decision.expected_turnover > 0.25 + 1e-12:
            reasons.append("TURNOVER_VIOLATION")
        approved = not reasons
        return RiskDecision(
            risk_decision_id=stable_id("rdec", decision.portfolio_decision_id),
            portfolio_decision_id=decision.portfolio_decision_id,
            approved=approved,
            approved_target_weights=dict(weights) if approved else {"USD_CASH": 1.0},
            rejected_reasons=reasons,
            forced_reduction_actions=[],
            risk_config_version="risk_v1",
            market_data_age_seconds=60.0,
            created_at=created_at,
        )

