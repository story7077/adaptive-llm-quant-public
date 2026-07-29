from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.data.q1_pit import AlignedDailyInputs, CompletedDailySeries
from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import Q1_ALGORITHM_VERSION
from trading.persistence.models import (
    MarketBarRow,
    MarketCalendarSessionRow,
    ResearchCandidateArtifactRow,
    ResearchCandidateEvaluationDatasetV2Row,
    ResearchCandidateEvaluationTraceV2Row,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveOutcomeFailureRow,
    ResearchCandidateProspectiveOutcomeRow,
    ResearchCandidateProspectiveRequestRow,
    ResearchReplayArtifactRow,
)
from trading.persistence.prospective import (
    ProspectiveCandidateRepository,
    ProspectivePersistenceError,
)
from trading.persistence.prospective_outcomes import (
    ProspectiveOutcomePersistenceError,
    ProspectiveOutcomeRepository,
)
from trading.research.candidate_evaluation import (
    CandidateEvaluationDatasetV2,
)
from trading.research.evaluation_contracts import (
    CandidateEvaluationTraceV1,
)
from trading.research.prospective import (
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
)
from trading.research.prospective_evaluation import (
    ProspectiveEvaluationRecord,
    build_candidate_outcomes,
)
from trading.research.prospective_outcomes import ProspectiveOutcomeFailureV1

_SQL_ID_BATCH_SIZE = 400


class ProspectiveEvaluationPersistenceError(RuntimeError):
    """Raised when a trusted evaluation artifact loses source binding."""


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationRecordSet:
    records: tuple[ProspectiveEvaluationRecord, ...]
    terminal_failures: tuple[ProspectiveOutcomeFailureV1, ...]


