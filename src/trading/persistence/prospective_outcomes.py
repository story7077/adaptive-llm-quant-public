from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash
from trading.persistence.models import (
    MarketBarRow,
    MarketCalendarSessionRow,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveOutcomeFailureRow,
    ResearchCandidateProspectiveOutcomeRow,
    ResearchCandidateProspectiveRequestRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.prospective import (
    ProspectiveCandidateRepository,
    ProspectivePersistenceError,
)
from trading.research.prospective import (
    ProspectiveExecutionEvidenceV1,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
)
from trading.research.prospective_outcomes import (
    ProspectiveOutcomeEvidenceV1,
    ProspectiveOutcomeFailureV1,
)


class ProspectiveOutcomePersistenceError(RuntimeError):
    """Raised when immutable forward outcomes fail a binding check."""


@dataclass(frozen=True, slots=True)
class PendingProspectiveOutcome:
    request: ProspectiveRequestEvidenceV1
    execution: ProspectiveExecutionEvidenceV1


class ProspectiveOutcomeRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def database_clock(self) -> datetime:
        with self._session_factory() as session:
            value = session.scalar(
                select(
                    func.clock_timestamp()
                    if session.get_bind().dialect.name == "postgresql"
                    else func.current_timestamp()
                )
            )
        if value is None:
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome database clock is unavailable"
            )
        return _utc(value)

    def next_pending(
        self,
        *,
        challenger_id: str,
    ) -> PendingProspectiveOutcome | None:
        outcome_exists = exists(
            select(ResearchCandidateProspectiveOutcomeRow.outcome_id).where(
                ResearchCandidateProspectiveOutcomeRow.prospective_request_id
                == ResearchCandidateProspectiveRequestRow.prospective_request_id
            )
        )
        failure_exists = exists(
            select(
                ResearchCandidateProspectiveOutcomeFailureRow.failure_id
            ).where(
                ResearchCandidateProspectiveOutcomeFailureRow.prospective_request_id
                == ResearchCandidateProspectiveRequestRow.prospective_request_id
            )
        )
        with self._session_factory() as session:
            row = session.execute(
                select(
                    ResearchCandidateProspectiveRequestRow,
                    ResearchCandidateProspectiveExecutionRow,
                )
                .join(
                    ResearchCandidateProspectiveExecutionRow,
                    ResearchCandidateProspectiveExecutionRow.prospective_request_id
                    == ResearchCandidateProspectiveRequestRow.prospective_request_id,
                )
                .where(
                    ResearchCandidateProspectiveRequestRow.challenger_id
                    == challenger_id,
                    ResearchCandidateProspectiveExecutionRow.status
                    == ProspectiveExecutionStatus.SUCCEEDED,
                    ~outcome_exists,
                    ~failure_exists,
                )
                .order_by(
                    ResearchCandidateProspectiveRequestRow.parent_scheduled_at,
                    ResearchCandidateProspectiveRequestRow.prospective_request_id,
                )
                .limit(1)
            ).one_or_none()
        if row is None:
            return None
        try:
            request = ProspectiveCandidateRepository.request_from_row(row[0])
            execution = ProspectiveCandidateRepository.execution_from_row(
                row[1]
            )
        except ProspectivePersistenceError as exc:
            raise ProspectiveOutcomePersistenceError(str(exc)) from exc
        return PendingProspectiveOutcome(
            request=request,
            execution=execution,
        )

    def store(self, evidence: ProspectiveOutcomeEvidenceV1) -> bool:
        try:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchCandidateProspectiveOutcomeRow,
                    evidence.outcome_id,
                )
                if existing is not None:
                    self._validate_row(existing, evidence)
                    return False
                duplicate = session.scalar(
                    select(ResearchCandidateProspectiveOutcomeRow).where(
                        ResearchCandidateProspectiveOutcomeRow.prospective_request_id
                        == evidence.prospective_request_id
                    )
                )
                if duplicate is not None:
                    self._validate_row(duplicate, evidence)
                    return False
                terminal_failure = session.scalar(
                    select(
                        ResearchCandidateProspectiveOutcomeFailureRow
                    ).where(
                        ResearchCandidateProspectiveOutcomeFailureRow.prospective_request_id
                        == evidence.prospective_request_id
                    )
                )
                if terminal_failure is not None:
                    raise ProspectiveOutcomePersistenceError(
                        "prospective outcome request already failed terminally"
                    )
                request_row = session.get(
                    ResearchCandidateProspectiveRequestRow,
                    evidence.prospective_request_id,
                )
                execution_row = session.get(
                    ResearchCandidateProspectiveExecutionRow,
                    evidence.execution_id,
                )
                if request_row is None or execution_row is None:
                    raise ProspectiveOutcomePersistenceError(
                        "prospective outcome references unknown evidence"
                    )
                request = ProspectiveCandidateRepository.request_from_row(
                    request_row
                )
                execution = ProspectiveCandidateRepository.execution_from_row(
                    execution_row
                )
                self._validate_evidence_bindings(
                    session,
                    evidence=evidence,
                    request=request,
                    execution=execution,
                )
                session.add(
                    ResearchCandidateProspectiveOutcomeRow(
                        outcome_id=evidence.outcome_id,
                        prospective_request_id=(
                            evidence.prospective_request_id
                        ),
                        execution_id=evidence.execution_id,
                        challenger_id=evidence.challenger_id,
                        candidate_artifact_hash=(
                            evidence.candidate_artifact_hash
                        ),
                        request_hash=evidence.request_hash,
                        execution_hash=evidence.execution_hash,
                        decision_calendar_session_id=(
                            evidence.decision_calendar_session_id
                        ),
                        implementation_calendar_session_id=(
                            evidence.implementation_calendar_session_id
                        ),
                        evaluation_calendar_session_id=(
                            evidence.evaluation_calendar_session_id
                        ),
                        calendar_version=evidence.calendar_version,
                        market_dataset_version=(
                            evidence.market_dataset_version
                        ),
                        decision_time=evidence.decision_time,
                        outcome_data_cutoff=evidence.outcome_data_cutoff,
                        outcome_available_at=evidence.outcome_available_at,
                        config_manifest_hash=evidence.config_manifest_hash,
                        source_manifest_hash=evidence.source_manifest_hash,
                        cost_model_hash=evidence.cost_model_hash,
                        outcome_hash=evidence.outcome_hash,
                        real_order_routing=False,
                        payload_json=model_payload(evidence),
                        created_at=evidence.created_at,
                    )
                )
                session.flush()
                return True
        except IntegrityError as exc:
            existing = self.for_request(evidence.prospective_request_id)
            if (
                existing is not None
                and existing.outcome_hash == evidence.outcome_hash
            ):
                return False
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome persistence conflict"
            ) from exc
        except ProspectivePersistenceError as exc:
            raise ProspectiveOutcomePersistenceError(str(exc)) from exc

    def store_failure(
        self,
        failure: ProspectiveOutcomeFailureV1,
    ) -> bool:
        try:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchCandidateProspectiveOutcomeFailureRow,
                    failure.failure_id,
                )
                if existing is not None:
                    self._validate_failure_row(existing, failure)
                    return False
                duplicate = session.scalar(
                    select(
                        ResearchCandidateProspectiveOutcomeFailureRow
                    ).where(
                        ResearchCandidateProspectiveOutcomeFailureRow.prospective_request_id
                        == failure.prospective_request_id
                    )
                )
                if duplicate is not None:
                    self._validate_failure_row(duplicate, failure)
                    return False
                successful = session.scalar(
                    select(ResearchCandidateProspectiveOutcomeRow).where(
                        ResearchCandidateProspectiveOutcomeRow.prospective_request_id
                        == failure.prospective_request_id
                    )
                )
                if successful is not None:
                    raise ProspectiveOutcomePersistenceError(
                        "prospective outcome request already succeeded"
                    )
                request_row = session.get(
                    ResearchCandidateProspectiveRequestRow,
                    failure.prospective_request_id,
                )
                execution_row = session.get(
                    ResearchCandidateProspectiveExecutionRow,
                    failure.execution_id,
                )
                if request_row is None or execution_row is None:
                    raise ProspectiveOutcomePersistenceError(
                        "prospective outcome failure references unknown evidence"
                    )
                request = ProspectiveCandidateRepository.request_from_row(
                    request_row
                )
                execution = ProspectiveCandidateRepository.execution_from_row(
                    execution_row
                )
                self._validate_failure_bindings(
                    session,
                    failure=failure,
                    request=request,
                    execution=execution,
                )
                session.add(
                    ResearchCandidateProspectiveOutcomeFailureRow(
                        failure_id=failure.failure_id,
                        prospective_request_id=(
                            failure.prospective_request_id
                        ),
                        execution_id=failure.execution_id,
                        challenger_id=failure.challenger_id,
                        candidate_artifact_hash=(
                            failure.candidate_artifact_hash
                        ),
                        request_hash=failure.request_hash,
                        execution_hash=failure.execution_hash,
                        implementation_calendar_session_id=(
                            failure.implementation_calendar_session_id
                        ),
                        evaluation_calendar_session_id=(
                            failure.evaluation_calendar_session_id
                        ),
                        outcome_data_cutoff=failure.outcome_data_cutoff,
                        error_code=failure.error_code,
                        config_manifest_hash=failure.config_manifest_hash,
                        failure_hash=failure.failure_hash,
                        real_order_routing=False,
                        payload_json=model_payload(failure),
                        created_at=failure.created_at,
                    )
                )
                session.flush()
                return True
        except IntegrityError as exc:
            existing = self.failure_for_request(
                failure.prospective_request_id
            )
            if (
                existing is not None
                and existing.failure_hash == failure.failure_hash
            ):
                return False
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome failure persistence conflict"
            ) from exc
        except ProspectivePersistenceError as exc:
            raise ProspectiveOutcomePersistenceError(str(exc)) from exc

    def for_request(
        self,
        prospective_request_id: str,
    ) -> ProspectiveOutcomeEvidenceV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchCandidateProspectiveOutcomeRow).where(
                    ResearchCandidateProspectiveOutcomeRow.prospective_request_id
                    == prospective_request_id
                )
            )
        return None if row is None else self._from_row(row)

    def failure_for_request(
        self,
        prospective_request_id: str,
    ) -> ProspectiveOutcomeFailureV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(
                    ResearchCandidateProspectiveOutcomeFailureRow
                ).where(
                    ResearchCandidateProspectiveOutcomeFailureRow.prospective_request_id
                    == prospective_request_id
                )
            )
        return None if row is None else self._failure_from_row(row)

    def outcomes(
        self,
        *,
        challenger_id: str,
    ) -> tuple[ProspectiveOutcomeEvidenceV1, ...]:
        with self._session_factory() as session:
            rows = tuple(
                session.scalars(
                    select(ResearchCandidateProspectiveOutcomeRow)
                    .where(
                        ResearchCandidateProspectiveOutcomeRow.challenger_id
                        == challenger_id
                    )
                    .order_by(
                        ResearchCandidateProspectiveOutcomeRow.decision_time,
                        ResearchCandidateProspectiveOutcomeRow.outcome_id,
                    )
                )
            )
        return tuple(self._from_row(row) for row in rows)

    def status(
        self,
        *,
        challenger_id: str,
        minimum_common_sessions: int,
        minimum_observations: int,
    ) -> dict[str, Any]:
        items = self.outcomes(challenger_id=challenger_id)
        with self._session_factory() as session:
            failure_rows = tuple(
                session.scalars(
                    select(
                        ResearchCandidateProspectiveOutcomeFailureRow
                    )
                    .where(
                        ResearchCandidateProspectiveOutcomeFailureRow.challenger_id
                        == challenger_id
                    )
                    .order_by(
                        ResearchCandidateProspectiveOutcomeFailureRow.outcome_data_cutoff,
                        ResearchCandidateProspectiveOutcomeFailureRow.failure_id,
                    )
                )
            )
        failures = tuple(
            self._failure_from_row(row) for row in failure_rows
        )
        observation_count = sum(
            len(item.forward_returns) for item in items
        )
        session_count = len(items)
        ready = (
            session_count >= minimum_common_sessions
            and observation_count >= minimum_observations
        )
        latest = None if not items else items[-1]
        return {
            "schema_version": "candidate_prospective_outcome_status_v1",
            "challenger_id": challenger_id,
            "outcome_count": session_count,
            "terminal_failure_count": len(failures),
            "observation_count": observation_count,
            "minimum_common_sessions": minimum_common_sessions,
            "minimum_observations": minimum_observations,
            "falsification_input_ready": ready,
            "latest": (
                None
                if latest is None
                else {
                    "outcome_id": latest.outcome_id,
                    "prospective_request_id": (
                        latest.prospective_request_id
                    ),
                    "decision_time": latest.decision_time.isoformat(),
                    "outcome_available_at": (
                        latest.outcome_available_at.isoformat()
                    ),
                    "outcome_hash": latest.outcome_hash,
                    "regime": latest.regime,
                }
            ),
            "latest_terminal_failure": (
                None
                if not failures
                else {
                    "failure_id": failures[-1].failure_id,
                    "prospective_request_id": (
                        failures[-1].prospective_request_id
                    ),
                    "outcome_data_cutoff": (
                        failures[-1].outcome_data_cutoff.isoformat()
                    ),
                    "error_code": failures[-1].error_code,
                    "failure_hash": failures[-1].failure_hash,
                }
            ),
            "challenger_status_advanced": False,
            "falsification_started": False,
            "oos_started": False,
            "shadow_started": False,
            "automatic_promotion_enabled": False,
            "broker_access_permitted": False,
            "real_order_routing": False,
        }

    @classmethod
    def _validate_evidence_bindings(
        cls,
        session: Session,
        *,
        evidence: ProspectiveOutcomeEvidenceV1,
        request: ProspectiveRequestEvidenceV1,
        execution: ProspectiveExecutionEvidenceV1,
    ) -> None:
        response = execution.primary_response
        if (
            execution.status is not ProspectiveExecutionStatus.SUCCEEDED
            or response is None
            or not execution.deterministic_match
            or request.challenger_id != evidence.challenger_id
            or request.candidate_artifact_hash
            != evidence.candidate_artifact_hash
            or request.request.request_hash != evidence.request_hash
            or request.calendar_session_id
            != evidence.decision_calendar_session_id
            or request.request.decision_time != evidence.decision_time
            or execution.execution_id != evidence.execution_id
            or execution.execution_hash != evidence.execution_hash
            or execution.prospective_request_id
            != evidence.prospective_request_id
        ):
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome request/execution binding is invalid"
            )
        candidate_current = {
            item.symbol: item.current_weight
            for item in request.request.instruments
        }
        candidate_target = {
            item.symbol: item.target_weight for item in response.targets
        }
        if (
            candidate_current != evidence.candidate_current_weights
            or candidate_target != evidence.candidate_target_weights
        ):
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome Candidate weights are not source-bound"
            )
        baseline_target = cls._parent_targets(request)
        if request.prior_prospective_request_id is None:
            baseline_current = {
                symbol: 0.0 for symbol in baseline_target
            }
        else:
            prior_row = session.get(
                ResearchCandidateProspectiveRequestRow,
                request.prior_prospective_request_id,
            )
            if prior_row is None:
                raise ProspectiveOutcomePersistenceError(
                    "prospective outcome prior request is unavailable"
                )
            prior_request = (
                ProspectiveCandidateRepository.request_from_row(prior_row)
            )
            baseline_current = cls._parent_targets(prior_request)
        if (
            baseline_current != evidence.baseline_current_weights
            or baseline_target != evidence.baseline_target_weights
        ):
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome baseline weights are not source-bound"
            )
        anchor = session.get(
            StrategyEvaluationAnchorRow,
            request.evaluation_anchor_id,
        )
        if (
            anchor is None
            or float(anchor.initial_nav_usd)
            != evidence.evaluation_nav_usd
        ):
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome evaluation NAV is invalid"
            )
        cls._validate_calendars(session, evidence)
        cls._validate_source_bars(session, evidence)
        cls._validate_adv(session, request=request, evidence=evidence)

    @staticmethod
    def _validate_failure_bindings(
        session: Session,
        *,
        failure: ProspectiveOutcomeFailureV1,
        request: ProspectiveRequestEvidenceV1,
        execution: ProspectiveExecutionEvidenceV1,
    ) -> None:
        implementation = session.get(
            MarketCalendarSessionRow,
            failure.implementation_calendar_session_id,
        )
        evaluation = session.get(
            MarketCalendarSessionRow,
            failure.evaluation_calendar_session_id,
        )
        if (
            execution.status is not ProspectiveExecutionStatus.SUCCEEDED
            or execution.primary_response is None
            or not execution.deterministic_match
            or request.challenger_id != failure.challenger_id
            or request.candidate_artifact_hash
            != failure.candidate_artifact_hash
            or request.request.request_hash != failure.request_hash
            or execution.execution_id != failure.execution_id
            or execution.execution_hash != failure.execution_hash
            or execution.prospective_request_id
            != failure.prospective_request_id
            or implementation is None
            or evaluation is None
            or implementation.session_date >= evaluation.session_date
            or _utc(implementation.available_at)
            > request.request.decision_time
            or _utc(evaluation.available_at)
            > request.request.decision_time
            or failure.outcome_data_cutoff <= _utc(evaluation.close_at)
        ):
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome failure binding is invalid"
            )

    @staticmethod
    def _parent_targets(
        request: ProspectiveRequestEvidenceV1,
    ) -> dict[str, float]:
        targets: dict[str, float] = {}
        for instrument in request.request.instruments:
            feature = next(
                (
                    item
                    for item in instrument.features
                    if item.name == "parent_target_weight"
                ),
                None,
            )
            if feature is None:
                raise ProspectiveOutcomePersistenceError(
                    "prospective outcome parent target is unavailable"
                )
            targets[instrument.symbol] = feature.value
        return targets

    @staticmethod
    def _validate_calendars(
        session: Session,
        evidence: ProspectiveOutcomeEvidenceV1,
    ) -> None:
        decision = session.get(
            MarketCalendarSessionRow,
            evidence.decision_calendar_session_id,
        )
        implementation = session.get(
            MarketCalendarSessionRow,
            evidence.implementation_calendar_session_id,
        )
        evaluation = session.get(
            MarketCalendarSessionRow,
            evidence.evaluation_calendar_session_id,
        )
        if (
            decision is None
            or implementation is None
            or evaluation is None
            or not (
                decision.session_date
                < implementation.session_date
                < evaluation.session_date
            )
            or decision.calendar_version != evidence.calendar_version
            or implementation.calendar_version != evidence.calendar_version
            or evaluation.calendar_version != evidence.calendar_version
            or _utc(implementation.close_at)
            != evidence.implementation_close_at
            or _utc(evaluation.close_at) != evidence.evaluation_close_at
            or _utc(decision.available_at) > evidence.decision_time
            or _utc(implementation.available_at) > evidence.decision_time
            or _utc(evaluation.available_at) > evidence.decision_time
            or {
                item.session_date for item in evidence.source_bars
            }
            != {implementation.session_date, evaluation.session_date}
        ):
            raise ProspectiveOutcomePersistenceError(
                "prospective outcome calendar binding is invalid"
            )

    @staticmethod
    def _validate_source_bars(
        session: Session,
        evidence: ProspectiveOutcomeEvidenceV1,
    ) -> None:
        for item in evidence.source_bars:
            row = session.get(MarketBarRow, item.bar_id)
            if (
                row is None
                or row.symbol != item.symbol
                or row.provider != "alpaca"
                or row.feed != "iex"
                or row.timeframe != evidence.timeframe
                or _utc(row.event_time) != item.source_event_time
                or _utc(row.available_at) != item.available_at
                or float(row.close) != item.adjusted_close
                or float(row.volume) != item.volume
                or row.payload_hash != item.payload_hash
                or row.payload_json.get("_adjustment")
                != evidence.adjustment
                or row.payload_json.get("_dataset_version")
                != evidence.market_dataset_version
            ):
                raise ProspectiveOutcomePersistenceError(
                    "prospective outcome source bar binding is invalid"
                )

    @staticmethod
    def _validate_adv(
        session: Session,
        *,
        request: ProspectiveRequestEvidenceV1,
        evidence: ProspectiveOutcomeEvidenceV1,
    ) -> None:
        lookback = evidence.adv_lookback_completed_sessions
        ids: dict[str, list[str]] = {
            symbol: [] for symbol in evidence.adv_usd
        }
        for item in request.source_manifest.source_bars:
            if item.symbol in ids:
                ids[item.symbol].append(item.bar_id)
        for symbol, bar_ids in sorted(ids.items()):
            rows = tuple(
                session.scalars(
                    select(MarketBarRow)
                    .where(MarketBarRow.bar_id.in_(bar_ids))
                    .order_by(MarketBarRow.event_time)
                )
            )
            if len(rows) < lookback:
                raise ProspectiveOutcomePersistenceError(
                    "prospective outcome ADV history is incomplete"
                )
            value = sum(
                (row.close * row.volume for row in rows[-lookback:]),
                Decimal("0"),
            ) / Decimal(lookback)
            if (
                abs(float(value) - evidence.adv_usd[symbol])
                > evidence.numeric_tolerance
            ):
                raise ProspectiveOutcomePersistenceError(
                    "prospective outcome ADV is not source-bound"
                )

    @staticmethod
    def _from_row(
        row: ResearchCandidateProspectiveOutcomeRow,
    ) -> ProspectiveOutcomeEvidenceV1:
        try:
            evidence = ProspectiveOutcomeEvidenceV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ProspectiveOutcomePersistenceError(
                "stored prospective outcome is invalid"
            ) from exc
        ProspectiveOutcomeRepository._validate_row(row, evidence)
        return evidence

    @staticmethod
    def _failure_from_row(
        row: ResearchCandidateProspectiveOutcomeFailureRow,
    ) -> ProspectiveOutcomeFailureV1:
        try:
            failure = ProspectiveOutcomeFailureV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ProspectiveOutcomePersistenceError(
                "stored prospective outcome failure is invalid"
            ) from exc
        ProspectiveOutcomeRepository._validate_failure_row(row, failure)
        return failure

    @staticmethod
    def outcome_from_row(
        row: ResearchCandidateProspectiveOutcomeRow,
    ) -> ProspectiveOutcomeEvidenceV1:
        """Validate and expose immutable outcome evidence to trusted hosts."""

        return ProspectiveOutcomeRepository._from_row(row)

    @staticmethod
    def failure_from_row(
        row: ResearchCandidateProspectiveOutcomeFailureRow,
    ) -> ProspectiveOutcomeFailureV1:
        """Validate and expose terminal outcome failure evidence."""

        return ProspectiveOutcomeRepository._failure_from_row(row)

    @staticmethod
    def _validate_row(
        row: ResearchCandidateProspectiveOutcomeRow,
        evidence: ProspectiveOutcomeEvidenceV1,
    ) -> None:
        if (
            row.outcome_id != evidence.outcome_id
            or row.prospective_request_id
            != evidence.prospective_request_id
            or row.execution_id != evidence.execution_id
            or row.challenger_id != evidence.challenger_id
            or row.candidate_artifact_hash
            != evidence.candidate_artifact_hash
            or row.request_hash != evidence.request_hash
            or row.execution_hash != evidence.execution_hash
            or row.decision_calendar_session_id
            != evidence.decision_calendar_session_id
            or row.implementation_calendar_session_id
            != evidence.implementation_calendar_session_id
            or row.evaluation_calendar_session_id
            != evidence.evaluation_calendar_session_id
            or row.calendar_version != evidence.calendar_version
            or row.market_dataset_version
            != evidence.market_dataset_version
            or _utc(row.decision_time) != evidence.decision_time
            or _utc(row.outcome_data_cutoff)
            != evidence.outcome_data_cutoff
            or _utc(row.outcome_available_at)
            != evidence.outcome_available_at
            or row.config_manifest_hash != evidence.config_manifest_hash
            or row.source_manifest_hash != evidence.source_manifest_hash
            or row.cost_model_hash != evidence.cost_model_hash
            or row.outcome_hash != evidence.outcome_hash
            or canonical_hash(row.payload_json)
            != canonical_hash(model_payload(evidence))
            or _utc(row.created_at) != evidence.created_at
            or row.real_order_routing
        ):
            raise ProspectiveOutcomePersistenceError(
                "stored prospective outcome binding is invalid"
            )

    @staticmethod
    def _validate_failure_row(
        row: ResearchCandidateProspectiveOutcomeFailureRow,
        failure: ProspectiveOutcomeFailureV1,
    ) -> None:
        if (
            row.failure_id != failure.failure_id
            or row.prospective_request_id
            != failure.prospective_request_id
            or row.execution_id != failure.execution_id
            or row.challenger_id != failure.challenger_id
            or row.candidate_artifact_hash
            != failure.candidate_artifact_hash
            or row.request_hash != failure.request_hash
            or row.execution_hash != failure.execution_hash
            or row.implementation_calendar_session_id
            != failure.implementation_calendar_session_id
            or row.evaluation_calendar_session_id
            != failure.evaluation_calendar_session_id
            or _utc(row.outcome_data_cutoff)
            != failure.outcome_data_cutoff
            or row.error_code != failure.error_code
            or row.config_manifest_hash
            != failure.config_manifest_hash
            or row.failure_hash != failure.failure_hash
            or canonical_hash(row.payload_json)
            != canonical_hash(model_payload(failure))
            or _utc(row.created_at) != failure.created_at
            or row.real_order_routing
        ):
            raise ProspectiveOutcomePersistenceError(
                "stored prospective outcome failure binding is invalid"
            )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
