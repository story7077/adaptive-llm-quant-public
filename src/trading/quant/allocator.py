from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext

from trading.domain.hashing import canonical_hash
from trading.quant.config import Q1MathConfig
from trading.quant.covariance import CovarianceEstimate, Q1MathError, portfolio_variance
from trading.quant.signals import Q1Signal


@dataclass(frozen=True, slots=True)
class AllocationResult:
    arm_kind: str
    target_weights: tuple[tuple[str, Decimal], ...]
    unconstrained_weights: tuple[tuple[str, Decimal], ...]
    volatility_scale: Decimal
    raw_portfolio_annualized_volatility: Decimal
    expected_annualized_volatility: Decimal
    soxx_variance_contribution: Decimal
    diagnostics: dict[str, object]
    allocation_hash: str

    def weight(self, symbol: str) -> Decimal:
        return _lookup_weight(self.target_weights, symbol)

    def weights_mapping(self) -> dict[str, Decimal]:
        return dict(self.target_weights)


@dataclass(frozen=True, slots=True)
class CurrentPortfolioWeights:
    nav_usd: Decimal
    settled_cash_usd: Decimal
    unsettled_receivables_usd: Decimal
    weights: tuple[tuple[str, Decimal], ...]

    def weight(self, symbol: str) -> Decimal:
        return _lookup_weight(self.weights, symbol)


@dataclass(frozen=True, slots=True)
class ProposedTrade:
    symbol: str
    side: str
    weight_delta: Decimal
    notional_usd: Decimal


@dataclass(frozen=True, slots=True)
class OmittedOrderDiagnostic:
    symbol: str
    side: str
    proposed_notional_usd: Decimal
    minimum_notional_usd: Decimal
    reason: str


@dataclass(frozen=True, slots=True)
class TurnoverResult:
    decision_kind: str
    proposed_one_way_turnover: Decimal
    remaining_daily_capacity: Decimal
    interpolation_alpha: Decimal
    adjusted_target_weights: tuple[tuple[str, Decimal], ...]
    executable_target_weights: tuple[tuple[str, Decimal], ...]
    executable_one_way_turnover: Decimal
    proposed_trades: tuple[ProposedTrade, ...]
    omitted_orders: tuple[OmittedOrderDiagnostic, ...]
    turnover_hash: str


