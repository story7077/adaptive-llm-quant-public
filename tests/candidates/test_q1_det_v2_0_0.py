from __future__ import annotations

import copy
import inspect
import math
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from trading.domain.hashing import canonical_hash
from trading.research.candidate_abi import CandidateDecisionResponseV1
from trading.strategies.challengers.q1_det_v2_0_0.decision import decide

CUTOFF = "2026-01-09T15:00:00Z"
AVAILABLE = "2026-01-09T14:59:00Z"
EVENT_TIME = "2026-01-09T14:58:00Z"
MEMBERSHIP_START = "2020-01-02T00:00:00Z"
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64


def _feature(name: str, value: float) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "source_event_time": EVENT_TIME,
        "available_at": AVAILABLE,
        "source_revision": 0,
        "revision_available_at": AVAILABLE,
        "revision_was_known_at_cutoff": True,
        "source_hash": HASH_A,
    }


def _instrument(
    symbol: str,
    features: dict[str, float],
    *,
    current_weight: float = 0.0,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "current_weight": current_weight,
        "membership_available_at": MEMBERSHIP_START,
        "membership_valid_from": MEMBERSHIP_START,
        "membership_valid_until": None,
        "instrument_is_non_survivor": False,
        "features": [
            _feature(name, value) for name, value in sorted(features.items())
        ],
    }


def _request(
    *,
    sessions_since_review: int = 21,
    currents: dict[str, float] | None = None,
) -> dict[str, Any]:
    current = currents or {}
    instruments = [
        _instrument(
            "GLD",
            {
                "downside_beta_126_qqq": 0.10,
                "downside_observation_count_126": 30,
                "moving_average_gap_200": 0.10,
                "realized_volatility_63": 0.10,
                "total_return_126": 0.14,
                "total_return_63": 0.08,
            },
            current_weight=current.get("GLD", 0.0),
        ),
        _instrument(
            "QQQ",
            {
                "completed_sessions_since_review": sessions_since_review,
                "parent_score": 0.80,
                "parent_target_weight": 0.40,
            },
            current_weight=current.get("QQQ", 0.40),
        ),
        _instrument(
            "SGOV",
            {"total_return_126": 0.02, "total_return_63": 0.01},
            current_weight=current.get("SGOV", 0.0),
        ),
        _instrument(
            "SOXX",
            {"parent_score": 0.40, "parent_target_weight": 0.10},
            current_weight=current.get("SOXX", 0.10),
        ),
        _instrument(
            "TLT",
            {
                "downside_beta_126_qqq": -0.10,
                "downside_observation_count_126": 30,
                "moving_average_gap_200": 0.05,
                "realized_volatility_63": 0.20,
                "total_return_126": 0.10,
                "total_return_63": 0.06,
            },
            current_weight=current.get("TLT", 0.0),
        ),
    ]
    caps = {symbol: 0.70 for symbol in ("GLD", "QQQ", "SGOV", "SOXX", "TLT")}
    return {
        "schema_version": "candidate_decision_request_v1",
        "request_id": "decision-request-001",
        "challenger_id": "q1-det-v2.0.0-challenger",
        "candidate_artifact_hash": HASH_B,
        "strategy_id": "Q1-DET",
        "strategy_version": "2.0.0",
        "decision_time": CUTOFF,
        "signal_data_cutoff": CUTOFF,
        "variant": {
            "parameter_neighborhood_id": "BASE",
            "data_ablation_id": "BASE",
            "date_shift_id": "BASE",
            "inversion_id": "BASE",
            "shuffle_id": "BASE",
        },
        "instruments": instruments,
        "constraints": {
            "long_only": True,
            "leverage_permitted": False,
            "new_symbols_permitted": False,
            "maximum_gross_weight": 0.90,
            "minimum_cash_weight": 0.10,
            "maximum_weight_by_symbol": caps,
            "numeric_tolerance": 1e-12,
        },
        "strategy_parameters": {},
        "strategy_parameters_hash": HASH_C,
        "source_data_manifest_hash": HASH_D,
        "request_hash": HASH_E,
    }


def _target_map(response: dict[str, Any]) -> dict[str, float]:
    return {item["symbol"]: item["target_weight"] for item in response["targets"]}


def _instrument_for(request: dict[str, Any], symbol: str) -> dict[str, Any]:
    return next(item for item in request["instruments"] if item["symbol"] == symbol)


def _set_feature(
    request: dict[str, Any],
    symbol: str,
    name: str,
    value: float,
    **metadata: Any,
) -> None:
    instrument = _instrument_for(request, symbol)
    features = instrument["features"]
    existing = next((item for item in features if item["name"] == name), None)
    if existing is None:
        existing = _feature(name, value)
        features.append(existing)
    existing["value"] = value
    existing.update(metadata)
    features.sort(key=lambda item: item["name"])


