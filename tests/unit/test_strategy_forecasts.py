from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.domain.contracts import FeatureSnapshot, FeatureValue
from trading.domain.enums import ForecastStatus, Horizon
from trading.strategies import (
    ForecastCalibration,
    R1Strategy,
    StrategyForecastContext,
    T1Strategy,
    X1Strategy,
)
from trading.strategies.common import StrategyInputError
from trading.strategies.x1 import X1_ASSETS

DECISION = datetime(2026, 7, 27, 19, 45, tzinfo=UTC)
CUTOFF = DECISION - timedelta(minutes=1)


def test_t1_forecast_is_calibrated_normalized_and_deterministic() -> None:
    snapshot = _snapshot(
        strategy_id="T1",
        symbol=None,
        values={
            "raw_signal": Decimal("1.2"),
            "beta_60": Decimal("1.1"),
            "cov.SOXX.SOXX": Decimal("0.0004"),
            "cov.SOXX.QQQ": Decimal("0.0002"),
            "cov.QQQ.SOXX": Decimal("0.0002"),
            "cov.QQQ.QQQ": Decimal("0.0003"),
        },
    )
    context = _forecast_context("T1", Horizon.H5D)

    forecast = T1Strategy().forecast(snapshot, context=context)
    repeated = T1Strategy().forecast(snapshot, context=context)

    assert forecast == repeated
    assert forecast.status is ForecastStatus.ACTIVE
    assert abs(sum(forecast.unit_exposure.values())) < 1e-12
    assert forecast.expected_net_return_bps == (
        forecast.expected_gross_return_bps - forecast.standalone_expected_cost_bps
    )
    assert forecast.feature_snapshot_ids == [snapshot.feature_snapshot_id]


def test_signal_and_cost_gates_produce_a_zero_risk_no_signal() -> None:
    snapshot = _snapshot(
        strategy_id="T1",
        symbol=None,
        values={
            "raw_signal": Decimal("0.5"),
            "beta_60": Decimal("1"),
            "cov.SOXX.SOXX": Decimal("0.0004"),
            "cov.SOXX.QQQ": Decimal("0.0001"),
            "cov.QQQ.SOXX": Decimal("0.0001"),
            "cov.QQQ.QQQ": Decimal("0.0003"),
        },
    )
    forecast = T1Strategy().forecast(snapshot, context=_forecast_context("T1", Horizon.H5D))
    assert forecast.status is ForecastStatus.NO_SIGNAL
    assert forecast.max_risk_units == 0


def test_future_or_in_sample_calibration_is_rejected() -> None:
    snapshot = _snapshot(
        strategy_id="R1",
        symbol="SMH",
        values={
            "raw_signal": Decimal("2.5"),
            "horizon_vol": Decimal("0.02"),
            "eligible": Decimal("1"),
        },
    )
    context = _forecast_context("R1", Horizon.H4)
    future_calibration = context.calibration.model_copy(update={"available_at": DECISION})

    with pytest.raises(StrategyInputError, match="unavailable"):
        R1Strategy().forecast(
            snapshot,
            context=context.model_copy(update={"calibration": future_calibration}),
        )

    in_sample = context.calibration.model_copy(update={"trained_through": DECISION})
    with pytest.raises(StrategyInputError, match="precede"):
        R1Strategy().forecast(
            snapshot,
            context=context.model_copy(update={"calibration": in_sample}),
        )


def test_r1_and_x1_emit_common_forecast_contracts() -> None:
    r1_snapshot = _snapshot(
        strategy_id="R1",
        symbol="SMH",
        values={
            "raw_signal": Decimal("2.5"),
            "horizon_vol": Decimal("0.02"),
            "eligible": Decimal("1"),
        },
    )
    r1 = R1Strategy().forecast(
        r1_snapshot,
        context=_forecast_context("R1", Horizon.H4),
    )
    assert r1.horizon is Horizon.H4
    assert r1.unit_exposure == {"SMH": 0.5, "USD_CASH": -0.5}

    x1_values: dict[str, Decimal] = {"raw_signal": Decimal("1.1")}
    for symbol in X1_ASSETS:
        x1_values[f"active_delta.{symbol}"] = Decimal("0.10") if symbol == "GLD" else Decimal("0")
        for other in X1_ASSETS:
            x1_values[f"cov.{symbol}.{other}"] = (
                Decimal("0.0004") if symbol == other else Decimal("0")
            )
    x1_values["active_delta.USD_CASH"] = Decimal("-0.10")
    x1_snapshot = _snapshot(strategy_id="X1", symbol=None, values=x1_values)
    x1 = X1Strategy().forecast(
        x1_snapshot,
        context=_forecast_context("X1", Horizon.H5D),
    )
    assert x1.horizon is Horizon.H5D
    assert x1.status is ForecastStatus.ACTIVE
    assert abs(sum(x1.unit_exposure.values())) < 1e-12


def _snapshot(
    *,
    strategy_id: str,
    symbol: str | None,
    values: dict[str, Decimal],
) -> FeatureSnapshot:
    return FeatureSnapshot(
        feature_snapshot_id=f"feature-{strategy_id}-{symbol}",
        symbol=symbol,
        decision_time=DECISION,
        data_available_cutoff=CUTOFF,
        feature_set_version=f"{strategy_id.lower()}_features_v1",
        values=[
            FeatureValue(
                name=name,
                value=float(value),
                unit="test",
                source_record_ids=["source-1"],
                feature_code_version=f"{strategy_id.lower()}_features_v1",
            )
            for name, value in values.items()
        ],
        input_manifest_hash=f"manifest-{strategy_id}-{symbol}",
        created_at=DECISION,
    )


def _forecast_context(
    strategy_id: str,
    horizon: Horizon,
) -> StrategyForecastContext:
    expiry = DECISION + (timedelta(hours=4) if horizon is Horizon.H4 else timedelta(days=7))
    return StrategyForecastContext(
        experiment_version="exp-b1-paper-v1",
        reference_portfolio_id="B0_VOL_V1",
        expires_at=expiry,
        created_at=DECISION,
        calibration=ForecastCalibration(
            strategy_id=strategy_id,
            calibration_version=f"cal-{strategy_id}-v1",
            trained_through=DECISION - timedelta(days=2),
            available_at=CUTOFF - timedelta(seconds=1),
            intercept_bps=Decimal("0"),
            slope_bps_per_signal=Decimal("20"),
            forecast_error_sd_bps=Decimal("50"),
            effective_sample_size=Decimal("120"),
            shrinkage=Decimal("0.8"),
        ),
        standalone_expected_cost_bps=Decimal("2"),
        capacity_usd=Decimal("1000000"),
        health_multiplier=Decimal("1"),
        code_commit="test-commit",
    )
