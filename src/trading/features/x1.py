from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from trading.features.models import (
    AdjustedPriceObservation,
    FeatureBuildContext,
    FeatureBuildResult,
    PortfolioWeightSnapshot,
    blocked_result,
    feature_snapshot,
)
from trading.features.pit import NEW_YORK, common_sessions, select_session_marks
from trading.features.statistics import (
    StatisticsError,
    covariance_matrix,
    robust_z,
    sample_std,
    simple_return,
)

STRATEGY_ID = "X1"
FEATURE_CODE_VERSION = "x1_features_v1"


@dataclass(frozen=True, slots=True)
class X1FeatureParameters:
    assets: tuple[str, ...] = (
        "SPY",
        "QQQ",
        "IWM",
        "SOXX",
        "XLK",
        "HYG",
        "TLT",
        "GLD",
    )
    cash_symbol: str = "USD_CASH"
    lookbacks: tuple[int, int] = (21, 63)
    risk_history_days: int = 63
    horizon_days: int = 5
    min_active_weight_change: Decimal = Decimal("0.03")
    max_single_weight: Decimal = Decimal("0.35")
    clusters: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            "US_TECH": ("QQQ", "SOXX", "SMH", "XLK"),
            "SEMICONDUCTOR": ("SOXX", "SMH"),
            "RATES_CREDIT": ("TLT", "HYG"),
        }
    )
    cluster_caps: dict[str, Decimal] = field(
        default_factory=lambda: {
            "US_TECH": Decimal("0.70"),
            "SEMICONDUCTOR": Decimal("0.55"),
            "RATES_CREDIT": Decimal("0.50"),
        }
    )

    def __post_init__(self) -> None:
        if not self.assets:
            raise ValueError("X1 assets cannot be empty")
        if len(self.lookbacks) != 2 or any(value <= 1 for value in self.lookbacks):
            raise ValueError("X1 requires two lookbacks greater than one")
        if self.risk_history_days <= 1 or self.horizon_days <= 0:
            raise ValueError("X1 risk history and horizon must be positive")
        if not Decimal("0") <= self.min_active_weight_change <= Decimal("1"):
            raise ValueError("X1 minimum active change must be in [0, 1]")
        if not Decimal("0") < self.max_single_weight <= Decimal("1"):
            raise ValueError("X1 max single weight must be in (0, 1]")
        if set(self.cluster_caps) != set(self.clusters):
            raise ValueError("X1 clusters and cluster caps must have the same keys")
        if any(not Decimal("0") < cap <= Decimal("1") for cap in self.cluster_caps.values()):
            raise ValueError("X1 cluster caps must be in (0, 1]")


