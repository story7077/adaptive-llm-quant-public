from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.domain.contracts import PortfolioDecision, StrategyForecast
from trading.domain.hashing import canonical_hash, stable_id

PHASE0_TARGET_WEIGHTS: dict[str, dict[str, float]] = {
    "B0-CASH": {"USD_CASH": 1.00},
    "B0-QQQ": {"QQQ": 0.20, "USD_CASH": 0.80},
    "B0-VOL": {"QQQ": 0.18, "GLD": 0.07, "USD_CASH": 0.75},
    "B1": {"QQQ": 0.15, "SOXX": 0.06, "GLD": 0.04, "USD_CASH": 0.75},
    "B2": {"QQQ": 0.15, "SOXX": 0.06, "GLD": 0.04, "USD_CASH": 0.75},
    "B3-RISK": {"QQQ": 0.10, "SOXX": 0.03, "GLD": 0.05, "USD_CASH": 0.82},
    "B3-FULL": {"QQQ": 0.15, "SOXX": 0.06, "GLD": 0.04, "USD_CASH": 0.75},
}


class Phase0PortfolioEngine:
    def decide(
        self,
        *,
        arm_id: str,
        forecasts: list[StrategyForecast],
        previous_weights: dict[str, float],
        decision_time: datetime,
        policy_version: int,
        input_snapshot_hash: str,
    ) -> PortfolioDecision:
        target = dict(PHASE0_TARGET_WEIGHTS[arm_id])
        turnover = sum(
            max(target.get(symbol, 0.0) - previous_weights.get(symbol, 0.0), 0.0)
            for symbol in set(target) | set(previous_weights)
            if symbol != "USD_CASH"
        )
        return PortfolioDecision(
            portfolio_decision_id=stable_id(
                "pdec", arm_id, decision_time, input_snapshot_hash, policy_version
            ),
            arm_id=arm_id,
            decision_time=decision_time,
            core_portfolio_version="phase0_fixture_targets_v1",
            policy_version=policy_version,
            forecast_ids=[forecast.forecast_id for forecast in forecasts],
            input_snapshot_hash=input_snapshot_hash,
            previous_weights=previous_weights,
            target_weights_pre_risk=target,
            expected_net_return_bps=0.0,
            expected_annualized_vol=0.0,
            expected_cvar_975=0.0,
            expected_turnover=turnover,
            expected_cost_usd=Decimal("0"),
            optimizer_status="DETERMINISTIC_NOOP",
            solver_name="NOT_REQUIRED_PHASE0",
            solver_diagnostics={
                "fixture_hash": canonical_hash(target),
                "phase1_optimizer_enabled": False,
            },
            created_at=decision_time,
        )

