from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from trading.domain.hashing import canonical_hash
from trading.quant import (
    AdjustedCloseObservation,
    Q1MathError,
    allocate_b0_vol,
    allocate_q1,
    apply_turnover_control,
    compute_current_weights,
    compute_q1_signal,
    ewma_annualized_variance,
    parse_q1_math_config,
)
from trading.quant.covariance import is_symmetric_positive_semidefinite


def _config(repository_root: Path):
    document = yaml.safe_load(
        (repository_root / "config" / "q1-math-core.yaml").read_text(encoding="utf-8")
    )
    return parse_q1_math_config(document), canonical_hash(document)


def _prices(factors: list[Decimal], *, start: Decimal = Decimal("100")) -> list[Decimal]:
    result = [start]
    for factor in factors:
        result.append(result[-1] * factor)
    return result


def _bars(
    qqq: list[Decimal],
    soxx: list[Decimal],
    *,
    prefix: str = "regime",
) -> tuple[list[AdjustedCloseObservation], tuple[str, ...], datetime, datetime]:
    assert len(qqq) == len(soxx)
    first_close = datetime(2025, 1, 2, 21, tzinfo=UTC)
    sessions = tuple(f"S{index:03d}" for index in range(len(qqq)))
    observations: list[AdjustedCloseObservation] = []
    for symbol, prices in (("QQQ", qqq), ("SOXX", soxx)):
        for index, (session_id, price) in enumerate(zip(sessions, prices, strict=True)):
            close_at = first_close + timedelta(days=index)
            observations.append(
                AdjustedCloseObservation(
                    bar_id=f"{prefix}-{symbol}-{session_id}",
                    symbol=symbol,
                    session_id=session_id,
                    session_close_at=close_at,
                    adjusted_close=price,
                    available_at=close_at + timedelta(minutes=5),
                )
            )
    current_open = first_close + timedelta(days=len(qqq), hours=13, minutes=30)
    scheduled = current_open + timedelta(minutes=30)
    return observations, sessions, current_open, scheduled


def _signal(
    repository_root: Path,
    qqq: list[Decimal],
    soxx: list[Decimal],
    *,
    prefix: str = "regime",
    extra: list[AdjustedCloseObservation] | None = None,
):
    config, manifest_hash = _config(repository_root)
    observations, sessions, current_open, scheduled = _bars(qqq, soxx, prefix=prefix)
    observations.extend(extra or [])
    return (
        compute_q1_signal(
            observations,
            completed_session_ids=sessions,
            calendar_session_id="CURRENT-SESSION",
            expected_latest_completed_session_id=sessions[-1],
            current_session_open_at=current_open,
            scheduled_at=scheduled,
            signal_data_cutoff=scheduled,
            config=config,
            config_manifest_hash=manifest_hash,
        ),
        config,
        manifest_hash,
        (observations, sessions, current_open, scheduled),
    )


def test_price_scale_invariance(repository_root: Path) -> None:
    qqq = _prices(
        [Decimal("1.008") if index % 3 else Decimal("0.994") for index in range(120)]
    )
    soxx = _prices(
        [Decimal("1.011") if index % 3 else Decimal("0.992") for index in range(120)]
    )
    base, config, _, _ = _signal(repository_root, qqq, soxx, prefix="scale")
    scaled, _, _, _ = _signal(
        repository_root,
        [price * Decimal("37") for price in qqq],
        [price * Decimal("37") for price in soxx],
        prefix="scale",
    )

    assert base.relative_strength == scaled.relative_strength
    assert base.market_gate == scaled.market_gate
    assert base.raw_scores == scaled.raw_scores
    assert base.confidence == scaled.confidence
    assert allocate_q1(base, config=config).target_weights == allocate_q1(
        scaled,
        config=config,
    ).target_weights


