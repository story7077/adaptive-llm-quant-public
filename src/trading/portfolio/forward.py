from __future__ import annotations

import math
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from trading.llm.policy_compiler import PolicyState

FORWARD_ORDER_ARMS = ("B0-CASH", "B0-QQQ", "B0-VOL", "B3-RISK")
CORE_SYMBOL = "QQQ"
CASH_SYMBOL = "USD_CASH"


class ForwardPortfolioError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ForwardCoreForecast:
    version: str
    lookback_sessions: int
    observations: int
    annualized_vol: float
    qqq_weight: float


@dataclass(frozen=True, slots=True)
class ForwardTarget:
    arm_id: str
    target_weights: dict[str, float]
    core_forecast: ForwardCoreForecast
    policy_version: int
    policy_risk_multiplier: float
    blocked_new_entries: frozenset[str]
    target_reason: str


class PriceQuote(Protocol):
    @property
    def midpoint(self) -> Decimal: ...


def build_core_forecast(
    closes: list[float],
    *,
    version: str,
    lookback_sessions: int,
    target_annualized_vol: float,
) -> ForwardCoreForecast:
    if lookback_sessions < 2:
        raise ForwardPortfolioError("vol_lookback_trading_days must be at least two")
    required = lookback_sessions + 1
    if len(closes) < required:
        raise ForwardPortfolioError(
            f"B0-VOL needs {required} QQQ closes; received {len(closes)}"
        )
    selected = closes[-required:]
    if any(value <= 0 or not math.isfinite(value) for value in selected):
        raise ForwardPortfolioError("QQQ close history must be positive and finite")
    returns = [
        math.log(selected[index] / selected[index - 1])
        for index in range(1, len(selected))
    ]
    annualized_vol = statistics.stdev(returns) * math.sqrt(252)
    if not math.isfinite(annualized_vol) or annualized_vol <= 0:
        raise ForwardPortfolioError("B0-VOL forecast volatility is not positive")
    qqq_weight = min(max(target_annualized_vol / annualized_vol, 0.0), 1.0)
    return ForwardCoreForecast(
        version=version,
        lookback_sessions=lookback_sessions,
        observations=len(returns),
        annualized_vol=annualized_vol,
        qqq_weight=qqq_weight,
    )


def target_for_arm(
    arm_id: str,
    *,
    core: ForwardCoreForecast,
    policy: PolicyState | None = None,
) -> ForwardTarget:
    active_policy = policy or PolicyState.default(arm_id)
    if arm_id == "B0-CASH":
        qqq_weight = 0.0
        reason = "USD_CASH_CONTROL"
    elif arm_id == "B0-QQQ":
        qqq_weight = 1.0
        reason = "QQQ_BUY_AND_HOLD_CONTROL"
    elif arm_id == "B0-VOL":
        qqq_weight = core.qqq_weight
        reason = "VOL_TARGETED_QQQ_CASH_CORE"
    elif arm_id == "B3-RISK":
        qqq_weight = core.qqq_weight * active_policy.portfolio_risk_multiplier
        reason = "VOL_CORE_WITH_LLM_RISK_REDUCTION"
    else:
        raise ForwardPortfolioError(f"Arm {arm_id!r} is not forward order-enabled")
    qqq_weight = min(max(qqq_weight, 0.0), 1.0)
    return ForwardTarget(
        arm_id=arm_id,
        target_weights={
            CORE_SYMBOL: qqq_weight,
            CASH_SYMBOL: 1.0 - qqq_weight,
        },
        core_forecast=core,
        policy_version=active_policy.version,
        policy_risk_multiplier=active_policy.portfolio_risk_multiplier,
        blocked_new_entries=active_policy.blocked_targets,
        target_reason=reason,
    )


def apply_core_rebalance_band(
    target: ForwardTarget,
    *,
    cash_usd: Decimal,
    positions: dict[str, Decimal],
    quotes: Mapping[str, PriceQuote],
    min_weight_delta: float,
) -> ForwardTarget:
    """Keep a settled QQQ/cash core unchanged inside its versioned no-trade band.

    The band is deliberately bypassed while inherited non-core positions remain and
    when B3-RISK is actively reducing risk. Those are risk-transition actions, not
    routine volatility-target rebalances.
    """
    if target.arm_id not in {"B0-VOL", "B3-RISK"}:
        return target
    if not 0 <= min_weight_delta <= 1:
        raise ForwardPortfolioError("min_rebalance_weight_delta must be within [0, 1]")
    non_core_positions = {
        symbol: quantity
        for symbol, quantity in positions.items()
        if symbol != CORE_SYMBOL and quantity != 0
    }
    if non_core_positions:
        return target
    if (
        target.arm_id == "B3-RISK"
        and target.policy_risk_multiplier < 1.0
    ):
        return target
    qqq_quantity = positions.get(CORE_SYMBOL, Decimal("0"))
    quote = quotes.get(CORE_SYMBOL)
    if quote is None:
        raise ForwardPortfolioError("QQQ quote is required for the rebalance band")
    nav = cash_usd + qqq_quantity * quote.midpoint
    if nav <= 0:
        raise ForwardPortfolioError("Core NAV must be positive")
    current_weight = float(qqq_quantity * quote.midpoint / nav)
    desired_weight = target.target_weights.get(CORE_SYMBOL, 0.0)
    if abs(desired_weight - current_weight) >= min_weight_delta:
        return target
    return replace(
        target,
        target_weights={
            CORE_SYMBOL: current_weight,
            CASH_SYMBOL: 1.0 - current_weight,
        },
        target_reason=f"{target.target_reason}_WITHIN_REBALANCE_BAND",
    )