def build_x1_features(
    *,
    context: FeatureBuildContext,
    prices: list[AdjustedPriceObservation],
    core_portfolio: PortfolioWeightSnapshot,
    previous_target: PortfolioWeightSnapshot,
    parameters: X1FeatureParameters | None = None,
) -> FeatureBuildResult:
    """Build X1 cross-asset trend scores and a capped defensive target."""

    if context.feature_set_version != FEATURE_CODE_VERSION:
        return blocked_result(STRATEGY_ID, None, "X1_FEATURE_VERSION_MISMATCH")
    parameters = parameters or X1FeatureParameters()
    if (
        core_portfolio.available_at > context.data_available_cutoff
        or previous_target.available_at > context.data_available_cutoff
    ):
        return blocked_result(STRATEGY_ID, None, "X1_PORTFOLIO_SNAPSHOT_FROM_FUTURE")

    required_symbols = (*parameters.assets, parameters.cash_symbol)
    supported = set(required_symbols)
    if any(
        symbol not in supported and weight != 0
        for snapshot in (core_portfolio, previous_target)
        for symbol, weight in snapshot.weights.items()
    ):
        return blocked_result(STRATEGY_ID, None, "X1_UNSUPPORTED_PORTFOLIO_SYMBOL")
    panel = select_session_marks(prices, reference_cutoff=context.data_available_cutoff)
    sessions = common_sessions(panel, required_symbols)
    required_history = max(
        *parameters.lookbacks,
        parameters.risk_history_days,
    )
    if len(sessions) <= required_history:
        return blocked_result(STRATEGY_ID, None, "X1_INSUFFICIENT_PRICE_HISTORY")

    current_session = sessions[-1]
    if current_session != context.data_available_cutoff.astimezone(NEW_YORK).date():
        return blocked_result(STRATEGY_ID, None, "X1_CURRENT_SESSION_MARK_REQUIRED")
    source_ids: set[str] = {
        core_portfolio.snapshot_id,
        previous_target.snapshot_id,
    }

    def price(symbol: str, index: int) -> Decimal:
        observation = panel[symbol][sessions[index]]
        source_ids.add(observation.source_record_id)
        return observation.adjusted_price

    try:
        ratios_by_lookback: dict[int, dict[str, Decimal]] = {}
        daily_returns: dict[str, list[Decimal]] = {}
        for symbol in parameters.assets:
            returns = [
                simple_return(price(symbol, index - 1), price(symbol, index))
                for index in range(
                    len(sessions) - parameters.risk_history_days,
                    len(sessions),
                )
            ]
            daily_returns[symbol] = returns

        for lookback in parameters.lookbacks:
            cash_return = simple_return(
                price(parameters.cash_symbol, len(sessions) - 1 - lookback),
                price(parameters.cash_symbol, len(sessions) - 1),
            )
            ratios: dict[str, Decimal] = {}
            for symbol in parameters.assets:
                period_return = simple_return(
                    price(symbol, len(sessions) - 1 - lookback),
                    price(symbol, len(sessions) - 1),
                )
                lookback_returns = [
                    simple_return(price(symbol, index - 1), price(symbol, index))
                    for index in range(len(sessions) - lookback, len(sessions))
                ]
                period_vol = sample_std(lookback_returns) * Decimal(lookback).sqrt()
                ratios[symbol] = (period_return - cash_return) / period_vol
            ratios_by_lookback[lookback] = ratios

        z_by_lookback = {
            lookback: {
                symbol: robust_z(
                    ratios_by_lookback[lookback][symbol],
                    list(ratios_by_lookback[lookback].values()),
                )
                for symbol in parameters.assets
            }
            for lookback in parameters.lookbacks
        }
        scores = {
            symbol: max(
                Decimal("0"),
                sum(
                    (z_by_lookback[lookback][symbol] for lookback in parameters.lookbacks),
                    Decimal("0"),
                )
                / Decimal(len(parameters.lookbacks)),
            )
            for symbol in parameters.assets
        }
        risk_covariance = covariance_matrix(
            daily_returns,
            horizon_periods=parameters.horizon_days,
        )
    except (KeyError, StatisticsError):
        return blocked_result(STRATEGY_ID, None, "X1_UNSTABLE_OR_INCOMPLETE_FEATURES")

    all_scores_zero = all(score == 0 for score in scores.values())
    if all_scores_zero:
        candidate = _complete_weights(
            core_portfolio.weights,
            required_symbols=required_symbols,
        )
    else:
        candidate = _risk_budget_target(
            scores=scores,
            covariance=risk_covariance,
        )
        candidate = _apply_weight_caps(candidate, parameters=parameters)
        candidate[parameters.cash_symbol] = Decimal("1") - sum(
            (weight for symbol, weight in candidate.items() if symbol != parameters.cash_symbol),
            Decimal("0"),
        )

    previous = _complete_weights(
        previous_target.weights,
        required_symbols=required_symbols,
    )
    core = _complete_weights(
        core_portfolio.weights,
        required_symbols=required_symbols,
    )
    one_way_change = sum(
        (
            abs(candidate.get(symbol, Decimal("0")) - previous.get(symbol, Decimal("0")))
            for symbol in set(candidate) | set(previous)
        ),
        Decimal("0"),
    ) / Decimal("2")
    rebalance_required = one_way_change >= parameters.min_active_weight_change
    effective_target = core if all_scores_zero else candidate if rebalance_required else previous
    active_delta = {
        symbol: effective_target.get(symbol, Decimal("0")) - core.get(symbol, Decimal("0"))
        for symbol in set(effective_target) | set(core)
    }
    active_delta[parameters.cash_symbol] = -sum(
        (value for symbol, value in active_delta.items() if symbol != parameters.cash_symbol),
        Decimal("0"),
    )
    raw_signal = sum(
        (effective_target.get(symbol, Decimal("0")) * scores[symbol] for symbol in scores),
        Decimal("0"),
    )

    lineage = sorted(source_ids)
    values: list[tuple[str, Decimal, str, list[str]]] = [
        ("raw_signal", raw_signal, "z_score", lineage),
        ("one_way_weight_change", one_way_change, "weight", lineage),
        ("rebalance_required", Decimal(int(rebalance_required)), "boolean", lineage),
    ]
    for symbol in parameters.assets:
        values.extend(
            (
                (f"score.{symbol}", scores[symbol], "z_score", lineage),
                (
                    f"target.{symbol}",
                    effective_target.get(symbol, Decimal("0")),
                    "weight",
                    lineage,
                ),
                (
                    f"active_delta.{symbol}",
                    active_delta.get(symbol, Decimal("0")),
                    "weight",
                    lineage,
                ),
            )
        )
    values.extend(
        (
            (
                f"target.{parameters.cash_symbol}",
                effective_target.get(parameters.cash_symbol, Decimal("0")),
                "weight",
                lineage,
            ),
            (
                f"active_delta.{parameters.cash_symbol}",
                active_delta[parameters.cash_symbol],
                "weight",
                lineage,
            ),
        )
    )
    for left_symbol in parameters.assets:
        for right_symbol in parameters.assets:
            values.append(
                (
                    f"cov.{left_symbol}.{right_symbol}",
                    risk_covariance[left_symbol][right_symbol],
                    "horizon_return_covariance",
                    lineage,
                )
            )

    manifest = {
        "strategy_id": STRATEGY_ID,
        "feature_code_version": FEATURE_CODE_VERSION,
        "parameters": asdict(parameters),
        "source_record_ids": lineage,
        "current_session": current_session.isoformat(),
        "core_portfolio_id": core_portfolio.portfolio_id,
    }
    return feature_snapshot(
        strategy_id=STRATEGY_ID,
        symbol=None,
        context=context,
        feature_code_version=FEATURE_CODE_VERSION,
        values=values,
        manifest=manifest,
    )


