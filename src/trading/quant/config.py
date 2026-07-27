from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast


class Q1ConfigError(ValueError):
    """Raised when the versioned Q1 mathematical configuration is invalid."""


@dataclass(frozen=True, slots=True)
class CovarianceParameters:
    half_life_sessions: int
    lambda_base: Decimal
    annualization_sessions: int
    full_weight: Decimal
    diagonal_weight: Decimal
    initialization: str
    variance_epsilon: Decimal
    psd_tolerance: Decimal
    decimal_precision: int


@dataclass(frozen=True, slots=True)
class SignalParameters:
    minimum_completed_sessions: int
    horizons_sessions: tuple[int, ...]
    relative_strength_horizon_sessions: int
    z_score_clip: Decimal
    relative_strength_coefficient: Decimal
    market_gate_offset: Decimal
    market_gate_width: Decimal
    market_gate_min: Decimal
    market_gate_max: Decimal
    confidence_denominator: Decimal
    confidence_min: Decimal
    confidence_max: Decimal
    return_quantum: Decimal
    numeric_rounding: str


@dataclass(frozen=True, slots=True)
class AllocationParameters:
    b0_vol_target: Decimal
    q1_target_vol: Decimal
    max_gross_risky_weight: Decimal
    qqq_max_weight: Decimal
    soxx_max_weight: Decimal
    soxx_max_variance_contribution: Decimal
    allow_volatility_scale_up: bool
    risk_contribution_bisection_iterations: int
    risk_contribution_tolerance: Decimal
    weight_sum_tolerance: Decimal


@dataclass(frozen=True, slots=True)
class TurnoverParameters:
    normal_daily_one_way_cap: Decimal
    no_trade_one_way_threshold: Decimal
    minimum_order_notional_usd: Decimal
    minimum_order_nav_fraction: Decimal
    emergency_bypasses_turnover_cap: bool
    emergency_bypasses_minimum_order: bool


@dataclass(frozen=True, slots=True)
class EvaluationParameters:
    minimum_common_out_of_sample_sessions: int
    decimal_precision: int
    result_quantum: Decimal
    risk_free_daily_return: Decimal
    downside_target_daily_return: Decimal
    newey_west_lag_sessions: int
    stationary_bootstrap_expected_block_sessions: int
    stationary_bootstrap_samples: int
    bootstrap_confidence: Decimal
    deterministic_bootstrap_seed: int
    annualization_sessions: int


@dataclass(frozen=True, slots=True)
class Q1MathConfig:
    version: str
    algorithm_version: str
    signal_model_version: str
    risky_symbols: tuple[str, str]
    cash_symbol: str
    disabled_symbols: tuple[str, ...]
    inherited_sell_only_symbols: tuple[str, ...]
    covariance: CovarianceParameters
    signal: SignalParameters
    allocation: AllocationParameters
    turnover: TurnoverParameters
    evaluation: EvaluationParameters