class ProspectiveEvaluationRepository:
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
            raise ProspectiveEvaluationPersistenceError(
                "prospective evaluation database clock is unavailable"
            )
        return _utc(value)

    def source_records(
        self,
        *,
        challenger_id: str,
        maximum_records: int,
    ) -> ProspectiveEvaluationRecordSet:
        if maximum_records <= 0:
            raise ValueError("maximum_records must be positive")
        with self._session_factory() as session:
            rows = tuple(
                session.execute(
                    select(
                        ResearchCandidateProspectiveRequestRow,
                        ResearchCandidateProspectiveExecutionRow,
                        ResearchCandidateProspectiveOutcomeRow,
                        MarketCalendarSessionRow,
                    )
                    .join(
                        ResearchCandidateProspectiveExecutionRow,
                        ResearchCandidateProspectiveExecutionRow.prospective_request_id
                        == ResearchCandidateProspectiveRequestRow.prospective_request_id,
                    )
                    .join(
                        ResearchCandidateProspectiveOutcomeRow,
                        ResearchCandidateProspectiveOutcomeRow.prospective_request_id
                        == ResearchCandidateProspectiveRequestRow.prospective_request_id,
                    )
                    .join(
                        MarketCalendarSessionRow,
                        MarketCalendarSessionRow.calendar_session_id
                        == ResearchCandidateProspectiveRequestRow.calendar_session_id,
                    )
                    .where(
                        ResearchCandidateProspectiveRequestRow.challenger_id
                        == challenger_id,
                        ResearchCandidateProspectiveExecutionRow.status
                        == ProspectiveExecutionStatus.SUCCEEDED,
                    )
                    .order_by(
                        ResearchCandidateProspectiveRequestRow.signal_data_cutoff,
                        ResearchCandidateProspectiveRequestRow.prospective_request_id,
                    )
                    .limit(maximum_records)
                )
            )
            parsed = [
                self._parse_source_row(session, row)
                for row in rows
            ]
            if parsed:
                latest_cutoff = (
                    parsed[-1].request.request.signal_data_cutoff
                )
                failure_rows = tuple(
                    session.scalars(
                        select(
                            ResearchCandidateProspectiveOutcomeFailureRow
                        )
                        .join(
                            ResearchCandidateProspectiveRequestRow,
                            ResearchCandidateProspectiveRequestRow.prospective_request_id
                            == ResearchCandidateProspectiveOutcomeFailureRow.prospective_request_id,
                        )
                        .where(
                            ResearchCandidateProspectiveOutcomeFailureRow.challenger_id
                            == challenger_id,
                            ResearchCandidateProspectiveRequestRow.signal_data_cutoff
                            <= latest_cutoff,
                        )
                        .order_by(
                            ResearchCandidateProspectiveOutcomeFailureRow.outcome_data_cutoff,
                            ResearchCandidateProspectiveOutcomeFailureRow.failure_id,
                        )
                    )
                )
                terminal_failures = tuple(
                    ProspectiveOutcomeRepository.failure_from_row(item)
                    for item in failure_rows
                )
                eligible_terminal_requests = int(
                    session.scalar(
                        select(
                            func.count(
                                func.distinct(
                                    ResearchCandidateProspectiveExecutionRow.prospective_request_id
                                )
                            )
                        )
                        .select_from(
                            ResearchCandidateProspectiveExecutionRow
                        )
                        .join(
                            ResearchCandidateProspectiveRequestRow,
                            ResearchCandidateProspectiveRequestRow.prospective_request_id
                            == ResearchCandidateProspectiveExecutionRow.prospective_request_id,
                        )
                        .where(
                            ResearchCandidateProspectiveExecutionRow.challenger_id
                            == challenger_id,
                            ResearchCandidateProspectiveExecutionRow.status
                            == ProspectiveExecutionStatus.SUCCEEDED,
                            ResearchCandidateProspectiveRequestRow.signal_data_cutoff
                            <= latest_cutoff,
                        )
                    )
                    or 0
                )
                if eligible_terminal_requests != (
                    len(parsed) + len(terminal_failures)
                ):
                    raise ProspectiveEvaluationPersistenceError(
                        "prospective evaluation cohort is not terminally complete"
                    )
            else:
                terminal_failures = ()
        return ProspectiveEvaluationRecordSet(
            records=tuple(parsed),
            terminal_failures=terminal_failures,
        )

    def store_dataset(
        self,
        *,
        dataset: CandidateEvaluationDatasetV2,
        config_manifest_hash: str,
        created_at: datetime,
    ) -> bool:
        timestamp = _utc(created_at)
        base_session_count = len(
            {
                item.request.decision_time
                for item in dataset.scenarios
                if item.request.variant.key
                == (
                    "BASE",
                    "BASE",
                    "BASE",
                    "BASE",
                    "BASE",
                )
            }
        )
        try:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchCandidateEvaluationDatasetV2Row,
                    dataset.dataset_id,
                )
                if existing is not None:
                    self._validate_dataset_row(
                        existing,
                        dataset=dataset,
                        config_manifest_hash=config_manifest_hash,
                        created_at=timestamp,
                    )
                    return False
                duplicate = session.scalar(
                    select(
                        ResearchCandidateEvaluationDatasetV2Row
                    ).where(
                        ResearchCandidateEvaluationDatasetV2Row.challenger_id
                        == dataset.challenger_id
                    )
                )
                if duplicate is not None:
                    self._validate_dataset_row(
                        duplicate,
                        dataset=dataset,
                        config_manifest_hash=config_manifest_hash,
                        created_at=timestamp,
                    )
                    return False
                artifact = session.scalar(
                    select(ResearchCandidateArtifactRow).where(
                        ResearchCandidateArtifactRow.challenger_id
                        == dataset.challenger_id,
                        ResearchCandidateArtifactRow.bundle_hash
                        == dataset.candidate_artifact_hash,
                    )
                )
                if artifact is None:
                    raise ProspectiveEvaluationPersistenceError(
                        "registered Candidate artifact is required"
                    )
                cohort = dataset.source_manifest.cohort_manifest
                cohort_request_ids = tuple(
                    item.prospective_request_id for item in cohort.entries
                )
                request_rows = tuple(
                    session.scalars(
                        select(
                            ResearchCandidateProspectiveRequestRow
                        ).where(
                            ResearchCandidateProspectiveRequestRow.prospective_request_id.in_(
                                cohort_request_ids
                            )
                        )
                    )
                )
                requests_by_id = {
                    item.prospective_request_id: (
                        ProspectiveCandidateRepository.request_from_row(item)
                    )
                    for item in request_rows
                }
                calendar_ids = {
                    item.calendar_session_id for item in requests_by_id.values()
                }
                calendar_rows = tuple(
                    session.scalars(
                        select(MarketCalendarSessionRow).where(
                            MarketCalendarSessionRow.calendar_session_id.in_(
                                calendar_ids
                            )
                        )
                    )
                )
                calendars_by_id = {
                    item.calendar_session_id: item
                    for item in calendar_rows
                }
                if (
                    set(requests_by_id) != set(cohort_request_ids)
                    or len(calendar_ids) != len(cohort.entries)
                    or set(calendars_by_id) != calendar_ids
                ):
                    raise ProspectiveEvaluationPersistenceError(
                        "evaluation cohort request coverage is invalid"
                    )
                outcome_hashes = {
                    item.outcome_source_hash
                    for item in dataset.source_manifest.bindings
                }
                outcome_rows = tuple(
                    session.scalars(
                        select(
                            ResearchCandidateProspectiveOutcomeRow
                        ).where(
                            ResearchCandidateProspectiveOutcomeRow.challenger_id
                            == dataset.challenger_id,
                            ResearchCandidateProspectiveOutcomeRow.outcome_hash.in_(
                                outcome_hashes
                            ),
                        )
                    )
                )
                stored_outcomes = {
                    item.outcome_hash: ProspectiveOutcomeRepository.outcome_from_row(
                        item
                    )
                    for item in outcome_rows
                }
                failure_hashes = set(
                    dataset.source_manifest.cohort_manifest
                    .terminal_failure_hashes
                )
                failure_rows = tuple(
                    session.scalars(
                        select(
                            ResearchCandidateProspectiveOutcomeFailureRow
                        ).where(
                            ResearchCandidateProspectiveOutcomeFailureRow.challenger_id
                            == dataset.challenger_id,
                            ResearchCandidateProspectiveOutcomeFailureRow.failure_hash.in_(
                                failure_hashes
                            ),
                        )
                    )
                )
                stored_failures = {
                    item.failure_hash: (
                        ProspectiveOutcomeRepository.failure_from_row(item)
                    )
                    for item in failure_rows
                }
                if (
                    set(stored_outcomes) != outcome_hashes
                    or set(stored_failures) != failure_hashes
                ):
                    raise ProspectiveEvaluationPersistenceError(
                        "evaluation dataset cites unknown forward evidence"
                    )
                outcome_config_hashes = {
                    item.config_manifest_hash
                    for item in stored_outcomes.values()
                }
                selected_request_ids = set(cohort_request_ids)
                if (
                    len(outcome_config_hashes) != 1
                    or any(
                        item.challenger_id != dataset.challenger_id
                        or item.candidate_artifact_hash
                        != dataset.candidate_artifact_hash
                        or item.outcome_data_cutoff
                        > cohort.selection_data_cutoff
                        for item in stored_outcomes.values()
                    )
                    or any(
                        item.challenger_id != dataset.challenger_id
                        or item.candidate_artifact_hash
                        != dataset.candidate_artifact_hash
                        or item.config_manifest_hash
                        not in outcome_config_hashes
                        or item.outcome_data_cutoff
                        > cohort.selection_data_cutoff
                        or item.prospective_request_id
                        in selected_request_ids
                        for item in stored_failures.values()
                    )
                ):
                    raise ProspectiveEvaluationPersistenceError(
                        "evaluation cohort terminal evidence is invalid"
                    )
                if len(
                    {
                        item.outcome_source_hash
                        for item in cohort.entries
                    }
                ) != len(cohort.entries):
                    raise ProspectiveEvaluationPersistenceError(
                        "evaluation cohort reuses forward evidence"
                    )
                prior_session_date = None
                for entry in cohort.entries:
                    request = requests_by_id[
                        entry.prospective_request_id
                    ]
                    outcome = stored_outcomes.get(
                        entry.outcome_source_hash
                    )
                    calendar = calendars_by_id[
                        request.calendar_session_id
                    ]
                    if (
                        request.challenger_id != dataset.challenger_id
                        or request.candidate_artifact_hash
                        != dataset.candidate_artifact_hash
                        or request.request.request_hash
                        != entry.request_hash
                        or request.request.decision_time
                        != entry.decision_time
                        or request.request.signal_data_cutoff
                        != entry.signal_data_cutoff
                        or outcome is None
                        or outcome.prospective_request_id
                        != entry.prospective_request_id
                        or outcome.request_hash != entry.request_hash
                        or outcome.decision_calendar_session_id
                        != request.calendar_session_id
                        or outcome.outcome_available_at
                        != entry.outcome_available_at
                        or calendar.algorithm_version
                        != Q1_ALGORITHM_VERSION
                        or calendar.calendar_version
                        != outcome.calendar_version
                        or _utc(calendar.available_at)
                        > entry.decision_time
                        or _utc(calendar.created_at)
                        > entry.decision_time
                        or (
                            prior_session_date is not None
                            and calendar.session_date
                            <= prior_session_date
                        )
                    ):
                        raise ProspectiveEvaluationPersistenceError(
                            "evaluation cohort source binding is invalid"
                        )
                    prior_session_date = calendar.session_date
                bindings = {
                    item.scenario_id: item
                    for item in dataset.source_manifest.bindings
                }
                for scenario in dataset.scenarios:
                    evidence = stored_outcomes[
                        bindings[scenario.scenario_id].outcome_source_hash
                    ]
                    if (
                        scenario.outcomes
                        != build_candidate_outcomes(evidence)
                        or scenario.evaluation_nav_usd
                        != evidence.evaluation_nav_usd
                    ):
                        raise ProspectiveEvaluationPersistenceError(
                            "evaluation scenario outcome payload is not source-bound"
                        )
                session.add(
                    ResearchCandidateEvaluationDatasetV2Row(
                        dataset_id=dataset.dataset_id,
                        challenger_id=dataset.challenger_id,
                        candidate_artifact_hash=(
                            dataset.candidate_artifact_hash
                        ),
                        source_manifest_hash=(
                            dataset.source_manifest.manifest_hash
                        ),
                        config_manifest_hash=config_manifest_hash,
                        base_session_count=base_session_count,
                        scenario_count=len(dataset.scenarios),
                        dataset_hash=dataset.dataset_hash,
                        real_order_routing=False,
                        payload_json=model_payload(dataset),
                        created_at=timestamp,
                    )
                )
                session.flush()
                return True
        except IntegrityError as exc:
            if self._dataset_conflict_is_identical(
                dataset=dataset,
                config_manifest_hash=config_manifest_hash,
                created_at=timestamp,
            ):
                return False
            raise ProspectiveEvaluationPersistenceError(
                "evaluation dataset persistence conflict"
            ) from exc

    def store_trace(
        self,
        *,
        dataset: CandidateEvaluationDatasetV2,
        trace: CandidateEvaluationTraceV1,
        replay_artifact_hash: str,
    ) -> bool:
        try:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchCandidateEvaluationTraceV2Row,
                    trace.trace_id,
                )
                if existing is not None:
                    self._validate_trace_row(
                        existing,
                        dataset=dataset,
                        trace=trace,
                        replay_artifact_hash=replay_artifact_hash,
                    )
                    return False
                duplicate = session.scalar(
                    select(
                        ResearchCandidateEvaluationTraceV2Row
                    ).where(
                        ResearchCandidateEvaluationTraceV2Row.challenger_id
                        == trace.challenger_id
                    )
                )
                if duplicate is not None:
                    self._validate_trace_row(
                        duplicate,
                        dataset=dataset,
                        trace=trace,
                        replay_artifact_hash=replay_artifact_hash,
                    )
                    return False
                dataset_row = session.get(
                    ResearchCandidateEvaluationDatasetV2Row,
                    dataset.dataset_id,
                )
                replay_row = session.scalar(
                    select(ResearchReplayArtifactRow).where(
                        ResearchReplayArtifactRow.artifact_hash
                        == replay_artifact_hash,
                        ResearchReplayArtifactRow.challenger_id
                        == trace.challenger_id,
                        ResearchReplayArtifactRow.candidate_artifact_hash
                        == trace.candidate_artifact_hash,
                        ResearchReplayArtifactRow.data_manifest_hash
                        == trace.data_manifest_hash,
                        ResearchReplayArtifactRow.deterministic_match.is_(
                            True
                        ),
                    )
                )
                if (
                    dataset_row is None
                    or dataset_row.dataset_hash != dataset.dataset_hash
                    or replay_row is None
                    or trace.challenger_id != dataset.challenger_id
                    or trace.candidate_artifact_hash
                    != dataset.candidate_artifact_hash
                    or trace.data_manifest_hash
                    != dataset.source_manifest.manifest_hash
                ):
                    raise ProspectiveEvaluationPersistenceError(
                        "evaluation trace prerequisite binding is invalid"
                    )
                session.add(
                    ResearchCandidateEvaluationTraceV2Row(
                        trace_id=trace.trace_id,
                        dataset_id=dataset.dataset_id,
                        challenger_id=trace.challenger_id,
                        candidate_artifact_hash=(
                            trace.candidate_artifact_hash
                        ),
                        source_manifest_hash=trace.data_manifest_hash,
                        evaluation_contract_hash=(
                            trace.evaluation_contract_hash
                        ),
                        replay_artifact_hash=replay_artifact_hash,
                        trace_hash=trace.trace_hash,
                        real_order_routing=False,
                        payload_json=model_payload(trace),
                        created_at=trace.created_at,
                    )
                )
                session.flush()
                return True
        except IntegrityError as exc:
            if self._trace_conflict_is_identical(
                dataset=dataset,
                trace=trace,
                replay_artifact_hash=replay_artifact_hash,
            ):
                return False
            raise ProspectiveEvaluationPersistenceError(
                "evaluation trace persistence conflict"
            ) from exc

    def dataset(
        self,
        *,
        challenger_id: str,
    ) -> CandidateEvaluationDatasetV2 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(
                    ResearchCandidateEvaluationDatasetV2Row
                ).where(
                    ResearchCandidateEvaluationDatasetV2Row.challenger_id
                    == challenger_id
                )
            )
        if row is None:
            return None
        try:
            dataset = CandidateEvaluationDatasetV2.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ProspectiveEvaluationPersistenceError(
                "stored evaluation dataset is invalid"
            ) from exc
        self._validate_dataset_row(
            row,
            dataset=dataset,
            config_manifest_hash=row.config_manifest_hash,
            created_at=_utc(row.created_at),
        )
        return dataset

    def trace(
        self,
        *,
        challenger_id: str,
    ) -> CandidateEvaluationTraceV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(
                    ResearchCandidateEvaluationTraceV2Row
                ).where(
                    ResearchCandidateEvaluationTraceV2Row.challenger_id
                    == challenger_id
                )
            )
            dataset_row = (
                None
                if row is None
                else session.get(
                    ResearchCandidateEvaluationDatasetV2Row,
                    row.dataset_id,
                )
            )
        if row is None or dataset_row is None:
            return None
        try:
            dataset = CandidateEvaluationDatasetV2.model_validate(
                dataset_row.payload_json
            )
            trace = CandidateEvaluationTraceV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ProspectiveEvaluationPersistenceError(
                "stored evaluation trace is invalid"
            ) from exc
        self._validate_trace_row(
            row,
            dataset=dataset,
            trace=trace,
            replay_artifact_hash=row.replay_artifact_hash,
        )
        return trace

    def status(
        self,
        *,
        challenger_id: str,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            dataset = session.scalar(
                select(
                    ResearchCandidateEvaluationDatasetV2Row
                ).where(
                    ResearchCandidateEvaluationDatasetV2Row.challenger_id
                    == challenger_id
                )
            )
            trace = session.scalar(
                select(
                    ResearchCandidateEvaluationTraceV2Row
                ).where(
                    ResearchCandidateEvaluationTraceV2Row.challenger_id
                    == challenger_id
                )
            )
        return {
            "schema_version": (
                "candidate_prospective_evaluation_status_v1"
            ),
            "challenger_id": challenger_id,
            "status": (
                "EVALUATION_TRACE_RECORDED"
                if trace is not None
                else (
                    "EVALUATION_DATASET_RECORDED"
                    if dataset is not None
                    else "WAITING_FOR_FORWARD_OUTCOMES"
                )
            ),
            "dataset": (
                None
                if dataset is None
                else {
                    "dataset_id": dataset.dataset_id,
                    "dataset_hash": dataset.dataset_hash,
                    "source_manifest_hash": (
                        dataset.source_manifest_hash
                    ),
                    "base_session_count": dataset.base_session_count,
                    "scenario_count": dataset.scenario_count,
                    "config_manifest_hash": (
                        dataset.config_manifest_hash
                    ),
                }
            ),
            "trace": (
                None
                if trace is None
                else {
                    "trace_id": trace.trace_id,
                    "trace_hash": trace.trace_hash,
                    "evaluation_contract_hash": (
                        trace.evaluation_contract_hash
                    ),
                    "replay_artifact_hash": (
                        trace.replay_artifact_hash
                    ),
                }
            ),
            "oos_started": False,
            "shadow_started": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    @classmethod
    def _parse_source_row(
        cls,
        session: Session,
        row: Row[
            tuple[
            ResearchCandidateProspectiveRequestRow,
            ResearchCandidateProspectiveExecutionRow,
            ResearchCandidateProspectiveOutcomeRow,
            MarketCalendarSessionRow,
            ]
        ],
    ) -> ProspectiveEvaluationRecord:
        request_row, execution_row, outcome_row, calendar_row = row
        try:
            request = ProspectiveCandidateRepository.request_from_row(
                request_row
            )
            execution = ProspectiveCandidateRepository.execution_from_row(
                execution_row
            )
            outcome = ProspectiveOutcomeRepository.outcome_from_row(
                outcome_row
            )
        except (
            ProspectivePersistenceError,
            ProspectiveOutcomePersistenceError,
        ) as exc:
            raise ProspectiveEvaluationPersistenceError(str(exc)) from exc
        if (
            calendar_row.algorithm_version != Q1_ALGORITHM_VERSION
            or calendar_row.calendar_session_id
            != request.calendar_session_id
            or calendar_row.calendar_version != outcome.calendar_version
            or _utc(calendar_row.available_at)
            > request.request.decision_time
            or _utc(calendar_row.created_at)
            > request.request.decision_time
        ):
            raise ProspectiveEvaluationPersistenceError(
                "prospective evaluation calendar binding is invalid"
            )
        market_inputs = cls._market_inputs(
            session,
            request_row=request_row,
            request=request,
        )
        calendar_dates = tuple(
            sorted(
                {
                    item
                    for item in session.scalars(
                        select(
                            MarketCalendarSessionRow.session_date
                        ).where(
                            MarketCalendarSessionRow.algorithm_version
                            == Q1_ALGORITHM_VERSION,
                            MarketCalendarSessionRow.calendar_version
                            == outcome.calendar_version,
                            MarketCalendarSessionRow.session_date
                            <= calendar_row.session_date,
                            MarketCalendarSessionRow.available_at
                            <= request.request.decision_time,
                            MarketCalendarSessionRow.created_at
                            <= request.request.decision_time,
                        )
                    )
                }
            )
        )
        if (
            not calendar_dates
            or calendar_dates[-1] != calendar_row.session_date
        ):
            raise ProspectiveEvaluationPersistenceError(
                "prospective evaluation calendar ordinal is unavailable"
            )
        calendar_hash = canonical_hash(
            {
                "calendar_version": outcome.calendar_version,
                "algorithm_version": Q1_ALGORITHM_VERSION,
                "session_dates": calendar_dates,
            }
        )
        return ProspectiveEvaluationRecord(
            request=request,
            execution=execution,
            outcome=outcome,
            market_inputs=market_inputs,
            decision_session_ordinal=len(calendar_dates) - 1,
            calendar_path_hash=calendar_hash,
        )

    @classmethod
    def _market_inputs(
        cls,
        session: Session,
        *,
        request_row: ResearchCandidateProspectiveRequestRow,
        request: ProspectiveRequestEvidenceV1,
    ) -> AlignedDailyInputs:
        source_items = request.source_manifest.source_bars
        source_ids = tuple(item.bar_id for item in source_items)
        rows_by_id: dict[str, MarketBarRow] = {}
        for start in range(0, len(source_ids), _SQL_ID_BATCH_SIZE):
            batch = source_ids[start : start + _SQL_ID_BATCH_SIZE]
            for row in session.scalars(
                select(MarketBarRow).where(MarketBarRow.bar_id.in_(batch))
            ):
                if row.bar_id in rows_by_id:
                    raise ProspectiveEvaluationPersistenceError(
                        "prospective source bar is duplicated"
                    )
                rows_by_id[row.bar_id] = row
        if set(rows_by_id) != set(source_ids):
            raise ProspectiveEvaluationPersistenceError(
                "prospective source bar is missing"
            )
        by_symbol: dict[str, list[tuple[Any, MarketBarRow]]] = {}
        for item in source_items:
            row = rows_by_id[item.bar_id]
            if (
                row.symbol != item.symbol
                or row.provider != "alpaca"
                or row.feed != "iex"
                or row.timeframe != "1Day"
                or _utc(row.event_time) != item.source_event_time
                or _utc(row.available_at) != item.available_at
                or row.payload_hash != item.payload_hash
                or row.payload_json.get("_adjustment") != "all"
                or row.payload_json.get("_dataset_version")
                != request.source_manifest.market_dataset_version
                or row.close <= 0
                or row.volume < 0
                or _utc(row.available_at)
                > request.request.signal_data_cutoff
            ):
                raise ProspectiveEvaluationPersistenceError(
                    "prospective source bar binding is invalid"
                )
            by_symbol.setdefault(item.symbol, []).append((item, row))
        series: dict[str, CompletedDailySeries] = {}
        for symbol, values in sorted(by_symbol.items()):
            ordered = sorted(
                values,
                key=lambda item: (
                    item[0].session_date,
                    item[0].bar_id,
                ),
            )
            session_dates = tuple(item.session_date for item, _ in ordered)
            if (
                session_dates
                != request.source_manifest.completed_session_dates
            ):
                raise ProspectiveEvaluationPersistenceError(
                    "prospective source series is not aligned"
                )
            series[symbol] = CompletedDailySeries(
                symbol=symbol,
                session_dates=session_dates,
                adjusted_closes=tuple(row.close for _, row in ordered),
                volumes=tuple(row.volume for _, row in ordered),
                bar_ids=tuple(row.bar_id for _, row in ordered),
                event_times=tuple(
                    _utc(row.event_time) for _, row in ordered
                ),
                available_ats=tuple(
                    _utc(row.available_at) for _, row in ordered
                ),
                payload_hashes=tuple(
                    row.payload_hash for _, row in ordered
                ),
            )
        if request_row.source_manifest_hash != (
            request.source_manifest.manifest_hash
        ):
            raise ProspectiveEvaluationPersistenceError(
                "prospective source manifest row differs from payload"
            )
        return AlignedDailyInputs(
            session_dates=request.source_manifest.completed_session_dates,
            series=series,
            source_bar_ids=tuple(sorted(source_ids)),
            signal_data_cutoff=request.request.signal_data_cutoff,
        )

    def _dataset_conflict_is_identical(
        self,
        *,
        dataset: CandidateEvaluationDatasetV2,
        config_manifest_hash: str,
        created_at: datetime,
    ) -> bool:
        with self._session_factory() as session:
            row = session.scalar(
                select(
                    ResearchCandidateEvaluationDatasetV2Row
                ).where(
                    ResearchCandidateEvaluationDatasetV2Row.challenger_id
                    == dataset.challenger_id
                )
            )
        if row is None:
            return False
        try:
            self._validate_dataset_row(
                row,
                dataset=dataset,
                config_manifest_hash=config_manifest_hash,
                created_at=created_at,
            )
        except ProspectiveEvaluationPersistenceError:
            return False
        return True

    def _trace_conflict_is_identical(
        self,
        *,
        dataset: CandidateEvaluationDatasetV2,
        trace: CandidateEvaluationTraceV1,
        replay_artifact_hash: str,
    ) -> bool:
        with self._session_factory() as session:
            row = session.scalar(
                select(
                    ResearchCandidateEvaluationTraceV2Row
                ).where(
                    ResearchCandidateEvaluationTraceV2Row.challenger_id
                    == trace.challenger_id
                )
            )
        if row is None:
            return False
        try:
            self._validate_trace_row(
                row,
                dataset=dataset,
                trace=trace,
                replay_artifact_hash=replay_artifact_hash,
            )
        except ProspectiveEvaluationPersistenceError:
            return False
        return True

    @staticmethod
    def _validate_dataset_row(
        row: ResearchCandidateEvaluationDatasetV2Row,
        *,
        dataset: CandidateEvaluationDatasetV2,
        config_manifest_hash: str,
        created_at: datetime,
    ) -> None:
        base_sessions = {
            item.request.decision_time
            for item in dataset.scenarios
            if item.request.variant.key
            == ("BASE", "BASE", "BASE", "BASE", "BASE")
        }
        if (
            row.dataset_id != dataset.dataset_id
            or row.challenger_id != dataset.challenger_id
            or row.candidate_artifact_hash
            != dataset.candidate_artifact_hash
            or row.source_manifest_hash
            != dataset.source_manifest.manifest_hash
            or row.config_manifest_hash != config_manifest_hash
            or row.base_session_count != len(base_sessions)
            or row.scenario_count != len(dataset.scenarios)
            or row.dataset_hash != dataset.dataset_hash
            or canonical_hash(row.payload_json)
            != canonical_hash(model_payload(dataset))
            or _utc(row.created_at) != _utc(created_at)
            or row.real_order_routing
        ):
            raise ProspectiveEvaluationPersistenceError(
                "stored evaluation dataset binding is invalid"
            )

    @staticmethod
    def _validate_trace_row(
        row: ResearchCandidateEvaluationTraceV2Row,
        *,
        dataset: CandidateEvaluationDatasetV2,
        trace: CandidateEvaluationTraceV1,
        replay_artifact_hash: str,
    ) -> None:
        if (
            row.trace_id != trace.trace_id
            or row.dataset_id != dataset.dataset_id
            or row.challenger_id != trace.challenger_id
            or row.candidate_artifact_hash
            != trace.candidate_artifact_hash
            or row.source_manifest_hash != trace.data_manifest_hash
            or row.evaluation_contract_hash
            != trace.evaluation_contract_hash
            or row.replay_artifact_hash != replay_artifact_hash
            or row.trace_hash != trace.trace_hash
            or canonical_hash(row.payload_json)
            != canonical_hash(model_payload(trace))
            or _utc(row.created_at) != trace.created_at
            or row.real_order_routing
        ):
            raise ProspectiveEvaluationPersistenceError(
                "stored evaluation trace binding is invalid"
            )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
