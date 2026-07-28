from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from trading.domain.contracts import Fill
from trading.experiments.ai_guard_factorial import FACTORIAL_ARM_IDS
from trading.experiments.ai_guard_factorial_runtime import (
    FactorialPaperArm,
    apply_factorial_fill,
    factorial_target_weights,
    initialize_factorial_paper_arms,
    plan_factorial_rebalance,
)
from trading.persistence.factorial import (
    FactorialCheckpointKind,
    FactorialPaperExperimentRepository,
    FactorialPersistenceError,
)
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    LedgerTransactionRow,
    NavSnapshotRow,
    OrderIntentRow,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
CONFIG_HASH = "c" * 64


def _arms() -> dict[str, FactorialPaperArm]:
    return initialize_factorial_paper_arms(
        starting_capital_usd=Decimal("100000"),
        effective_at=NOW,
        common_market_manifest_hash="a" * 64,
        forecast_hash="b" * 64,
        policy_version="operational-risk-v1",
        decision_schedule_version="research-daily-v1",
        execution_scenario_version="matched-paper-v1",
        cost_model_version="paper-cost-v1",
        config_manifest_hash=CONFIG_HASH,
    )


def _planned() -> dict[str, FactorialPaperArm]:
    arms = _arms()
    targets = factorial_target_weights(
        base_weights={
            "QQQ": Decimal("0.60"),
            "USD_CASH": Decimal("0.40"),
        },
        guard_risk_multiplier=Decimal("0.50"),
        ai_risk_multiplier=Decimal("0.75"),
    )
    return plan_factorial_rebalance(
        arms=arms,
        targets=targets,
        prices={"QQQ": Decimal("500")},
        created_at=NOW + timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=20),
        decision_scope="factorial-cycle-1",
        minimum_notional_usd=Decimal("25"),
    )


def _filled(
    planned: dict[str, FactorialPaperArm],
) -> dict[str, FactorialPaperArm]:
    filled: dict[str, FactorialPaperArm] = {}
    for arm_id, arm in planned.items():
        order = arm.pending_orders[0].intent
        fill = Fill(
            fill_id=f"fill-{arm_id.lower()}",
            order_intent_id=order.order_intent_id,
            arm_id=arm_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            price=Decimal("500"),
            commission_usd=Decimal("0"),
            execution_scenario_id="matched-paper-v1",
            effective_at=NOW + timedelta(minutes=2),
            created_at=NOW + timedelta(minutes=2),
        )
        filled[arm_id] = apply_factorial_fill(
            arm,
            fill=fill,
            prices={"QQQ": Decimal("500")},
        )
    return filled


def _mark(
    arms: dict[str, FactorialPaperArm],
    *,
    price: Decimal,
    manifest_hash: str,
    forecast_hash: str,
) -> dict[str, FactorialPaperArm]:
    return {
        arm_id: replace(
            arm,
            latest_nav_usd=(
                arm.portfolio.cash_usd
                + arm.portfolio.positions.get("QQQ", Decimal("0")) * price
            ),
            common_market_manifest_hash=manifest_hash,
            forecast_hash=forecast_hash,
        )
        for arm_id, arm in arms.items()
    }