def test_future_available_bar_cannot_change_past_signal_hash(repository_root: Path) -> None:
    qqq = _prices([Decimal("1.003")] * 120)
    soxx = _prices([Decimal("1.004")] * 120)
    base, config, manifest_hash, inputs = _signal(
        repository_root,
        qqq,
        soxx,
        prefix="future",
    )
    observations, sessions, current_open, scheduled = inputs
    future_revision = AdjustedCloseObservation(
        bar_id="future-revision",
        symbol="SOXX",
        session_id=sessions[-1],
        session_close_at=next(
            item.session_close_at
            for item in observations
            if item.symbol == "SOXX" and item.session_id == sessions[-1]
        ),
        adjusted_close=Decimal("999999"),
        available_at=scheduled + timedelta(microseconds=1),
    )
    replay = compute_q1_signal(
        [*observations, future_revision],
        completed_session_ids=sessions,
        calendar_session_id="CURRENT-SESSION",
        expected_latest_completed_session_id=sessions[-1],
        current_session_open_at=current_open,
        scheduled_at=scheduled,
        signal_data_cutoff=scheduled,
        config=config,
        config_manifest_hash=manifest_hash,
    )

    assert replay.signal_hash == base.signal_hash


def test_covariance_is_symmetric_psd_and_uses_fixed_shrinkage(
    repository_root: Path,
) -> None:
    qqq = _prices(
        [Decimal("1.012") if index % 2 else Decimal("0.991") for index in range(120)]
    )
    soxx = _prices(
        [Decimal("1.016") if index % 2 else Decimal("0.987") for index in range(120)]
    )
    signal, config, _, _ = _signal(repository_root, qqq, soxx, prefix="psd")

    assert signal.covariance.value("QQQ", "SOXX") == signal.covariance.value(
        "SOXX",
        "QQQ",
    )
    assert is_symmetric_positive_semidefinite(
        signal.covariance,
        tolerance=config.covariance.psd_tolerance,
    )
    assert signal.covariance.variance("QQQ") >= config.covariance.variance_epsilon
    assert signal.covariance.variance("SOXX") >= config.covariance.variance_epsilon


def test_monotonic_trends_and_broad_market_gate(repository_root: Path) -> None:
    up, config, _, _ = _signal(
        repository_root,
        _prices([Decimal("1.003")] * 120),
        _prices([Decimal("1.004")] * 120),
        prefix="up",
    )
    down, _, _, _ = _signal(
        repository_root,
        _prices([Decimal("0.997")] * 120),
        _prices([Decimal("0.996")] * 120),
        prefix="down",
    )

    assert up.trend_for("QQQ").trend_score > 0
    assert up.trend_for("SOXX").trend_score > 0
    assert sum(
        allocate_q1(up, config=config).weight(symbol)
        for symbol in config.risky_symbols
    ) > 0
    down_allocation = allocate_q1(down, config=config)
    assert down.market_gate == 0
    assert down_allocation.weight("QQQ") == 0
    assert down_allocation.weight("SOXX") == 0
    assert down_allocation.weight("USD_CASH") == 1


def test_relative_strength_tilt_requires_broad_market_gate(repository_root: Path) -> None:
    qqq_factors = [
        Decimal("1.020") if index % 2 else Decimal("0.982")
        for index in range(120)
    ]
    equal, config, _, _ = _signal(
        repository_root,
        _prices(qqq_factors),
        _prices(qqq_factors),
        prefix="rs-equal",
    )
    stronger, _, _, _ = _signal(
        repository_root,
        _prices(qqq_factors),
        _prices(
                [
                    factor + Decimal("0.0002")
                    for factor in qqq_factors
                ]
        ),
        prefix="rs-strong",
    )
    risk_off, _, _, _ = _signal(
        repository_root,
        _prices([Decimal("0.997")] * 120),
        _prices([Decimal("0.998")] * 120),
        prefix="rs-gated",
    )

    assert stronger.relative_strength > equal.relative_strength
    assert allocate_q1(stronger, config=config).weight("SOXX") > allocate_q1(
        equal,
        config=config,
    ).weight("SOXX")
    assert risk_off.relative_strength > 0
    assert risk_off.market_gate == 0
    assert allocate_q1(risk_off, config=config).weight("SOXX") == 0


def test_allocation_preserves_confidence_and_all_constraints(repository_root: Path) -> None:
    signal, config, _, _ = _signal(
        repository_root,
        _prices(
            [Decimal("1.012") if index % 2 else Decimal("0.993") for index in range(120)]
        ),
        _prices(
            [Decimal("1.018") if index % 2 else Decimal("0.989") for index in range(120)]
        ),
        prefix="constraints",
    )
    allocation = allocate_q1(signal, config=config)
    weights = allocation.weights_mapping()

    assert all(weight >= 0 for weight in weights.values())
    assert abs(sum(weights.values(), Decimal("0")) - Decimal("1")) <= (
        config.allocation.weight_sum_tolerance
    )
    assert allocation.weight("QQQ") <= Decimal("0.80")
    assert allocation.weight("SOXX") <= Decimal("0.45")
    assert (
        allocation.soxx_variance_contribution
        <= config.allocation.soxx_max_variance_contribution
        + config.allocation.risk_contribution_tolerance
    )
    assert (
        allocation.expected_annualized_volatility
        <= config.allocation.q1_target_vol + config.allocation.risk_contribution_tolerance
    )
    assert allocation.weight("QQQ") + allocation.weight("SOXX") <= 1
    assert sum(
        weight for symbol, weight in allocation.unconstrained_weights if symbol != "USD_CASH"
    ) == signal.confidence


def test_b0_vol_is_qqq_only_and_does_not_require_soxx_history(
    repository_root: Path,
) -> None:
    config, manifest_hash = _config(repository_root)
    qqq_completed_returns = tuple(
        Decimal("0.0198026273")
        if index % 2
        else Decimal("-0.0151136378")
        for index in range(config.signal.minimum_completed_sessions - 1)
    )
    qqq_variance = ewma_annualized_variance(
        qqq_completed_returns,
        parameters=config.covariance,
    )
    allocation = allocate_b0_vol(
        qqq_variance,
        config=config,
        config_manifest_hash=manifest_hash,
    )

    expected = min(
        Decimal("1"),
        config.allocation.b0_vol_target / qqq_variance.sqrt(),
    )
    assert allocation.diagnostics["qqq_annualized_variance"] == qqq_variance
    assert allocation.weight("QQQ") == expected
    assert allocation.weight("SOXX") == 0
    assert allocation.weight("USD_CASH") == 1 - expected


def test_qqq_only_variance_fails_closed_for_missing_returns(
    repository_root: Path,
) -> None:
    config, _ = _config(repository_root)

    with pytest.raises(Q1MathError, match="non-empty"):
        ewma_annualized_variance((), parameters=config.covariance)


def test_current_weights_include_unsettled_cash_but_keep_it_non_spendable(
    repository_root: Path,
) -> None:
    config, _ = _config(repository_root)
    current = compute_current_weights(
        positions={"QQQ": Decimal("2"), "SOXX": Decimal("1")},
        settled_cash_usd=Decimal("100"),
        unsettled_receivables={"sale-a": Decimal("50")},
        midpoint_quotes={"QQQ": Decimal("100"), "SOXX": Decimal("50")},
        config=config,
    )

    assert current.nav_usd == 400
    assert current.settled_cash_usd == 100
    assert current.unsettled_receivables_usd == 50
    assert current.weight("USD_CASH") == Decimal("0.375")


def test_turnover_interpolates_and_never_exceeds_remaining_capacity(
    repository_root: Path,
) -> None:
    config, _ = _config(repository_root)
    result = apply_turnover_control(
        current_weights={
            "QQQ": Decimal("0.40"),
            "SOXX": Decimal("0.20"),
            "USD_CASH": Decimal("0.40"),
        },
        proposed_target_weights={
            "QQQ": Decimal("0.80"),
            "SOXX": Decimal("0.20"),
            "USD_CASH": Decimal("0.00"),
        },
        current_nav_usd=Decimal("10000"),
        used_normal_turnover=Decimal("0.10"),
        emergency_reduction=False,
        config=config,
    )

    assert result.proposed_one_way_turnover == Decimal("0.40")
    assert result.remaining_daily_capacity == Decimal("0.10")
    assert result.interpolation_alpha == Decimal("0.25")
    assert result.executable_one_way_turnover <= result.remaining_daily_capacity


def test_below_band_has_no_trades_and_emergency_sells_bypass_cap(
    repository_root: Path,
) -> None:
    config, _ = _config(repository_root)
    current = {
        "QQQ": Decimal("0.50"),
        "SOXX": Decimal("0.20"),
        "USD_CASH": Decimal("0.30"),
    }
    below_band = apply_turnover_control(
        current_weights=current,
        proposed_target_weights={
            "QQQ": Decimal("0.51"),
            "SOXX": Decimal("0.20"),
            "USD_CASH": Decimal("0.29"),
        },
        current_nav_usd=Decimal("10000"),
        used_normal_turnover=Decimal("0"),
        emergency_reduction=False,
        config=config,
    )
    emergency = apply_turnover_control(
        current_weights=current,
        proposed_target_weights={
            "QQQ": Decimal("0.10"),
            "SOXX": Decimal("0.00"),
            "USD_CASH": Decimal("0.90"),
        },
        current_nav_usd=Decimal("10000"),
        used_normal_turnover=Decimal("0.20"),
        emergency_reduction=True,
        config=config,
    )

    assert below_band.decision_kind == "NO_TRADE_BELOW_BAND"
    assert below_band.proposed_trades == ()
    assert emergency.interpolation_alpha == 1
    assert emergency.executable_one_way_turnover == Decimal("0.60")
    assert all(trade.side == "SELL" for trade in emergency.proposed_trades)


def test_b0_qqq_bypasses_alpha_turnover_cap(
    repository_root: Path,
) -> None:
    config, _ = _config(repository_root)
    result = apply_turnover_control(
        current_weights={
            "QQQ": Decimal("0"),
            "SOXX": Decimal("0"),
            "USD_CASH": Decimal("1"),
        },
        proposed_target_weights={
            "QQQ": Decimal("1"),
            "SOXX": Decimal("0"),
            "USD_CASH": Decimal("0"),
        },
        current_nav_usd=Decimal("10000"),
        used_normal_turnover=Decimal("0"),
        emergency_reduction=False,
        config=config,
        bypass_normal_turnover_cap=True,
    )

    assert result.interpolation_alpha == Decimal("1")
    assert dict(result.executable_target_weights) == {
        "QQQ": Decimal("1"),
        "SOXX": Decimal("0"),
        "USD_CASH": Decimal("0"),
    }


def test_small_orders_are_omitted_without_redistribution(repository_root: Path) -> None:
    config, _ = _config(repository_root)
    result = apply_turnover_control(
        current_weights={
            "QQQ": Decimal("0.40"),
            "SOXX": Decimal("0.20"),
            "USD_CASH": Decimal("0.40"),
        },
        proposed_target_weights={
            "QQQ": Decimal("0.43"),
            "SOXX": Decimal("0.199"),
            "USD_CASH": Decimal("0.371"),
        },
        current_nav_usd=Decimal("10000"),
        used_normal_turnover=Decimal("0"),
        emergency_reduction=False,
        config=config,
    )

    assert {trade.symbol for trade in result.proposed_trades} == {"QQQ"}
    assert len(result.omitted_orders) == 1
    assert result.omitted_orders[0].symbol == "SOXX"
    assert dict(result.executable_target_weights)["SOXX"] == Decimal("0.20")
    assert dict(result.executable_target_weights)["USD_CASH"] == Decimal("0.37")


