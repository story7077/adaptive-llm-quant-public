from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.persistence.experiment_outcomes import (
    ExperimentOutcomeRepository,
)
from trading.persistence.models import ResearchActionPlanRow
from trading.research.experiment_outcomes import ResearchActionKind
from trading.research.meta_controller import (
    MetaControllerParametersV1,
    MetaControllerTrainingViewV1,
    ResearchActionPlanV1,
    ResearchContextV1,
    build_meta_controller_training_view,
    build_research_action_plan,
)


class MetaControllerPersistenceError(RuntimeError):
    pass


class MetaControllerRepository:
    """Append-only trusted host storage for deterministic research plans."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._outcomes = ExperimentOutcomeRepository(session_factory)

    def build_training_view(
        self,
        *,
        snapshot_id: str,
    ) -> MetaControllerTrainingViewV1:
        snapshot, events = self._outcomes.memory_snapshot_with_verified_events(
            snapshot_id
        )
        cycle_ids = tuple(
            sorted({event.research_cycle_id for event in events})
        )
        contexts: dict[str, ResearchContextV1] = {}
        if cycle_ids:
            with self._session_factory() as session:
                rows = tuple(
                    session.scalars(
                        select(ResearchActionPlanRow)
                        .where(
                            ResearchActionPlanRow.research_cycle_id.in_(
                                cycle_ids
                            ),
                            ResearchActionPlanRow.generated_at
                            <= snapshot.data_available_cutoff,
                        )
                        .order_by(
                            ResearchActionPlanRow.research_cycle_id
                        )
                    )
                )
                contexts = {
                    row.research_cycle_id: self._plan_from_row(row).context
                    for row in rows
                }
        return build_meta_controller_training_view(
            snapshot=snapshot,
            verified_events=events,
            contexts_by_research_cycle=contexts,
        )

    def build_plan(
        self,
        *,
        research_cycle_id: str,
        snapshot_id: str,
        context: ResearchContextV1,
        parameters: MetaControllerParametersV1,
        config_hash: str,
        available_action_kinds: tuple[ResearchActionKind, ...],
        maximum_total_submissions: int,
        idempotency_key: str,
        generated_at: datetime,
        persist: bool,
    ) -> tuple[ResearchActionPlanV1, bool]:
        snapshot, _ = self._outcomes.memory_snapshot_with_verified_events(
            snapshot_id
        )
        training_view = self.build_training_view(snapshot_id=snapshot_id)
        plan = build_research_action_plan(
            research_cycle_id=research_cycle_id,
            snapshot=snapshot,
            training_view=training_view,
            context=context,
            parameters=parameters,
            config_hash=config_hash,
            available_action_kinds=available_action_kinds,
            maximum_total_submissions=maximum_total_submissions,
            idempotency_key=idempotency_key,
            generated_at=generated_at,
        )
        if not persist:
            return plan, False
        return plan, self.store_plan(plan)

    def store_plan(self, plan: ResearchActionPlanV1) -> bool:
        with self._session_factory.begin() as session:
            self._cycle_lock(session, plan.research_cycle_id)
            existing = session.scalar(
                select(ResearchActionPlanRow).where(
                    or_(
                        ResearchActionPlanRow.action_plan_id
                        == plan.action_plan_id,
                        ResearchActionPlanRow.research_cycle_id
                        == plan.research_cycle_id,
                        ResearchActionPlanRow.idempotency_key
                        == plan.idempotency_key,
                    )
                )
            )
            if existing is not None:
                stored = self._plan_from_row(existing)
                if stored.plan_hash != plan.plan_hash:
                    raise MetaControllerPersistenceError(
                        "research action plan idempotency conflict"
                    )
                return False
            session.add(
                ResearchActionPlanRow(
                    action_plan_id=plan.action_plan_id,
                    research_cycle_id=plan.research_cycle_id,
                    policy_version=plan.policy_version,
                    research_memory_snapshot_hash=(
                        plan.research_memory_snapshot_hash
                    ),
                    training_view_hash=plan.training_view_hash,
                    context_hash=plan.context_hash,
                    config_hash=plan.config_hash,
                    plan_hash=plan.plan_hash,
                    idempotency_key=plan.idempotency_key,
                    payload_json=model_payload(plan),
                    generated_at=plan.generated_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise MetaControllerPersistenceError(
                    "research action plan uniqueness conflict"
                ) from exc
            return True

    def get_plan(self, action_plan_id: str) -> ResearchActionPlanV1 | None:
        with self._session_factory() as session:
            row = session.get(ResearchActionPlanRow, action_plan_id)
            return None if row is None else self._plan_from_row(row)

    def get_plan_for_cycle(
        self,
        research_cycle_id: str,
    ) -> ResearchActionPlanV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchActionPlanRow).where(
                    ResearchActionPlanRow.research_cycle_id
                    == research_cycle_id
                )
            )
            return None if row is None else self._plan_from_row(row)

    def status(self) -> dict[str, Any]:
        with self._session_factory() as session:
            count = session.scalar(
                select(func.count()).select_from(ResearchActionPlanRow)
            )
            return {
                "schema_version": "meta_controller_status_v1",
                "action_plan_count": int(count or 0),
                "automatic_execution_enabled": False,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }

    @staticmethod
    def _plan_from_row(row: ResearchActionPlanRow) -> ResearchActionPlanV1:
        try:
            plan = ResearchActionPlanV1.model_validate(row.payload_json)
        except ValueError as exc:
            raise MetaControllerPersistenceError(
                "stored research action plan is invalid"
            ) from exc
        generated_at = row.generated_at
        if generated_at.tzinfo is None:
            generated_at = generated_at.replace(tzinfo=UTC)
        else:
            generated_at = generated_at.astimezone(UTC)
        if (
            plan.action_plan_id != row.action_plan_id
            or plan.research_cycle_id != row.research_cycle_id
            or plan.policy_version != row.policy_version
            or plan.research_memory_snapshot_hash
            != row.research_memory_snapshot_hash
            or plan.training_view_hash != row.training_view_hash
            or plan.context_hash != row.context_hash
            or plan.config_hash != row.config_hash
            or plan.plan_hash != row.plan_hash
            or plan.idempotency_key != row.idempotency_key
            or plan.generated_at != generated_at
        ):
            raise MetaControllerPersistenceError(
                "stored research action plan binding is invalid"
            )
        return plan

    @staticmethod
    def _cycle_lock(session: Session, research_cycle_id: str) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:cycle_key, 0))"
                ),
                {"cycle_key": f"research-action-plan:{research_cycle_id}"},
            )
