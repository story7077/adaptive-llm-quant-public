from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from trading.evaluation.matched import EvaluationConfig
from trading.execution.q1_paper import Q1ExecutionConfig
from trading.risk.state_machine import RiskEngineConfig
from trading.runtime.q1_planning import OrderPlanningConfig
from trading.settings import Q1ConfigBundle
from trading.settlement.service import SettlementPolicy


@dataclass(frozen=True, slots=True)
class Q1OperationalConfig:
    calendar_sync_interval_hours: int
    calendar_history_lookback_days: int
    calendar_forward_days: int


@dataclass(frozen=True, slots=True)
class Q1LlmTransportConfig:
    provider_timeout_seconds: Decimal
    transport_timeout_seconds: Decimal
    transport_poll_interval_seconds: Decimal

    def __post_init__(self) -> None:
        if self.provider_timeout_seconds <= 0:
            raise ValueError("Q1 LLM provider timeout must be positive")
        if self.transport_timeout_seconds <= 0:
            raise ValueError("Q1 LLM transport timeout must be positive")
        if self.transport_poll_interval_seconds <= 0:
            raise ValueError("Q1 LLM transport poll interval must be positive")
        if self.transport_timeout_seconds >= self.provider_timeout_seconds:
            raise ValueError(
                "Q1 LLM transport timeout must be shorter than the outer "
                "provider timeout"
            )
        if self.transport_poll_interval_seconds > self.transport_timeout_seconds:
            raise ValueError(
                "Q1 LLM transport poll interval must not exceed its timeout"
            )


def operational_config(bundle: Q1ConfigBundle) -> Q1OperationalConfig:
    operations = _section(bundle.document, "operations")
    signal = _section(bundle.document, "signal")
    settlement = _section(bundle.document, "settlement")
    result = Q1OperationalConfig(
        calendar_sync_interval_hours=_positive_integer(
            operations,
            "calendar_sync_interval_hours",
        ),
        calendar_history_lookback_days=_positive_integer(
            operations,
            "calendar_history_lookback_days",
        ),
        calendar_forward_days=_positive_integer(
            operations,
            "calendar_forward_days",
        ),
    )
    minimum_completed_sessions = _positive_integer(
        signal,
        "minimum_completed_sessions",
    )
    if result.calendar_history_lookback_days < minimum_completed_sessions:
        raise ValueError(
            "Q1 calendar history lookback must cover at least the minimum "
            "completed signal sessions"
        )
    settlement_lag = _integer(
        settlement,
        "settlement_lag_business_sessions",
    )
    if result.calendar_forward_days < settlement_lag:
        raise ValueError(
            "Q1 calendar forward coverage must cover the settlement lag"
        )
    return result


def execution_config(bundle: Q1ConfigBundle) -> Q1ExecutionConfig:
    document = _section(bundle.document, "execution")
    commission = _section(bundle.cost_document, "commission")
    configured_rate = _decimal(document, "commission_rate")
    configured_waiver = _decimal(
        document,
        "commission_waiver_notional_usd",
    )
    cost_rate = _decimal(commission, "us_equity_rate")
    cost_waiver = _decimal(commission, "waive_if_order_total_usd_lte")
    if configured_rate != cost_rate or configured_waiver != cost_waiver:
        raise ValueError("Q1 execution commission must match costs.yaml")
    sensitivity = tuple(
        Decimal(str(item))
        for item in _sequence(document, "evaluation_sensitivity_bps")
    )
    return Q1ExecutionConfig(
        displayed_participation=_decimal(
            document,
            "displayed_side_participation_fraction",
        ),
        adv_participation=_decimal(
            document,
            "maximum_20d_iex_adv_fraction",
        ),
        delay_penalty_bps=_decimal(document, "delay_penalty_bps"),
        guard_min_bps=_decimal(document, "price_guard_minimum_bps"),
        guard_max_bps=_decimal(document, "price_guard_maximum_bps"),
        guard_spread_multiplier=_decimal(
            document,
            "price_guard_spread_multiplier",
        ),
        quantity_precision=_decimal(document, "quantity_increment"),
        price_precision=_decimal(document, "price_increment_usd"),
        commission_rate=cost_rate,
        commission_waiver_threshold_usd=cost_waiver,
        commission_precision=_decimal(
            document,
            "commission_increment_usd",
        ),
        sensitivity_bps=sensitivity,
    )


