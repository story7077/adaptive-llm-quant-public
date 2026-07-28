from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self, cast

from pydantic import Field, field_validator, model_validator
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import (
    DomainModel,
    Fill,
    LedgerEntry,
    OrderIntent,
    model_payload,
)
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.experiments.ai_guard_factorial import (
    FACTORIAL_ARM_IDS,
    FACTORIAL_EXPERIMENT_VERSION,
    FactorialArmContract,
    factorial_arm_contracts,
)
from trading.experiments.ai_guard_factorial_runtime import (
    FactorialOrderState,
    FactorialPaperArm,
    factorial_state_hash,
)
from trading.experiments.arms import ArmState
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    LedgerPostingRow,
    LedgerTransactionRow,
    NavSnapshotRow,
    OrderIntentRow,
    RunRow,
    ShadowArmRow,
)
from trading.research.shadow import FactorialAttribution, calculate_ai_guard_factorial

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_HASH = re.compile(r"^[a-f0-9]{64}$")
_ZERO = Decimal("0")


class FactorialPersistenceError(RuntimeError):
    pass


class FactorialCheckpointKind(StrEnum):
    INITIAL = "INITIAL"
    PLANNED = "PLANNED"
    FILL = "FILL"
    DAILY_CLOSE = "DAILY_CLOSE"


class FactorialArmContractSnapshot(DomainModel):
    arm_id: str
    deterministic_loss_guard: bool
    operational_risk_commander: bool
    independent_cash_positions_orders_ledger: bool
    real_order_routing: bool


class FactorialPortfolioSnapshot(DomainModel):
    arm_id: str
    initial_cash_usd: Decimal
    cash_usd: Decimal
    positions: dict[str, Decimal]
    sequence: int = Field(ge=0)

    def to_state(self) -> ArmState:
        return ArmState(
            arm_id=self.arm_id,
            initial_cash_usd=self.initial_cash_usd,
            cash_usd=self.cash_usd,
            positions=dict(self.positions),
            sequence=self.sequence,
        )