def _all_keys(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def test_entrypoint_is_one_raw_object_callable_and_response_is_bound() -> None:
    request = _request()
    response = decide(request)

    assert tuple(inspect.signature(decide).parameters) == ("request",)
    assert response == decide(copy.deepcopy(request))
    assert response["request_id"] == request["request_id"]
    assert response["request_hash"] == request["request_hash"]
    assert response["challenger_id"] == request["challenger_id"]
    assert response["candidate_artifact_hash"] == request["candidate_artifact_hash"]
    assert [item["symbol"] for item in response["targets"]] == [
        "GLD",
        "QQQ",
        "SGOV",
        "SOXX",
        "TLT",
    ]
    unhashed = {key: value for key, value in response.items() if key != "output_hash"}
    assert response["output_hash"] == canonical_hash(unhashed)
    assert CandidateDecisionResponseV1.model_validate(response)
    forbidden = {
        "order",
        "orders",
        "fill",
        "fills",
        "realized_return",
        "expected_return",
        "pnl",
        "broker_action",
        "broker_actions",
    }
    assert forbidden.isdisjoint(_all_keys(response))


def test_inverse_volatility_caps_and_sgov_residual() -> None:
    targets = _target_map(decide(_request()))

    assert targets["QQQ"] == pytest.approx(0.40)
    assert targets["SOXX"] == pytest.approx(0.10)
    assert targets["GLD"] == pytest.approx(0.20)
    assert targets["TLT"] == pytest.approx(0.35 / 3.0)
    assert targets["SGOV"] == pytest.approx(0.35 - 0.20 - 0.35 / 3.0)
    assert sum(targets.values()) == pytest.approx(0.85)


def test_review_clock_holds_sleeve_until_twenty_one_completed_sessions() -> None:
    currents = {"GLD": 0.08, "SGOV": 0.12, "TLT": 0.04}
    before = decide(_request(sessions_since_review=20, currents=currents))
    due = decide(_request(sessions_since_review=21, currents=currents))

    before_targets = _target_map(before)
    assert before["diagnostics"]["review_due"] is False
    assert before_targets["GLD"] == pytest.approx(0.08)
    assert before_targets["SGOV"] == pytest.approx(0.12)
    assert before_targets["TLT"] == pytest.approx(0.04)
    assert due["diagnostics"]["review_due"] is True
    assert _target_map(due) != before_targets


def test_entry_exit_hysteresis_uses_asset_specific_beta_thresholds() -> None:
    request = _request(currents={"GLD": 0.10})
    _set_feature(request, "GLD", "total_return_63", 0.00)
    _set_feature(request, "GLD", "total_return_126", 0.01)
    _set_feature(request, "GLD", "moving_average_gap_200", -0.01)
    _set_feature(request, "GLD", "downside_beta_126_qqq", 0.30)
    _set_feature(request, "TLT", "downside_beta_126_qqq", 0.05)

    response = decide(request)
    targets = _target_map(response)

    assert response["diagnostics"]["eligible_diversifiers"] == ["GLD"]
    assert targets["GLD"] == pytest.approx(0.20)
    assert targets["TLT"] == 0.0
    assert targets["SGOV"] == pytest.approx(0.15)


def test_no_trade_band_suppresses_small_sleeve_changes() -> None:
    currents = {"GLD": 0.19, "SGOV": 0.04, "TLT": 0.11}
    response = decide(_request(currents=currents))
    targets = _target_map(response)

    assert targets["GLD"] == pytest.approx(0.19)
    assert targets["SGOV"] == pytest.approx(0.04)
    assert targets["TLT"] == pytest.approx(0.11)
    assert response["diagnostics"]["no_trade_band_applied"] == [
        "GLD",
        "SGOV",
        "TLT",
    ]


@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf")])
def test_missing_future_or_nonfinite_features_fail_closed(
    invalid_value: float,
) -> None:
    request = _request()
    gld = _instrument_for(request, "GLD")
    gld["features"] = [
        item for item in gld["features"] if item["name"] != "realized_volatility_63"
    ]
    _set_feature(request, "GLD", "unused_invalid_feature", invalid_value)
    _set_feature(
        request,
        "TLT",
        "total_return_63",
        0.06,
        revision_available_at="2026-01-09T15:01:00Z",
    )

    response = decide(request)
    targets = _target_map(response)

    assert response["diagnostics"]["input_feature_integrity"] is False
    assert response["diagnostics"]["eligible_diversifiers"] == []
    assert targets["GLD"] == 0.0
    assert targets["TLT"] == 0.0
    assert targets["SGOV"] == pytest.approx(0.35)


def test_invalid_sgov_inputs_leave_the_residual_as_cash() -> None:
    request = _request()
    sgov = _instrument_for(request, "SGOV")
    sgov["features"] = [
        item for item in sgov["features"] if item["name"] != "total_return_126"
    ]

    response = decide(request)
    targets = _target_map(response)

    assert response["diagnostics"]["reserve_available"] is False
    assert targets["GLD"] == 0.0
    assert targets["SGOV"] == 0.0
    assert targets["TLT"] == 0.0
    assert sum(targets.values()) == pytest.approx(0.50)


def test_adjacent_lookback_parameters_select_only_matching_pit_features() -> None:
    request = _request()
    request["strategy_parameters"] = {
        "short_return_sessions": 42,
        "long_return_sessions": 105,
        "moving_average_sessions": 180,
        "downside_beta_sessions": 105,
    }
    for symbol, short, long, volatility, gap, beta in (
        ("SGOV", 0.01, 0.02, None, None, None),
        ("GLD", 0.08, 0.14, 0.10, 0.10, 0.10),
        ("TLT", 0.06, 0.10, 0.20, 0.05, -0.10),
    ):
        _set_feature(request, symbol, "total_return_42", short)
        _set_feature(request, symbol, "total_return_105", long)
        if volatility is not None and gap is not None and beta is not None:
            _set_feature(request, symbol, "realized_volatility_42", volatility)
            _set_feature(request, symbol, "moving_average_gap_180", gap)
            _set_feature(request, symbol, "downside_beta_105_qqq", beta)
            _set_feature(request, symbol, "downside_observation_count_105", 30)

    response = decide(request)

    assert response["diagnostics"]["parameters_valid"] is True
    assert response["diagnostics"]["eligible_diversifiers"] == ["GLD", "TLT"]


def test_parent_targets_are_exact_when_sleeve_is_disabled() -> None:
    request = _request(sessions_since_review=20)
    request["strategy_parameters"] = {"sleeve_cap": 0.0}

    targets = _target_map(decide(request))

    assert targets["QQQ"] == 0.40
    assert targets["SOXX"] == 0.10
    assert targets["GLD"] == 0.0
    assert targets["SGOV"] == 0.0
    assert targets["TLT"] == 0.0


def test_randomized_host_caps_gross_cash_and_symbol_set_are_never_weakened() -> None:
    generator = random.Random(20260728)
    expected_symbols = {"GLD", "QQQ", "SGOV", "SOXX", "TLT"}
    for _ in range(100):
        request = _request()
        constraints = request["constraints"]
        constraints["maximum_gross_weight"] = generator.uniform(0.20, 1.0)
        constraints["minimum_cash_weight"] = generator.uniform(0.0, 0.60)
        constraints["maximum_weight_by_symbol"] = {
            symbol: generator.uniform(0.0, 0.75)
            for symbol in sorted(expected_symbols)
        }

        response = decide(request)
        targets = response["targets"]
        actual_symbols = {item["symbol"] for item in targets}
        gross = sum(item["target_weight"] for item in targets)
        limit = min(
            constraints["maximum_gross_weight"],
            1.0 - constraints["minimum_cash_weight"],
        )

        assert actual_symbols == expected_symbols
        assert [item["symbol"] for item in targets] == sorted(expected_symbols)
        assert gross <= limit + 1e-10
        for item in targets:
            weight = item["target_weight"]
            assert math.isfinite(weight)
            assert 0.0 <= weight <= constraints["maximum_weight_by_symbol"][
                item["symbol"]
            ] + 1e-12


def test_config_predeclares_host_owned_falsification_gates(
    repository_root: Path,
) -> None:
    config = yaml.safe_load(
        (
            repository_root
            / "config"
            / "strategies"
            / "challengers"
            / "q1-det-v2.0.0.yaml"
        ).read_text(encoding="utf-8")
    )
    falsification = config["falsification"]

    assert config["declared_entrypoint"] == (
        "trading.strategies.challengers.q1_det_v2_0_0.decision:decide"
    )
    assert falsification["minimum_net_portfolio_delta_sharpe_at_2x_costs"] == 0.05
    assert falsification["net_effect_at_3x_costs_must_be_positive"] is True
    assert falsification["maximum_parent_drawdown_increase"] == 0.02
    assert falsification["maximum_total_one_way_daily_turnover"] == 0.055
    assert falsification["minimum_capacity_usd"] == 250_000
    assert falsification["required_cost_multipliers"] == [1, 2, 3]
    assert falsification["host_owned_locked_oos_minimum_session_gate_required"] is True
