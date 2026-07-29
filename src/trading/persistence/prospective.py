from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash
from trading.persistence.models import (
    PortfolioDecisionRow,
    ResearchCandidateArtifactRow,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveRequestRow,
    StrategyEvaluationAnchorRow,
)
from trading.research.prospective import (
    ProspectiveExecutionEvidenceV1,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
)


class ProspectivePersistenceError(RuntimeError):
    """Raised when immutable prospective evidence fails a binding check."""


@dataclass(frozen=True, slots=True)
class PersistedProspectiveState:
    request: ProspectiveRequestEvidenceV1
    execution: ProspectiveExecutionEvidenceV1
    last_review_calendar_session_id: str | None


class ProspectiveCandidateRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def store_request(self, evidence: ProspectiveRequestEvidenceV1) -> bool:
        try:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchCandidateProspectiveRequestRow,
                    evidence.prospective_request_id,
                )
                if existing is not None:
                    self._validate_request_row(existing, evidence)
                    return False
                duplicate_parent = session.scalar(
                    select(ResearchCandidateProspectiveRequestRow).where(
                        ResearchCandidateProspectiveRequestRow.challenger_id
                        == evidence.challenger_id,
                        ResearchCandidateProspectiveRequestRow.parent_portfolio_decision_id
                        == evidence.parent_portfolio_decision_id,
                    )
                )
                if duplicate_parent is not None:
                    duplicate = self._request_from_row(duplicate_parent)
                    if duplicate.evidence_hash != evidence.evidence_hash:
                        raise ProspectivePersistenceError(
                            "prospective parent decision already has different evidence"
                        )
                    return False
                artifact = session.get(
                    ResearchCandidateArtifactRow,
                    evidence.candidate_artifact_bundle_id,
                )
                if (
                    artifact is None
                    or artifact.challenger_id != evidence.challenger_id
                    or artifact.bundle_hash != evidence.candidate_artifact_hash
                    or artifact.config_hash != evidence.candidate_config_hash
                    or artifact.real_order_routing
                ):
                    raise ProspectivePersistenceError(
                        "prospective Candidate artifact binding is invalid"
                    )
                parent = self._locked_portfolio_decision(
                    session,
                    evidence.parent_portfolio_decision_id,
                )
                if (
                    parent is None
                    or parent.run_id != evidence.parent_run_id
                    or parent.decision_hash
                    != evidence.source_manifest.parent_decision_hash
                    or parent.input_manifest_hash
                    != evidence.source_manifest.parent_input_manifest_hash
                ):
                    raise ProspectivePersistenceError(
                        "prospective parent decision binding is invalid"
                    )
                anchor = session.get(
                    StrategyEvaluationAnchorRow,
                    evidence.evaluation_anchor_id,
                )
                if (
                    anchor is None
                    or anchor.run_id != evidence.parent_run_id
                    or anchor.anchor_hash
                    != evidence.source_manifest.evaluation_anchor_hash
                ):
                    raise ProspectivePersistenceError(
                        "prospective evaluation anchor binding is invalid"
                    )
                self._validate_prior_binding(session, evidence)
                row = ResearchCandidateProspectiveRequestRow(
                    prospective_request_id=evidence.prospective_request_id,
                    challenger_id=evidence.challenger_id,
                    candidate_artifact_bundle_id=(
                        evidence.candidate_artifact_bundle_id
                    ),
                    candidate_artifact_hash=evidence.candidate_artifact_hash,
                    candidate_config_hash=evidence.candidate_config_hash,
                    strategy_config_content_sha256=(
                        evidence.strategy_config_content_sha256
                    ),
                    parent_run_id=evidence.parent_run_id,
                    parent_portfolio_decision_id=(
                        evidence.parent_portfolio_decision_id
                    ),
                    calendar_session_id=evidence.calendar_session_id,
                    evaluation_anchor_id=evidence.evaluation_anchor_id,
                    prior_prospective_request_id=(
                        evidence.prior_prospective_request_id
                    ),
                    parent_scheduled_at=evidence.parent_scheduled_at,
                    signal_data_cutoff=evidence.request.signal_data_cutoff,
                    request_hash=evidence.request.request_hash,
                    source_manifest_hash=(
                        evidence.source_manifest.manifest_hash
                    ),
                    host_config_manifest_hash=(
                        evidence.source_manifest.host_config_manifest_hash
                    ),
                    evidence_hash=evidence.evidence_hash,
                    real_order_routing=False,
                    payload_json=model_payload(evidence),
                    source_manifest_json=model_payload(
                        evidence.source_manifest
                    ),
                    created_at=evidence.created_at,
                )
                session.add(row)
                session.flush()
                return True
        except IntegrityError as exc:
            if self._request_conflict_is_identical(evidence):
                return False
            raise ProspectivePersistenceError(
                "prospective request persistence conflict"
            ) from exc

    def _request_conflict_is_identical(
        self,
        evidence: ProspectiveRequestEvidenceV1,
    ) -> bool:
        with self._session_factory() as session:
            row = session.get(
                ResearchCandidateProspectiveRequestRow,
                evidence.prospective_request_id,
            )
            if row is None:
                row = session.scalar(
                    select(ResearchCandidateProspectiveRequestRow).where(
                        ResearchCandidateProspectiveRequestRow.challenger_id
                        == evidence.challenger_id,
                        ResearchCandidateProspectiveRequestRow.parent_portfolio_decision_id
                        == evidence.parent_portfolio_decision_id,
                    )
                )
            if row is None:
                return False
            try:
                self._validate_request_row(row, evidence)
            except ProspectivePersistenceError:
                return False
            return True

    def store_execution(
        self,
        evidence: ProspectiveExecutionEvidenceV1,
    ) -> bool:
        try:
            with self._session_factory.begin() as session:
                existing = session.get(
                    ResearchCandidateProspectiveExecutionRow,
                    evidence.execution_id,
                )
                if existing is not None:
                    self._validate_execution_row(existing, evidence)
                    return False
                request_row = self._locked_request(
                    session,
                    evidence.prospective_request_id,
                )
                if request_row is None:
                    raise ProspectivePersistenceError(
                        "prospective execution references unknown request"
                    )
                request = self._request_from_row(request_row)
                if (
                    evidence.challenger_id != request.challenger_id
                    or evidence.candidate_artifact_hash
                    != request.candidate_artifact_hash
                    or evidence.request_hash != request.request.request_hash
                ):
                    raise ProspectivePersistenceError(
                        "prospective execution request binding is invalid"
                    )
                success_identity = (
                    evidence.prospective_request_id
                    if evidence.status == ProspectiveExecutionStatus.SUCCEEDED
                    else None
                )
                if success_identity is not None:
                    successful = session.scalar(
                        select(ResearchCandidateProspectiveExecutionRow).where(
                            ResearchCandidateProspectiveExecutionRow.success_identity
                            == success_identity
                        )
                    )
                    if successful is not None:
                        stored = self._execution_from_row(successful)
                        if stored.execution_hash != evidence.execution_hash:
                            raise ProspectivePersistenceError(
                                "prospective request already has different success"
                            )
                        return False
                primary_hash = (
                    None
                    if evidence.primary_response is None
                    else evidence.primary_response.output_hash
                )
                replay_hash = (
                    None
                    if evidence.replay_response is None
                    else evidence.replay_response.output_hash
                )
                session.add(
                    ResearchCandidateProspectiveExecutionRow(
                        execution_id=evidence.execution_id,
                        prospective_request_id=evidence.prospective_request_id,
                        challenger_id=evidence.challenger_id,
                        candidate_artifact_hash=(
                            evidence.candidate_artifact_hash
                        ),
                        request_hash=evidence.request_hash,
                        status=evidence.status,
                        runtime_attestation_hash=(
                            evidence.runtime_attestation_hash
                        ),
                        security_contract_hash=(
                            evidence.security_contract_hash
                        ),
                        primary_response_hash=primary_hash,
                        replay_response_hash=replay_hash,
                        deterministic_match=evidence.deterministic_match,
                        error_code=evidence.error_code,
                        success_identity=success_identity,
                        execution_hash=evidence.execution_hash,
                        real_order_routing=False,
                        payload_json=model_payload(evidence),
                        created_at=evidence.created_at,
                    )
                )
                session.flush()
                return True
        except IntegrityError as exc:
            if self._execution_conflict_is_identical(evidence):
                return False
            raise ProspectivePersistenceError(
                "prospective execution persistence conflict"
            ) from exc

    def _execution_conflict_is_identical(
        self,
        evidence: ProspectiveExecutionEvidenceV1,
    ) -> bool:
        with self._session_factory() as session:
            row = session.get(
                ResearchCandidateProspectiveExecutionRow,
                evidence.execution_id,
            )
            if row is None and (
                evidence.status == ProspectiveExecutionStatus.SUCCEEDED
            ):
                row = session.scalar(
                    select(ResearchCandidateProspectiveExecutionRow).where(
                        ResearchCandidateProspectiveExecutionRow.success_identity
                        == evidence.prospective_request_id
                    )
                )
            if row is None:
                return False
            try:
                self._validate_execution_row(row, evidence)
            except ProspectivePersistenceError:
                return False
            return True

    def request(
        self,
        prospective_request_id: str,
    ) -> ProspectiveRequestEvidenceV1 | None:
        with self._session_factory() as session:
            row = session.get(
                ResearchCandidateProspectiveRequestRow,
                prospective_request_id,
            )
            return None if row is None else self._request_from_row(row)

    def request_for_parent(
        self,
        *,
        challenger_id: str,
        parent_portfolio_decision_id: str,
    ) -> ProspectiveRequestEvidenceV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchCandidateProspectiveRequestRow).where(
                    ResearchCandidateProspectiveRequestRow.challenger_id
                    == challenger_id,
                    ResearchCandidateProspectiveRequestRow.parent_portfolio_decision_id
                    == parent_portfolio_decision_id,
                )
            )
            return None if row is None else self._request_from_row(row)

    def successful_execution(
        self,
        prospective_request_id: str,
    ) -> ProspectiveExecutionEvidenceV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchCandidateProspectiveExecutionRow).where(
                    ResearchCandidateProspectiveExecutionRow.success_identity
                    == prospective_request_id
                )
            )
            return None if row is None else self._execution_from_row(row)

    def status(self) -> dict[str, Any]:
        with self._session_factory() as session:
            request_count = int(
                session.scalar(
                    select(func.count()).select_from(
                        ResearchCandidateProspectiveRequestRow
                    )
                )
                or 0
            )
            success_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ResearchCandidateProspectiveExecutionRow)
                    .where(
                        ResearchCandidateProspectiveExecutionRow.status
                        == ProspectiveExecutionStatus.SUCCEEDED
                    )
                )
                or 0
            )
            failure_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(ResearchCandidateProspectiveExecutionRow)
                    .where(
                        ResearchCandidateProspectiveExecutionRow.status
                        == ProspectiveExecutionStatus.FAILED
                    )
                )
                or 0
            )
            latest_request_row = session.scalar(
                select(ResearchCandidateProspectiveRequestRow)
                .order_by(
                    desc(
                        ResearchCandidateProspectiveRequestRow.parent_scheduled_at
                    ),
                    desc(
                        ResearchCandidateProspectiveRequestRow.prospective_request_id
                    ),
                )
                .limit(1)
            )
            execution_rows = (
                []
                if latest_request_row is None
                else list(
                    session.scalars(
                        select(ResearchCandidateProspectiveExecutionRow)
                        .where(
                            ResearchCandidateProspectiveExecutionRow.prospective_request_id
                            == latest_request_row.prospective_request_id
                        )
                        .order_by(
                            desc(
                                ResearchCandidateProspectiveExecutionRow.execution_id
                            )
                        )
                    )
                )
            )
        if latest_request_row is None:
            return {
                "status": "WAITING_FOR_PARENT_DECISION",
                "request_count": request_count,
                "success_count": success_count,
                "failure_count": failure_count,
                "latest": None,
                "outcome_status": "NO_FORWARD_OBSERVATION",
                "challenger_status_advanced": False,
                "shadow_started": False,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }
        request = self._request_from_row(latest_request_row)
        successful_row = next(
            (
                row
                for row in execution_rows
                if row.status == ProspectiveExecutionStatus.SUCCEEDED
            ),
            None,
        )
        effective_row = successful_row or (
            None if not execution_rows else execution_rows[0]
        )
        execution = (
            None
            if effective_row is None
            else self._execution_from_row(effective_row)
        )
        response = None if execution is None else execution.primary_response
        targets = (
            {}
            if response is None
            else {
                item.symbol: item.target_weight
                for item in response.targets
            }
        )
        if targets:
            targets["USD_CASH"] = max(0.0, 1.0 - sum(targets.values()))
        status = (
            "REQUEST_RECORDED_PENDING_EXECUTION"
            if execution is None
            else (
                "PROSPECTIVE_TARGET_RECORDED"
                if execution.status == ProspectiveExecutionStatus.SUCCEEDED
                else "EXECUTION_FAILED_RETRY_PERMITTED"
            )
        )
        return {
            "status": status,
            "request_count": request_count,
            "success_count": success_count,
            "failure_count": failure_count,
            "latest": {
                "prospective_request_id": request.prospective_request_id,
                "challenger_id": request.challenger_id,
                "parent_run_id": request.parent_run_id,
                "parent_portfolio_decision_id": (
                    request.parent_portfolio_decision_id
                ),
                "parent_scheduled_at": request.parent_scheduled_at.isoformat(),
                "request_recorded_at": _utc(
                    latest_request_row.recorded_at
                ).isoformat(),
                "signal_data_cutoff": (
                    request.request.signal_data_cutoff.isoformat()
                ),
                "completed_data_through": (
                    request.source_manifest.completed_session_dates[-1].isoformat()
                ),
                "completed_sessions": len(
                    request.source_manifest.completed_session_dates
                ),
                "source_bar_count": len(
                    request.source_manifest.source_bars
                ),
                "request_hash": request.request.request_hash,
                "source_manifest_hash": request.source_manifest.manifest_hash,
                "execution_status": (
                    None if execution is None else execution.status
                ),
                "execution_hash": (
                    None if execution is None else execution.execution_hash
                ),
                "execution_recorded_at": (
                    None
                    if effective_row is None
                    else _utc(effective_row.recorded_at).isoformat()
                ),
                "deterministic_match": (
                    False if execution is None else execution.deterministic_match
                ),
                "error_code": (
                    None if execution is None else execution.error_code
                ),
                "targets": dict(sorted(targets.items())),
            },
            "outcome_status": (
                "IMMATURE_FORWARD_ONLY"
                if execution is not None
                and execution.status == ProspectiveExecutionStatus.SUCCEEDED
                else "NO_VALID_FORWARD_TARGET"
            ),
            "challenger_status_advanced": False,
            "shadow_started": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    def prior_state(
        self,
        *,
        challenger_id: str,
        before_parent_scheduled_at: datetime,
    ) -> PersistedProspectiveState | None:
        with self._session_factory() as session:
            rows = list(
                session.execute(
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
                        ResearchCandidateProspectiveRequestRow.parent_scheduled_at
                        < before_parent_scheduled_at,
                        ResearchCandidateProspectiveExecutionRow.status
                        == ProspectiveExecutionStatus.SUCCEEDED,
                    )
                    .order_by(
                        desc(
                            ResearchCandidateProspectiveRequestRow.parent_scheduled_at
                        ),
                        desc(
                            ResearchCandidateProspectiveRequestRow.prospective_request_id
                        ),
                    )
                )
            )
            if not rows:
                return None
            latest_request = self._request_from_row(rows[0][0])
            latest_execution = self._execution_from_row(rows[0][1])
            last_review = next(
                (
                    request_row.calendar_session_id
                    for request_row, execution_row in rows
                    if self._execution_review_due(execution_row)
                ),
                None,
            )
            return PersistedProspectiveState(
                request=latest_request,
                execution=latest_execution,
                last_review_calendar_session_id=last_review,
            )

    @staticmethod
    def _locked_portfolio_decision(
        session: Session,
        identity: str,
    ) -> PortfolioDecisionRow | None:
        statement = select(PortfolioDecisionRow).where(
            PortfolioDecisionRow.portfolio_decision_id == identity
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.scalar(statement)

    @staticmethod
    def _locked_request(
        session: Session,
        identity: str,
    ) -> ResearchCandidateProspectiveRequestRow | None:
        statement = select(
            ResearchCandidateProspectiveRequestRow
        ).where(
            ResearchCandidateProspectiveRequestRow.prospective_request_id
            == identity
        )
        if session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.scalar(statement)

    def _validate_prior_binding(
        self,
        session: Session,
        evidence: ProspectiveRequestEvidenceV1,
    ) -> None:
        prior_id = evidence.prior_prospective_request_id
        if prior_id is None:
            prior_success = session.scalar(
                select(ResearchCandidateProspectiveExecutionRow)
                .join(
                    ResearchCandidateProspectiveRequestRow,
                    ResearchCandidateProspectiveRequestRow.prospective_request_id
                    == ResearchCandidateProspectiveExecutionRow.prospective_request_id,
                )
                .where(
                    ResearchCandidateProspectiveRequestRow.challenger_id
                    == evidence.challenger_id,
                    ResearchCandidateProspectiveExecutionRow.status
                    == ProspectiveExecutionStatus.SUCCEEDED,
                )
                .limit(1)
            )
            if prior_success is not None:
                raise ProspectivePersistenceError(
                    "prospective initial state ignored existing verified target"
                )
            return
        prior_request = session.get(
            ResearchCandidateProspectiveRequestRow,
            prior_id,
        )
        prior_execution = session.scalar(
            select(ResearchCandidateProspectiveExecutionRow).where(
                ResearchCandidateProspectiveExecutionRow.prospective_request_id
                == prior_id,
                ResearchCandidateProspectiveExecutionRow.status
                == ProspectiveExecutionStatus.SUCCEEDED,
            )
        )
        if (
            prior_request is None
            or prior_execution is None
            or prior_request.challenger_id != evidence.challenger_id
            or prior_request.parent_scheduled_at
            >= evidence.request.decision_time
            or prior_execution.execution_hash
            != evidence.source_manifest.prior_execution_hash
        ):
            raise ProspectivePersistenceError(
                "prospective prior target-state binding is invalid"
            )

    @staticmethod
    def _request_from_row(
        row: ResearchCandidateProspectiveRequestRow,
    ) -> ProspectiveRequestEvidenceV1:
        try:
            evidence = ProspectiveRequestEvidenceV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ProspectivePersistenceError(
                "stored prospective request is invalid"
            ) from exc
        ProspectiveCandidateRepository._validate_request_row(row, evidence)
        return evidence

    @staticmethod
    def _execution_from_row(
        row: ResearchCandidateProspectiveExecutionRow,
    ) -> ProspectiveExecutionEvidenceV1:
        try:
            evidence = ProspectiveExecutionEvidenceV1.model_validate(
                row.payload_json
            )
        except ValueError as exc:
            raise ProspectivePersistenceError(
                "stored prospective execution is invalid"
            ) from exc
        ProspectiveCandidateRepository._validate_execution_row(row, evidence)
        return evidence

    @staticmethod
    def request_from_row(
        row: ResearchCandidateProspectiveRequestRow,
    ) -> ProspectiveRequestEvidenceV1:
        """Validate and expose immutable request evidence to trusted hosts."""

        return ProspectiveCandidateRepository._request_from_row(row)

    @staticmethod
    def execution_from_row(
        row: ResearchCandidateProspectiveExecutionRow,
    ) -> ProspectiveExecutionEvidenceV1:
        """Validate and expose immutable execution evidence to trusted hosts."""

        return ProspectiveCandidateRepository._execution_from_row(row)

    @staticmethod
    def _validate_request_row(
        row: ResearchCandidateProspectiveRequestRow,
        evidence: ProspectiveRequestEvidenceV1,
    ) -> None:
        if (
            row.prospective_request_id != evidence.prospective_request_id
            or row.challenger_id != evidence.challenger_id
            or row.candidate_artifact_bundle_id
            != evidence.candidate_artifact_bundle_id
            or row.candidate_artifact_hash
            != evidence.candidate_artifact_hash
            or row.candidate_config_hash != evidence.candidate_config_hash
            or row.strategy_config_content_sha256
            != evidence.strategy_config_content_sha256
            or row.parent_run_id != evidence.parent_run_id
            or row.parent_portfolio_decision_id
            != evidence.parent_portfolio_decision_id
            or row.calendar_session_id != evidence.calendar_session_id
            or row.evaluation_anchor_id != evidence.evaluation_anchor_id
            or row.prior_prospective_request_id
            != evidence.prior_prospective_request_id
            or _utc(row.parent_scheduled_at) != evidence.parent_scheduled_at
            or _utc(row.signal_data_cutoff)
            != evidence.request.signal_data_cutoff
            or row.request_hash != evidence.request.request_hash
            or row.source_manifest_hash
            != evidence.source_manifest.manifest_hash
            or row.host_config_manifest_hash
            != evidence.source_manifest.host_config_manifest_hash
            or row.evidence_hash != evidence.evidence_hash
            or canonical_hash(row.source_manifest_json)
            != canonical_hash(model_payload(evidence.source_manifest))
            or _utc(row.created_at) != evidence.created_at
            or row.real_order_routing
        ):
            raise ProspectivePersistenceError(
                "stored prospective request binding is invalid"
            )

    @staticmethod
    def _validate_execution_row(
        row: ResearchCandidateProspectiveExecutionRow,
        evidence: ProspectiveExecutionEvidenceV1,
    ) -> None:
        if (
            row.execution_id != evidence.execution_id
            or row.prospective_request_id
            != evidence.prospective_request_id
            or row.challenger_id != evidence.challenger_id
            or row.candidate_artifact_hash
            != evidence.candidate_artifact_hash
            or row.request_hash != evidence.request_hash
            or row.status != evidence.status
            or row.runtime_attestation_hash
            != evidence.runtime_attestation_hash
            or row.security_contract_hash
            != evidence.security_contract_hash
            or row.primary_response_hash
            != (
                None
                if evidence.primary_response is None
                else evidence.primary_response.output_hash
            )
            or row.replay_response_hash
            != (
                None
                if evidence.replay_response is None
                else evidence.replay_response.output_hash
            )
            or row.deterministic_match != evidence.deterministic_match
            or row.error_code != evidence.error_code
            or row.success_identity
            != (
                evidence.prospective_request_id
                if evidence.status == ProspectiveExecutionStatus.SUCCEEDED
                else None
            )
            or row.execution_hash != evidence.execution_hash
            or _utc(row.created_at) != evidence.created_at
            or row.real_order_routing
        ):
            raise ProspectivePersistenceError(
                "stored prospective execution binding is invalid"
            )

    @staticmethod
    def _execution_review_due(
        row: ResearchCandidateProspectiveExecutionRow,
    ) -> bool:
        evidence = ProspectiveCandidateRepository._execution_from_row(row)
        response = evidence.primary_response
        return bool(
            response is not None
            and response.diagnostics.get("review_due") is True
        )


def _utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )
