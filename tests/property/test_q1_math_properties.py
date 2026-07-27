from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from trading.domain.hashing import canonical_hash
from trading.quant import (
    AdjustedCloseObservation,
    allocate_b0_vol,
    allocate_q1,
    compute_q1_signal,
    ewma_annualized_variance,
    ewma_covariance,
    parse_q1_math_config,
)
from trading.quant.covariance import is_symmetric_positive_semidefinite


def _load(repository_root: Path):
    document = yaml.safe_load(
        (repository_root / "config" / "q1-math-core.yaml").read_text(encoding="utf-8")
    )
    return parse_q1_math_config(document), canonical_hash(document)


def _path(basis_points: list[int], scale: Decimal) -> list[Decimal]:
    prices = [Decimal("100") * scale]
    for move in basis_points:
        prices.append(
            prices[-1] * (Decimal("1") + Decimal(move) / Decimal("10000"))
        )
    return prices


def _compute(
    repository_root: Path,
    qqq_moves: list[int],
    soxx_moves: list[int],
    scale: Decimal,
):
    config, manifest_hash = _load(repository_root)
    first_close = datetime(2025, 1, 2, 21, tzinfo=UTC)
    sessions = tuple(f"P{index:03d}" for index in range(121))
    observations: list[AdjustedCloseObservation] = []
    for symbol, prices in (
        ("QQQ", _path(qqq_moves, scale)),
        ("SOXX", _path(soxx_moves, scale)),
    ):
        for index, (session_id, price) in enumerate(zip(sessions, prices, strict=True)):
            close_at = first_close + timedelta(days=index)
            observations.append(
                AdjustedCloseObservation(
                    bar_id=f"property-{symbol}-{session_id}",
                    symbol=symbol,
                    session_id=session_id,
                    session_close_at=close_at,
                    adjusted_close=price,
                    available_at=close_at + timedelta(minutes=1),
                )
            )
    current_open = first_close + timedelta(days=121, hours=13, minutes=30)
    scheduled = current_open + timedelta(minutes=30)
    signal = compute_q1_signal(
        observations,
        completed_session_ids=sessions,
        calendar_session_id="PROPERTY-CURRENT-SESSION",
        expected_latest_completed_session_id=sessions[-1],
        current_session_open_at=current_open,
        scheduled_at=scheduled,
        signal_data_cutoff=scheduled,
        config=config,
        config_manifest_hash=manifest_hash,
    )
    return signal, allocate_q1(signal, config=config), config


@settings(max_examples=30, deadline=None)
@given(
    qqq_moves=st.lists(
        st.integers(min_value=-300, max_value=300),
        min_size=120,
        max_size=120,
    ),
    soxx_moves=st.lists(
        st.integers(min_value=-400, max_value=400),
        min_size=120,
        max_size=120,
    ),
    scale=st.integers(min_value=1, max_value=100),
)
def test_q1_covariance_and_allocation_invariants(
    repository_root: Path,
    qqq_moves: list[int],
    soxx_moves: list[int],
    scale: int,
) -> None:
    signal, allocation, config = _compute(
        repository_root,
        qqq_moves,
        soxx_moves,
        Decimal(scale),
    )
    weights = allocation.weights_mapping()

    assert is_symmetric_positive_semidefinite(
        signal.covariance,
        tolerance=config.covariance.psd_tolerance,
    )
    assert all(value >= 0 for value in weights.values())
    assert abs(sum(weights.values(), Decimal("0")) - Decimal("1")) <= (
        config.allocation.weight_sum_tolerance
    )
    assert weights["QQQ"] <= config.allocation.qqq_max_weight
    assert weights["SOXX"] <= config.allocation.soxx_max_weight
    assert weights["QQQ"] + weights["SOXX"] <= config.allocation.max_gross_risky_weight
    assert (
        allocation.expected_annualized_volatility
        <= config.allocation.q1_target_vol + config.allocation.risk_contribution_tolerance
    )
    assert (
        allocation.soxx_variance_contribution
        <= config.allocation.soxx_max_variance_contribution
        + config.allocation.risk_contribution_tolerance
    )


@settings(max_examples=20, deadline=None)
@given(
    qqq_moves=st.lists(
        st.integers(min_value=-200, max_value=300),
        min_size=120,
        max_size=120,
    ),
    soxx_moves=st.lists(
        st.integers(min_value=-300, max_value=400),
        min_size=120,
        max_size=120,
    ),
    scale=st.integers(min_value=2, max_value=50),
)
def test_q1_property_price_scale_invariance(
    repository_root: Path,
    qqq_moves: list[int],
    soxx_moves: list[int],
    scale: int,
) -> None:
    base_signal, base_allocation, _ = _compute(
        repository_root,
        qqq_moves,
        soxx_moves,
        Decimal("1"),
    )
    scaled_signal, scaled_allocation, _ = _compute(
        repository_root,
        qqq_moves,
        soxx_moves,
        Decimal(scale),
    )

    assert base_signal.raw_scores == scaled_signal.raw_scores
    assert base_signal.confidence == scaled_signal.confidence
    assert base_allocation.target_weights == scaled_allocation.target_weights


@settings(max_examples=20, deadline=None)
@given(
    qqq_moves=st.lists(
        st.integers(min_value=-400, max_value=400),
        min_size=120,
        max_size=120,
    ),
    first_soxx_moves=st.lists(
        st.integers(min_value=-800, max_value=800),
        min_size=120,
        max_size=120,
    ),
    second_soxx_moves=st.lists(
        st.integers(min_value=-800, max_value=800),
        min_size=120,
        max_size=120,
    ),
)
def test_b0_vol_qqq_variance_is_independent_of_soxx_history(
    repository_root: Path,
    qqq_moves: list[int],
    first_soxx_moves: list[int],
    second_soxx_moves: list[int],
) -> None:
    config, manifest_hash = _load(repository_root)
    divisor = Decimal("100000")
    qqq_returns = tuple(Decimal(value) / divisor for value in qqq_moves)
    first_soxx_returns = tuple(
        Decimal(value) / divisor for value in first_soxx_moves
    )
    second_soxx_returns = tuple(
        Decimal(value) / divisor for value in second_soxx_moves
    )

    qqq_only_variance = ewma_annualized_variance(
        qqq_returns,
        parameters=config.covariance,
    )
    first_joint = ewma_covariance(
        {"QQQ": qqq_returns, "SOXX": first_soxx_returns},
        parameters=config.covariance,
    )
    second_joint = ewma_covariance(
        {"QQQ": qqq_returns, "SOXX": second_soxx_returns},
        parameters=config.covariance,
    )
    allocation = allocate_b0_vol(
        qqq_only_variance,
        config=config,
        config_manifest_hash=manifest_hash,
    )

    assert qqq_only_variance == first_joint.variance("QQQ")
    assert qqq_only_variance == second_joint.variance("QQQ")
    assert allocation.weight("SOXX") == 0
    assert allocation.weight("QQQ") == min(
        Decimal("1"),
        config.allocation.b0_vol_target / qqq_only_variance.sqrt(),
    )