class FactorialOrderSnapshot(DomainModel):
    intent: OrderIntent
    remaining_quantity: Decimal = Field(gt=0)
    valid_until: datetime

    @field_validator("valid_until", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class FactorialArmSnapshotV1(DomainModel):
    schema_version: Literal["factorial_arm_snapshot_v1"] = (
        "factorial_arm_snapshot_v1"
    )
    run_id: str
    checkpoint_id: str
    checkpoint_kind: FactorialCheckpointKind
    checkpoint_sequence: int = Field(ge=0)
    contract: FactorialArmContractSnapshot
    portfolio: FactorialPortfolioSnapshot
    pending_orders: list[FactorialOrderSnapshot]
    fills: list[Fill]
    ledger: list[LedgerEntry]
    latest_nav_usd: Decimal = Field(gt=0)
    common_market_manifest_hash: str
    forecast_hash: str
    policy_version: str
    decision_schedule_version: str
    execution_scenario_version: str
    cost_model_version: str
    starting_capital_usd: Decimal = Field(gt=0)
    config_manifest_hash: str
    real_order_routing: bool = False
    created_at: datetime
    state_hash: str

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if self.real_order_routing or self.contract.real_order_routing:
            raise ValueError("factorial snapshots are paper-only")
        if self.contract.arm_id != self.portfolio.arm_id:
            raise ValueError("factorial snapshot arm mismatch")
        for value in (
            self.common_market_manifest_hash,
            self.forecast_hash,
            self.config_manifest_hash,
        ):
            if _HASH.fullmatch(value) is None:
                raise ValueError("factorial snapshot hash binding is invalid")
        payload = self.model_dump(mode="python", exclude={"state_hash"})
        if canonical_hash(payload) != self.state_hash:
            raise ValueError("factorial snapshot state_hash mismatch")
        self.to_arm()
        return self

    @classmethod
    def from_arm(
        cls,
        *,
        run_id: str,
        checkpoint_id: str,
        checkpoint_kind: FactorialCheckpointKind,
        checkpoint_sequence: int,
        arm: FactorialPaperArm,
        created_at: datetime,
    ) -> FactorialArmSnapshotV1:
        contract = arm.contract
        payload: dict[str, object] = {
            "schema_version": "factorial_arm_snapshot_v1",
            "run_id": run_id,
            "checkpoint_id": checkpoint_id,
            "checkpoint_kind": checkpoint_kind,
            "checkpoint_sequence": checkpoint_sequence,
            "contract": {
                "arm_id": contract.arm_id,
                "deterministic_loss_guard": contract.deterministic_loss_guard,
                "operational_risk_commander": contract.operational_risk_commander,
                "independent_cash_positions_orders_ledger": (
                    contract.independent_cash_positions_orders_ledger
                ),
                "real_order_routing": contract.real_order_routing,
            },
            "portfolio": arm.portfolio.as_payload(),
            "pending_orders": [
                {
                    "intent": order.intent.model_dump(mode="python"),
                    "remaining_quantity": order.remaining_quantity,
                    "valid_until": order.valid_until,
                }
                for order in arm.pending_orders
            ],
            "fills": [fill.model_dump(mode="python") for fill in arm.fills],
            "ledger": [entry.model_dump(mode="python") for entry in arm.ledger],
            "latest_nav_usd": arm.latest_nav_usd,
            "common_market_manifest_hash": arm.common_market_manifest_hash,
            "forecast_hash": arm.forecast_hash,
            "policy_version": arm.policy_version,
            "decision_schedule_version": arm.decision_schedule_version,
            "execution_scenario_version": arm.execution_scenario_version,
            "cost_model_version": arm.cost_model_version,
            "starting_capital_usd": arm.starting_capital_usd,
            "config_manifest_hash": arm.config_manifest_hash,
            "real_order_routing": arm.real_order_routing,
            "created_at": require_aware_utc(created_at),
        }
        return cls.model_validate(
            {
                **payload,
                "state_hash": canonical_hash(payload),
            }
        )

    def to_arm(self) -> FactorialPaperArm:
        expected_contract = _contract_by_arm().get(self.contract.arm_id)
        if expected_contract is None:
            raise ValueError("unknown factorial arm contract")
        actual_contract = FactorialArmContract(
            arm_id=self.contract.arm_id,
            deterministic_loss_guard=self.contract.deterministic_loss_guard,
            operational_risk_commander=self.contract.operational_risk_commander,
            independent_cash_positions_orders_ledger=(
                self.contract.independent_cash_positions_orders_ledger
            ),
            real_order_routing=self.contract.real_order_routing,
        )
        if actual_contract != expected_contract:
            raise ValueError("factorial treatment contract changed")
        return FactorialPaperArm(
            contract=actual_contract,
            portfolio=self.portfolio.to_state(),
            pending_orders=tuple(
                FactorialOrderState(
                    intent=order.intent,
                    remaining_quantity=order.remaining_quantity,
                    valid_until=order.valid_until,
                )
                for order in self.pending_orders
            ),
            fills=tuple(self.fills),
            ledger=tuple(self.ledger),
            latest_nav_usd=self.latest_nav_usd,
            common_market_manifest_hash=self.common_market_manifest_hash,
            forecast_hash=self.forecast_hash,
            policy_version=self.policy_version,
            decision_schedule_version=self.decision_schedule_version,
            execution_scenario_version=self.execution_scenario_version,
            cost_model_version=self.cost_model_version,
            starting_capital_usd=self.starting_capital_usd,
            config_manifest_hash=self.config_manifest_hash,
            real_order_routing=self.real_order_routing,
        )


@dataclass(frozen=True, slots=True)
class FactorialReplayResult:
    run_id: str
    checkpoint_count: int
    final_state_hash: str
    replay_hash: str
    arms: dict[str, FactorialPaperArm]


class FactorialPaperExperimentRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def initialize(
        self,
        *,
        run_id: str,
        arms: dict[str, FactorialPaperArm],
        config_manifest_hash: str,
        code_commit: str,
        effective_at: datetime,
    ) -> bool:
        _validate_identifier(run_id, "run_id")
        timestamp = require_aware_utc(effective_at)
        _validate_matched_arms(arms, config_manifest_hash=config_manifest_hash)
        with self._session_factory.begin() as session:
            existing = session.get(RunRow, run_id)
            if existing is None:
                session.add(
                    RunRow(
                        run_id=run_id,
                        mode="RESEARCH_PAPER",
                        experiment_version=FACTORIAL_EXPERIMENT_VERSION,
                        config_manifest_hash=config_manifest_hash,
                        code_commit=code_commit,
                        started_at=timestamp,
                        ended_at=None,
                        status="SHADOW_RUNNING",
                        result_manifest=None,
                        result_hash=None,
                    )
                )
                session.flush()
            else:
                _require_matching_run(
                    existing,
                    config_manifest_hash=config_manifest_hash,
                    code_commit=code_commit,
                    started_at=timestamp,
                )
            created = self._append_checkpoint(
                session,
                run_id=run_id,
                checkpoint_id="INITIAL",
                checkpoint_kind=FactorialCheckpointKind.INITIAL,
                arms=arms,
                as_of=timestamp,
                config_manifest_hash=config_manifest_hash,
            )
            return created

    def append_checkpoint(
        self,
        *,
        run_id: str,
        checkpoint_id: str,
        checkpoint_kind: FactorialCheckpointKind,
        arms: dict[str, FactorialPaperArm],
        as_of: datetime,
    ) -> bool:
        _validate_identifier(run_id, "run_id")
        _validate_identifier(checkpoint_id, "checkpoint_id")
        timestamp = require_aware_utc(as_of)
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(RunRow)
                .where(RunRow.run_id == run_id)
                .with_for_update()
            )
            if run is None:
                raise FactorialPersistenceError("unknown factorial paper run")
            if run.experiment_version != FACTORIAL_EXPERIMENT_VERSION:
                raise FactorialPersistenceError(
                    "run belongs to another experiment version"
                )
            _validate_matched_arms(
                arms,
                config_manifest_hash=run.config_manifest_hash,
            )
            return self._append_checkpoint(
                session,
                run_id=run_id,
                checkpoint_id=checkpoint_id,
                checkpoint_kind=checkpoint_kind,
                arms=arms,
                as_of=timestamp,
                config_manifest_hash=run.config_manifest_hash,
            )

    def latest_arms(self, run_id: str) -> dict[str, FactorialPaperArm]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(ArmStateSnapshotRow)
                    .where(
                        ArmStateSnapshotRow.run_id == run_id,
                        ArmStateSnapshotRow.arm_id.in_(FACTORIAL_ARM_IDS),
                    )
                    .order_by(
                        ArmStateSnapshotRow.sequence.desc(),
                        ArmStateSnapshotRow.arm_id,
                    )
                )
            )
        if not rows:
            raise FactorialPersistenceError("factorial paper run has no state")
        latest_sequence = rows[0].sequence
        selected = [row for row in rows if row.sequence == latest_sequence]
        return _arms_from_rows(selected)

    def replay(self, run_id: str) -> FactorialReplayResult:
        with self._session_factory() as session:
            run = session.get(RunRow, run_id)
            if run is None or run.experiment_version != FACTORIAL_EXPERIMENT_VERSION:
                raise FactorialPersistenceError("unknown factorial paper run")
            rows = list(
                session.scalars(
                    select(ArmStateSnapshotRow)
                    .where(
                        ArmStateSnapshotRow.run_id == run_id,
                        ArmStateSnapshotRow.arm_id.in_(FACTORIAL_ARM_IDS),
                    )
                    .order_by(
                        ArmStateSnapshotRow.sequence,
                        ArmStateSnapshotRow.arm_id,
                    )
                )
            )
            order_rows = list(
                session.scalars(
                    select(OrderIntentRow).where(OrderIntentRow.run_id == run_id)
                )
            )
            fill_rows = list(
                session.scalars(select(FillRow).where(FillRow.run_id == run_id))
            )
            ledger_rows = list(
                session.scalars(
                    select(LedgerTransactionRow).where(
                        LedgerTransactionRow.run_id == run_id
                    )
                )
            )
            ledger_posting_rows = list(
                session.scalars(
                    select(LedgerPostingRow)
                    .join(
                        LedgerTransactionRow,
                        LedgerPostingRow.ledger_transaction_id
                        == LedgerTransactionRow.ledger_transaction_id,
                    )
                    .where(LedgerTransactionRow.run_id == run_id)
                )
            )
            nav_rows = list(
                session.scalars(
                    select(NavSnapshotRow).where(
                        NavSnapshotRow.run_id == run_id,
                        NavSnapshotRow.arm_id.in_(FACTORIAL_ARM_IDS),
                    )
                )
            )
        grouped = _group_checkpoint_rows(rows)
        if not grouped:
            raise FactorialPersistenceError("factorial paper run has no checkpoints")
        checkpoint_hashes: list[str] = []
        checkpoint_snapshots: list[
            dict[str, FactorialArmSnapshotV1]
        ] = []
        historical_orders: dict[
            str, tuple[str, FactorialOrderState]
        ] = {}
        final_arms: dict[str, FactorialPaperArm] | None = None
        for expected_sequence, (sequence, checkpoint_rows) in enumerate(
            sorted(grouped.items())
        ):
            if sequence != expected_sequence:
                raise FactorialPersistenceError("factorial checkpoint sequence gap")
            arms = _arms_from_rows(checkpoint_rows)
            _validate_matched_arms(
                arms,
                config_manifest_hash=run.config_manifest_hash,
            )
            snapshots = {
                row.arm_id: FactorialArmSnapshotV1.model_validate(
                    row.payload_json
                )
                for row in checkpoint_rows
            }
            checkpoint_snapshots.append(snapshots)
            for arm_id, arm in arms.items():
                for pending in arm.pending_orders:
                    order_id = pending.intent.order_intent_id
                    previous = historical_orders.get(order_id)
                    current = (arm_id, pending)
                    if previous is not None and (
                        previous[0] != current[0]
                        or canonical_hash(previous[1].intent)
                        != canonical_hash(current[1].intent)
                        or previous[1].valid_until
                        != current[1].valid_until
                    ):
                        raise FactorialPersistenceError(
                            "factorial historical order identity conflict"
                        )
                    historical_orders[order_id] = current
            checkpoint_hashes.append(factorial_state_hash(arms))
            final_arms = arms
        assert final_arms is not None
        _validate_materialized_records(
            final_arms,
            run_id=run_id,
            order_rows=order_rows,
            fill_rows=fill_rows,
            ledger_rows=ledger_rows,
            ledger_posting_rows=ledger_posting_rows,
            historical_orders=historical_orders,
        )
        _validate_nav_records(
            run,
            checkpoints=checkpoint_snapshots,
            nav_rows=nav_rows,
        )
        final_hash = factorial_state_hash(final_arms)
        replay_hash = canonical_hash(
            {
                "run_id": run_id,
                "experiment_version": FACTORIAL_EXPERIMENT_VERSION,
                "checkpoint_hashes": checkpoint_hashes,
                "final_state_hash": final_hash,
            }
        )
        return FactorialReplayResult(
            run_id=run_id,
            checkpoint_count=len(grouped),
            final_state_hash=final_hash,
            replay_hash=replay_hash,
            arms=final_arms,
        )

    def status(
        self,
        *,
        minimum_common_sessions: int,
        schedule_timezone: str,
        scheduled_time: str,
        run_id: str | None = None,
        expected_config_manifest_hash: str | None = None,
        decision_schedule_version: str | None = None,
        execution_scenario_version: str | None = None,
        cost_model_version: str | None = None,
    ) -> dict[str, Any]:
        if minimum_common_sessions <= 0:
            raise ValueError("minimum_common_sessions must be positive")
        for label, value in (
            ("schedule timezone", schedule_timezone),
            ("scheduled time", scheduled_time),
            ("decision schedule version", decision_schedule_version),
            ("execution scenario version", execution_scenario_version),
            ("cost model version", cost_model_version),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{label} must not be blank")
        if (
            expected_config_manifest_hash is not None
            and _HASH.fullmatch(expected_config_manifest_hash) is None
        ):
            raise ValueError("invalid expected factorial config manifest hash")
        resolved_run_id = run_id or self._latest_run_id()
        base: dict[str, Any] = {
            "experiment_version": FACTORIAL_EXPERIMENT_VERSION,
            "schedule": {
                "timezone": schedule_timezone,
                "daily_aggregation_time": scheduled_time,
                "decision_schedule_version": decision_schedule_version,
                "authority": "RESEARCH_PLANE_CONFIG",
            },
            "matched_conditions": {
                "common_market_input": True,
                "common_forecast_input": True,
                "common_decision_schedule": True,
                "common_execution_scenario": True,
                "common_cost_model": True,
                "common_starting_capital": True,
                "execution_scenario_version": execution_scenario_version,
                "cost_model_version": cost_model_version,
            },
            "required_arms": list(FACTORIAL_ARM_IDS),
            "minimum_common_sessions": minimum_common_sessions,
            "real_order_routing": False,
        }
        if resolved_run_id is None:
            return {
                **base,
                "status": "NOT_INITIALIZED",
                "run_id": None,
                "matched_conditions_ready": False,
                "arms": _empty_arm_status(),
                "effects": _effect_status(
                    common_sessions=0,
                    minimum_common_sessions=minimum_common_sessions,
                    attribution=None,
                ),
            }
        try:
            with self._session_factory() as session:
                run = session.get(RunRow, resolved_run_id)
                if run is None:
                    raise FactorialPersistenceError(
                        "unknown factorial paper run"
                    )
                if (
                    expected_config_manifest_hash is not None
                    and run.config_manifest_hash
                    != expected_config_manifest_hash
                ):
                    raise FactorialPersistenceError(
                        "factorial run does not match the active research config"
                    )
            replay = self.replay(resolved_run_id)
            for arm in replay.arms.values():
                if (
                    decision_schedule_version is not None
                    and arm.decision_schedule_version
                    != decision_schedule_version
                ):
                    raise FactorialPersistenceError(
                        "factorial decision schedule version mismatch"
                    )
                if (
                    execution_scenario_version is not None
                    and arm.execution_scenario_version
                    != execution_scenario_version
                ):
                    raise FactorialPersistenceError(
                        "factorial execution scenario version mismatch"
                    )
                if (
                    cost_model_version is not None
                    and arm.cost_model_version != cost_model_version
                ):
                    raise FactorialPersistenceError(
                        "factorial cost model version mismatch"
                    )
            checkpoints = self._checkpoint_snapshots(resolved_run_id)
            returns = _matched_daily_returns(checkpoints)
            attribution = (
                None
                if not returns["B0-VOL"]
                else calculate_ai_guard_factorial(
                    b0_vol=returns["B0-VOL"],
                    b3_guard=returns["B3-GUARD"],
                    b3_ai=returns["B3-AI"],
                    b3_ai_guard=returns["B3-AI-GUARD"],
                )
            )
            arm_status = {
                arm_id: {
                    "latest_nav_usd": str(arm.latest_nav_usd),
                    "cash_usd": str(arm.portfolio.cash_usd),
                    "positions": {
                        symbol: str(quantity)
                        for symbol, quantity in sorted(
                            arm.portfolio.positions.items()
                        )
                    },
                    "pending_order_count": len(arm.pending_orders),
                    "fill_count": len(arm.fills),
                    "ledger_transaction_count": len(arm.ledger),
                    "state_sequence": arm.portfolio.sequence,
                    "state_hash": factorial_state_hash({arm_id: arm}),
                    "real_order_routing": False,
                }
                for arm_id, arm in replay.arms.items()
            }
            common_sessions = len(returns["B0-VOL"])
            return {
                **base,
                "status": "SHADOW_RUNNING",
                "run_id": resolved_run_id,
                "matched_conditions_ready": True,
                "checkpoint_count": replay.checkpoint_count,
                "replay_hash": replay.replay_hash,
                "arms": arm_status,
                "effects": _effect_status(
                    common_sessions=common_sessions,
                    minimum_common_sessions=minimum_common_sessions,
                    attribution=attribution,
                ),
            }
        except (FactorialPersistenceError, ValueError) as exc:
            return {
                **base,
                "status": "BLOCKED_MATCHED_CONDITIONS",
                "run_id": resolved_run_id,
                "matched_conditions_ready": False,
                "reason": str(exc),
                "arms": _empty_arm_status(),
                "effects": _effect_status(
                    common_sessions=0,
                    minimum_common_sessions=minimum_common_sessions,
                    attribution=None,
                ),
            }

    def _append_checkpoint(
        self,
        session: Session,
        *,
        run_id: str,
        checkpoint_id: str,
        checkpoint_kind: FactorialCheckpointKind,
        arms: dict[str, FactorialPaperArm],
        as_of: datetime,
        config_manifest_hash: str,
    ) -> bool:
        snapshot_ids = {
            arm_id: stable_id(
                "factorial-arm-snapshot",
                run_id,
                checkpoint_id,
                arm_id,
            )
            for arm_id in FACTORIAL_ARM_IDS
        }
        existing = {
            arm_id: session.get(ArmStateSnapshotRow, snapshot_id)
            for arm_id, snapshot_id in snapshot_ids.items()
        }
        present = {arm_id for arm_id, row in existing.items() if row is not None}
        if present and present != set(FACTORIAL_ARM_IDS):
            raise FactorialPersistenceError(
                "partial factorial checkpoint already exists"
            )
        if present:
            sequence = cast(ArmStateSnapshotRow, existing["B0-VOL"]).sequence
            expected = _build_snapshots(
                run_id=run_id,
                checkpoint_id=checkpoint_id,
                checkpoint_kind=checkpoint_kind,
                checkpoint_sequence=sequence,
                arms=arms,
                as_of=as_of,
            )
            for arm_id, snapshot in expected.items():
                row = cast(ArmStateSnapshotRow, existing[arm_id])
                if (
                    row.state_hash != snapshot.state_hash
                    or canonical_hash(row.payload_json)
                    != canonical_hash(model_payload(snapshot))
                ):
                    raise FactorialPersistenceError(
                        "factorial checkpoint idempotency conflict"
                    )
            return False
        maximum = session.scalar(
            select(func.max(ArmStateSnapshotRow.sequence)).where(
                ArmStateSnapshotRow.run_id == run_id,
                ArmStateSnapshotRow.arm_id.in_(FACTORIAL_ARM_IDS),
            )
        )
        sequence = 0 if maximum is None else int(maximum) + 1
        snapshots = _build_snapshots(
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            checkpoint_kind=checkpoint_kind,
            checkpoint_sequence=sequence,
            arms=arms,
            as_of=as_of,
        )
        run = session.get(RunRow, run_id)
        if run is None:
            raise FactorialPersistenceError("factorial run disappeared")
        self._persist_materialized_records(
            session,
            run_id=run_id,
            arms=arms,
            config_manifest_hash=config_manifest_hash,
        )
        for arm_id in FACTORIAL_ARM_IDS:
            arm = arms[arm_id]
            snapshot = snapshots[arm_id]
            arm_instance_id = stable_id("factorial-arm", run_id, arm_id)
            if session.get(ShadowArmRow, arm_instance_id) is None:
                session.add(
                    ShadowArmRow(
                        arm_instance_id=arm_instance_id,
                        run_id=run_id,
                        arm_id=arm_id,
                        created_at=as_of,
                    )
                )
            session.add(
                ArmStateSnapshotRow(
                    arm_state_snapshot_id=snapshot_ids[arm_id],
                    run_id=run_id,
                    arm_id=arm_id,
                    sequence=sequence,
                    source_cycle_id=None,
                    state_hash=snapshot.state_hash,
                    payload_json=model_payload(snapshot),
                    created_at=as_of,
                )
            )
            nav_payload = {
                "schema_version": "factorial_nav_snapshot_v1",
                "run_id": run_id,
                "arm_id": arm_id,
                "checkpoint_id": checkpoint_id,
                "checkpoint_kind": checkpoint_kind.value,
                "checkpoint_sequence": sequence,
                "as_of": as_of.isoformat(),
                "nav_usd": str(arm.latest_nav_usd),
                "state_hash": snapshot.state_hash,
                "common_market_manifest_hash": (
                    arm.common_market_manifest_hash
                ),
                "forecast_hash": arm.forecast_hash,
                "execution_scenario_version": (
                    arm.execution_scenario_version
                ),
                "cost_model_version": arm.cost_model_version,
                "real_order_routing": False,
            }
            nav_id = stable_id("factorial-nav", run_id, checkpoint_id, arm_id)
            session.add(
                NavSnapshotRow(
                    nav_snapshot_id=nav_id,
                    run_id=run_id,
                    arm_id=arm_id,
                    source_cycle_id=None,
                    quote_manifest_hash=arm.common_market_manifest_hash,
                    algorithm_version=FACTORIAL_EXPERIMENT_VERSION,
                    config_manifest_hash=config_manifest_hash,
                    code_version=run.code_commit,
                    model_version=arm.policy_version,
                    source_manifest_hash=arm.common_market_manifest_hash,
                    as_of=as_of,
                    nav_usd=arm.latest_nav_usd,
                    payload_json=nav_payload,
                )
            )
        return True

    def _persist_materialized_records(
        self,
        session: Session,
        *,
        run_id: str,
        arms: dict[str, FactorialPaperArm],
        config_manifest_hash: str,
    ) -> None:
        run = session.get(RunRow, run_id)
        if run is None:
            raise FactorialPersistenceError("factorial run disappeared")
        for arm in arms.values():
            for order in arm.pending_orders:
                intent = order.intent
                intent_hash = canonical_hash(intent)
                existing_order = session.get(OrderIntentRow, intent.order_intent_id)
                if existing_order is None:
                    session.add(
                        OrderIntentRow(
                            order_intent_id=intent.order_intent_id,
                            run_id=run_id,
                            arm_id=arm.contract.arm_id,
                            source_cycle_id=None,
                            input_state_sequence=arm.portfolio.sequence,
                            symbol=intent.symbol,
                            side=intent.side.value,
                            quantity=intent.quantity,
                            created_at=intent.created_at,
                            valid_until=order.valid_until,
                            decision_quote_id=None,
                            decision_reference_price=None,
                            algorithm_version=FACTORIAL_EXPERIMENT_VERSION,
                            config_manifest_hash=config_manifest_hash,
                            code_version=run.code_commit,
                            model_version=arm.policy_version,
                            source_manifest_hash=arm.common_market_manifest_hash,
                            decision_spread_bps=None,
                            idempotency_key=intent.idempotency_key,
                            payload_json=model_payload(intent),
                            intent_hash=intent_hash,
                        )
                    )
                elif (
                    existing_order.run_id != run_id
                    or existing_order.arm_id != arm.contract.arm_id
                    or existing_order.intent_hash != intent_hash
                ):
                    raise FactorialPersistenceError(
                        "factorial order idempotency conflict"
                    )
            for fill in arm.fills:
                existing_fill = session.get(FillRow, fill.fill_id)
                fill_hash = canonical_hash(fill)
                if existing_fill is None:
                    order = session.get(OrderIntentRow, fill.order_intent_id)
                    if order is None:
                        raise FactorialPersistenceError(
                            "factorial fill requires a persisted paper order"
                        )
                    quote_id = stable_id(
                        "factorial-fill-input",
                        arm.common_market_manifest_hash,
                        fill.effective_at,
                        fill.symbol,
                        str(fill.price),
                    )
                    session.add(
                        FillRow(
                            fill_id=fill.fill_id,
                            order_intent_id=fill.order_intent_id,
                            run_id=run_id,
                            arm_id=arm.contract.arm_id,
                            source_cycle_id=None,
                            quote_id=quote_id,
                            quote_event_time=fill.effective_at,
                            quote_available_at=fill.effective_at,
                            symbol=fill.symbol,
                            side=fill.side.value,
                            quantity=fill.quantity,
                            price=fill.price,
                            commission_usd=fill.commission_usd,
                            execution_scenario_id=fill.execution_scenario_id,
                            fill_hash=fill_hash,
                            algorithm_version=FACTORIAL_EXPERIMENT_VERSION,
                            config_manifest_hash=config_manifest_hash,
                            code_version=run.code_commit,
                            model_version=arm.policy_version,
                            source_manifest_hash=arm.common_market_manifest_hash,
                            base_fill_cost_usd=None,
                            sensitivity_5bp_cost_usd=None,
                            sensitivity_10bp_cost_usd=None,
                            effective_at=fill.effective_at,
                            payload_json=model_payload(fill),
                        )
                    )
                elif (
                    existing_fill.run_id != run_id
                    or existing_fill.arm_id != arm.contract.arm_id
                    or existing_fill.fill_hash != fill_hash
                ):
                    raise FactorialPersistenceError(
                        "factorial fill idempotency conflict"
                    )
            for entry in arm.ledger:
                transaction = entry.transaction
                existing_transaction = session.get(
                    LedgerTransactionRow,
                    transaction.ledger_transaction_id,
                )
                if existing_transaction is None:
                    session.add(
                        LedgerTransactionRow(
                            ledger_transaction_id=(
                                transaction.ledger_transaction_id
                            ),
                            run_id=run_id,
                            arm_id=arm.contract.arm_id,
                            source_id=transaction.source_id,
                            effective_at=transaction.effective_at,
                            payload_json=model_payload(transaction),
                        )
                    )
                    # The ORM rows intentionally have no relationship mapping.
                    # Flush the parent explicitly so SQLite and PostgreSQL both
                    # enforce the posting foreign key in the intended order.
                    session.flush()
                    for posting in entry.postings:
                        session.add(
                            LedgerPostingRow(
                                posting_id=posting.posting_id,
                                ledger_transaction_id=(
                                    transaction.ledger_transaction_id
                                ),
                                account_code=posting.account_code,
                                asset_code=posting.asset_code,
                                quantity_delta=posting.quantity_delta,
                                usd_value_delta=posting.usd_value_delta,
                                payload_json=model_payload(posting),
                            )
                        )
                elif (
                    existing_transaction.run_id != run_id
                    or existing_transaction.arm_id != arm.contract.arm_id
                    or canonical_hash(existing_transaction.payload_json)
                    != canonical_hash(model_payload(transaction))
                ):
                    raise FactorialPersistenceError(
                        "factorial ledger idempotency conflict"
                    )

    def _latest_run_id(self) -> str | None:
        with self._session_factory() as session:
            return session.scalar(
                select(RunRow.run_id)
                .where(
                    RunRow.experiment_version
                    == FACTORIAL_EXPERIMENT_VERSION
                )
                .order_by(desc(RunRow.started_at), desc(RunRow.run_id))
                .limit(1)
            )

    def _checkpoint_snapshots(
        self,
        run_id: str,
    ) -> list[dict[str, FactorialArmSnapshotV1]]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(ArmStateSnapshotRow)
                    .where(
                        ArmStateSnapshotRow.run_id == run_id,
                        ArmStateSnapshotRow.arm_id.in_(FACTORIAL_ARM_IDS),
                    )
                    .order_by(
                        ArmStateSnapshotRow.sequence,
                        ArmStateSnapshotRow.arm_id,
                    )
                )
            )
        return [
            {
                row.arm_id: FactorialArmSnapshotV1.model_validate(
                    row.payload_json
                )
                for row in group_rows
            }
            for _, group_rows in sorted(_group_checkpoint_rows(rows).items())
        ]