def test_inconsistent_duplicate_and_current_session_bar_fail_closed(
    repository_root: Path,
) -> None:
    config, manifest_hash = _config(repository_root)
    observations, sessions, current_open, scheduled = _bars(
        _prices([Decimal("1.003")] * 120),
        _prices([Decimal("1.004")] * 120),
        prefix="invalid",
    )
    source = next(
        item
        for item in observations
        if item.symbol == "QQQ" and item.session_id == sessions[-1]
    )
    inconsistent = source.__class__(
        bar_id="inconsistent",
        symbol=source.symbol,
        session_id=source.session_id,
        session_close_at=source.session_close_at,
        adjusted_close=source.adjusted_close + Decimal("1"),
        available_at=source.available_at,
    )
    with pytest.raises(Q1MathError, match="inconsistent duplicate"):
        compute_q1_signal(
            [*observations, inconsistent],
            completed_session_ids=sessions,
            calendar_session_id="CURRENT-SESSION",
            expected_latest_completed_session_id=sessions[-1],
            current_session_open_at=current_open,
            scheduled_at=scheduled,
            signal_data_cutoff=scheduled,
            config=config,
            config_manifest_hash=manifest_hash,
        )

    partial = source.__class__(
        bar_id="partial",
        symbol="QQQ",
        session_id=sessions[-1],
        session_close_at=current_open + timedelta(hours=6, minutes=30),
        adjusted_close=source.adjusted_close,
        available_at=scheduled,
    )
    with pytest.raises(Q1MathError, match="current-session"):
        compute_q1_signal(
            [item for item in observations if item is not source] + [partial],
            completed_session_ids=sessions,
            calendar_session_id="CURRENT-SESSION",
            expected_latest_completed_session_id=sessions[-1],
            current_session_open_at=current_open,
            scheduled_at=scheduled,
            signal_data_cutoff=scheduled,
            config=config,
            config_manifest_hash=manifest_hash,
        )

    with pytest.raises(Q1MathError, match="stale"):
        compute_q1_signal(
            observations,
            completed_session_ids=sessions,
            calendar_session_id="CURRENT-SESSION",
            expected_latest_completed_session_id="MISSING-LATEST-SESSION",
            current_session_open_at=current_open,
            scheduled_at=scheduled,
            signal_data_cutoff=scheduled,
            config=config,
            config_manifest_hash=manifest_hash,
        )


def test_synthetic_regime_golden_outputs(repository_root: Path) -> None:
    regimes = {
        "uptrend": (
            [Decimal("1.003")] * 120,
            [Decimal("1.004")] * 120,
        ),
        "tilt": (
            [
                Decimal("1.020") if index % 2 else Decimal("0.982")
                for index in range(120)
            ],
            [
                Decimal("1.0202") if index % 2 else Decimal("0.9822")
                for index in range(120)
            ],
        ),
        "downtrend": (
            [Decimal("0.997")] * 120,
            [Decimal("0.996")] * 120,
        ),
        "volatility_spike": (
            [
                Decimal("1.060") if index % 2 else Decimal("0.950")
                for index in range(120)
            ],
            [
                Decimal("1.080") if index % 2 else Decimal("0.935")
                for index in range(120)
            ],
        ),
    }
    expected = json.loads(
        (repository_root / "tests" / "fixtures" / "q1_math_regimes.json").read_text(
            encoding="utf-8"
        )
    )
    actual: dict[str, dict[str, str]] = {}
    gross: dict[str, Decimal] = {}
    for name, (qqq_factors, soxx_factors) in regimes.items():
        signal, config, _, _ = _signal(
            repository_root,
            _prices(qqq_factors),
            _prices(soxx_factors),
            prefix=f"golden-{name}",
        )
        allocation = allocate_q1(signal, config=config)
        actual[name] = {
            "signal_hash": signal.signal_hash,
            "allocation_hash": allocation.allocation_hash,
            "T_QQQ": str(signal.trend_for("QQQ").trend_score),
            "T_SOXX": str(signal.trend_for("SOXX").trend_score),
            "RS": str(signal.relative_strength),
            "market_gate": str(signal.market_gate),
            "confidence": str(signal.confidence),
            "QQQ": str(allocation.weight("QQQ")),
            "SOXX": str(allocation.weight("SOXX")),
            "USD_CASH": str(allocation.weight("USD_CASH")),
            "vol": str(allocation.expected_annualized_volatility),
            "rc_soxx": str(allocation.soxx_variance_contribution),
        }
        gross[name] = allocation.weight("QQQ") + allocation.weight("SOXX")

    assert actual == expected
    assert gross["uptrend"] > 0
    assert Decimal(actual["tilt"]["SOXX"]) > Decimal(actual["tilt"]["QQQ"])
    assert gross["downtrend"] == 0
    assert gross["volatility_spike"] < gross["uptrend"]
