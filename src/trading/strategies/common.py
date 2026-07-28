from __future__ import annotations

import math
from collections.abc import Mapping
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot, StrategyForecast
from trading.domain.enums import ExposureKind, ForecastStatus, Horizon
from trading.domain.hashing import stable_id
from trading.features.statistics import StatisticsError, normalize_active_exposure
from trading.strategies.models import StrategyForecastContext


class StrategyInputError(ValueError):
    """Raised when a feature/calibration contract is unsafe for forecasting."""


def feature_values(snapshot: FeatureSnapshot) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for feature in snapshot.values:
        if feature.name in result:
            raise StrategyInputError(f"duplicate feature value: {feature.name}")
        result[feature.name] = Decimal(str(feature.value))
    return result


def require_feature_contract(
    snapshot: FeatureSnapshot,
    *,
    expected_feature_set_version: str,
    expected_feature_code_version: str,
) -> None:
    if snapshot.feature_set_version != expected_feature_set_version:
        raise StrategyInputError(
            f"expected feature set {expected_feature_set_version}, "
            f"received {snapshot.feature_set_version}"
        )
    if any(
        value.feature_code_version != expected_feature_code_version for value in snapshot.values
    ):
        raise StrategyInputError(f"feature code version must be {expected_feature_code_version}")


def require_feature(values: Mapping[str, Decimal], name: str) -> Decimal:
    try:
        return values[name]
    except KeyError as exc:
        raise StrategyInputError(f"missing required feature: {name}") from exc


def covariance_features(
    values: Mapping[str, Decimal],
    symbols: tuple[str, ...],
) -> dict[str, dict[str, Decimal]]:
    return {
        left: {right: require_feature(values, f"cov.{left}.{right}") for right in symbols}
        for left in symbols
    }


def normalized_float_exposure(
    exposure: Mapping[str, Decimal],
    covariance: Mapping[str, Mapping[str, Decimal]],
    *,
    target_volatility: Decimal = Decimal("0.01"),
) -> dict[str, float]:
    normalized = normalize_active_exposure(
        exposure,
        covariance,
        target_volatility=target_volatility,
    )
    risky = {symbol: float(weight) for symbol, weight in normalized.items() if symbol != "USD_CASH"}
    risky["USD_CASH"] = -sum(risky.values())
    return risky


def zero_float_exposure(symbols: tuple[str, ...]) -> dict[str, float]:
    return {symbol: 0.0 for symbol in (*symbols, "USD_CASH")}


def calibrated_forecast(
    *,
    snapshot: FeatureSnapshot,
    context: StrategyForecastContext,
    strategy_id: str,
    strategy_version: str,
    hypothesis_id: str,
    raw_signal_definition_version: str,
    horizon: Horizon,
    unit_exposure: dict[str, float],
    raw_signal: Decimal,
    signal_eligible: bool,
    max_risk_units: Decimal,
    risk_unit_horizon_vol: Decimal = Decimal("0.01"),
) -> StrategyForecast:
    calibration = context.calibration
    if calibration.strategy_id != strategy_id:
        raise StrategyInputError(f"{strategy_id} cannot use {calibration.strategy_id} calibration")
    if calibration.available_at > snapshot.data_available_cutoff:
        raise StrategyInputError("calibration was unavailable at feature cutoff")
    if calibration.trained_through >= snapshot.decision_time:
        raise StrategyInputError("calibration training cutoff must precede decision_time")
    if context.expires_at <= snapshot.decision_time:
        raise StrategyInputError("forecast expiry must follow decision_time")
    if context.created_at < snapshot.decision_time:
        raise StrategyInputError("forecast created_at cannot precede decision_time")

    gross_decimal = calibration.shrinkage * (
        calibration.intercept_bps + calibration.slope_bps_per_signal * raw_signal
    )
    cost_decimal = context.standalone_expected_cost_bps
    net_decimal = gross_decimal - cost_decimal
    edge_threshold = context.min_edge_to_cost_ratio * cost_decimal
    active = signal_eligible and net_decimal > edge_threshold

    gross = float(gross_decimal)
    cost = float(cost_decimal)
    net = gross - cost
    error = float(calibration.forecast_error_sd_bps)
    probability_positive = _normal_cdf(net / error)
    quantile_offset = 1.2815515655446004 * error
    status = ForecastStatus.ACTIVE if active else ForecastStatus.NO_SIGNAL
    forecast_id = stable_id(
        "fcst",
        strategy_id,
        strategy_version,
        context.experiment_version,
        snapshot.decision_time,
        horizon,
        snapshot.input_manifest_hash,
        calibration.calibration_version,
    )
    return StrategyForecast(
        forecast_id=forecast_id,
        hypothesis_id=hypothesis_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        experiment_version=context.experiment_version,
        decision_time=snapshot.decision_time,
        data_available_cutoff=snapshot.data_available_cutoff,
        horizon=horizon,
        expires_at=context.expires_at,
        reference_portfolio_id=context.reference_portfolio_id,
        exposure_kind=ExposureKind.ACTIVE_DELTA,
        unit_exposure=unit_exposure,
        risk_unit_horizon_vol=float(risk_unit_horizon_vol),
        raw_signal=float(raw_signal),
        raw_signal_definition_version=raw_signal_definition_version,
        expected_gross_return_bps=gross,
        standalone_expected_cost_bps=cost,
        expected_net_return_bps=net,
        forecast_error_sd_bps=error,
        probability_net_positive=probability_positive,
        quantile_10_bps=net - quantile_offset,
        quantile_50_bps=net,
        quantile_90_bps=net + quantile_offset,
        effective_sample_size=float(calibration.effective_sample_size),
        calibration_shrinkage=float(calibration.shrinkage),
        health_multiplier=float(context.health_multiplier),
        max_risk_units=float(max_risk_units) if active else 0.0,
        capacity_usd=context.capacity_usd,
        feature_snapshot_ids=[snapshot.feature_snapshot_id],
        calibration_version=calibration.calibration_version,
        code_commit=context.code_commit,
        status=status,
        created_at=context.created_at,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


__all__ = [
    "StatisticsError",
    "StrategyInputError",
    "calibrated_forecast",
    "covariance_features",
    "feature_values",
    "normalized_float_exposure",
    "require_feature",
    "require_feature_contract",
    "zero_float_exposure",
]