def _risk_budget_target(
    *,
    scores: dict[str, Decimal],
    covariance: dict[str, dict[str, Decimal]],
) -> dict[str, Decimal]:
    positive = {symbol: score for symbol, score in scores.items() if score > 0}
    total_score = sum(positive.values(), Decimal("0"))
    budgets = {symbol: score / total_score for symbol, score in positive.items()}
    weights = {symbol: budgets[symbol] / covariance[symbol][symbol].sqrt() for symbol in positive}
    weights = _normalize_positive(weights)

    for _ in range(100):
        marginal = {
            symbol: sum(
                (covariance[symbol][other] * weights[other] for other in weights),
                Decimal("0"),
            )
            for symbol in weights
        }
        total_variance = sum(
            (weights[symbol] * marginal[symbol] for symbol in weights),
            Decimal("0"),
        )
        if total_variance <= 0 or any(marginal[symbol] <= 0 for symbol in weights):
            break
        risk_fractions = {
            symbol: weights[symbol] * marginal[symbol] / total_variance for symbol in weights
        }
        updated = {
            symbol: weights[symbol] * (budgets[symbol] / risk_fractions[symbol]).sqrt()
            for symbol in weights
        }
        updated = _normalize_positive(updated)
        difference = max(abs(updated[symbol] - weights[symbol]) for symbol in weights)
        weights = updated
        if difference < Decimal("1e-10"):
            break
    return weights


def _apply_weight_caps(
    weights: dict[str, Decimal],
    *,
    parameters: X1FeatureParameters,
) -> dict[str, Decimal]:
    capped = {
        symbol: min(weight, parameters.max_single_weight) for symbol, weight in weights.items()
    }
    for _ in range(10):
        changed = False
        for cluster_id, members in parameters.clusters.items():
            cluster_symbols = [symbol for symbol in members if symbol in capped]
            cluster_weight = sum(
                (capped[symbol] for symbol in cluster_symbols),
                Decimal("0"),
            )
            cap = parameters.cluster_caps[cluster_id]
            if cluster_weight > cap:
                scale = cap / cluster_weight
                for symbol in cluster_symbols:
                    capped[symbol] *= scale
                changed = True
        if not changed:
            break
    return capped


def _normalize_positive(weights: dict[str, Decimal]) -> dict[str, Decimal]:
    total = sum(weights.values(), Decimal("0"))
    if total <= 0:
        raise StatisticsError("positive weights cannot be normalized")
    return {symbol: weight / total for symbol, weight in weights.items()}


def _complete_weights(
    weights: dict[str, Decimal],
    *,
    required_symbols: tuple[str, ...],
) -> dict[str, Decimal]:
    return {symbol: weights.get(symbol, Decimal("0")) for symbol in required_symbols}
