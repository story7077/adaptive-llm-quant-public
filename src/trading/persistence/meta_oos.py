from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    ChronologicalMetaOosPlanRow,
    ChronologicalMetaOosResultRow,
    MetaOosEpochArmAuditRecordRow,
    MetaOosOuterAuditReservationRow,
)
from trading.research.chronological_meta_oos import (
    META_OOS_POLICY_ARMS,
    ChronologicalMetaOosPlanV1,
    ChronologicalMetaOosResultV1,
    ChronologicalMetaOosRunV1,
    MetaOosEvaluationContractV1,
    MetaOosOuterAuditReservationV1,
    build_meta_oos_outer_audit_reservation,
    verify_chronological_meta_oos_plan,
    verify_chronological_meta_oos_result,
)


class MetaOosPersistenceError(RuntimeError):
    """Raised when isolated meta-OOS persistence fails closed."""


class MetaOosRepository:
    """Persistence namespace isolated from the production research ledger."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def store_plan(
        self,
        plan: ChronologicalMetaOosPlanV1,
        evaluation_contract: MetaOosEvaluationContractV1,
    ) -> bool:
        verify_chronological_meta_oos_plan(
            plan=plan,
            evaluation_contract=evaluation_contract,
        )
        with self._session_factory.begin() as session:
            self._lock_dataset(session, plan.outer_audit_dataset_id)
            existing = session.get(ChronologicalMetaOosPlanRow, plan.plan_id)
            if existing is not None:
                if existing.plan_hash != plan.plan_hash:
                    raise MetaOosPersistenceError(
                        "meta-OOS plan identity conflict"
                    )
                self._validate_plan_row(existing, plan)
                return False
            existing_budget = session.scalar(
                select(ChronologicalMetaOosPlanRow).where(
                    ChronologicalMetaOosPlanRow.outer_audit_dataset_id
                    == plan.outer_audit_dataset_id,
                    ChronologicalMetaOosPlanRow.outer_audit_budget_ordinal
                    == plan.outer_audit_budget_ordinal,
                )
            )
            if existing_budget is not None:
                raise MetaOosPersistenceError(
                    "outer-audit dataset budget ordinal already planned"
                )
            session.add(
                ChronologicalMetaOosPlanRow(
                    plan_id=plan.plan_id,
                    plan_version=plan.plan_version,
                    initial_champion_manifest_hash=(
                        plan.initial_champion_manifest_hash
                    ),
                    evaluation_contract_hash=(
                        plan.evaluation_contract_hash
                    ),
                    outer_audit_dataset_id=plan.outer_audit_dataset_id,
                    outer_audit_budget_ordinal=(
                        plan.outer_audit_budget_ordinal
                    ),
                    plan_hash=plan.plan_hash,
                    real_order_routing=False,
                    payload_json=model_payload(plan),
                    created_at=plan.created_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise MetaOosPersistenceError(
                    "meta-OOS plan persistence conflict"
                ) from exc
            return True

    def plan(self, plan_id: str) -> ChronologicalMetaOosPlanV1 | None:
        with self._session_factory() as session:
            row = session.get(ChronologicalMetaOosPlanRow, plan_id)
            if row is None:
                return None
            return self._plan_from_row(row)

    def reserve_outer_audit(
        self,
        *,
        plan_id: str,
        idempotency_key: str,
        maximum_dataset_uses: int,
        maximum_ttl_hours: int,
        created_at: datetime,
        expires_at: datetime,
    ) -> tuple[MetaOosOuterAuditReservationV1, bool]:
        if maximum_dataset_uses < 1:
            raise MetaOosPersistenceError(
                "outer-audit maximum uses must be positive"
            )
        if maximum_ttl_hours < 1:
            raise MetaOosPersistenceError(
                "outer-audit maximum TTL must be positive"
            )
        reservation_created_at = require_aware_utc(created_at)
        reservation_expires_at = require_aware_utc(expires_at)
        if reservation_expires_at > reservation_created_at + timedelta(
            hours=maximum_ttl_hours
        ):
            raise MetaOosPersistenceError(
                "outer-audit reservation TTL exceeds contract"
            )
        with self._session_factory.begin() as session:
            plan_row = session.get(ChronologicalMetaOosPlanRow, plan_id)
            if plan_row is None:
                raise MetaOosPersistenceError("unknown meta-OOS plan")
            plan = self._plan_from_row(plan_row)
            self._lock_dataset(session, plan.outer_audit_dataset_id)
            existing_for_plan = session.scalar(
                select(MetaOosOuterAuditReservationRow).where(
                    MetaOosOuterAuditReservationRow.plan_id == plan.plan_id
                )
            )
            if existing_for_plan is not None:
                reservation = self._reservation_from_row(existing_for_plan)
                if reservation.idempotency_key != idempotency_key:
                    raise MetaOosPersistenceError(
                        "meta-OOS plan already has a different reservation"
                    )
                return reservation, False
            existing_key = session.scalar(
                select(MetaOosOuterAuditReservationRow).where(
                    MetaOosOuterAuditReservationRow.outer_audit_dataset_id
                    == plan.outer_audit_dataset_id,
                    MetaOosOuterAuditReservationRow.idempotency_key
                    == idempotency_key,
                )
            )
            if existing_key is not None:
                raise MetaOosPersistenceError(
                    "outer-audit reservation idempotency conflict"
                )
            used = int(
                session.scalar(
                    select(func.count()).select_from(
                        MetaOosOuterAuditReservationRow
                    ).where(
                        MetaOosOuterAuditReservationRow.outer_audit_dataset_id
                        == plan.outer_audit_dataset_id
                    )
                )
                or 0
            )
            if (
                used >= maximum_dataset_uses
                or plan.outer_audit_budget_ordinal > maximum_dataset_uses
            ):
                raise MetaOosPersistenceError(
                    "outer-audit dataset budget exceeded"
                )
            if plan.outer_audit_budget_ordinal != used + 1:
                raise MetaOosPersistenceError(
                    "outer-audit dataset budget ordinal has a gap"
                )
            reservation = build_meta_oos_outer_audit_reservation(
                plan=plan,
                idempotency_key=idempotency_key,
                created_at=reservation_created_at,
                expires_at=reservation_expires_at,
            )
            session.add(
                MetaOosOuterAuditReservationRow(
                    reservation_id=reservation.reservation_id,
                    plan_id=reservation.plan_id,
                    plan_hash=reservation.plan_hash,
                    outer_audit_dataset_id=(
                        reservation.outer_audit_dataset_id
                    ),
                    outer_audit_budget_ordinal=(
                        reservation.outer_audit_budget_ordinal
                    ),
                    idempotency_key=reservation.idempotency_key,
                    reservation_hash=reservation.reservation_hash,
                    real_order_routing=False,
                    payload_json=model_payload(reservation),
                    created_at=reservation.created_at,
                    expires_at=reservation.expires_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise MetaOosPersistenceError(
                    "outer-audit reservation conflict"
                ) from exc
            return reservation, True

    def reservation(
        self,
        plan_id: str,
    ) -> MetaOosOuterAuditReservationV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(MetaOosOuterAuditReservationRow).where(
                    MetaOosOuterAuditReservationRow.plan_id == plan_id
                )
            )
            if row is None:
                return None
            return self._reservation_from_row(row)

    def store_run(
        self,
        *,
        run: ChronologicalMetaOosRunV1,
        reservation: MetaOosOuterAuditReservationV1,
        evaluation_contract: MetaOosEvaluationContractV1,
        created_at: datetime,
    ) -> bool:
        timestamp = require_aware_utc(created_at)
        result = run.result
        verify_chronological_meta_oos_result(
            plan=self._require_plan(result.plan_id),
            evaluation_contract=evaluation_contract,
            result=result,
        )
        _reject_raw_audit_keys(result.model_dump(mode="python"))
        for record in run.audit_records:
            _reject_raw_audit_keys(record.model_dump(mode="python"))
        with self._session_factory.begin() as session:
            plan_row = session.get(
                ChronologicalMetaOosPlanRow,
                result.plan_id,
            )
            if plan_row is None:
                raise MetaOosPersistenceError("unknown meta-OOS result plan")
            plan = self._plan_from_row(plan_row)
            self._lock_dataset(session, plan.outer_audit_dataset_id)
            reservation_row = session.get(
                MetaOosOuterAuditReservationRow,
                reservation.reservation_id,
            )
            if reservation_row is None:
                raise MetaOosPersistenceError(
                    "meta-OOS result requires an outer reservation"
                )
            stored_reservation = self._reservation_from_row(reservation_row)
            if (
                stored_reservation.reservation_hash
                != reservation.reservation_hash
                or result.outer_audit_reservation_hash
                != reservation.reservation_hash
                or result.plan_hash != plan.plan_hash
                or result.outer_audit_dataset_id
                != plan.outer_audit_dataset_id
                or result.outer_audit_budget_ordinal
                != plan.outer_audit_budget_ordinal
                or result.evaluation_contract_hash
                != plan.evaluation_contract_hash
            ):
                raise MetaOosPersistenceError(
                    "meta-OOS result binding mismatch"
                )
            if (
                result.evaluated_at < reservation.created_at
                or result.evaluated_at > reservation.expires_at
                or timestamp < result.evaluated_at
            ):
                raise MetaOosPersistenceError(
                    "meta-OOS result is outside its reservation window"
                )
            expected_records = len(plan.epochs) * len(META_OOS_POLICY_ARMS)
            if (
                len(run.audit_records) != expected_records
                or tuple(item.record_hash for item in run.audit_records)
                != result.audit_record_hashes
            ):
                raise MetaOosPersistenceError(
                    "meta-OOS epoch audit record set mismatch"
                )
            expected_identities = {
                (epoch.epoch_id, arm)
                for epoch in plan.epochs
                for arm in META_OOS_POLICY_ARMS
            }
            actual_identities = {
                (item.epoch_id, item.arm)
                for item in run.audit_records
            }
            if actual_identities != expected_identities:
                raise MetaOosPersistenceError(
                    "meta-OOS epoch arm matrix is incomplete"
                )
            existing_result = session.scalar(
                select(ChronologicalMetaOosResultRow).where(
                    ChronologicalMetaOosResultRow.plan_id == plan.plan_id
                )
            )
            if existing_result is not None:
                if existing_result.result_hash != result.result_hash:
                    raise MetaOosPersistenceError(
                        "meta-OOS result hash conflict"
                    )
                return False
            existing_records = tuple(
                session.scalars(
                    select(MetaOosEpochArmAuditRecordRow).where(
                        MetaOosEpochArmAuditRecordRow.plan_id
                        == plan.plan_id
                    )
                )
            )
            if existing_records:
                raise MetaOosPersistenceError(
                    "partial meta-OOS epoch record set exists"
                )
            for record in run.audit_records:
                session.add(
                    MetaOosEpochArmAuditRecordRow(
                        record_id=record.record_id,
                        plan_id=record.plan_id,
                        epoch_id=record.epoch_id,
                        arm=record.arm.value,
                        decision_hash=record.decision_hash,
                        memory_snapshot_hash=(
                            record.memory_snapshot_hash
                        ),
                        private_outcome_hash=record.private_outcome_hash,
                        record_hash=record.record_hash,
                        real_order_routing=False,
                        payload_json=model_payload(record),
                        created_at=record.created_at,
                    )
                )
            session.add(
                ChronologicalMetaOosResultRow(
                    result_id=result.result_id,
                    plan_id=result.plan_id,
                    reservation_id=reservation.reservation_id,
                    evaluation_contract_hash=(
                        result.evaluation_contract_hash
                    ),
                    adaptive_system_pass=result.adaptive_system_pass,
                    result_hash=result.result_hash,
                    real_order_routing=False,
                    payload_json=model_payload(result),
                    evaluated_at=result.evaluated_at,
                    created_at=timestamp,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise MetaOosPersistenceError(
                    "meta-OOS result persistence conflict"
                ) from exc
            return True

    def _require_plan(
        self,
        plan_id: str,
    ) -> ChronologicalMetaOosPlanV1:
        plan = self.plan(plan_id)
        if plan is None:
            raise MetaOosPersistenceError("unknown meta-OOS result plan")
        return plan

    def result(
        self,
        plan_id: str,
    ) -> ChronologicalMetaOosResultV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ChronologicalMetaOosResultRow).where(
                    ChronologicalMetaOosResultRow.plan_id == plan_id
                )
            )
            if row is None:
                return None
            try:
                return ChronologicalMetaOosResultV1.model_validate(
                    row.payload_json
                )
            except ValueError as exc:
                raise MetaOosPersistenceError(
                    "stored meta-OOS result is invalid"
                ) from exc

    def status(self) -> dict[str, object]:
        with self._session_factory() as session:
            plan_count = int(
                session.scalar(
                    select(func.count()).select_from(
                        ChronologicalMetaOosPlanRow
                    )
                )
                or 0
            )
            reservation_count = int(
                session.scalar(
                    select(func.count()).select_from(
                        MetaOosOuterAuditReservationRow
                    )
                )
                or 0
            )
            result_count = int(
                session.scalar(
                    select(func.count()).select_from(
                        ChronologicalMetaOosResultRow
                    )
                )
                or 0
            )
            latest = session.scalar(
                select(ChronologicalMetaOosResultRow)
                .order_by(
                    ChronologicalMetaOosResultRow.evaluated_at.desc(),
                    ChronologicalMetaOosResultRow.result_id.desc(),
                )
                .limit(1)
            )
            return {
                "schema_version": "meta_oos_ledger_status_v1",
                "plan_count": plan_count,
                "reservation_count": reservation_count,
                "result_count": result_count,
                "latest_result_id": (
                    None if latest is None else latest.result_id
                ),
                "latest_adaptive_system_pass": (
                    None
                    if latest is None
                    else latest.adaptive_system_pass
                ),
                "checked_at": datetime.now(UTC).isoformat(),
                "real_order_routing": False,
            }

    @staticmethod
    def _lock_dataset(session: Session, dataset_id: str) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                {"scope": f"meta-oos-dataset:{dataset_id}"},
            )

    @staticmethod
    def _validate_plan_row(
        row: ChronologicalMetaOosPlanRow,
        plan: ChronologicalMetaOosPlanV1,
    ) -> None:
        if (
            row.plan_id != plan.plan_id
            or row.plan_hash != plan.plan_hash
            or row.plan_version != plan.plan_version
            or row.initial_champion_manifest_hash
            != plan.initial_champion_manifest_hash
            or row.evaluation_contract_hash
            != plan.evaluation_contract_hash
            or row.outer_audit_dataset_id
            != plan.outer_audit_dataset_id
            or row.outer_audit_budget_ordinal
            != plan.outer_audit_budget_ordinal
            or row.real_order_routing
        ):
            raise MetaOosPersistenceError(
                "stored meta-OOS plan columns do not match payload"
            )

    @classmethod
    def _plan_from_row(
        cls,
        row: ChronologicalMetaOosPlanRow,
    ) -> ChronologicalMetaOosPlanV1:
        try:
            plan = ChronologicalMetaOosPlanV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise MetaOosPersistenceError(
                "stored meta-OOS plan is invalid"
            ) from exc
        cls._validate_plan_row(row, plan)
        return plan

    @staticmethod
    def _reservation_from_row(
        row: MetaOosOuterAuditReservationRow,
    ) -> MetaOosOuterAuditReservationV1:
        try:
            reservation = MetaOosOuterAuditReservationV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise MetaOosPersistenceError(
                "stored meta-OOS reservation is invalid"
            ) from exc
        if (
            row.reservation_id != reservation.reservation_id
            or row.plan_id != reservation.plan_id
            or row.plan_hash != reservation.plan_hash
            or row.outer_audit_dataset_id
            != reservation.outer_audit_dataset_id
            or row.outer_audit_budget_ordinal
            != reservation.outer_audit_budget_ordinal
            or row.idempotency_key != reservation.idempotency_key
            or row.reservation_hash != reservation.reservation_hash
            or row.real_order_routing
        ):
            raise MetaOosPersistenceError(
                "stored meta-OOS reservation columns do not match payload"
            )
        return reservation


FORBIDDEN_RAW_AUDIT_KEYS = {
    "daily_returns",
    "trade_returns",
    "session_key",
    "session_keys",
    "private_observation",
    "private_observations",
    "bootstrap_samples",
    "raw_returns",
}


def _reject_raw_audit_keys(value: object) -> None:
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        for key, nested in mapping.items():
            if str(key).lower() in FORBIDDEN_RAW_AUDIT_KEYS:
                raise MetaOosPersistenceError(
                    "raw meta-OOS evidence cannot be persisted"
                )
            _reject_raw_audit_keys(nested)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        sequence = cast(Sequence[object], value)
        for item in sequence:
            _reject_raw_audit_keys(item)


__all__ = [
    "FORBIDDEN_RAW_AUDIT_KEYS",
    "MetaOosPersistenceError",
    "MetaOosRepository",
]
