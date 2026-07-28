from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from trading.data.synthetic import (
    build_demo_scenario,
    build_feature_fixtures,
    build_forecast_fixtures,
    source_record_for_scenario,
)
from trading.domain.enums import ForecastStatus


def test_domain_contracts_round_trip() -> None:
    scenario = build_demo_scenario()
    source = source_record_for_scenario(scenario)
    features = build_feature_fixtures(scenario, source)
    forecasts = build_forecast_fixtures(scenario, features)

    source_type = type(source)
    forecast_type = type(forecasts[0])
    assert source_type.model_validate_json(source.model_dump_json()) == source
    assert forecast_type.model_validate_json(forecasts[0].model_dump_json()) == forecasts[0]


def test_naive_datetime_is_rejected() -> None:
    scenario = build_demo_scenario()
    source = source_record_for_scenario(scenario)
    payload = source.model_dump()
    payload["published_at"] = datetime(2026, 7, 20, 12, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        type(source).model_validate(payload)


def test_feature_future_cutoff_is_rejected() -> None:
    scenario = build_demo_scenario()
    source = source_record_for_scenario(scenario)
    feature = build_feature_fixtures(scenario, source)[0]
    payload = feature.model_dump()
    payload["data_available_cutoff"] = feature.decision_time + timedelta(seconds=1)
    with pytest.raises(ValidationError, match="must not exceed"):
        type(feature).model_validate(payload)


def test_forecast_arithmetic_mismatch_is_rejected() -> None:
    scenario = build_demo_scenario()
    source = source_record_for_scenario(scenario)
    feature = build_feature_fixtures(scenario, source)
    forecast = build_forecast_fixtures(scenario, feature)[0]
    payload = forecast.model_dump()
    payload["expected_net_return_bps"] = forecast.expected_net_return_bps + 1
    with pytest.raises(ValidationError, match="arithmetic mismatch"):
        type(forecast).model_validate(payload)


def test_no_signal_cannot_carry_risk_units() -> None:
    scenario = build_demo_scenario()
    source = source_record_for_scenario(scenario)
    feature = build_feature_fixtures(scenario, source)
    forecast = build_forecast_fixtures(scenario, feature)[0]
    payload = forecast.model_dump()
    payload["status"] = ForecastStatus.NO_SIGNAL
    payload["max_risk_units"] = 0.5
    with pytest.raises(ValidationError, match="max_risk_units=0"):
        type(forecast).model_validate(payload)


def test_timezone_normalizes_to_utc() -> None:
    scenario = build_demo_scenario()
    assert scenario.decision_time.tzinfo is UTC