def order_planning_config(bundle: Q1ConfigBundle) -> OrderPlanningConfig:
    execution = execution_config(bundle)
    return OrderPlanningConfig(
        quantity_increment=execution.quantity_precision,
        commission_rate=execution.commission_rate,
        commission_waiver_notional_usd=(
            execution.commission_waiver_threshold_usd
        ),
        delay_penalty_bps=execution.delay_penalty_bps,
        commission_precision=execution.commission_precision,
    )


def risk_engine_config(bundle: Q1ConfigBundle) -> RiskEngineConfig:
    risk = _section(bundle.document, "risk")
    signal = _section(bundle.document, "signal")
    execution = _section(bundle.document, "execution")
    return RiskEngineConfig(
        version=str(bundle.document["version"]),
        annualization_sessions=_decimal(
            signal,
            "annualization_sessions",
        ),
        soft_sigma_multiple=_decimal(
            risk,
            "dynamic_soft_daily_sigma_multiple",
        ),
        hard_sigma_multiple=_decimal(
            risk,
            "dynamic_hard_daily_sigma_multiple",
        ),
        soft_daily_floor=_decimal(risk, "soft_daily_min"),
        soft_daily_ceiling=_decimal(risk, "soft_daily_max"),
        hard_daily_floor=_decimal(risk, "hard_daily_min"),
        hard_daily_ceiling=_decimal(risk, "hard_daily_max"),
        soft_drawdown_threshold=_decimal(
            risk,
            "soft_stop_run_drawdown",
        ),
        hard_drawdown_threshold=_decimal(
            risk,
            "hard_reduce_run_drawdown",
        ),
        critical_drawdown_threshold=_decimal(
            risk,
            "critical_exit_run_drawdown",
        ),
        q1_hard_gross_cap=_decimal(
            risk,
            "hard_reduce_max_risky_gross",
        ),
        q1_hard_soxx_weight_cap=_decimal(
            risk,
            "hard_reduce_max_soxx_weight",
        ),
        live_mirror_semiconductor_weight_cap=_decimal(
            risk,
            "live_mirror_max_semiconductor_weight",
        ),
        release_daily_loss_soft_fraction=_decimal(
            risk,
            "release_daily_loss_soft_threshold_fraction",
        ),
        release_drawdown_threshold=_decimal(
            risk,
            "release_max_run_drawdown",
        ),
        release_consecutive_valid_checks=_integer(
            risk,
            "release_required_consecutive_checks",
        ),
        quantity_precision=_decimal(execution, "quantity_increment"),
        leveraged_symbols=frozenset(
            _strings(risk, "leveraged_symbols")
        ),
        semiconductor_symbols=frozenset(
            _strings(risk, "semiconductor_symbols")
        ),
    )


def settlement_policy(bundle: Q1ConfigBundle) -> SettlementPolicy:
    document = _section(bundle.document, "settlement")
    return SettlementPolicy(
        version=str(document["policy_version"]),
        calendar_version=str(document["business_calendar_version"]),
        lag_business_sessions=_integer(
            document,
            "settlement_lag_business_sessions",
        ),
    )