def parse_q1_math_config(document: dict[str, Any]) -> Q1MathConfig:
    """Parse the math-owned portion of the versioned YAML document.

    This function deliberately accepts an already-loaded document. Configuration
    I/O and manifest hashing remain runtime responsibilities.
    """

    if document.get("version") != "q1_math_core_v1":
        raise Q1ConfigError("Q1 config version must be q1_math_core_v1")
    if document.get("algorithm_version") != "q1_math_core_v1":
        raise Q1ConfigError("Q1 algorithm_version must be q1_math_core_v1")
    if document.get("real_order_routing") is not False:
        raise Q1ConfigError("Q1 real_order_routing must be false")

    universe = _section(document, "active_universe")
    strategy_symbols = _symbols(universe, "strategy_symbols")
    risky_symbols = _symbols(universe, "risky_symbols")
    cash_symbol = _string(universe, "cash_symbol")
    disabled_symbols = _symbols(universe, "disabled_symbols")
    inherited_sell_only_symbols = _symbols(
        universe,
        "inherited_sell_only_symbols",
    )
    if strategy_symbols != ("QQQ", "SOXX", "USD_CASH"):
        raise Q1ConfigError("Q1 strategy universe must be exactly QQQ, SOXX, USD_CASH")
    if risky_symbols != ("QQQ", "SOXX"):
        raise Q1ConfigError("Q1 risky universe must be exactly QQQ, SOXX")
    if cash_symbol != "USD_CASH":
        raise Q1ConfigError("Q1 cash symbol must be USD_CASH")
    if disabled_symbols != ("SOXS",):
        raise Q1ConfigError("SOXS must be the explicitly disabled Q1 symbol")
    if set(inherited_sell_only_symbols) & set(strategy_symbols):
        raise Q1ConfigError("inherited sell-only symbols cannot enter strategy arms")
    if "SOXL" not in inherited_sell_only_symbols:
        raise Q1ConfigError("SOXL must remain inherited sell-only")

    signal = _section(document, "signal")
    allocation = _section(document, "allocation")
    turnover = _section(document, "turnover")
    evaluation = _section(document, "evaluation")
    if not _boolean(allocation, "long_only"):
        raise Q1ConfigError("Q1 allocation must remain long-only")
    if _boolean(allocation, "allow_short_selling"):
        raise Q1ConfigError("Q1 short selling must remain disabled")
    if _boolean(allocation, "allow_leverage"):
        raise Q1ConfigError("Q1 leverage must remain disabled")
    horizons = tuple(_positive_int(item, "signal.horizons_sessions") for item in _list(
        signal,
        "horizons_sessions",
    ))
    signal_parameters = SignalParameters(
        minimum_completed_sessions=_positive_int(
            signal.get("minimum_completed_sessions"),
            "signal.minimum_completed_sessions",
        ),
        horizons_sessions=horizons,
        relative_strength_horizon_sessions=_positive_int(
            signal.get("relative_strength_horizon_sessions"),
            "signal.relative_strength_horizon_sessions",
        ),
        z_score_clip=_positive_decimal(signal, "z_score_clip"),
        relative_strength_coefficient=_non_negative_decimal(
            signal,
            "relative_strength_coefficient",
        ),
        market_gate_offset=_decimal(signal, "market_gate_offset"),
        market_gate_width=_positive_decimal(signal, "market_gate_width"),
        market_gate_min=_decimal(signal, "market_gate_min"),
        market_gate_max=_decimal(signal, "market_gate_max"),
        confidence_denominator=_positive_decimal(signal, "confidence_denominator"),
        confidence_min=_decimal(signal, "confidence_min"),
        confidence_max=_decimal(signal, "confidence_max"),
        return_quantum=_positive_decimal(signal, "return_quantum"),
        numeric_rounding=_string(signal, "numeric_rounding"),
    )
    if horizons != (20, 60, 120):
        raise Q1ConfigError("Q1 signal horizons must be exactly 20, 60, 120")
    if signal_parameters.relative_strength_horizon_sessions not in horizons:
        raise Q1ConfigError("relative-strength horizon must be one of the trend horizons")
    if signal_parameters.minimum_completed_sessions <= max(horizons):
        raise Q1ConfigError("minimum completed sessions must exceed the longest horizon")
    if signal_parameters.market_gate_min >= signal_parameters.market_gate_max:
        raise Q1ConfigError("market-gate bounds are invalid")
    if signal_parameters.confidence_min >= signal_parameters.confidence_max:
        raise Q1ConfigError("confidence bounds are invalid")
    if signal_parameters.numeric_rounding != "HALF_EVEN":
        raise Q1ConfigError("q1_math_core_v1 numeric rounding must be HALF_EVEN")

    covariance = CovarianceParameters(
        half_life_sessions=_positive_int(
            signal.get("covariance_half_life_sessions"),
            "signal.covariance_half_life_sessions",
        ),
        lambda_base=_unit_interval_decimal(signal, "covariance_lambda_base", strict=True),
        annualization_sessions=_positive_int(
            signal.get("annualization_sessions"),
            "signal.annualization_sessions",
        ),
        full_weight=_unit_interval_decimal(signal, "covariance_full_weight"),
        diagonal_weight=_unit_interval_decimal(signal, "covariance_diagonal_weight"),
        initialization=_string(signal, "covariance_initialization"),
        variance_epsilon=_positive_decimal(signal, "variance_epsilon"),
        psd_tolerance=_non_negative_decimal(signal, "psd_tolerance"),
        decimal_precision=_positive_int(
            signal.get("decimal_precision"),
            "signal.decimal_precision",
        ),
    )
    if covariance.full_weight + covariance.diagonal_weight != Decimal("1"):
        raise Q1ConfigError("covariance weights must sum to one")
    if covariance.initialization != "ZERO":
        raise Q1ConfigError("q1_math_core_v1 covariance initialization must be ZERO")

    allocation_parameters = AllocationParameters(
        b0_vol_target=_positive_decimal(
            allocation,
            "b0_vol_target_annualized_volatility",
        ),
        q1_target_vol=_positive_decimal(
            allocation,
            "q1_target_annualized_volatility",
        ),
        max_gross_risky_weight=_unit_interval_decimal(
            allocation,
            "max_gross_risky_weight",
        ),
        qqq_max_weight=_unit_interval_decimal(allocation, "qqq_max_weight"),
        soxx_max_weight=_unit_interval_decimal(allocation, "soxx_max_weight"),
        soxx_max_variance_contribution=_unit_interval_decimal(
            allocation,
            "soxx_max_variance_contribution",
        ),
        allow_volatility_scale_up=_boolean(allocation, "allow_volatility_scale_up"),
        risk_contribution_bisection_iterations=_positive_int(
            allocation.get("risk_contribution_bisection_iterations"),
            "allocation.risk_contribution_bisection_iterations",
        ),
        risk_contribution_tolerance=_positive_decimal(
            allocation,
            "risk_contribution_tolerance",
        ),
        weight_sum_tolerance=_positive_decimal(allocation, "weight_sum_tolerance"),
    )
    if allocation_parameters.allow_volatility_scale_up:
        raise Q1ConfigError("q1_math_core_v1 may not scale exposure upward for volatility")

    turnover_parameters = TurnoverParameters(
        normal_daily_one_way_cap=_unit_interval_decimal(
            turnover,
            "normal_daily_one_way_cap",
            strict=True,
        ),
        no_trade_one_way_threshold=_unit_interval_decimal(
            turnover,
            "no_trade_one_way_threshold",
            strict=True,
        ),
        minimum_order_notional_usd=_positive_decimal(
            turnover,
            "minimum_order_notional_usd",
        ),
        minimum_order_nav_fraction=_unit_interval_decimal(
            turnover,
            "minimum_order_nav_fraction",
            strict=True,
        ),
        emergency_bypasses_turnover_cap=_boolean(
            turnover,
            "emergency_bypasses_turnover_cap",
        ),
        emergency_bypasses_minimum_order=_boolean(
            turnover,
            "emergency_bypasses_minimum_order",
        ),
    )
    if (
        turnover_parameters.no_trade_one_way_threshold
        >= turnover_parameters.normal_daily_one_way_cap
    ):
        raise Q1ConfigError("no-trade threshold must be below the daily turnover cap")

    evaluation_parameters = EvaluationParameters(
        minimum_common_out_of_sample_sessions=_positive_int(
            evaluation.get("minimum_common_out_of_sample_sessions"),
            "evaluation.minimum_common_out_of_sample_sessions",
        ),
        decimal_precision=_positive_int(
            evaluation.get("decimal_precision"),
            "evaluation.decimal_precision",
        ),
        result_quantum=_positive_decimal(
            evaluation,
            "result_quantum",
        ),
        risk_free_daily_return=_decimal(evaluation, "risk_free_daily_return"),
        downside_target_daily_return=_decimal(
            evaluation,
            "downside_target_daily_return",
        ),
        newey_west_lag_sessions=_positive_int(
            evaluation.get("newey_west_lag_sessions"),
            "evaluation.newey_west_lag_sessions",
        ),
        stationary_bootstrap_expected_block_sessions=_positive_int(
            evaluation.get("stationary_bootstrap_expected_block_sessions"),
            "evaluation.stationary_bootstrap_expected_block_sessions",
        ),
        stationary_bootstrap_samples=_positive_int(
            evaluation.get("stationary_bootstrap_samples"),
            "evaluation.stationary_bootstrap_samples",
        ),
        bootstrap_confidence=_unit_interval_decimal(
            evaluation,
            "bootstrap_confidence",
            strict=True,
        ),
        deterministic_bootstrap_seed=_non_negative_int(
            evaluation.get("deterministic_bootstrap_seed"),
            "evaluation.deterministic_bootstrap_seed",
        ),
        annualization_sessions=_positive_int(
            evaluation.get("annualization_sessions"),
            "evaluation.annualization_sessions",
        ),
    )
    if evaluation_parameters.minimum_common_out_of_sample_sessions < 126:
        raise Q1ConfigError(
            "promotion review requires at least 126 common out-of-sample sessions"
        )
    if (
        evaluation_parameters.annualization_sessions
        != covariance.annualization_sessions
    ):
        raise Q1ConfigError("signal and evaluation annualization sessions must match")

    return Q1MathConfig(
        version=str(document["version"]),
        algorithm_version=str(document["algorithm_version"]),
        signal_model_version=_string(document, "signal_model_version"),
        risky_symbols=(risky_symbols[0], risky_symbols[1]),
        cash_symbol=cash_symbol,
        disabled_symbols=disabled_symbols,
        inherited_sell_only_symbols=inherited_sell_only_symbols,
        covariance=covariance,
        signal=signal_parameters,
        allocation=allocation_parameters,
        turnover=turnover_parameters,
        evaluation=evaluation_parameters,
    )