def allocate_q1(
    signal: Q1Signal,
    *,
    config: Q1MathConfig,
) -> AllocationResult:
    """Allocate the Q1 signal without renormalizing away confidence cash."""

    covariance = signal.covariance
    qqq_score = signal.score_for("QQQ")
    soxx_score = signal.score_for("SOXX")
    with localcontext() as context:
        context.prec = config.covariance.decimal_precision
        if qqq_score == 0 and soxx_score == 0:
            raw_weights = {"QQQ": Decimal("0"), "SOXX": Decimal("0")}
        else:
            inverse_vol_scores = {
                "QQQ": qqq_score / covariance.variance("QQQ").sqrt(),
                "SOXX": soxx_score / covariance.variance("SOXX").sqrt(),
            }
            score_sum = sum(inverse_vol_scores.values(), Decimal("0"))
            if score_sum <= 0:
                raise Q1MathError("positive Q1 scores produced no inverse-volatility budget")
            raw_weights = {
                symbol: signal.confidence * inverse_vol_scores[symbol] / score_sum
                for symbol in config.risky_symbols
            }

        unconstrained = {
            "QQQ": raw_weights["QQQ"],
            "SOXX": raw_weights["SOXX"],
        }
        raw_variance = portfolio_variance(unconstrained, covariance)
        raw_volatility = raw_variance.sqrt()
        if raw_volatility > config.allocation.q1_target_vol:
            volatility_scale = config.allocation.q1_target_vol / raw_volatility
        else:
            volatility_scale = Decimal("1")
        constrained = {
            "QQQ": min(
                raw_weights["QQQ"] * volatility_scale,
                config.allocation.qqq_max_weight,
            ),
            "SOXX": min(
                raw_weights["SOXX"] * volatility_scale,
                config.allocation.soxx_max_weight,
            ),
        }
        gross = sum(constrained.values(), Decimal("0"))
        if gross > config.allocation.max_gross_risky_weight:
            gross_scale = config.allocation.max_gross_risky_weight / gross
            constrained = {
                symbol: weight * gross_scale
                for symbol, weight in constrained.items()
            }
        constrained["SOXX"] = _cap_soxx_risk_contribution(
            qqq_weight=constrained["QQQ"],
            soxx_weight=constrained["SOXX"],
            covariance=covariance,
            config=config,
        )
        final_variance = portfolio_variance(constrained, covariance)
        expected_volatility = final_variance.sqrt()
        soxx_contribution = _soxx_risk_contribution(
            qqq_weight=constrained["QQQ"],
            soxx_weight=constrained["SOXX"],
            covariance=covariance,
        )
        risky_sum = sum(constrained.values(), Decimal("0"))
        cash_weight = Decimal("1") - risky_sum
        final = {
            "QQQ": constrained["QQQ"],
            "SOXX": constrained["SOXX"],
            config.cash_symbol: cash_weight,
        }
        _validate_q1_weights(final, config)

        diagnostics: dict[str, object] = {
            "covariance_matrix": covariance.as_mapping(),
            "trend_z_scores": {
                trend.symbol: dict(trend.z_scores_by_horizon)
                for trend in signal.trends
            },
            "T_QQQ": signal.trend_for("QQQ").trend_score,
            "T_SOXX": signal.trend_for("SOXX").trend_score,
            "RS": signal.relative_strength,
            "spread_return_60": signal.spread_return,
            "spread_variance": signal.spread_variance,
            "market_gate": signal.market_gate,
            "raw_scores": dict(signal.raw_scores),
            "confidence": signal.confidence,
            "raw_weights": {
                **unconstrained,
                config.cash_symbol: Decimal("1") - sum(unconstrained.values(), Decimal("0")),
            },
            "constrained_weights": final,
            "volatility_scale": volatility_scale,
            "raw_portfolio_annualized_volatility": raw_volatility,
            "expected_annualized_volatility": expected_volatility,
            "soxx_risk_contribution": soxx_contribution,
            "final_cash_weight": cash_weight,
        }
        hash_payload = {
            "algorithm_version": config.algorithm_version,
            "config_manifest_hash": signal.config_manifest_hash,
            "signal_hash": signal.signal_hash,
            "arm_kind": "Q1",
            "diagnostics": diagnostics,
        }
        return AllocationResult(
            arm_kind="Q1",
            target_weights=tuple(
                (symbol, +final[symbol])
                for symbol in (*config.risky_symbols, config.cash_symbol)
            ),
            unconstrained_weights=tuple(
                (symbol, +(unconstrained.get(symbol, Decimal("1") - sum(
                    unconstrained.values(),
                    Decimal("0"),
                ))))
                for symbol in (*config.risky_symbols, config.cash_symbol)
            ),
            volatility_scale=+volatility_scale,
            raw_portfolio_annualized_volatility=+raw_volatility,
            expected_annualized_volatility=+expected_volatility,
            soxx_variance_contribution=+soxx_contribution,
            diagnostics=diagnostics,
            allocation_hash=canonical_hash(hash_payload),
        )


def allocate_b0_vol(
    qqq_annualized_variance: Decimal,
    *,
    config: Q1MathConfig,
    config_manifest_hash: str,
) -> AllocationResult:
    """Build B0-VOL from its independent QQQ-only variance estimate."""

    qqq_variance = _positive(
        qqq_annualized_variance,
        "qqq_annualized_variance",
    )
    if qqq_variance < config.covariance.variance_epsilon:
        raise Q1MathError(
            "qqq_annualized_variance must respect the configured variance floor"
        )
    qqq_sigma = qqq_variance.sqrt()
    qqq_weight = min(Decimal("1"), config.allocation.b0_vol_target / qqq_sigma)
    cash_weight = Decimal("1") - qqq_weight
    weights = (
        ("QQQ", qqq_weight),
        ("SOXX", Decimal("0")),
        (config.cash_symbol, cash_weight),
    )
    expected_volatility = qqq_weight * qqq_sigma
    diagnostics: dict[str, object] = {
        "qqq_annualized_variance": qqq_variance,
        "qqq_annualized_volatility": qqq_sigma,
        "target_annualized_volatility": config.allocation.b0_vol_target,
        "qqq_weight": qqq_weight,
        "cash_weight": cash_weight,
    }
    return AllocationResult(
        arm_kind="B0-VOL",
        target_weights=weights,
        unconstrained_weights=weights,
        volatility_scale=Decimal("1"),
        raw_portfolio_annualized_volatility=+expected_volatility,
        expected_annualized_volatility=+expected_volatility,
        soxx_variance_contribution=Decimal("0"),
        diagnostics=diagnostics,
        allocation_hash=canonical_hash(
            {
                "algorithm_version": config.algorithm_version,
                "config_manifest_hash": config_manifest_hash,
                "arm_kind": "B0-VOL",
                "diagnostics": diagnostics,
            }
        ),
    )