def evaluation_config(bundle: Q1ConfigBundle) -> EvaluationConfig:
    document = _section(bundle.document, "evaluation")
    return EvaluationConfig(
        version=str(bundle.document["version"]),
        decimal_precision=_integer(document, "decimal_precision"),
        result_quantum=_decimal(document, "result_quantum"),
        annualization_sessions=_integer(
            document,
            "annualization_sessions",
        ),
        risk_free_daily_return=_decimal(
            document,
            "risk_free_daily_return",
        ),
        downside_target_daily_return=_decimal(
            document,
            "downside_target_daily_return",
        ),
        newey_west_lag=_integer(
            document,
            "newey_west_lag_sessions",
        ),
        bootstrap_samples=_integer(
            document,
            "stationary_bootstrap_samples",
        ),
        stationary_block_mean_length=_decimal(
            document,
            "stationary_bootstrap_expected_block_sessions",
        ),
        bootstrap_confidence=_decimal(
            document,
            "bootstrap_confidence",
        ),
        bootstrap_seed=_integer(
            document,
            "deterministic_bootstrap_seed",
        ),
        promotion_min_common_sessions=_integer(
            document,
            "minimum_common_out_of_sample_sessions",
        ),
    )


def evaluation_closing_quote_max_age_seconds(
    bundle: Q1ConfigBundle,
) -> int:
    return _positive_integer(
        _section(bundle.document, "evaluation"),
        "closing_quote_max_age_seconds",
    )


def displayed_size_unit_shares(bundle: Q1ConfigBundle) -> Decimal:
    return _decimal(
        _section(bundle.document, "execution"),
        "displayed_size_unit_shares",
    )


def maximum_quote_age_seconds(bundle: Q1ConfigBundle) -> int:
    return _integer(
        _section(bundle.document, "execution"),
        "maximum_quote_age_seconds",
    )


def maximum_quote_skew_seconds(bundle: Q1ConfigBundle) -> int:
    return _integer(
        _section(bundle.document, "execution"),
        "maximum_multi_symbol_quote_skew_seconds",
    )


def adv_lookback_sessions(bundle: Q1ConfigBundle) -> int:
    return _integer(
        _section(bundle.document, "execution"),
        "adv_lookback_completed_sessions",
    )


def critical_reconciliation_conditions(
    bundle: Q1ConfigBundle,
) -> frozenset[str]:
    return frozenset(
        _strings(
            _section(bundle.document, "risk"),
            "critical_reconciliation_conditions",
        )
    )


def maximum_llm_evidence_events(bundle: Q1ConfigBundle) -> int:
    return _positive_integer(
        _section(bundle.document, "llm"),
        "maximum_evidence_events",
    )


def llm_provider_timeout_seconds(bundle: Q1ConfigBundle) -> Decimal:
    return llm_transport_config(bundle).provider_timeout_seconds


def llm_transport_config(bundle: Q1ConfigBundle) -> Q1LlmTransportConfig:
    document = _section(bundle.document, "llm")
    return Q1LlmTransportConfig(
        provider_timeout_seconds=_decimal(
            document,
            "provider_timeout_seconds",
        ),
        transport_timeout_seconds=_decimal(
            document,
            "transport_timeout_seconds",
        ),
        transport_poll_interval_seconds=_decimal(
            document,
            "transport_poll_interval_seconds",
        ),
    )


def _section(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Q1 config {key!r} must be an object")
    return cast(dict[str, Any], value)


def _sequence(document: dict[str, Any], key: str) -> tuple[object, ...]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"Q1 config {key!r} must be a list")
    return tuple(cast(list[object], value))


def _strings(document: dict[str, Any], key: str) -> tuple[str, ...]:
    values = tuple(str(value).strip().upper() for value in _sequence(document, key))
    if not values or any(not value for value in values):
        raise ValueError(f"Q1 config {key!r} must contain strings")
    return values


def _decimal(document: dict[str, Any], key: str) -> Decimal:
    value = Decimal(str(document[key]))
    if not value.is_finite():
        raise ValueError(f"Q1 config {key!r} must be finite")
    return value


def _integer(document: dict[str, Any], key: str) -> int:
    value = document[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Q1 config {key!r} must be an integer")
    return value


def _positive_integer(document: dict[str, Any], key: str) -> int:
    value = _integer(document, key)
    if value <= 0:
        raise ValueError(f"Q1 config {key!r} must be positive")
    return value