def test_factorial_state_is_durable_replayable_and_attribution_ready(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = FactorialPaperExperimentRepository(factory)
    arms = _arms()
    assert repository.initialize(
        run_id="factorial-run-1",
        arms=arms,
        config_manifest_hash=CONFIG_HASH,
        code_commit="test-commit",
        effective_at=NOW,
    )
    assert not repository.initialize(
        run_id="factorial-run-1",
        arms=arms,
        config_manifest_hash=CONFIG_HASH,
        code_commit="test-commit",
        effective_at=NOW,
    )

    planned = _planned()
    assert repository.append_checkpoint(
        run_id="factorial-run-1",
        checkpoint_id="planned-1",
        checkpoint_kind=FactorialCheckpointKind.PLANNED,
        arms=planned,
        as_of=NOW + timedelta(minutes=1),
    )
    filled = _filled(planned)
    assert repository.append_checkpoint(
        run_id="factorial-run-1",
        checkpoint_id="filled-1",
        checkpoint_kind=FactorialCheckpointKind.FILL,
        arms=filled,
        as_of=NOW + timedelta(minutes=2),
    )
    first_close = _mark(
        filled,
        price=Decimal("510"),
        manifest_hash="d" * 64,
        forecast_hash="e" * 64,
    )
    assert repository.append_checkpoint(
        run_id="factorial-run-1",
        checkpoint_id="close-2026-07-27",
        checkpoint_kind=FactorialCheckpointKind.DAILY_CLOSE,
        arms=first_close,
        as_of=NOW + timedelta(hours=1),
    )
    second_close = _mark(
        first_close,
        price=Decimal("520"),
        manifest_hash="f" * 64,
        forecast_hash="1" * 64,
    )
    assert repository.append_checkpoint(
        run_id="factorial-run-1",
        checkpoint_id="close-2026-07-28",
        checkpoint_kind=FactorialCheckpointKind.DAILY_CLOSE,
        arms=second_close,
        as_of=NOW + timedelta(days=1, hours=1),
    )
    assert not repository.append_checkpoint(
        run_id="factorial-run-1",
        checkpoint_id="close-2026-07-28",
        checkpoint_kind=FactorialCheckpointKind.DAILY_CLOSE,
        arms=second_close,
        as_of=NOW + timedelta(days=1, hours=1),
    )

    first_replay = repository.replay("factorial-run-1")
    second_replay = repository.replay("factorial-run-1")
    assert first_replay.replay_hash == second_replay.replay_hash
    assert first_replay.final_state_hash == second_replay.final_state_hash
    assert first_replay.checkpoint_count == 5
    assert set(first_replay.arms) == set(FACTORIAL_ARM_IDS)

    status = repository.status(
        run_id="factorial-run-1",
        minimum_common_sessions=2,
        schedule_timezone="America/New_York",
        scheduled_time="18:00",
    )
    assert status["status"] == "SHADOW_RUNNING"
    assert status["matched_conditions_ready"] is True
    assert status["effects"]["ready"] is True
    assert status["effects"]["common_sessions"] == 2
    assert status["effects"]["preliminary_values"] is not None
    assert all(
        metric["ready"] is True
        for metric in status["effects"]["metrics"].values()
    )
    assert status["real_order_routing"] is False
    assert set(status["arms"]) == set(FACTORIAL_ARM_IDS)
    assert all(
        item["real_order_routing"] is False
        for item in status["arms"].values()
    )

    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ArmStateSnapshotRow)) == 20
        assert session.scalar(select(func.count()).select_from(OrderIntentRow)) == 4
        assert session.scalar(select(func.count()).select_from(FillRow)) == 4
        assert session.scalar(select(func.count()).select_from(LedgerTransactionRow)) == 8
        assert session.scalar(select(func.count()).select_from(NavSnapshotRow)) == 20


def test_factorial_persistence_fails_closed_on_unmatched_inputs(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = FactorialPaperExperimentRepository(factory)
    arms = _arms()
    repository.initialize(
        run_id="factorial-run-mismatch",
        arms=arms,
        config_manifest_hash=CONFIG_HASH,
        code_commit="test-commit",
        effective_at=NOW,
    )
    mismatched = {
        **arms,
        "B3-AI": replace(
            arms["B3-AI"],
            common_market_manifest_hash="9" * 64,
        ),
    }
    with pytest.raises(
        FactorialPersistenceError,
        match="matched market/input/fill",
    ):
        repository.append_checkpoint(
            run_id="factorial-run-mismatch",
            checkpoint_id="mismatched",
            checkpoint_kind=FactorialCheckpointKind.DAILY_CLOSE,
            arms=mismatched,
            as_of=NOW + timedelta(hours=1),
        )

    status = repository.status(
        run_id="unknown-factorial-run",
        minimum_common_sessions=63,
        schedule_timezone="America/New_York",
        scheduled_time="18:00",
    )
    assert status["status"] == "BLOCKED_MATCHED_CONDITIONS"
    assert status["matched_conditions_ready"] is False
    assert status["effects"]["ready"] is False
    assert status["real_order_routing"] is False

    active_config_mismatch = repository.status(
        run_id="factorial-run-mismatch",
        minimum_common_sessions=63,
        schedule_timezone="America/New_York",
        scheduled_time="18:00",
        expected_config_manifest_hash="d" * 64,
    )
    assert active_config_mismatch["status"] == (
        "BLOCKED_MATCHED_CONDITIONS"
    )
    assert "active research config" in active_config_mismatch["reason"]


def test_factorial_rebalance_cannot_silently_replace_pending_orders() -> None:
    planned = _planned()
    with pytest.raises(ValueError, match="cannot replace unresolved"):
        plan_factorial_rebalance(
            arms=planned,
            targets=factorial_target_weights(
                base_weights={
                    "QQQ": Decimal("0.60"),
                    "USD_CASH": Decimal("0.40"),
                },
                guard_risk_multiplier=Decimal("0.50"),
                ai_risk_multiplier=Decimal("0.75"),
            ),
            prices={"QQQ": Decimal("500")},
            created_at=NOW + timedelta(minutes=2),
            valid_until=NOW + timedelta(minutes=20),
            decision_scope="factorial-cycle-2",
            minimum_notional_usd=Decimal("25"),
        )