def compute_current_weights(
    *,
    positions: Mapping[str, Decimal],
    settled_cash_usd: Decimal,
    unsettled_receivables: Mapping[str, Decimal],
    midpoint_quotes: Mapping[str, Decimal],
    config: Q1MathConfig,
) -> CurrentPortfolioWeights:
    """Value a clean strategy arm with explicit settled and unsettled balances."""

    settled = _non_negative(settled_cash_usd, "settled_cash_usd")
    unsettled = sum(
        (
            _non_negative(value, f"unsettled_receivable[{key}]")
            for key, value in unsettled_receivables.items()
        ),
        Decimal("0"),
    )
    values: dict[str, Decimal] = {}
    for symbol in config.risky_symbols:
        quantity = _non_negative(positions.get(symbol, Decimal("0")), f"position[{symbol}]")
        if quantity == 0:
            values[symbol] = Decimal("0")
            continue
        try:
            midpoint = _positive(midpoint_quotes[symbol], f"midpoint_quote[{symbol}]")
        except KeyError as exc:
            raise Q1MathError(f"missing midpoint quote for held symbol {symbol}") from exc
        values[symbol] = quantity * midpoint
    unexpected = {
        symbol
        for symbol, quantity in positions.items()
        if quantity != 0 and symbol not in config.risky_symbols
    }
    if unexpected:
        raise Q1MathError(
            f"strategy arm contains symbols outside Q1 universe: {sorted(unexpected)}"
        )
    nav = settled + unsettled + sum(values.values(), Decimal("0"))
    if nav <= 0:
        raise Q1MathError("current NAV must be positive")
    weights = (
        *((symbol, values[symbol] / nav) for symbol in config.risky_symbols),
        (config.cash_symbol, (settled + unsettled) / nav),
    )
    return CurrentPortfolioWeights(
        nav_usd=nav,
        settled_cash_usd=settled,
        unsettled_receivables_usd=unsettled,
        weights=weights,
    )