def _section(document: dict[str, Any], name: str) -> dict[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise Q1ConfigError(f"{name} must be an object")
    return cast(dict[str, Any], value)


def _list(document: dict[str, Any], name: str) -> list[Any]:
    value = document.get(name)
    if not isinstance(value, list):
        raise Q1ConfigError(f"{name} must be a list")
    return cast(list[Any], value)


def _symbols(document: dict[str, Any], name: str) -> tuple[str, ...]:
    values = _list(document, name)
    symbols = tuple(str(item).strip().upper() for item in values)
    if not symbols or any(not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
        raise Q1ConfigError(f"{name} must contain unique non-empty symbols")
    return symbols


def _string(document: dict[str, Any], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value.strip():
        raise Q1ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _boolean(document: dict[str, Any], name: str) -> bool:
    value = document.get(name)
    if not isinstance(value, bool):
        raise Q1ConfigError(f"{name} must be a boolean")
    return value


def _decimal(document: dict[str, Any], name: str) -> Decimal:
    value = document.get(name)
    if isinstance(value, bool) or value is None:
        raise Q1ConfigError(f"{name} must be numeric")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise Q1ConfigError(f"{name} must be numeric") from exc
    if not parsed.is_finite():
        raise Q1ConfigError(f"{name} must be finite")
    return parsed


def _positive_decimal(document: dict[str, Any], name: str) -> Decimal:
    value = _decimal(document, name)
    if value <= 0:
        raise Q1ConfigError(f"{name} must be positive")
    return value


def _non_negative_decimal(document: dict[str, Any], name: str) -> Decimal:
    value = _decimal(document, name)
    if value < 0:
        raise Q1ConfigError(f"{name} must be non-negative")
    return value


def _unit_interval_decimal(
    document: dict[str, Any],
    name: str,
    *,
    strict: bool = False,
) -> Decimal:
    value = _decimal(document, name)
    lower_valid = value > 0 if strict else value >= 0
    upper_valid = value < 1 if strict else value <= 1
    if not lower_valid or not upper_valid:
        qualifier = "strictly inside" if strict else "inside"
        raise Q1ConfigError(f"{name} must be {qualifier} [0, 1]")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise Q1ConfigError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Q1ConfigError(f"{name} must be a non-negative integer")
    return value
