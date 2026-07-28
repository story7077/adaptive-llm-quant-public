from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    ResearchExperimentActionRow,
    ResearchExperimentOutcomeEventRow,
    ResearchMemorySnapshotRow,
)
from trading.research.experiment_outcomes import (
    ExperimentMaturityStatus,
    ExperimentOutcomeEventKind,
    ExperimentOutcomeEventV1,
    ExperimentOutcomeMaturationInputV1,
    ResearchExperimentActionV1,
    ResearchMemorySnapshotV1,
    build_outcome_event,
    build_research_memory_snapshot_from_verified_events,
)


class ExperimentOutcomePersistenceError(RuntimeError):
    pass


def _effective_events(
    chain: tuple[ExperimentOutcomeEventV1, ...],
) -> tuple[ExperimentOutcomeEventV1, ...]:
    superseded_ids = {
        event.supersedes_event_id
        for event in chain
        if event.supersedes_event_id is not None
    }
    return tuple(
        event for event in chain if event.event_id not in superseded_ids
    )


def _is_terminal_outcome(event: ExperimentOutcomeEventV1) -> bool:
    return (
        _has_economic_values(event)
        or event.maturity_status
        in {
            ExperimentMaturityStatus.CENSORED,
            ExperimentMaturityStatus.INVALIDATED,
            ExperimentMaturityStatus.SUPERSEDED,
        }
        or event.technical_success is False
    )


class ExperimentOutcomeRepository:
    """Trusted append-only persistence for recursive-research outcomes."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def register_action(self, action: ResearchExperimentActionV1) -> bool:
        with self._session_factory.begin() as session:
            self._experiment_lock(session, action.experiment_id)
            existing = session.scalar(
                select(ResearchExperimentActionRow).where(
                    or_(
                        ResearchExperimentActionRow.action_id == action.action_id,
                        ResearchExperimentActionRow.experiment_id
                        == action.experiment_id,
                        ResearchExperimentActionRow.idempotency_key
                        == action.idempotency_key,
                    )
                )
            )
            if existing is not None:
                stored = self._action_from_row(existing)
                if stored.action_hash != action.action_hash:
                    raise ExperimentOutcomePersistenceError(
                        "experiment action idempotency conflict"
                    )
                return False
            session.add(
                ResearchExperimentActionRow(
                    action_id=action.action_id,
                    experiment_id=action.experiment_id,
                    research_cycle_id=action.research_cycle_id,
                    proposal_id=action.proposal_id,
                    challenger_id=action.challenger_id,
                    information_role=action.information_role.value,
                    primary_action_kind=action.primary_action_kind.value,
                    maturity_due_at=action.maturity_due_at,
                    meta_training_permitted=action.meta_training_permitted,
                    idempotency_key=action.idempotency_key,
                    action_hash=action.action_hash,
                    payload_json=model_payload(action),
                    created_at=action.created_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ExperimentOutcomePersistenceError(
                    "experiment action uniqueness conflict"
                ) from exc
            return True

    def get_action(
        self,
        experiment_id: str,
    ) -> ResearchExperimentActionV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchExperimentActionRow).where(
                    ResearchExperimentActionRow.experiment_id == experiment_id
                )
            )
            return None if row is None else self._action_from_row(row)

    def due_experiments(
        self,
        *,
        as_of: datetime,
    ) -> tuple[ResearchExperimentActionV1, ...]:
        instant = require_aware_utc(as_of)
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(ResearchExperimentActionRow).order_by(
                        ResearchExperimentActionRow.maturity_due_at,
                        ResearchExperimentActionRow.experiment_id,
                    )
                )
            )
            due: list[ResearchExperimentActionV1] = []
            for row in rows:
                action = self._action_from_row(row)
                if (
                    action.created_at > instant
                    or action.maturity_due_at > instant
                ):
                    continue
                chain = self._verified_chain(
                    session,
                    action.experiment_id,
                    created_at_cutoff=instant,
                )
                if not chain:
                    due.append(action)
                    continue
                if not any(
                    _is_terminal_outcome(event)
                    for event in _effective_events(chain)
                ):
                    due.append(action)
            return tuple(due)

    def prepare_outcome(
        self,
        maturation: ExperimentOutcomeMaturationInputV1,
    ) -> tuple[ExperimentOutcomeEventV1, bool]:
        """Validate and build an event without writing it.

        The boolean is true when an identical event already exists.
        """

        with self._session_factory() as session:
            action = self._require_action(session, maturation.experiment_id)
            chain = self._verified_chain(session, maturation.experiment_id)
            existing = self._matching_idempotency_event(
                chain,
                maturation=maturation,
            )
            if existing is not None:
                return existing, True
            self._validate_supersession(chain, maturation)
            self._validate_outcome_transition(chain, maturation)
            event = build_outcome_event(
                action=action,
                maturation=maturation,
                previous_event=None if not chain else chain[-1],
            )
            return event, False

    def append_outcome(
        self,
        maturation: ExperimentOutcomeMaturationInputV1,
    ) -> tuple[ExperimentOutcomeEventV1, bool]:
        with self._session_factory.begin() as session:
            self._experiment_lock(session, maturation.experiment_id)
            action = self._require_action(session, maturation.experiment_id)
            chain = self._verified_chain(session, maturation.experiment_id)
            existing = self._matching_idempotency_event(
                chain,
                maturation=maturation,
            )
            if existing is not None:
                return existing, False
            self._validate_supersession(chain, maturation)
            self._validate_outcome_transition(chain, maturation)
            event = build_outcome_event(
                action=action,
                maturation=maturation,
                previous_event=None if not chain else chain[-1],
            )
            session.add(
                ResearchExperimentOutcomeEventRow(
                    event_id=event.event_id,
                    action_id=action.action_id,
                    experiment_id=event.experiment_id,
                    research_cycle_id=event.research_cycle_id,
                    proposal_id=event.proposal_id,
                    challenger_id=event.challenger_id,
                    information_role=event.information_role.value,
                    primary_action_kind=event.primary_action_kind.value,
                    event_kind=event.event_kind.value,
                    experiment_stage=event.experiment_stage.value,
                    event_sequence=event.event_sequence,
                    available_at=event.available_at,
                    maturity_due_at=event.maturity_due_at,
                    maturity_status=event.maturity_status.value,
                    eligible_for_meta_training=(
                        event.eligible_for_meta_training
                    ),
                    previous_event_hash=event.previous_event_hash,
                    supersedes_event_id=event.supersedes_event_id,
                    idempotency_key=event.idempotency_key,
                    maturation_input_hash=event.maturation_input_hash,
                    event_hash=event.event_hash,
                    payload_json=model_payload(event),
                    created_at=event.created_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome uniqueness conflict"
                ) from exc
            return event, True

    def event_chain(
        self,
        experiment_id: str,
    ) -> tuple[ExperimentOutcomeEventV1, ...]:
        with self._session_factory() as session:
            self._require_action(session, experiment_id)
            return self._verified_chain(session, experiment_id)

    def verify_all_chains(
        self,
        *,
        as_of: datetime | None = None,
    ) -> tuple[str, ...]:
        cutoff = None if as_of is None else require_aware_utc(as_of)
        with self._session_factory() as session:
            experiment_ids = tuple(
                session.scalars(
                    select(ResearchExperimentActionRow.experiment_id).order_by(
                        ResearchExperimentActionRow.experiment_id
                    )
                )
            )
            hashes: list[str] = []
            for experiment_id in experiment_ids:
                chain = self._verified_chain(
                    session,
                    experiment_id,
                    created_at_cutoff=cutoff,
                )
                hashes.extend(event.event_hash for event in chain)
            return tuple(hashes)

    def materialize_memory(
        self,
        *,
        as_of: datetime,
        data_available_cutoff: datetime,
        created_at: datetime,
        persist: bool,
    ) -> tuple[ResearchMemorySnapshotV1, bool]:
        instant = require_aware_utc(as_of)
        cutoff = require_aware_utc(data_available_cutoff)
        created = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            action_rows = tuple(
                session.scalars(
                    select(ResearchExperimentActionRow).order_by(
                        ResearchExperimentActionRow.experiment_id
                    )
                )
            )
            actions = {
                row.experiment_id: self._action_from_row(row)
                for row in action_rows
            }
            event_rows = tuple(
                session.scalars(
                    select(ResearchExperimentOutcomeEventRow)
                    .where(
                        ResearchExperimentOutcomeEventRow.created_at <= instant
                    )
                    .order_by(
                        ResearchExperimentOutcomeEventRow.experiment_id,
                        ResearchExperimentOutcomeEventRow.event_sequence,
                    )
                )
            )
            events = self._verified_event_rows(actions=actions, rows=event_rows)
            snapshot = build_research_memory_snapshot_from_verified_events(
                events=events,
                as_of=instant,
                data_available_cutoff=cutoff,
                created_at=created,
            )
            if not persist:
                return snapshot, False
            existing = session.get(ResearchMemorySnapshotRow, snapshot.snapshot_id)
            if existing is not None:
                stored = self._snapshot_from_row(existing)
                if stored.snapshot_hash != snapshot.snapshot_hash:
                    raise ExperimentOutcomePersistenceError(
                        "research memory snapshot identity conflict"
                    )
                return stored, False
            same_hash = session.scalar(
                select(ResearchMemorySnapshotRow).where(
                    ResearchMemorySnapshotRow.snapshot_hash
                    == snapshot.snapshot_hash
                )
            )
            if same_hash is not None:
                return self._snapshot_from_row(same_hash), False
            session.add(
                ResearchMemorySnapshotRow(
                    snapshot_id=snapshot.snapshot_id,
                    as_of=snapshot.as_of,
                    data_available_cutoff=snapshot.data_available_cutoff,
                    snapshot_hash=snapshot.snapshot_hash,
                    payload_json=model_payload(snapshot),
                    created_at=snapshot.created_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ExperimentOutcomePersistenceError(
                    "research memory snapshot uniqueness conflict"
                ) from exc
            return snapshot, True

    def get_memory_snapshot(
        self,
        snapshot_id: str,
    ) -> ResearchMemorySnapshotV1 | None:
        with self._session_factory() as session:
            row = session.get(ResearchMemorySnapshotRow, snapshot_id)
            return None if row is None else self._snapshot_from_row(row)

    def memory_snapshot_with_verified_events(
        self,
        snapshot_id: str,
    ) -> tuple[
        ResearchMemorySnapshotV1,
        tuple[ExperimentOutcomeEventV1, ...],
    ]:
        """Resolve an immutable snapshot to its exact verified event prefix."""

        with self._session_factory() as session:
            row = session.get(ResearchMemorySnapshotRow, snapshot_id)
            if row is None:
                raise ExperimentOutcomePersistenceError(
                    "unknown research memory snapshot"
                )
            snapshot = self._snapshot_from_row(row)
            action_rows = tuple(
                session.scalars(
                    select(ResearchExperimentActionRow).order_by(
                        ResearchExperimentActionRow.experiment_id
                    )
                )
            )
            actions = {
                action_row.experiment_id: self._action_from_row(action_row)
                for action_row in action_rows
            }
            event_rows = tuple(
                session.scalars(
                    select(ResearchExperimentOutcomeEventRow)
                    .where(
                        ResearchExperimentOutcomeEventRow.created_at
                        <= snapshot.as_of
                    )
                    .order_by(
                        ResearchExperimentOutcomeEventRow.experiment_id,
                        ResearchExperimentOutcomeEventRow.event_sequence,
                    )
                )
            )
            verified = self._verified_event_rows(
                actions=actions,
                rows=event_rows,
            )
            by_hash = {event.event_hash: event for event in verified}
            if any(
                event_hash not in by_hash
                for event_hash in snapshot.included_event_hashes
            ):
                raise ExperimentOutcomePersistenceError(
                    "research memory snapshot references an unknown event"
                )
            events = tuple(
                by_hash[event_hash]
                for event_hash in snapshot.included_event_hashes
            )
            if any(
                event.available_at > snapshot.data_available_cutoff
                for event in events
            ):
                raise ExperimentOutcomePersistenceError(
                    "research memory snapshot contains future event data"
                )
            return snapshot, events

    def status(self) -> dict[str, Any]:
        with self._session_factory() as session:
            action_count = session.scalar(
                select(func.count()).select_from(ResearchExperimentActionRow)
            )
            event_count = session.scalar(
                select(func.count()).select_from(
                    ResearchExperimentOutcomeEventRow
                )
            )
            experiment_ids = tuple(
                session.scalars(
                    select(
                        ResearchExperimentActionRow.experiment_id
                    ).order_by(ResearchExperimentActionRow.experiment_id)
                )
            )
            verified_events = tuple(
                event
                for experiment_id in experiment_ids
                for event in self._verified_chain(session, experiment_id)
            )
            superseded_event_ids = {
                event.supersedes_event_id
                for event in verified_events
                if event.supersedes_event_id is not None
            }
            effective_events = tuple(
                event
                for event in verified_events
                if event.event_id not in superseded_event_ids
            )
            eligible_count = sum(
                event.eligible_for_meta_training
                for event in effective_events
            )
            snapshot_count = session.scalar(
                select(func.count()).select_from(ResearchMemorySnapshotRow)
            )
            latest = session.scalar(
                select(ResearchMemorySnapshotRow)
                .order_by(
                    desc(ResearchMemorySnapshotRow.as_of),
                    desc(ResearchMemorySnapshotRow.created_at),
                )
                .limit(1)
            )
        return {
            "action_count": int(action_count or 0),
            "event_count": int(event_count or 0),
            "effective_unsuperseded_event_count": len(effective_events),
            "superseded_event_count": len(superseded_event_ids),
            "eligible_learning_forward_event_count": eligible_count,
            "effective_eligible_learning_forward_event_count": eligible_count,
            "snapshot_count": int(snapshot_count or 0),
            "latest_snapshot": (
                None
                if latest is None
                else self._snapshot_from_row(latest).model_dump(mode="json")
            ),
            "real_order_routing": False,
        }

    @staticmethod
    def _require_action(
        session: Session,
        experiment_id: str,
    ) -> ResearchExperimentActionV1:
        row = session.scalar(
            select(ResearchExperimentActionRow).where(
                ResearchExperimentActionRow.experiment_id == experiment_id
            )
        )
        if row is None:
            raise ExperimentOutcomePersistenceError(
                f"unknown research experiment: {experiment_id}"
            )
        return ExperimentOutcomeRepository._action_from_row(row)

    @staticmethod
    def _verified_chain(
        session: Session,
        experiment_id: str,
        *,
        created_at_cutoff: datetime | None = None,
    ) -> tuple[ExperimentOutcomeEventV1, ...]:
        statement = (
            select(ResearchExperimentOutcomeEventRow)
            .where(
                ResearchExperimentOutcomeEventRow.experiment_id
                == experiment_id
            )
            .order_by(ResearchExperimentOutcomeEventRow.event_sequence)
        )
        rows = tuple(session.scalars(statement))
        action = ExperimentOutcomeRepository._require_action(
            session,
            experiment_id,
        )
        events: list[ExperimentOutcomeEventV1] = []
        previous_hash: str | None = None
        previous_created_at: datetime | None = None
        for row in rows:
            event = ExperimentOutcomeRepository._event_from_row(row)
            ExperimentOutcomeRepository._validate_event_action_binding(
                action=action,
                event=event,
                action_id=row.action_id,
            )
            if (
                created_at_cutoff is not None
                and event.created_at > created_at_cutoff
            ):
                continue
            expected_sequence = len(events) + 1
            if event.event_sequence != expected_sequence:
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome event sequence gap"
                )
            if event.previous_event_hash != previous_hash:
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome previous hash mismatch"
                )
            if (
                previous_created_at is not None
                and event.created_at < previous_created_at
            ):
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome creation time regressed"
                )
            events.append(event)
            previous_hash = event.event_hash
            previous_created_at = event.created_at
        event_ids = {event.event_id for event in events}
        if any(
            event.supersedes_event_id is not None
            and event.supersedes_event_id not in event_ids
            for event in events
        ):
            raise ExperimentOutcomePersistenceError(
                "experiment outcome supersedes an unknown event"
            )
        return tuple(events)

    @staticmethod
    def _verified_event_rows(
        *,
        actions: dict[str, ResearchExperimentActionV1],
        rows: tuple[ResearchExperimentOutcomeEventRow, ...],
    ) -> tuple[ExperimentOutcomeEventV1, ...]:
        events: list[ExperimentOutcomeEventV1] = []
        chain_state: dict[str, tuple[int, str | None, datetime | None]] = {}
        event_ids: dict[str, set[str]] = {}
        supersessions: list[tuple[str, str]] = []
        for row in rows:
            action = actions.get(row.experiment_id)
            if action is None:
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome references an unknown action"
                )
            event = ExperimentOutcomeRepository._event_from_row(row)
            ExperimentOutcomeRepository._validate_event_action_binding(
                action=action,
                event=event,
                action_id=row.action_id,
            )
            sequence, previous_hash, previous_created_at = chain_state.get(
                event.experiment_id,
                (0, None, None),
            )
            if event.event_sequence != sequence + 1:
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome event sequence gap"
                )
            if event.previous_event_hash != previous_hash:
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome previous hash mismatch"
                )
            if (
                previous_created_at is not None
                and event.created_at < previous_created_at
            ):
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome creation time regressed"
                )
            chain_state[event.experiment_id] = (
                event.event_sequence,
                event.event_hash,
                event.created_at,
            )
            event_ids.setdefault(event.experiment_id, set()).add(event.event_id)
            if event.supersedes_event_id is not None:
                supersessions.append(
                    (event.experiment_id, event.supersedes_event_id)
                )
            events.append(event)
        if any(
            superseded_event_id not in event_ids.get(experiment_id, set())
            for experiment_id, superseded_event_id in supersessions
        ):
            raise ExperimentOutcomePersistenceError(
                "experiment outcome supersedes an unknown event"
            )
        return tuple(events)

    @staticmethod
    def _matching_idempotency_event(
        chain: tuple[ExperimentOutcomeEventV1, ...],
        *,
        maturation: ExperimentOutcomeMaturationInputV1,
    ) -> ExperimentOutcomeEventV1 | None:
        for event in chain:
            if event.idempotency_key != maturation.idempotency_key:
                continue
            if event.maturation_input_hash != canonical_hash(maturation):
                raise ExperimentOutcomePersistenceError(
                    "experiment outcome idempotency conflict"
                )
            return event
        return None

    @staticmethod
    def _validate_supersession(
        chain: tuple[ExperimentOutcomeEventV1, ...],
        maturation: ExperimentOutcomeMaturationInputV1,
    ) -> None:
        supersedes_event_id = maturation.supersedes_event_id
        if supersedes_event_id is None:
            return
        targets = tuple(
            event for event in chain if event.event_id == supersedes_event_id
        )
        if not targets:
            raise ExperimentOutcomePersistenceError(
                "superseded outcome event does not exist"
            )
        if any(
            event.supersedes_event_id == supersedes_event_id
            for event in chain
        ):
            raise ExperimentOutcomePersistenceError(
                "outcome event was already superseded"
            )
        if maturation.available_at < targets[0].available_at:
            raise ExperimentOutcomePersistenceError(
                "correction availability cannot predate the superseded outcome"
            )

    @staticmethod
    def _validate_outcome_transition(
        chain: tuple[ExperimentOutcomeEventV1, ...],
        maturation: ExperimentOutcomeMaturationInputV1,
    ) -> None:
        active = _effective_events(chain)
        active_terminal = tuple(
            event
            for event in active
            if _is_terminal_outcome(event)
        )
        if active_terminal and (
            maturation.event_kind
            is not ExperimentOutcomeEventKind.OUTCOME_CORRECTED
            or len(active_terminal) != 1
            or maturation.supersedes_event_id
            != active_terminal[0].event_id
        ):
            raise ExperimentOutcomePersistenceError(
                "an effective terminal outcome may be replaced only by "
                "an explicit correction of that outcome"
            )
        if (
            chain
            and maturation.event_kind
            is ExperimentOutcomeEventKind.EXPERIMENT_REGISTERED
        ):
            raise ExperimentOutcomePersistenceError(
                "EXPERIMENT_REGISTERED is permitted only as the first event"
            )

    @staticmethod
    def _action_from_row(
        row: ResearchExperimentActionRow,
    ) -> ResearchExperimentActionV1:
        try:
            action = ResearchExperimentActionV1.model_validate(row.payload_json)
        except ValueError as exc:
            raise ExperimentOutcomePersistenceError(
                "stored experiment action payload is invalid"
            ) from exc
        if (
            action.action_id != row.action_id
            or action.experiment_id != row.experiment_id
            or action.research_cycle_id != row.research_cycle_id
            or action.proposal_id != row.proposal_id
            or action.challenger_id != row.challenger_id
            or action.action_hash != row.action_hash
            or action.idempotency_key != row.idempotency_key
            or action.information_role.value != row.information_role
            or action.primary_action_kind.value != row.primary_action_kind
            or action.maturity_due_at != _row_time(row.maturity_due_at)
            or (
                action.meta_training_permitted
                is not row.meta_training_permitted
            )
            or action.created_at != _row_time(row.created_at)
        ):
            raise ExperimentOutcomePersistenceError(
                "stored experiment action binding is invalid"
            )
        return action

    @staticmethod
    def _event_from_row(
        row: ResearchExperimentOutcomeEventRow,
    ) -> ExperimentOutcomeEventV1:
        try:
            event = ExperimentOutcomeEventV1.model_validate(row.payload_json)
        except ValueError as exc:
            raise ExperimentOutcomePersistenceError(
                "stored experiment outcome payload is invalid"
            ) from exc
        if (
            event.event_id != row.event_id
            or event.experiment_id != row.experiment_id
            or event.event_sequence != row.event_sequence
            or event.event_hash != row.event_hash
            or event.previous_event_hash != row.previous_event_hash
            or event.supersedes_event_id != row.supersedes_event_id
            or event.idempotency_key != row.idempotency_key
            or event.maturation_input_hash != row.maturation_input_hash
            or event.maturity_status.value != row.maturity_status
            or event.information_role.value != row.information_role
            or event.primary_action_kind.value != row.primary_action_kind
            or event.research_cycle_id != row.research_cycle_id
            or event.proposal_id != row.proposal_id
            or event.challenger_id != row.challenger_id
            or event.event_kind.value != row.event_kind
            or event.experiment_stage.value != row.experiment_stage
            or event.available_at != _row_time(row.available_at)
            or event.maturity_due_at != _row_time(row.maturity_due_at)
            or (
                event.eligible_for_meta_training
                is not row.eligible_for_meta_training
            )
            or event.created_at != _row_time(row.created_at)
        ):
            raise ExperimentOutcomePersistenceError(
                "stored experiment outcome binding is invalid"
            )
        return event

    @staticmethod
    def _snapshot_from_row(
        row: ResearchMemorySnapshotRow,
    ) -> ResearchMemorySnapshotV1:
        try:
            snapshot = ResearchMemorySnapshotV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ExperimentOutcomePersistenceError(
                "stored research memory snapshot is invalid"
            ) from exc
        if (
            snapshot.snapshot_id != row.snapshot_id
            or snapshot.snapshot_hash != row.snapshot_hash
            or snapshot.as_of != _row_time(row.as_of)
            or snapshot.data_available_cutoff
            != _row_time(row.data_available_cutoff)
            or snapshot.created_at != _row_time(row.created_at)
        ):
            raise ExperimentOutcomePersistenceError(
                "stored research memory snapshot binding is invalid"
            )
        return snapshot

    @staticmethod
    def _validate_event_action_binding(
        *,
        action: ResearchExperimentActionV1,
        event: ExperimentOutcomeEventV1,
        action_id: str,
    ) -> None:
        if (
            action_id != action.action_id
            or event.experiment_id != action.experiment_id
            or event.research_cycle_id != action.research_cycle_id
            or event.proposal_id != action.proposal_id
            or event.challenger_id != action.challenger_id
            or event.parent_strategy_id != action.parent_strategy_id
            or (
                event.parent_strategy_version
                != action.parent_strategy_version
            )
            or (
                event.candidate_strategy_version
                != action.candidate_strategy_version
            )
            or event.primary_action_kind is not action.primary_action_kind
            or event.secondary_action_kinds != action.secondary_action_kinds
            or event.mechanism_tags != action.mechanism_tags
            or event.information_role is not action.information_role
            or event.decision_at != action.decision_at
            or event.maturity_due_at != action.maturity_due_at
            or event.complexity_delta != action.complexity_delta
            or (
                event.predicted_delta_sharpe_lower
                != action.predicted_delta_sharpe_lower
            )
            or (
                event.predicted_delta_sharpe_median
                != action.predicted_delta_sharpe_median
            )
            or (
                event.predicted_delta_sharpe_upper
                != action.predicted_delta_sharpe_upper
            )
            or (
                event.candidate_artifact_hash
                != action.candidate_artifact_hash
            )
            or (
                event.evaluation_contract_hash
                != action.evaluation_contract_hash
            )
        ):
            raise ExperimentOutcomePersistenceError(
                "experiment outcome is not bound to its registered action"
            )

    @staticmethod
    def _experiment_lock(session: Session, experiment_id: str) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:experiment_key, 0))"
                ),
                {"experiment_key": f"research-experiment:{experiment_id}"},
            )


def research_memory_snapshot_payload(
    snapshot: ResearchMemorySnapshotV1,
) -> dict[str, Any]:
    """Return the point-in-time aggregate payload for a future Request V2."""

    return model_payload(snapshot)


def _row_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _has_economic_values(
    value: ExperimentOutcomeEventV1 | ExperimentOutcomeMaturationInputV1,
) -> bool:
    return any(
        item is not None
        for item in (
            value.portfolio_delta_sharpe_point,
            value.portfolio_delta_sharpe_lcb,
            value.portfolio_delta_sharpe_ucb,
            value.worst_cost_delta_sharpe_lcb,
            value.drawdown_delta,
            value.tail_loss_delta,
            value.turnover_delta,
            value.cost_delta_bps,
        )
    )