def apply_turnover_control(
    *,
    current_weights: Mapping[str, Decimal],
    proposed_target_weights: Mapping[str, Decimal],
    current_nav_usd: Decimal,
    used_normal_turnover: Decimal,
    emergency_reduction: bool,
    config: Q1MathConfig,
    bypass_normal_turnover_cap: bool = False,
) -> TurnoverResult:
    """Apply one-way turnover and minimum-notional controls deterministically."""

    nav = _positive(current_nav_usd, "current_nav_usd")
    used = _non_negative(used_normal_turnover, "used_normal_turnover")
    symbols = (*config.risky_symbols, config.cash_symbol)
    current = _normalized_weight_mapping(current_weights, symbols, config)
    proposed = _normalized_weight_mapping(proposed_target_weights, symbols, config)
    if emergency_reduction and any(
        proposed[symbol] > current[symbol] for symbol in config.risky_symbols
    ):
        raise Q1MathError("emergency turnover control accepts sell-only risky targets")

    proposed_turnover = _one_way_turnover(current, proposed)
    remaining = max(
        Decimal("0"),
        config.turnover.normal_daily_one_way_cap - used,
    )
    if (
        not emergency_reduction
        and not bypass_normal_turnover_cap
        and proposed_turnover < config.turnover.no_trade_one_way_threshold
    ):
        return _turnover_result(
            decision_kind="NO_TRADE_BELOW_BAND",
            proposed_turnover=proposed_turnover,
            remaining=remaining,
            alpha=Decimal("0"),
            current=current,
            adjusted=current,
            executable=current,
            trades=(),
            omitted=(),
            config=config,
        )

    if emergency_reduction:
        if not config.turnover.emergency_bypasses_turnover_cap:
            raise Q1MathError("emergency turnover bypass is disabled")
        alpha = Decimal("1")
    elif bypass_normal_turnover_cap:
        alpha = Decimal("1") if proposed_turnover > 0 else Decimal("0")
    elif proposed_turnover == 0:
        alpha = Decimal("0")
    elif proposed_turnover > remaining:
        alpha = remaining / proposed_turnover
    else:
        alpha = Decimal("1")
    adjusted = {
        symbol: current[symbol] + alpha * (proposed[symbol] - current[symbol])
        for symbol in symbols
    }
    threshold = max(
        config.turnover.minimum_order_notional_usd,
        config.turnover.minimum_order_nav_fraction * nav,
    )
    executable = dict(current)
    trades: list[ProposedTrade] = []
    omitted: list[OmittedOrderDiagnostic] = []
    for symbol in config.risky_symbols:
        delta = adjusted[symbol] - current[symbol]
        if delta == 0:
            continue
        side = "BUY" if delta > 0 else "SELL"
        notional = abs(delta) * nav
        bypass_minimum = (
            emergency_reduction
            and config.turnover.emergency_bypasses_minimum_order
        )
        if not bypass_minimum and notional < threshold:
            omitted.append(
                OmittedOrderDiagnostic(
                    symbol=symbol,
                    side=side,
                    proposed_notional_usd=+notional,
                    minimum_notional_usd=+threshold,
                    reason="BELOW_MINIMUM_ORDER_NOTIONAL",
                )
            )
            continue
        executable[symbol] = adjusted[symbol]
        trades.append(
            ProposedTrade(
                symbol=symbol,
                side=side,
                weight_delta=+delta,
                notional_usd=+notional,
            )
        )
    executable[config.cash_symbol] = Decimal("1") - sum(
        (executable[symbol] for symbol in config.risky_symbols),
        Decimal("0"),
    )
    _validate_basic_weights(executable, config)
    decision_kind = (
        "EMERGENCY_REDUCTION"
        if emergency_reduction
        else "NORMAL_REBALANCE"
        if trades
        else "NO_EXECUTABLE_ORDERS"
    )
    return _turnover_result(
        decision_kind=decision_kind,
        proposed_turnover=proposed_turnover,
        remaining=remaining,
        alpha=alpha,
        current=current,
        adjusted=adjusted,
        executable=executable,
        trades=tuple(trades),
        omitted=tuple(omitted),
        config=config,
    )


def _cap_soxx_risk_contribution(
    *,
    qqq_weight: Decimal,
    soxx_weight: Decimal,
    covariance: CovarianceEstimate,
    config: Q1MathConfig,
) -> Decimal:
    if soxx_weight == 0:
        return Decimal("0")
    cap = config.allocation.soxx_max_variance_contribution
    current = _soxx_risk_contribution(
        qqq_weight=qqq_weight,
        soxx_weight=soxx_weight,
        covariance=covariance,
    )
    if current <= cap + config.allocation.risk_contribution_tolerance:
        return soxx_weight
    low = Decimal("0")
    high = soxx_weight
    for _ in range(config.allocation.risk_contribution_bisection_iterations):
        midpoint = (low + high) / Decimal("2")
        contribution = _soxx_risk_contribution(
            qqq_weight=qqq_weight,
            soxx_weight=midpoint,
            covariance=covariance,
        )
        if contribution <= cap:
            low = midpoint
        else:
            high = midpoint
    result = low
    final = _soxx_risk_contribution(
        qqq_weight=qqq_weight,
        soxx_weight=result,
        covariance=covariance,
    )
    if final > cap + config.allocation.risk_contribution_tolerance:
        raise Q1MathError("SOXX risk-contribution cap did not converge")
    return result


def _soxx_risk_contribution(
    *,
    qqq_weight: Decimal,
    soxx_weight: Decimal,
    covariance: CovarianceEstimate,
) -> Decimal:
    variance = portfolio_variance(
        {"QQQ": qqq_weight, "SOXX": soxx_weight},
        covariance,
    )
    if variance == 0 or soxx_weight == 0:
        return Decimal("0")
    sigma_w_soxx = (
        covariance.value("SOXX", "QQQ") * qqq_weight
        + covariance.value("SOXX", "SOXX") * soxx_weight
    )
    return soxx_weight * sigma_w_soxx / variance


