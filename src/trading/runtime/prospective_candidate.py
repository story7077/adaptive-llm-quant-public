from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy import desc, exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.q1_pit import Q1PointInTimeMarketData
from trading.domain.q1 import (
    Q1_ALGORITHM_VERSION,
    Q1ArmId,
    Q1StrategyDecision,
    StrategyEvaluationAnchor,
)
from trading.persistence.models import (
    ChallengerManifestRow,
    MarketCalendarSessionRow,
    PaperCycleRow,
    PortfolioDecisionRow,
    ResearchCandidateProspectiveExecutionRow,
    ResearchCandidateProspectiveRequestRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.prospective import (
    PersistedProspectiveState,
    ProspectiveCandidateRepository,
)
from trading.persistence.research import ResearchRepository
from trading.research.candidate_artifact import CandidateArtifactBundleV1
from trading.research.commander_candidate import (
    CommanderCandidateError,
    ConnectedCandidateRuntime,
    connect_candidate_runtime,
)
from trading.research.config import ResearchConfigBundle
from trading.research.prospective import (
    PriorProspectiveState,
    ProspectiveCandidateConfigBundle,
    ProspectiveCandidateError,
    ProspectiveExecutionEvidenceV1,
    ProspectiveExecutionStatus,
    ProspectiveRequestEvidenceV1,
    build_failed_execution_evidence,
    build_prospective_request_evidence,
    build_successful_execution_evidence,
)


@dataclass(frozen=True, slots=True)
class PreparedProspectiveCandidate:
    request_evidence: ProspectiveRequestEvidenceV1
    artifact: CandidateArtifactBundleV1
    request_created: bool


@dataclass(frozen=True, slots=True)
class ProspectiveCollectionResult:
    request_evidence: ProspectiveRequestEvidenceV1
    execution_evidence: ProspectiveExecutionEvidenceV1
    request_created: bool
    execution_created: bool


class ProspectiveCandidateCollector:
    """Bind a Candidate request to one completed Q1-DET strategic decision."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: ProspectiveCandidateConfigBundle,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._repository = ProspectiveCandidateRepository(session_factory)
        self._research = ResearchRepository(session_factory)
        self._market = Q1PointInTimeMarketData(session_factory)

    def prepare(
        self,
        *,
        parent_run_id: str,
        challenger_id: str,
        parent_portfolio_decision_id: str | None = None,
    ) -> PreparedProspectiveCandidate:
        artifact = self._research.candidate_artifact(challenger_id)
        if artifact is None:
            raise ProspectiveCandidateError("CANDIDATE_ARTIFACT_NOT_REGISTERED")
        self._validate_challenger(artifact)
        parent = self._parent_decision(
            parent_run_id=parent_run_id,
            challenger_id=challenger_id,
            parent_portfolio_decision_id=parent_portfolio_decision_id,
        )
        existing = self._repository.request_for_parent(
            challenger_id=challenger_id,
            parent_portfolio_decision_id=parent.portfolio_decision_id,
        )
        if existing is not None:
            if existing.candidate_artifact_hash != artifact.bundle_hash:
                raise ProspectiveCandidateError(
                    "PROSPECTIVE_EXISTING_ARTIFACT_MISMATCH"
                )
            return PreparedProspectiveCandidate(
                request_evidence=existing,
                artifact=artifact,
                request_created=False,
            )
        anchor = self._evaluation_anchor(parent_run_id)
        current_session, latest_completed_session = self._calendar_context(parent)
        persisted = self._repository.prior_state(
            challenger_id=challenger_id,
            before_parent_scheduled_at=parent.scheduled_at,
        )
        prior_state = self._prior_state(
            persisted=persisted,
            current_calendar_session_id=current_session.calendar_session_id,
        )
        market = self._market.aligned_completed_daily_inputs(
            symbols=self._config.config.reference_universe,
            current_session_date=current_session.session_date,
            expected_latest_completed_session=latest_completed_session.session_date,
            signal_data_cutoff=parent.decision_created_at,
            minimum_completed_sessions=(
                self._config.config.market_data.required_completed_sessions
            ),
            query_limit=self._config.config.market_data.query_limit,
            dataset_version=self._config.config.market_data.dataset_version,
        )
        evidence = build_prospective_request_evidence(
            config_bundle=self._config,
            artifact=artifact,
            parent_decision=parent,
            evaluation_anchor=anchor,
            market_inputs=market,
            prior_state=prior_state,
        )
        created = self._repository.store_request(evidence)
        return PreparedProspectiveCandidate(
            request_evidence=evidence,
            artifact=artifact,
            request_created=created,
        )

    def execute(
        self,
        *,
        prepared: PreparedProspectiveCandidate,
        runtime: ConnectedCandidateRuntime,
    ) -> ProspectiveCollectionResult:
        request = prepared.request_evidence
        existing = self._repository.successful_execution(
            request.prospective_request_id
        )
        if existing is not None:
            return ProspectiveCollectionResult(
                request_evidence=request,
                execution_evidence=existing,
                request_created=prepared.request_created,
                execution_created=False,
            )
        try:
            primary = runtime.primary_executor.execute(request.request)
            replay = runtime.replay_executor.execute(request.request)
            execution = build_successful_execution_evidence(
                request_evidence=request,
                attestation=runtime.attestation,
                security=runtime.security,
                primary_response=primary,
                replay_response=replay,
            )
        except Exception as exc:
            error_code = (
                exc.code
                if isinstance(exc, CommanderCandidateError)
                else "COMMANDER_CANDIDATE_EXECUTION_REJECTED"
            )
            failure = build_failed_execution_evidence(
                request_evidence=request,
                attestation=runtime.attestation,
                security=runtime.security,
                error_code=error_code,
            )
            self._repository.store_execution(failure)
            raise ProspectiveCandidateError(error_code) from None
        created = self._repository.store_execution(execution)
        return ProspectiveCollectionResult(
            request_evidence=request,
            execution_evidence=execution,
            request_created=prepared.request_created,
            execution_created=created,
        )

    def collect(
        self,
        *,
        parent_run_id: str,
        challenger_id: str,
        commander_root: Path,
        commander_run: Path,
        research_config: ResearchConfigBundle,
        parent_portfolio_decision_id: str | None = None,
    ) -> ProspectiveCollectionResult:
        prepared = self.prepare(
            parent_run_id=parent_run_id,
            challenger_id=challenger_id,
            parent_portfolio_decision_id=parent_portfolio_decision_id,
        )
        existing = self._repository.successful_execution(
            prepared.request_evidence.prospective_request_id
        )
        if existing is not None:
            return ProspectiveCollectionResult(
                request_evidence=prepared.request_evidence,
                execution_evidence=existing,
                request_created=prepared.request_created,
                execution_created=False,
            )
        runtime = connect_candidate_runtime(
            bundle=prepared.artifact,
            commander_root=commander_root,
            run_root=commander_run,
            research_config=research_config,
        )
        try:
            runtime.attestation.assert_config_file(
                path=f"config/{self._config.config.strategy_config_path}",
                sha256=self._config.config.strategy_config_content_sha256,
            )
        except CommanderCandidateError as exc:
            raise ProspectiveCandidateError(exc.code) from None
        return self.execute(prepared=prepared, runtime=runtime)

    def _parent_decision(
        self,
        *,
        parent_run_id: str,
        challenger_id: str,
        parent_portfolio_decision_id: str | None,
    ) -> Q1StrategyDecision:
        with self._session_factory() as session:
            row = self._select_parent_decision_row(
                session,
                parent_run_id=parent_run_id,
                challenger_id=challenger_id,
                parent_portfolio_decision_id=parent_portfolio_decision_id,
            )
        if row is None:
            raise ProspectiveCandidateError("PARENT_DECISION_NOT_AVAILABLE")
        try:
            decision = Q1StrategyDecision.model_validate(row.payload_json)
        except ValueError as exc:
            raise ProspectiveCandidateError("PARENT_DECISION_INVALID") from exc
        if (
            decision.portfolio_decision_id != row.portfolio_decision_id
            or decision.decision_hash != row.decision_hash
            or decision.run_id != parent_run_id
            or decision.arm_id is not Q1ArmId.Q1_DET
            or decision.decision_kind == "EMERGENCY_REDUCTION"
        ):
            raise ProspectiveCandidateError("PARENT_DECISION_BINDING_INVALID")
        return decision

    def next_pending_parent_decision_id(
        self,
        *,
        parent_run_id: str,
        challenger_id: str,
    ) -> str | None:
        """Return the oldest completed decision lacking successful evidence."""

        with self._session_factory() as session:
            row = self._select_parent_decision_row(
                session,
                parent_run_id=parent_run_id,
                challenger_id=challenger_id,
                parent_portfolio_decision_id=None,
            )
            return None if row is None else row.portfolio_decision_id

    @staticmethod
    def _select_parent_decision_row(
        session: Session,
        *,
        parent_run_id: str,
        challenger_id: str,
        parent_portfolio_decision_id: str | None,
    ) -> PortfolioDecisionRow | None:
        statement = (
            select(PortfolioDecisionRow)
            .join(
                PaperCycleRow,
                PaperCycleRow.cycle_id
                == PortfolioDecisionRow.source_cycle_id,
            )
            .where(
                PortfolioDecisionRow.run_id == parent_run_id,
                PortfolioDecisionRow.arm_id == Q1ArmId.Q1_DET.value,
                PortfolioDecisionRow.algorithm_version
                == Q1_ALGORITHM_VERSION,
                PaperCycleRow.cycle_kind == "Q1_STRATEGIC",
                PaperCycleRow.status == "COMPLETED",
            )
        )
        if parent_portfolio_decision_id is not None:
            statement = statement.where(
                PortfolioDecisionRow.portfolio_decision_id
                == parent_portfolio_decision_id
            )
            ordering = (
                desc(PortfolioDecisionRow.scheduled_at),
                desc(PortfolioDecisionRow.portfolio_decision_id),
            )
        else:
            successful_parent_exists = exists(
                select(
                    ResearchCandidateProspectiveExecutionRow.execution_id
                )
                .join(
                    ResearchCandidateProspectiveRequestRow,
                    ResearchCandidateProspectiveRequestRow.prospective_request_id
                    == ResearchCandidateProspectiveExecutionRow.prospective_request_id,
                )
                .where(
                    ResearchCandidateProspectiveRequestRow.challenger_id
                    == challenger_id,
                    ResearchCandidateProspectiveRequestRow.parent_portfolio_decision_id
                    == PortfolioDecisionRow.portfolio_decision_id,
                    ResearchCandidateProspectiveExecutionRow.status
                    == ProspectiveExecutionStatus.SUCCEEDED,
                )
            )
            statement = statement.where(~successful_parent_exists)
            ordering = (
                PortfolioDecisionRow.scheduled_at,
                PortfolioDecisionRow.portfolio_decision_id,
            )
        return session.scalar(statement.order_by(*ordering).limit(1))

    def _evaluation_anchor(self, parent_run_id: str) -> StrategyEvaluationAnchor:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(StrategyEvaluationAnchorRow).where(
                        StrategyEvaluationAnchorRow.run_id == parent_run_id,
                        StrategyEvaluationAnchorRow.algorithm_version
                        == Q1_ALGORITHM_VERSION,
                    )
                )
            )
        if len(rows) != 1:
            raise ProspectiveCandidateError("EVALUATION_ANCHOR_NOT_AVAILABLE")
        try:
            anchor = StrategyEvaluationAnchor.model_validate(
                rows[0].payload_json
            )
        except ValueError as exc:
            raise ProspectiveCandidateError("EVALUATION_ANCHOR_INVALID") from exc
        if (
            anchor.evaluation_anchor_id != rows[0].evaluation_anchor_id
            or anchor.anchor_hash != rows[0].anchor_hash
            or anchor.run_id != parent_run_id
        ):
            raise ProspectiveCandidateError("EVALUATION_ANCHOR_BINDING_INVALID")
        return anchor

    def _calendar_context(
        self,
        parent: Q1StrategyDecision,
    ) -> tuple[MarketCalendarSessionRow, MarketCalendarSessionRow]:
        with self._session_factory() as session:
            current = session.get(
                MarketCalendarSessionRow,
                parent.input_manifest.calendar_session_id,
            )
            if current is None:
                raise ProspectiveCandidateError(
                    "PARENT_CALENDAR_SESSION_NOT_AVAILABLE"
                )
            previous = session.scalar(
                select(MarketCalendarSessionRow)
                .where(
                    MarketCalendarSessionRow.session_date
                    < current.session_date,
                    MarketCalendarSessionRow.algorithm_version
                    == Q1_ALGORITHM_VERSION,
                )
                .order_by(desc(MarketCalendarSessionRow.session_date))
                .limit(1)
            )
        if (
            previous is None
            or _aware(parent.scheduled_at) < _aware(current.open_at)
            or _aware(parent.scheduled_at) >= _aware(current.close_at)
        ):
            raise ProspectiveCandidateError("PARENT_CALENDAR_CONTEXT_INVALID")
        return current, previous

    def _prior_state(
        self,
        *,
        persisted: PersistedProspectiveState | None,
        current_calendar_session_id: str,
    ) -> PriorProspectiveState | None:
        if persisted is None:
            return None
        response = persisted.execution.primary_response
        if (
            persisted.execution.status != ProspectiveExecutionStatus.SUCCEEDED
            or response is None
        ):
            raise ProspectiveCandidateError("PRIOR_VERIFIED_TARGET_UNAVAILABLE")
        target_weights = {
            target.symbol: target.target_weight for target in response.targets
        }
        completed_since_review = self._completed_sessions_since_review(
            persisted=persisted,
            current_calendar_session_id=current_calendar_session_id,
        )
        return PriorProspectiveState(
            request_id=persisted.request.prospective_request_id,
            execution_hash=persisted.execution.execution_hash,
            target_weights=target_weights,
            completed_sessions_since_review=completed_since_review,
        )

    def _completed_sessions_since_review(
        self,
        *,
        persisted: PersistedProspectiveState,
        current_calendar_session_id: str,
    ) -> int:
        anchor_session_id = persisted.last_review_calendar_session_id
        carried = self._review_clock(persisted.request)
        if anchor_session_id is None:
            anchor_session_id = persisted.request.calendar_session_id
        else:
            carried = 0
        with self._session_factory() as session:
            anchor = session.get(MarketCalendarSessionRow, anchor_session_id)
            current = session.get(
                MarketCalendarSessionRow,
                current_calendar_session_id,
            )
            if anchor is None or current is None:
                raise ProspectiveCandidateError(
                    "PROSPECTIVE_REVIEW_CALENDAR_UNAVAILABLE"
                )
            elapsed = int(
                session.scalar(
                    select(func.count())
                    .select_from(MarketCalendarSessionRow)
                    .where(
                        MarketCalendarSessionRow.session_date
                        > anchor.session_date,
                        MarketCalendarSessionRow.session_date
                        <= current.session_date,
                        MarketCalendarSessionRow.algorithm_version
                        == Q1_ALGORITHM_VERSION,
                    )
                )
                or 0
            )
        return carried + elapsed

    @staticmethod
    def _review_clock(evidence: ProspectiveRequestEvidenceV1) -> int:
        qqq = next(
            (
                item
                for item in evidence.request.instruments
                if item.symbol == "QQQ"
            ),
            None,
        )
        if qqq is None:
            raise ProspectiveCandidateError("PROSPECTIVE_REVIEW_CLOCK_MISSING")
        feature = next(
            (
                item
                for item in qqq.features
                if item.name == "completed_sessions_since_review"
            ),
            None,
        )
        if (
            feature is None
            or feature.value < 0
            or not feature.value.is_integer()
        ):
            raise ProspectiveCandidateError("PROSPECTIVE_REVIEW_CLOCK_INVALID")
        return int(feature.value)

    def _validate_challenger(
        self,
        artifact: CandidateArtifactBundleV1,
    ) -> None:
        with self._session_factory() as session:
            row = session.get(ChallengerManifestRow, artifact.challenger_id)
        config = self._config.config
        if (
            row is None
            or row.strategy_id != config.strategy_id
            or row.strategy_version != config.strategy_version
            or row.config_hash != artifact.config_hash
            or row.code_hash != artifact.code_hash
            or artifact.real_order_routing
        ):
            raise ProspectiveCandidateError("CHALLENGER_BINDING_INVALID")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def prospective_candidate_status(
    repository: ProspectiveCandidateRepository,
    *,
    config: ProspectiveCandidateConfigBundle,
) -> dict[str, object]:
    status = repository.status()
    return {
        **status,
        "producer_version": config.config.producer_version,
        "strategy_id": config.config.strategy_id,
        "strategy_version": config.config.strategy_version,
        "reference_universe": list(config.config.reference_universe),
        "host_config_manifest_hash": config.manifest_hash,
        "state_contract": {
            "initial": config.config.state.initial_state,
            "subsequent": config.config.state.subsequent_state,
            "decision_time_source": (
                config.config.state.decision_time_source
            ),
        },
        "broker_access_permitted": False,
        "challenger_lifecycle_advance_enabled": False,
        "shadow_activation_enabled": False,
        "automatic_promotion_enabled": False,
        "real_order_routing": False,
    }


def resolve_prospective_challenger_id(
    *,
    prospective_status: Mapping[str, object],
    persisted_status: Mapping[str, object],
) -> str | None:
    raw_latest = prospective_status.get("latest")
    if isinstance(raw_latest, Mapping):
        latest = cast(Mapping[str, object], raw_latest)
        latest_challenger_id = latest.get("challenger_id")
        if isinstance(latest_challenger_id, str):
            return latest_challenger_id

    strategy_id = prospective_status.get("strategy_id")
    strategy_version = prospective_status.get("strategy_version")
    if not isinstance(strategy_id, str) or not isinstance(strategy_version, str):
        return None

    raw_artifacts = persisted_status.get("candidate_artifacts")
    artifacts = (
        cast(list[object], raw_artifacts)
        if isinstance(raw_artifacts, list)
        else []
    )
    artifact_challenger_ids: set[str] = set()
    for raw_artifact in artifacts:
        if not isinstance(raw_artifact, Mapping):
            continue
        artifact = cast(Mapping[str, object], raw_artifact)
        challenger_id = artifact.get("challenger_id")
        if isinstance(challenger_id, str):
            artifact_challenger_ids.add(challenger_id)

    raw_challengers = persisted_status.get("challengers")
    challengers = (
        cast(list[object], raw_challengers)
        if isinstance(raw_challengers, list)
        else []
    )
    matching_challenger_ids: set[str] = set()
    for raw_challenger in challengers:
        if not isinstance(raw_challenger, Mapping):
            continue
        challenger = cast(Mapping[str, object], raw_challenger)
        challenger_id = challenger.get("challenger_id")
        if (
            challenger.get("strategy_id") == strategy_id
            and challenger.get("strategy_version") == strategy_version
            and isinstance(challenger_id, str)
            and challenger_id in artifact_challenger_ids
        ):
            matching_challenger_ids.add(challenger_id)
    if len(matching_challenger_ids) != 1:
        return None
    return next(iter(matching_challenger_ids))