def _contract_by_arm() -> dict[str, FactorialArmContract]:
    return {contract.arm_id: contract for contract in factorial_arm_contracts()}


def _validate_identifier(value: str, label: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise FactorialPersistenceError(f"invalid {label}")


def _validate_matched_arms(
    arms: dict[str, FactorialPaperArm],
    *,
    config_manifest_hash: str,
) -> None:
    if set(arms) != set(FACTORIAL_ARM_IDS):
        raise FactorialPersistenceError(
            "factorial checkpoint requires exactly four arms"
        )
    if _HASH.fullmatch(config_manifest_hash) is None:
        raise FactorialPersistenceError("invalid factorial config manifest hash")
    expected_contracts = _contract_by_arm()
    bindings: set[
        tuple[str, str, str, str, str, str, Decimal, str]
    ] = set()
    object_ids: set[int] = set()
    order_ids: set[str] = set()
    fill_ids: set[str] = set()
    for arm_id in FACTORIAL_ARM_IDS:
        arm = arms[arm_id]
        if arm.contract != expected_contracts[arm_id]:
            raise FactorialPersistenceError(
                "factorial treatment contract mismatch"
            )
        if arm.real_order_routing or arm.contract.real_order_routing:
            raise FactorialPersistenceError(
                "real routing is forbidden for factorial arms"
            )
        if arm.config_manifest_hash != config_manifest_hash:
            raise FactorialPersistenceError(
                "factorial arm config hash mismatch"
            )
        object_ids.add(id(arm.portfolio.positions))
        bindings.add(
            (
                arm.common_market_manifest_hash,
                arm.forecast_hash,
                arm.policy_version,
                arm.decision_schedule_version,
                arm.execution_scenario_version,
                arm.cost_model_version,
                arm.starting_capital_usd,
                arm.config_manifest_hash,
            )
        )
        for order in arm.pending_orders:
            if order.intent.order_intent_id in order_ids:
                raise FactorialPersistenceError(
                    "factorial order belongs to multiple arms"
                )
            order_ids.add(order.intent.order_intent_id)
        for fill in arm.fills:
            if fill.fill_id in fill_ids:
                raise FactorialPersistenceError(
                    "factorial fill belongs to multiple arms"
                )
            fill_ids.add(fill.fill_id)
            if fill.execution_scenario_id != arm.execution_scenario_version:
                raise FactorialPersistenceError(
                    "factorial fill scenario mismatch"
                )
    if len(bindings) != 1:
        raise FactorialPersistenceError(
            "factorial arms do not share matched market/input/fill conditions"
        )
    if len(object_ids) != len(FACTORIAL_ARM_IDS):
        raise FactorialPersistenceError(
            "factorial arm position state is not independent"
        )


def _build_snapshots(
    *,
    run_id: str,
    checkpoint_id: str,
    checkpoint_kind: FactorialCheckpointKind,
    checkpoint_sequence: int,
    arms: dict[str, FactorialPaperArm],
    as_of: datetime,
) -> dict[str, FactorialArmSnapshotV1]:
    return {
        arm_id: FactorialArmSnapshotV1.from_arm(
            run_id=run_id,
            checkpoint_id=checkpoint_id,
            checkpoint_kind=checkpoint_kind,
            checkpoint_sequence=checkpoint_sequence,
            arm=arms[arm_id],
            created_at=as_of,
        )
        for arm_id in FACTORIAL_ARM_IDS
    }


def _require_matching_run(
    run: RunRow,
    *,
    config_manifest_hash: str,
    code_commit: str,
    started_at: datetime,
) -> None:
    if (
        run.mode != "RESEARCH_PAPER"
        or run.experiment_version != FACTORIAL_EXPERIMENT_VERSION
        or run.config_manifest_hash != config_manifest_hash
        or run.code_commit != code_commit
        or _aware(run.started_at) != started_at
        or run.status != "SHADOW_RUNNING"
        or run.result_manifest is not None
        or run.result_hash is not None
    ):
        raise FactorialPersistenceError("factorial run identity conflict")


def _arms_from_rows(
    rows: list[ArmStateSnapshotRow],
) -> dict[str, FactorialPaperArm]:
    if {row.arm_id for row in rows} != set(FACTORIAL_ARM_IDS):
        raise FactorialPersistenceError(
            "factorial checkpoint is missing an arm"
        )
    sequences = {row.sequence for row in rows}
    if len(sequences) != 1:
        raise FactorialPersistenceError(
            "factorial checkpoint sequence mismatch"
        )
    arms: dict[str, FactorialPaperArm] = {}
    checkpoint_ids: set[str] = set()
    for row in rows:
        snapshot = FactorialArmSnapshotV1.model_validate(row.payload_json)
        if (
            snapshot.run_id != row.run_id
            or snapshot.contract.arm_id != row.arm_id
            or snapshot.checkpoint_sequence != row.sequence
            or snapshot.state_hash != row.state_hash
        ):
            raise FactorialPersistenceError(
                "factorial snapshot row binding mismatch"
            )
        checkpoint_ids.add(snapshot.checkpoint_id)
        arms[row.arm_id] = snapshot.to_arm()
    if len(checkpoint_ids) != 1:
        raise FactorialPersistenceError(
            "factorial checkpoint id mismatch"
        )
    return arms


def _group_checkpoint_rows(
    rows: list[ArmStateSnapshotRow],
) -> dict[int, list[ArmStateSnapshotRow]]:
    grouped: defaultdict[int, list[ArmStateSnapshotRow]] = defaultdict(list)
    for row in rows:
        grouped[row.sequence].append(row)
    return dict(grouped)


def _validate_materialized_records(
    arms: dict[str, FactorialPaperArm],
    *,
    run_id: str,
    order_rows: list[OrderIntentRow],
    fill_rows: list[FillRow],
    ledger_rows: list[LedgerTransactionRow],
    ledger_posting_rows: list[LedgerPostingRow],
    historical_orders: dict[str, tuple[str, FactorialOrderState]],
) -> None:
    orders_by_id = {row.order_intent_id: row for row in order_rows}
    fills_by_id = {row.fill_id: row for row in fill_rows}
    ledger_by_id = {
        row.ledger_transaction_id: row for row in ledger_rows
    }
    postings_by_id = {
        row.posting_id: row for row in ledger_posting_rows
    }
    final_fill_ids: set[str] = set()
    final_ledger_ids: set[str] = set()
    final_posting_ids: set[str] = set()
    if set(historical_orders) != set(orders_by_id):
        raise FactorialPersistenceError(
            "factorial order history differs from checkpoint history"
        )
    for order_id, (arm_id, order) in historical_orders.items():
        row = orders_by_id[order_id]
        intent = order.intent
        if (
            row.arm_id != arm_id
            or row.run_id != run_id
            or row.intent_hash != canonical_hash(intent)
            or row.valid_until is None
            or _aware(row.valid_until) != order.valid_until
            or canonical_hash(row.payload_json)
            != canonical_hash(model_payload(intent))
        ):
            raise FactorialPersistenceError(
                "factorial order materialization mismatch"
            )
    for arm in arms.values():
        for pending in arm.pending_orders:
            row = orders_by_id.get(pending.intent.order_intent_id)
            if (
                row is None
                or row.arm_id != arm.contract.arm_id
                or row.intent_hash != canonical_hash(pending.intent)
            ):
                raise FactorialPersistenceError(
                    "factorial pending order materialization mismatch"
                )
        for fill in arm.fills:
            final_fill_ids.add(fill.fill_id)
            row = fills_by_id.get(fill.fill_id)
            order = orders_by_id.get(fill.order_intent_id)
            if (
                row is None
                or order is None
                or row.arm_id != arm.contract.arm_id
                or row.fill_hash != canonical_hash(fill)
            ):
                raise FactorialPersistenceError(
                    "factorial fill materialization mismatch"
                )
        for entry in arm.ledger:
            transaction = entry.transaction
            final_ledger_ids.add(transaction.ledger_transaction_id)
            row = ledger_by_id.get(transaction.ledger_transaction_id)
            if (
                row is None
                or row.arm_id != arm.contract.arm_id
                or canonical_hash(row.payload_json)
                != canonical_hash(model_payload(transaction))
            ):
                raise FactorialPersistenceError(
                    "factorial ledger materialization mismatch"
                )
            for posting in entry.postings:
                final_posting_ids.add(posting.posting_id)
                posting_row = postings_by_id.get(posting.posting_id)
                if (
                    posting_row is None
                    or posting_row.ledger_transaction_id
                    != transaction.ledger_transaction_id
                    or canonical_hash(posting_row.payload_json)
                    != canonical_hash(model_payload(posting))
                ):
                    raise FactorialPersistenceError(
                        "factorial ledger posting materialization mismatch"
                    )
    if final_fill_ids != set(fills_by_id):
        raise FactorialPersistenceError(
            "factorial fill history differs from the latest state"
        )
    if final_ledger_ids != set(ledger_by_id):
        raise FactorialPersistenceError(
            "factorial ledger history differs from the latest state"
        )
    if final_posting_ids != set(postings_by_id):
        raise FactorialPersistenceError(
            "factorial ledger posting history differs from the latest state"
        )


def _validate_nav_records(
    run: RunRow,
    *,
    checkpoints: list[dict[str, FactorialArmSnapshotV1]],
    nav_rows: list[NavSnapshotRow],
) -> None:
    rows_by_id = {row.nav_snapshot_id: row for row in nav_rows}
    expected_ids: set[str] = set()
    for checkpoint in checkpoints:
        for arm_id, snapshot in checkpoint.items():
            nav_id = stable_id(
                "factorial-nav",
                run.run_id,
                snapshot.checkpoint_id,
                arm_id,
            )
            expected_ids.add(nav_id)
            row = rows_by_id.get(nav_id)
            expected_payload = {
                "schema_version": "factorial_nav_snapshot_v1",
                "run_id": run.run_id,
                "arm_id": arm_id,
                "checkpoint_id": snapshot.checkpoint_id,
                "checkpoint_kind": snapshot.checkpoint_kind.value,
                "checkpoint_sequence": snapshot.checkpoint_sequence,
                "as_of": snapshot.created_at.isoformat(),
                "nav_usd": str(snapshot.latest_nav_usd),
                "state_hash": snapshot.state_hash,
                "common_market_manifest_hash": (
                    snapshot.common_market_manifest_hash
                ),
                "forecast_hash": snapshot.forecast_hash,
                "execution_scenario_version": (
                    snapshot.execution_scenario_version
                ),
                "cost_model_version": snapshot.cost_model_version,
                "real_order_routing": False,
            }
            if (
                row is None
                or row.run_id != run.run_id
                or row.arm_id != arm_id
                or row.quote_manifest_hash
                != snapshot.common_market_manifest_hash
                or row.algorithm_version != FACTORIAL_EXPERIMENT_VERSION
                or row.config_manifest_hash != snapshot.config_manifest_hash
                or row.code_version != run.code_commit
                or row.model_version != snapshot.policy_version
                or row.source_manifest_hash
                != snapshot.common_market_manifest_hash
                or _aware(row.as_of) != snapshot.created_at
                or row.nav_usd != snapshot.latest_nav_usd
                or canonical_hash(row.payload_json)
                != canonical_hash(expected_payload)
            ):
                raise FactorialPersistenceError(
                    "factorial NAV materialization mismatch"
                )
    if expected_ids != set(rows_by_id):
        raise FactorialPersistenceError(
            "factorial NAV history differs from checkpoint history"
        )


def _matched_daily_returns(
    checkpoints: list[dict[str, FactorialArmSnapshotV1]],
) -> dict[str, list[float]]:
    returns: dict[str, list[float]] = {
        arm_id: [] for arm_id in FACTORIAL_ARM_IDS
    }
    previous: dict[str, Decimal] | None = None
    for checkpoint in checkpoints:
        if set(checkpoint) != set(FACTORIAL_ARM_IDS):
            raise FactorialPersistenceError(
                "daily attribution checkpoint is incomplete"
            )
        kinds = {item.checkpoint_kind for item in checkpoint.values()}
        if len(kinds) != 1:
            raise FactorialPersistenceError(
                "factorial checkpoint kind mismatch"
            )
        kind = kinds.pop()
        if kind not in {
            FactorialCheckpointKind.INITIAL,
            FactorialCheckpointKind.DAILY_CLOSE,
        }:
            continue
        current = {
            arm_id: checkpoint[arm_id].latest_nav_usd
            for arm_id in FACTORIAL_ARM_IDS
        }
        if previous is not None and kind is FactorialCheckpointKind.DAILY_CLOSE:
            for arm_id in FACTORIAL_ARM_IDS:
                if previous[arm_id] <= 0:
                    raise FactorialPersistenceError(
                        "factorial previous NAV is not positive"
                    )
                returns[arm_id].append(
                    float(current[arm_id] / previous[arm_id] - Decimal("1"))
                )
        previous = current
    lengths = {len(values) for values in returns.values()}
    if len(lengths) != 1:
        raise FactorialPersistenceError(
            "factorial return histories are not matched"
        )
    return returns


def _effect_status(
    *,
    common_sessions: int,
    minimum_common_sessions: int,
    attribution: FactorialAttribution | None,
) -> dict[str, Any]:
    ready = (
        attribution is not None
        and common_sessions >= minimum_common_sessions
    )
    values = (
        None
        if attribution is None
        else {
            "guard_main_effect": attribution.guard_main_effect,
            "ai_main_effect": attribution.ai_main_effect,
            "ai_guard_interaction_effect": (
                attribution.ai_guard_interaction_effect
            ),
        }
    )
    metrics = {
        metric: {
            "ready": ready,
            "preliminary_value": (
                None if values is None else values[metric]
            ),
        }
        for metric in (
            "guard_main_effect",
            "ai_main_effect",
            "ai_guard_interaction_effect",
        )
    }
    return {
        "status": "READY" if ready else "COLLECTING_MATCHED_SESSIONS",
        "ready": ready,
        "common_sessions": common_sessions,
        "minimum_common_sessions": minimum_common_sessions,
        "preliminary_values": values,
        "metrics": metrics,
        "interpretation": (
            "Factorial attribution only; no standalone AI alpha claim."
        ),
    }


def _empty_arm_status() -> dict[str, dict[str, object]]:
    return {
        arm_id: {
            "status": "NOT_INITIALIZED",
            "real_order_routing": False,
        }
        for arm_id in FACTORIAL_ARM_IDS
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