def _turnover_result(
    *,
    decision_kind: str,
    proposed_turnover: Decimal,
    remaining: Decimal,
    alpha: Decimal,
    current: dict[str, Decimal],
    adjusted: dict[str, Decimal],
    executable: dict[str, Decimal],
    trades: tuple[ProposedTrade, ...],
    omitted: tuple[OmittedOrderDiagnostic, ...],
    config: Q1MathConfig,
) -> TurnoverResult:
    symbols = (*config.risky_symbols, config.cash_symbol)
    adjusted_tuple = tuple((symbol, +adjusted[symbol]) for symbol in symbols)
    executable_tuple = tuple((symbol, +executable[symbol]) for symbol in symbols)
    executable_turnover = _one_way_turnover(current, executable)
    payload = {
        "algorithm_version": config.algorithm_version,
        "decision_kind": decision_kind,
        "proposed_one_way_turnover": proposed_turnover,
        "remaining_daily_capacity": remaining,
        "interpolation_alpha": alpha,
        "adjusted_target_weights": adjusted_tuple,
        "executable_target_weights": executable_tuple,
        "proposed_trades": [
            {
                "symbol": item.symbol,
                "side": item.side,
                "weight_delta": item.weight_delta,
                "notional_usd": item.notional_usd,
            }
            for item in trades
        ],
        "omitted_orders": [
            {
                "symbol": item.symbol,
                "side": item.side,
                "proposed_notional_usd": item.proposed_notional_usd,
                "minimum_notional_usd": item.minimum_notional_usd,
                "reason": item.reason,
            }
            for item in omitted
        ],
    }
    return TurnoverResult(
        decision_kind=decision_kind,
        proposed_one_way_turnover=+proposed_turnover,
        remaining_daily_capacity=+remaining,
        interpolation_alpha=+alpha,
        adjusted_target_weights=adjusted_tuple,
        executable_target_weights=executable_tuple,
        executable_one_way_turnover=+executable_turnover,
        proposed_trades=trades,
        omitted_orders=omitted,
        turnover_hash=canonical_hash(payload),
    )


def _one_way_turnover(
    left: Mapping[str, Decimal],
    right: Mapping[str, Decimal],
) -> Decimal:
    return Decimal("0.5") * sum(
        (abs(right[symbol] - left[symbol]) for symbol in left),
        Decimal("0"),
    )


def _normalized_weight_mapping(
    values: Mapping[str, Decimal],
    symbols: tuple[str, ...],
    config: Q1MathConfig,
) -> dict[str, Decimal]:
    extras = {symbol for symbol, value in values.items() if symbol not in symbols and value != 0}
    if extras:
        raise Q1MathError(f"weights contain symbols outside Q1 universe: {sorted(extras)}")
    normalized = {
        symbol: _non_negative(values.get(symbol, Decimal("0")), f"weight[{symbol}]")
        for symbol in symbols
    }
    _validate_basic_weights(normalized, config)
    return normalized


def _validate_basic_weights(
    weights: Mapping[str, Decimal],
    config: Q1MathConfig,
) -> None:
    if any(value < 0 or not value.is_finite() for value in weights.values()):
        raise Q1MathError("portfolio weights must be non-negative and finite")
    total = sum(weights.values(), Decimal("0"))
    if abs(total - Decimal("1")) > config.allocation.weight_sum_tolerance:
        raise Q1MathError("portfolio weights must sum to one")


def _validate_q1_weights(weights: Mapping[str, Decimal], config: Q1MathConfig) -> None:
    _validate_basic_weights(weights, config)
    risky_sum = sum(
        (weights.get(symbol, Decimal("0")) for symbol in config.risky_symbols),
        Decimal("0"),
    )
    if risky_sum > config.allocation.max_gross_risky_weight:
        raise Q1MathError("portfolio exceeds gross risky-weight cap")
    if weights.get("QQQ", Decimal("0")) > config.allocation.qqq_max_weight:
        raise Q1MathError("portfolio exceeds QQQ cap")
    if weights.get("SOXX", Decimal("0")) > config.allocation.soxx_max_weight:
        raise Q1MathError("portfolio exceeds SOXX cap")


def _lookup_weight(items: tuple[tuple[str, Decimal], ...], symbol: str) -> Decimal:
    try:
        return next(value for item_symbol, value in items if item_symbol == symbol)
    except StopIteration as exc:
        raise Q1MathError(f"missing weight for {symbol}") from exc


def _positive(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value <= 0:
        raise Q1MathError(f"{name} must be a positive finite Decimal")
    return value


def _non_negative(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value < 0:
        raise Q1MathError(f"{name} must be a non-negative finite Decimal")
    return value
