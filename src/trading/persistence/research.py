from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import desc, func, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    AlgorithmProposalRow,
    AlgorithmProposalV2Row,
    ChallengerEventRow,
    ChallengerManifestRow,
    DomainEventRow,
    ExperimentBudgetEventRow,
    FalsificationReportRow,
    OosBudgetReservationRow,
    OosLockboxResultRow,
    PortfolioComparisonContractRow,
    ResearchCandidateArtifactRow,
    ResearchChampionDesignationRow,
    ResearchCommanderSelectionRow,
    ResearchCycleEventRow,
    ResearchCycleRow,
    ResearchEvidenceSourceRow,
    ResearchPromotionDecisionRow,
    ResearchPromotionEvidenceRow,
    ResearchReplayArtifactRow,
    ResearchShadowArmRegistrationRow,
    ResearchShadowPerformanceSummaryRow,
    RunRow,
    TrustedPromotionEvaluationRow,
)
from trading.research.candidate_artifact import CandidateArtifactBundleV1
from trading.research.contracts import (
    AlgorithmProposalV1,
    CandidateTestFailureV1,
    ChallengerManifestV1,
    ChallengerStatus,
    CommanderSelectionV1,
    FalsificationReportV1,
    OosBudgetReservationV1,
    OosLockboxResultV1,
    OosVerdict,
    PromotionDecisionV1,
    PromotionVerdict,
    ResearchCommanderKind,
    ResearchDecisionV1,
    ResearchRequestV1,
)
from trading.research.experiment_outcomes import AlgorithmProposalV2
from trading.research.oos_v2 import OosLockboxResultV2
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
)
from trading.research.promotion_v2 import (
    PromotionEvaluationContractV2,
    PromotionEvidenceV2,
    TrustedPromotionEvaluationV2,
    TrustedShadowPerformanceSummaryV2,
    build_promotion_evidence_v2,
    evaluate_trusted_promotion_evidence_v2,
)
from trading.research.v2_contracts import (
    ResearchDecisionV2,
    ResearchRequestV2,
)

if TYPE_CHECKING:
    from trading.persistence.experiment_outcomes import (
        ExperimentOutcomeRepository,
    )
    from trading.persistence.meta_controller import MetaControllerRepository
    from trading.persistence.portfolio_sharpe import PortfolioSharpeRepository
    from trading.research.oos_lockbox import OosEvaluationRequest
from trading.research.evidence import ResearchEvidenceBundleV1
from trading.research.promotion import REQUIRED_PROMOTION_CRITERIA
from trading.research.promotion_evidence import (
    ChampionDesignationV1,
    PromotionEvaluationContractV1,
    PromotionEvidenceV1,
    TrustedPromotionEvaluationV1,
    TrustedShadowPerformanceSummaryV1,
    build_promotion_evidence,
    evaluate_trusted_promotion_evidence,
)
from trading.research.prospective_shadow import (
    PROSPECTIVE_SHADOW_SOURCE_AGGREGATE,
    TRUSTED_SHADOW_CYCLE_EVENT,
    ProspectiveShadowCycleSourceV1,
)
from trading.research.replay import DeterministicReplayArtifactV1
from trading.research.shadow import (
    ShadowArmIdentity,
    require_matched_shadow_contract,
)
from trading.research.shadow_runtime import (
    SHADOW_RUNTIME_VERSION,
    MatchedShadowCycleResultV1,
    ShadowPairRuntimeSpecV1,
    summarize_matched_shadow_results,
)


class ResearchPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CandidateExperimentContext:
    """Validated immutable inputs for one Candidate experiment registration."""

    request: ResearchRequestV2
    proposal: AlgorithmProposalV2
    manifest: ChallengerManifestV1
    artifact: CandidateArtifactBundleV1
    cycle_created_at: datetime
    proposal_created_at: datetime
    manifest_created_at: datetime
    artifact_created_at: datetime


ALLOWED_TRANSITIONS: dict[ChallengerStatus, frozenset[ChallengerStatus]] = {
    ChallengerStatus.PROPOSED: frozenset(
        {
            ChallengerStatus.BUILD_FAILED,
            ChallengerStatus.TEST_FAILED,
            ChallengerStatus.REPLAY_FAILED,
            ChallengerStatus.OOS_REJECTED,
            ChallengerStatus.SHADOW_PENDING,
            ChallengerStatus.REJECTED,
        }
    ),
    ChallengerStatus.SHADOW_PENDING: frozenset(
        {
            ChallengerStatus.SHADOW_RUNNING,
            ChallengerStatus.REJECTED,
            ChallengerStatus.RETIRED,
        }
    ),
    ChallengerStatus.SHADOW_RUNNING: frozenset(
        {
            ChallengerStatus.PROMOTION_ELIGIBLE,
            ChallengerStatus.REJECTED,
            ChallengerStatus.RETIRED,
        }
    ),
    ChallengerStatus.PROMOTION_ELIGIBLE: frozenset(
        {
            ChallengerStatus.PROMOTED,
            ChallengerStatus.REJECTED,
            ChallengerStatus.RETIRED,
        }
    ),
    ChallengerStatus.PROMOTED: frozenset({ChallengerStatus.RETIRED}),
    ChallengerStatus.BUILD_FAILED: frozenset(),
    ChallengerStatus.TEST_FAILED: frozenset(),
    ChallengerStatus.REPLAY_FAILED: frozenset(),
    ChallengerStatus.OOS_REJECTED: frozenset(),
    ChallengerStatus.REJECTED: frozenset(),
    ChallengerStatus.RETIRED: frozenset(),
}

LIFECYCLE_GATED_TRANSITIONS = frozenset(
    {
        ChallengerStatus.TEST_FAILED,
        ChallengerStatus.REPLAY_FAILED,
        ChallengerStatus.OOS_REJECTED,
        ChallengerStatus.SHADOW_PENDING,
        ChallengerStatus.SHADOW_RUNNING,
        ChallengerStatus.PROMOTION_ELIGIBLE,
        ChallengerStatus.PROMOTED,
    }
)


class ResearchRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def experiment_outcomes(self) -> ExperimentOutcomeRepository:
        """Return the trusted recursive-outcome ledger bound to this database."""

        from trading.persistence.experiment_outcomes import (
            ExperimentOutcomeRepository,
        )

        return ExperimentOutcomeRepository(self._session_factory)

    def meta_controller(self) -> MetaControllerRepository:
        """Return the trusted deterministic research-policy repository."""

        from trading.persistence.meta_controller import (
            MetaControllerRepository,
        )

        return MetaControllerRepository(self._session_factory)

    def portfolio_sharpe(self) -> PortfolioSharpeRepository:
        """Return the trusted whole-portfolio comparison repository."""

        from trading.persistence.portfolio_sharpe import (
            PortfolioSharpeRepository,
        )

        return PortfolioSharpeRepository(self._session_factory)

    def select_commander(
        self,
        commander: ResearchCommanderKind,
        *,
        config_hash: str,
        effective_at: datetime,
        created_at: datetime,
        expected_version: int | None = None,
    ) -> CommanderSelectionV1:
        effective = require_aware_utc(effective_at)
        created = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            self._selection_lock(session)
            latest = session.scalar(
                select(ResearchCommanderSelectionRow)
                .order_by(desc(ResearchCommanderSelectionRow.version))
                .limit(1)
            )
            current_version = 0 if latest is None else latest.version
            if expected_version is not None and expected_version != current_version:
                raise ResearchPersistenceError("research commander selection version conflict")
            version = current_version + 1
            payload = {
                "schema_version": "research_commander_selection_v1",
                "selection_id": stable_id(
                    "research-selection",
                    version,
                    commander.value,
                    config_hash,
                    effective,
                ),
                "version": version,
                "selected_commander": commander,
                "effective_at": effective,
                "created_at": created,
                "config_hash": config_hash,
            }
            selection = CommanderSelectionV1.model_validate(payload)
            session.add(
                ResearchCommanderSelectionRow(
                    selection_id=selection.selection_id,
                    version=selection.version,
                    selected_commander=selection.selected_commander.value,
                    effective_at=selection.effective_at,
                    config_hash=selection.config_hash,
                    payload_json=model_payload(selection),
                    created_at=selection.created_at,
                )
            )
            return selection

    def current_selection(self) -> CommanderSelectionV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchCommanderSelectionRow)
                .order_by(desc(ResearchCommanderSelectionRow.version))
                .limit(1)
            )
            return None if row is None else CommanderSelectionV1.model_validate(row.payload_json)

    def get_proposal(self, proposal_id: str) -> AlgorithmProposalV1 | None:
        """Read one exact append-only proposal without scanning status history."""

        with self._session_factory() as session:
            row = session.get(AlgorithmProposalRow, proposal_id)
            if row is None:
                return None
            try:
                proposal = AlgorithmProposalV1.model_validate(row.payload_json)
            except ValueError as exc:
                raise ResearchPersistenceError("stored proposal payload is invalid") from exc
            self._validate_stored_proposal(proposal, row)
            return proposal

    def get_proposal_v2(
        self,
        proposal_id: str,
    ) -> AlgorithmProposalV2 | None:
        with self._session_factory() as session:
            row = session.get(AlgorithmProposalV2Row, proposal_id)
            if row is None:
                return None
            try:
                proposal = AlgorithmProposalV2.model_validate(
                    row.payload_json
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored AlgorithmProposalV2 payload is invalid"
                ) from exc
            if (
                proposal.proposal_id != row.proposal_id
                or proposal.hypothesis_id != row.hypothesis_id
                or proposal.parent_strategy_id != row.parent_strategy_id
                or proposal.parent_strategy_version
                != row.parent_strategy_version
                or proposal.proposed_strategy_id
                != row.proposed_strategy_id
                or proposal.proposed_strategy_version
                != row.proposed_strategy_version
                or proposal.primary_action_kind.value
                != row.primary_action_kind
                or proposal.proposal_hash != row.proposal_hash
            ):
                raise ResearchPersistenceError(
                    "stored AlgorithmProposalV2 binding is invalid"
                )
            return proposal

    def create_cycle(
        self,
        request: ResearchRequestV1 | ResearchRequestV2,
    ) -> bool:
        with self._session_factory.begin() as session:
            existing = session.scalar(
                select(ResearchCycleRow).where(ResearchCycleRow.request_id == request.request_id)
            )
            if existing is not None:
                if existing.request_hash != canonical_hash(request):
                    raise ResearchPersistenceError(
                        "request_id already belongs to different research request"
                    )
                return False
            selection = session.scalar(
                select(ResearchCommanderSelectionRow)
                .order_by(desc(ResearchCommanderSelectionRow.version))
                .limit(1)
            )
            if selection is None:
                raise ResearchPersistenceError("no Research Commander selected")
            if (
                selection.selection_id != request.commander_selection_id
                or selection.version != request.commander_selection_version
                or selection.selected_commander != request.selected_commander.value
            ):
                raise ResearchPersistenceError("STALE_SELECTION")
            session.add(
                ResearchCycleRow(
                    research_cycle_id=request.research_cycle_id,
                    request_id=request.request_id,
                    selection_id=selection.selection_id,
                    selection_version=selection.version,
                    selected_commander=request.selected_commander.value,
                    source_snapshot_commit=request.source_snapshot_commit,
                    champion_version=request.champion_version,
                    experiment_family=request.experiment_family,
                    as_of=request.as_of,
                    data_available_cutoff=request.data_available_cutoff,
                    expires_at=request.expires_at,
                    context_manifest_hash=request.context_manifest_hash,
                    request_hash=canonical_hash(request),
                    payload_json=model_payload(request),
                    created_at=request.created_at,
                )
            )
            self._add_cycle_event(
                session,
                cycle_id=request.research_cycle_id,
                event_type="REQUEST_CREATED",
                actor_role="HOST",
                artifact_hash=request.context_manifest_hash,
                idempotency_key=f"request:{request.request_id}",
                payload={"request_id": request.request_id},
                created_at=request.created_at,
            )
            return True

    def accept_decision(
        self,
        decision: ResearchDecisionV1,
        *,
        received_at: datetime,
    ) -> str | None:
        received = require_aware_utc(received_at)
        with self._session_factory.begin() as session:
            cycle = session.get(ResearchCycleRow, decision.research_cycle_id)
            if cycle is None:
                raise ResearchPersistenceError("unknown research cycle")
            request = ResearchRequestV1.model_validate(cycle.payload_json)
            if (
                cycle.selection_id != request.commander_selection_id
                or cycle.selection_version != request.commander_selection_version
                or cycle.selected_commander != request.selected_commander.value
            ):
                raise ResearchPersistenceError("stored research cycle selection binding mismatch")
            selection_row = session.scalar(
                select(ResearchCommanderSelectionRow)
                .order_by(desc(ResearchCommanderSelectionRow.version))
                .limit(1)
            )
            if selection_row is None:
                raise ResearchPersistenceError("no Research Commander selected")
            selection = CommanderSelectionV1.model_validate(selection_row.payload_json)
            try:
                decision.assert_bound_to(
                    request,
                    received_at=received,
                    current_selection=selection,
                )
            except ValueError as exc:
                raise ResearchPersistenceError(str(exc)) from exc
            self._add_cycle_event(
                session,
                cycle_id=cycle.research_cycle_id,
                event_type="DECISION_ACCEPTED",
                actor_role="RESEARCH_COMMANDER",
                artifact_hash=decision.output_hash,
                idempotency_key=f"decision:{decision.output_hash}",
                payload=model_payload(decision),
                created_at=received,
            )
            if decision.proposal is None:
                return None
            existing = session.get(AlgorithmProposalRow, decision.proposal.proposal_id)
            if existing is not None:
                if existing.proposal_hash != decision.proposal.proposal_hash:
                    raise ResearchPersistenceError("proposal ID hash conflict")
                return decision.proposal.proposal_id
            proposal = decision.proposal
            session.add(
                AlgorithmProposalRow(
                    proposal_id=proposal.proposal_id,
                    research_cycle_id=cycle.research_cycle_id,
                    hypothesis_id=proposal.hypothesis_id,
                    parent_strategy_id=proposal.parent_strategy_id,
                    parent_strategy_version=proposal.parent_strategy_version,
                    proposed_strategy_id=proposal.proposed_strategy_id,
                    proposed_strategy_version=proposal.proposed_strategy_version,
                    proposal_hash=proposal.proposal_hash,
                    evidence_manifest_hash=canonical_hash(sorted(proposal.evidence_source_ids)),
                    payload_json=model_payload(proposal),
                    created_at=received,
                )
            )
            return proposal.proposal_id

    def accept_decision_v2(
        self,
        decision: ResearchDecisionV2,
        *,
        received_at: datetime,
    ) -> str | None:
        received = require_aware_utc(received_at)
        with self._session_factory.begin() as session:
            cycle = session.get(ResearchCycleRow, decision.research_cycle_id)
            if cycle is None:
                raise ResearchPersistenceError("unknown research cycle")
            try:
                request = ResearchRequestV2.model_validate(cycle.payload_json)
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored research cycle is not ResearchRequestV2"
                ) from exc
            selection_row = session.scalar(
                select(ResearchCommanderSelectionRow)
                .order_by(desc(ResearchCommanderSelectionRow.version))
                .limit(1)
            )
            if selection_row is None:
                raise ResearchPersistenceError("no Research Commander selected")
            selection = CommanderSelectionV1.model_validate(
                selection_row.payload_json
            )
            try:
                decision.assert_bound_to_v2(
                    request,
                    received_at=received,
                    current_selection=selection,
                )
            except ValueError as exc:
                raise ResearchPersistenceError(str(exc)) from exc
            self._add_cycle_event(
                session,
                cycle_id=cycle.research_cycle_id,
                event_type="DECISION_V2_ACCEPTED",
                actor_role="RESEARCH_COMMANDER",
                artifact_hash=decision.output_hash,
                idempotency_key=f"decision-v2:{decision.output_hash}",
                payload=model_payload(decision),
                created_at=received,
            )
            if decision.proposal is None:
                return None
            proposal = decision.proposal
            existing = session.get(
                AlgorithmProposalV2Row,
                proposal.proposal_id,
            )
            if existing is not None:
                if existing.proposal_hash != proposal.proposal_hash:
                    raise ResearchPersistenceError(
                        "AlgorithmProposalV2 ID hash conflict"
                    )
            else:
                session.add(
                    AlgorithmProposalV2Row(
                        proposal_id=proposal.proposal_id,
                        research_cycle_id=cycle.research_cycle_id,
                        hypothesis_id=proposal.hypothesis_id,
                        parent_strategy_id=proposal.parent_strategy_id,
                        parent_strategy_version=(
                            proposal.parent_strategy_version
                        ),
                        proposed_strategy_id=proposal.proposed_strategy_id,
                        proposed_strategy_version=(
                            proposal.proposed_strategy_version
                        ),
                        primary_action_kind=(
                            proposal.primary_action_kind.value
                        ),
                        action_plan_hash=(
                            decision.research_action_plan_hash
                        ),
                        proposal_hash=proposal.proposal_hash,
                        payload_json=model_payload(proposal),
                        created_at=received,
                    )
                )
            compatibility = session.get(
                AlgorithmProposalRow,
                proposal.proposal_id,
            )
            if compatibility is not None:
                if compatibility.proposal_hash != proposal.proposal_hash:
                    raise ResearchPersistenceError(
                        "AlgorithmProposalV2 compatibility-row conflict"
                    )
            else:
                session.add(
                    AlgorithmProposalRow(
                        proposal_id=proposal.proposal_id,
                        research_cycle_id=cycle.research_cycle_id,
                        hypothesis_id=proposal.hypothesis_id,
                        parent_strategy_id=proposal.parent_strategy_id,
                        parent_strategy_version=(
                            proposal.parent_strategy_version
                        ),
                        proposed_strategy_id=proposal.proposed_strategy_id,
                        proposed_strategy_version=(
                            proposal.proposed_strategy_version
                        ),
                        proposal_hash=proposal.proposal_hash,
                        evidence_manifest_hash=canonical_hash(
                            sorted(proposal.evidence_source_ids)
                        ),
                        payload_json=model_payload(proposal),
                        created_at=received,
                    )
                )
            return proposal.proposal_id

    def store_evidence_bundle(
        self,
        bundle: ResearchEvidenceBundleV1,
        *,
        created_at: datetime,
    ) -> bool:
        timestamp = require_aware_utc(created_at)
        bundle_hash = canonical_hash(bundle)
        with self._session_factory.begin() as session:
            cycle = session.get(ResearchCycleRow, bundle.research_cycle_id)
            if cycle is None:
                raise ResearchPersistenceError("unknown research cycle")
            existing_event = session.scalar(
                select(ResearchCycleEventRow).where(
                    ResearchCycleEventRow.research_cycle_id == bundle.research_cycle_id,
                    ResearchCycleEventRow.idempotency_key == f"evidence:{bundle_hash}",
                )
            )
            if existing_event is not None:
                return False
            for source in bundle.sources:
                existing_source = session.get(
                    ResearchEvidenceSourceRow,
                    source.source_id,
                )
                if existing_source is not None:
                    if (
                        existing_source.content_hash != source.content_hash
                        or existing_source.research_cycle_id != bundle.research_cycle_id
                    ):
                        raise ResearchPersistenceError("evidence source ID hash conflict")
                    continue
                session.add(
                    ResearchEvidenceSourceRow(
                        source_id=source.source_id,
                        research_cycle_id=bundle.research_cycle_id,
                        url=source.url,
                        title=source.title,
                        source_name=source.publisher,
                        published_at=source.published_at,
                        first_available_at=source.first_available_at,
                        captured_at=source.captured_at,
                        source_tier=source.source_tier.value,
                        content_hash=source.content_hash,
                        excerpt=source.excerpt,
                        license_note=source.license_note,
                        corroborated=source.corroborated,
                        contradiction=source.contradiction,
                        payload_json=source.model_dump(mode="json"),
                        created_at=timestamp,
                    )
                )
            self._add_cycle_event(
                session,
                cycle_id=bundle.research_cycle_id,
                event_type="EVIDENCE_CAPTURED",
                actor_role="WEB_SCOUT",
                artifact_hash=bundle_hash,
                idempotency_key=f"evidence:{bundle_hash}",
                payload={
                    "bundle_hash": bundle_hash,
                    "source_count": len(bundle.sources),
                    "claim_count": len(bundle.claims),
                },
                created_at=timestamp,
            )
            return True

    def register_challenger(
        self,
        manifest: ChallengerManifestV1,
        *,
        proposal_id: str,
    ) -> bool:
        if manifest.status is not ChallengerStatus.PROPOSED:
            raise ResearchPersistenceError("new Challenger must start PROPOSED")
        with self._session_factory.begin() as session:
            self._challenger_registration_lock(
                session,
                strategy_id=manifest.strategy_id,
                strategy_version=manifest.strategy_version,
            )
            proposal_statement = select(AlgorithmProposalRow).where(
                AlgorithmProposalRow.proposal_id == proposal_id
            )
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                proposal_statement = proposal_statement.with_for_update()
            proposal_row = session.scalar(proposal_statement)
            if proposal_row is None:
                raise ResearchPersistenceError("unknown accepted proposal")
            try:
                proposal = _algorithm_proposal_from_payload(
                    proposal_row.payload_json
                )
            except ValueError as exc:
                raise ResearchPersistenceError("stored proposal payload is invalid") from exc
            cycle = session.get(
                ResearchCycleRow,
                proposal_row.research_cycle_id,
            )
            if cycle is None:
                raise ResearchPersistenceError("accepted proposal Research cycle is missing")
            self._validate_challenger_registration(
                manifest=manifest,
                proposal=proposal,
                proposal_row=proposal_row,
                cycle=cycle,
            )
            existing = session.scalar(
                select(ChallengerManifestRow).where(
                    ChallengerManifestRow.challenger_id == manifest.challenger_id
                )
            )
            if existing is not None:
                if (
                    existing.manifest_hash != manifest.manifest_hash
                    or existing.proposal_id != proposal_id
                ):
                    raise ResearchPersistenceError("challenger ID hash conflict")
                return False
            version_owner = session.scalar(
                select(ChallengerManifestRow).where(
                    ChallengerManifestRow.strategy_id == manifest.strategy_id,
                    ChallengerManifestRow.strategy_version == manifest.strategy_version,
                )
            )
            if version_owner is not None:
                raise ResearchPersistenceError("Challenger strategy version is already registered")
            session.add(
                ChallengerManifestRow(
                    challenger_id=manifest.challenger_id,
                    proposal_id=proposal_id,
                    strategy_id=manifest.strategy_id,
                    strategy_version=manifest.strategy_version,
                    parent_version=manifest.parent_version,
                    experiment_family=manifest.experiment_family,
                    source_commit=manifest.source_commit,
                    patch_hash=manifest.patch_hash,
                    code_hash=manifest.code_hash,
                    config_hash=manifest.config_hash,
                    test_manifest_hash=manifest.test_manifest_hash,
                    initial_status=manifest.status.value,
                    manifest_hash=manifest.manifest_hash,
                    payload_json=model_payload(manifest),
                    created_at=manifest.created_at,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ResearchPersistenceError(
                    "Challenger identity or strategy version conflict"
                ) from exc
            return True

    def register_candidate_artifact(
        self,
        bundle: CandidateArtifactBundleV1,
        *,
        created_at: datetime,
    ) -> bool:
        """Bind one immutable Builder artifact to its exact trusted inputs."""

        timestamp = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            manifest_row = self._challenger_for_update(
                session,
                bundle.challenger_id,
            )
            existing_by_id = session.get(
                ResearchCandidateArtifactRow,
                bundle.bundle_id,
            )
            existing_by_challenger = session.scalar(
                select(ResearchCandidateArtifactRow).where(
                    ResearchCandidateArtifactRow.challenger_id == bundle.challenger_id
                )
            )
            existing = existing_by_id or existing_by_challenger
            if existing is not None:
                if (
                    existing.bundle_id != bundle.bundle_id
                    or existing.challenger_id != bundle.challenger_id
                    or existing.bundle_hash != bundle.bundle_hash
                ):
                    raise ResearchPersistenceError(
                        "candidate artifact identity or Challenger conflict"
                    )
                self._validate_stored_candidate_artifact(bundle, existing)
                return False

            proposal_row = session.get(
                AlgorithmProposalRow,
                manifest_row.proposal_id,
            )
            if proposal_row is None:
                raise ResearchPersistenceError("candidate artifact proposal is missing")
            cycle_row = session.get(
                ResearchCycleRow,
                proposal_row.research_cycle_id,
            )
            if cycle_row is None:
                raise ResearchPersistenceError("candidate artifact Research cycle is missing")
            try:
                request = _research_request_from_payload(
                    cycle_row.payload_json
                )
                proposal = _algorithm_proposal_from_payload(
                    proposal_row.payload_json
                )
                manifest = ChallengerManifestV1.model_validate(manifest_row.payload_json)
                bundle.assert_bound_to(
                    request=request,
                    proposal=proposal,
                    manifest=manifest,
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "candidate artifact trusted-input binding mismatch"
                ) from exc
            self._validate_stored_proposal(proposal, proposal_row)
            self._validate_challenger_registration(
                manifest=manifest,
                proposal=proposal,
                proposal_row=proposal_row,
                cycle=cycle_row,
            )
            session.add(
                ResearchCandidateArtifactRow(
                    bundle_id=bundle.bundle_id,
                    challenger_id=bundle.challenger_id,
                    proposal_id=proposal.proposal_id,
                    research_cycle_id=request.research_cycle_id,
                    candidate_tree_hash=bundle.candidate_tree_hash,
                    code_hash=bundle.code_hash,
                    config_hash=bundle.config_hash,
                    test_manifest_hash=bundle.test_manifest_hash,
                    declared_entrypoint=bundle.declared_entrypoint,
                    bundle_hash=bundle.bundle_hash,
                    real_order_routing=False,
                    payload_json=model_payload(bundle),
                    created_at=timestamp,
                )
            )
            try:
                session.flush()
            except IntegrityError as exc:
                raise ResearchPersistenceError("candidate artifact registration conflict") from exc
            return True

    def record_candidate_test_failure(
        self,
        failure: CandidateTestFailureV1,
    ) -> bool:
        """Persist a typed pre-artifact test failure and terminally reject it."""

        with self._session_factory.begin() as session:
            manifest_row = self._challenger_for_update(
                session,
                failure.challenger_id,
            )
            try:
                manifest = ChallengerManifestV1.model_validate(
                    manifest_row.payload_json
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored Challenger manifest payload is invalid"
                ) from exc
            if (
                manifest.challenger_id != manifest_row.challenger_id
                or manifest.manifest_hash != manifest_row.manifest_hash
            ):
                raise ResearchPersistenceError(
                    "stored Candidate test failure bindings are invalid"
                )
            proposal_row = session.get(
                AlgorithmProposalRow,
                manifest_row.proposal_id,
            )
            if proposal_row is None:
                raise ResearchPersistenceError(
                    "Candidate test failure proposal is missing"
                )
            if manifest.proposal_hash != proposal_row.proposal_hash:
                raise ResearchPersistenceError(
                    "stored Candidate test failure bindings are invalid"
                )
            if failure.created_at < manifest.created_at:
                raise ResearchPersistenceError(
                    "Candidate test failure predates its Challenger manifest"
                )
            bindings: tuple[tuple[str, object, object], ...] = (
                (
                    "challenger_id",
                    failure.challenger_id,
                    manifest.challenger_id,
                ),
                (
                    "candidate_manifest_hash",
                    failure.candidate_manifest_hash,
                    manifest.manifest_hash,
                ),
                (
                    "proposal_hash",
                    failure.proposal_hash,
                    manifest.proposal_hash,
                ),
                (
                    "stored_proposal_hash",
                    failure.proposal_hash,
                    proposal_row.proposal_hash,
                ),
                ("patch_hash", failure.patch_hash, manifest.patch_hash),
                (
                    "test_manifest_hash",
                    failure.test_manifest_hash,
                    manifest.test_manifest_hash,
                ),
            )
            mismatches = [
                name
                for name, actual, expected in bindings
                if actual != expected
            ]
            if mismatches:
                raise ResearchPersistenceError(
                    "Candidate test failure binding mismatch: "
                    + ",".join(mismatches)
                )
            candidate_artifact = session.scalar(
                select(ResearchCandidateArtifactRow).where(
                    ResearchCandidateArtifactRow.challenger_id
                    == failure.challenger_id
                )
            )
            if candidate_artifact is not None:
                raise ResearchPersistenceError(
                    "pre-artifact Candidate test failure cannot follow "
                    "candidate artifact registration"
                )
            current = self._current_challenger_status(
                session,
                manifest_row,
            )
            existing = session.scalar(
                select(ChallengerEventRow).where(
                    ChallengerEventRow.challenger_id
                    == failure.challenger_id,
                    ChallengerEventRow.idempotency_key
                    == f"candidate-test-failure:{failure.failure_hash}",
                )
            )
            if existing is None and current is not ChallengerStatus.PROPOSED:
                raise ResearchPersistenceError(
                    "Candidate test failure requires PROPOSED"
                )
            return self._append_challenger_transition(
                session,
                manifest=manifest_row,
                to_status=ChallengerStatus.TEST_FAILED,
                reason_code=failure.failure_reason_code,
                artifact_hash=failure.failure_hash,
                idempotency_key=(
                    f"candidate-test-failure:{failure.failure_hash}"
                ),
                created_at=failure.created_at,
                artifact_payload=model_payload(failure),
            )

    def candidate_artifact(
        self,
        challenger_id: str,
    ) -> CandidateArtifactBundleV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchCandidateArtifactRow).where(
                    ResearchCandidateArtifactRow.challenger_id == challenger_id
                )
            )
            if row is None:
                return None
            try:
                bundle = CandidateArtifactBundleV1.model_validate(row.payload_json)
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored candidate artifact payload is invalid"
                ) from exc
            self._validate_stored_candidate_artifact(bundle, row)
            return bundle

    def challenger_manifest(
        self,
        challenger_id: str,
    ) -> ChallengerManifestV1:
        with self._session_factory() as session:
            row = session.get(ChallengerManifestRow, challenger_id)
            if row is None:
                raise ResearchPersistenceError("unknown Challenger")
            try:
                manifest = ChallengerManifestV1.model_validate(
                    row.payload_json
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored Challenger manifest payload is invalid"
                ) from exc
            if (
                manifest.challenger_id != row.challenger_id
                or manifest.manifest_hash != row.manifest_hash
                or manifest.strategy_id != row.strategy_id
                or manifest.strategy_version != row.strategy_version
                or manifest.experiment_family != row.experiment_family
            ):
                raise ResearchPersistenceError(
                    "stored Challenger manifest columns do not match payload"
                )
            return manifest

    def candidate_experiment_context(
        self,
        challenger_id: str,
    ) -> CandidateExperimentContext:
        """Load the exact V2 request, proposal, manifest, and sealed artifact."""

        with self._session_factory() as session:
            artifact_row = session.scalar(
                select(ResearchCandidateArtifactRow).where(
                    ResearchCandidateArtifactRow.challenger_id
                    == challenger_id
                )
            )
            if artifact_row is None:
                raise ResearchPersistenceError(
                    "registered candidate artifact is required"
                )
            manifest_row = session.get(
                ChallengerManifestRow,
                challenger_id,
            )
            if manifest_row is None:
                raise ResearchPersistenceError(
                    "candidate Challenger manifest is missing"
                )
            proposal_row = session.get(
                AlgorithmProposalV2Row,
                artifact_row.proposal_id,
            )
            compatibility_row = session.get(
                AlgorithmProposalRow,
                artifact_row.proposal_id,
            )
            if proposal_row is None or compatibility_row is None:
                raise ResearchPersistenceError(
                    "candidate AlgorithmProposalV2 is missing"
                )
            cycle_row = session.get(
                ResearchCycleRow,
                artifact_row.research_cycle_id,
            )
            if cycle_row is None:
                raise ResearchPersistenceError(
                    "candidate ResearchRequestV2 is missing"
                )
            try:
                artifact = CandidateArtifactBundleV1.model_validate(
                    artifact_row.payload_json
                )
                manifest = ChallengerManifestV1.model_validate(
                    manifest_row.payload_json
                )
                proposal = AlgorithmProposalV2.model_validate(
                    proposal_row.payload_json
                )
                request = ResearchRequestV2.model_validate(
                    cycle_row.payload_json
                )
                artifact.assert_bound_to(
                    request=request,
                    proposal=proposal,
                    manifest=manifest,
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "candidate experiment trusted-input binding mismatch"
                ) from exc
            self._validate_stored_candidate_artifact(
                artifact,
                artifact_row,
            )
            self._validate_stored_proposal(
                proposal,
                compatibility_row,
            )
            self._validate_challenger_registration(
                manifest=manifest,
                proposal=proposal,
                proposal_row=compatibility_row,
                cycle=cycle_row,
            )
            if (
                proposal.proposal_id != proposal_row.proposal_id
                or proposal_row.research_cycle_id
                != request.research_cycle_id
                or proposal.proposal_hash != proposal_row.proposal_hash
                or proposal.primary_action_kind.value
                != proposal_row.primary_action_kind
                or proposal_row.action_plan_hash
                != request.research_action_plan.plan_hash
                or artifact_row.research_cycle_id
                != request.research_cycle_id
                or artifact_row.challenger_id != manifest.challenger_id
                or manifest_row.proposal_id != proposal.proposal_id
                or manifest_row.manifest_hash != manifest.manifest_hash
            ):
                raise ResearchPersistenceError(
                    "candidate experiment stored-row binding mismatch"
                )
            return CandidateExperimentContext(
                request=request,
                proposal=proposal,
                manifest=manifest,
                artifact=artifact,
                cycle_created_at=_stored_time(cycle_row.created_at),
                proposal_created_at=_stored_time(proposal_row.created_at),
                manifest_created_at=_stored_time(manifest_row.created_at),
                artifact_created_at=_stored_time(artifact_row.created_at),
            )

    def transition_challenger(
        self,
        *,
        challenger_id: str,
        to_status: ChallengerStatus,
        reason_code: str,
        artifact_hash: str | None,
        idempotency_key: str,
        created_at: datetime,
    ) -> bool:
        if to_status in LIFECYCLE_GATED_TRANSITIONS:
            raise ResearchPersistenceError(
                f"{to_status.value} requires the trusted Research Lifecycle gate"
            )
        timestamp = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            manifest = session.get(ChallengerManifestRow, challenger_id)
            if manifest is None:
                raise ResearchPersistenceError("unknown Challenger")
            return self._append_challenger_transition(
                session,
                manifest=manifest,
                to_status=to_status,
                reason_code=reason_code,
                artifact_hash=artifact_hash,
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )

    def record_falsification_report(
        self,
        report: FalsificationReportV1,
    ) -> bool:
        """Persist the one authoritative report and apply its fail-closed gate."""

        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(session, report.challenger_id)
            candidate = self._require_registered_candidate_artifact(
                session,
                challenger_id=report.challenger_id,
            )
            replay = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id == report.challenger_id,
                    ResearchReplayArtifactRow.candidate_artifact_hash == candidate.bundle_hash,
                    ResearchReplayArtifactRow.deterministic_match.is_(True),
                )
            )
            if replay is None:
                raise ResearchPersistenceError(
                    "falsification requires a matching deterministic replay"
                )
            self._require_falsification_bindings(
                report,
                candidate_artifact_hash=candidate.bundle_hash,
                data_manifest_hash=replay.data_manifest_hash,
                replay_hash=replay.artifact_hash,
            )
            existing = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id == report.challenger_id
                )
            )
            if existing is not None:
                if existing.report_hash != report.report_hash:
                    raise ResearchPersistenceError(
                        "Challenger already has a different falsification report"
                    )
                return False
            current = self._current_challenger_status(session, manifest)
            if current is not ChallengerStatus.PROPOSED:
                raise ResearchPersistenceError(
                    f"falsification requires PROPOSED, got {current.value}"
                )
            session.add(
                FalsificationReportRow(
                    falsification_report_id=stable_id(
                        "falsification-report",
                        report.challenger_id,
                        report.report_hash,
                    ),
                    challenger_id=report.challenger_id,
                    mandatory_passed=report.mandatory_passed,
                    report_hash=report.report_hash,
                    payload_json=model_payload(report),
                    created_at=report.created_at,
                )
            )
            if not report.mandatory_passed:
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.TEST_FAILED,
                    reason_code="MANDATORY_FALSIFICATION_FAILED",
                    artifact_hash=report.report_hash,
                    idempotency_key=f"falsification:{report.report_hash}",
                    created_at=report.created_at,
                )
            return True

    def has_passed_falsification(
        self,
        *,
        challenger_id: str,
        candidate_artifact_hash: str,
        evaluation_contract_hash: str,
    ) -> bool:
        with self._session_factory() as session:
            report = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id == challenger_id
                )
            )
            if report is None or not report.mandatory_passed:
                return False
            try:
                parsed = FalsificationReportV1.model_validate(report.payload_json)
                bindings = self._falsification_bindings(parsed)
            except (ResearchPersistenceError, ValueError):
                return False
            return (
                bindings["candidate_artifact_hash"] == candidate_artifact_hash
                and bindings["evaluation_contract_hash"] == evaluation_contract_hash
            )

    def falsification_report(
        self,
        challenger_id: str,
    ) -> FalsificationReportV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id == challenger_id
                )
            )
        if row is None:
            return None
        try:
            report = FalsificationReportV1.model_validate(
                row.payload_json
            )
            self._falsification_bindings(report)
        except (ResearchPersistenceError, ValueError) as exc:
            raise ResearchPersistenceError(
                "stored falsification report is invalid"
            ) from exc
        if (
            row.report_hash != report.report_hash
            or row.mandatory_passed != report.mandatory_passed
            or _stored_time(row.created_at) != report.created_at
        ):
            raise ResearchPersistenceError(
                "stored falsification report binding mismatch"
            )
        return report

    def challenger_status(self, challenger_id: str) -> ChallengerStatus:
        with self._session_factory() as session:
            manifest = session.get(ChallengerManifestRow, challenger_id)
            if manifest is None:
                raise ResearchPersistenceError("unknown Challenger")
            return self._current_challenger_status(session, manifest)

    def record_replay_artifact(
        self,
        artifact: DeterministicReplayArtifactV1,
    ) -> bool:
        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(session, artifact.challenger_id)
            candidate = self._require_registered_candidate_artifact(
                session,
                challenger_id=artifact.challenger_id,
                candidate_artifact_hash=artifact.candidate_artifact_hash,
            )
            existing = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id == artifact.challenger_id
                )
            )
            if existing is not None:
                if existing.artifact_hash != artifact.artifact_hash:
                    raise ResearchPersistenceError(
                        "Challenger already has a different replay artifact"
                    )
                return False
            if (
                artifact.config_hash != manifest.config_hash
                or artifact.code_hash != manifest.code_hash
                or artifact.config_hash != candidate.config_hash
                or artifact.code_hash != candidate.code_hash
            ):
                raise ResearchPersistenceError("replay artifact code/config binding mismatch")
            current = self._current_challenger_status(session, manifest)
            if current is not ChallengerStatus.PROPOSED:
                raise ResearchPersistenceError(
                    f"replay verification requires PROPOSED, got {current.value}"
                )
            payload = artifact.payload()
            session.add(
                ResearchReplayArtifactRow(
                    replay_artifact_id=stable_id(
                        "research-replay-artifact",
                        artifact.challenger_id,
                        artifact.artifact_hash,
                    ),
                    challenger_id=artifact.challenger_id,
                    candidate_artifact_hash=artifact.candidate_artifact_hash,
                    config_hash=artifact.config_hash,
                    code_hash=artifact.code_hash,
                    data_manifest_hash=artifact.data_manifest_hash,
                    first_replay_hash=artifact.first_replay_hash,
                    second_replay_hash=artifact.second_replay_hash,
                    deterministic_match=artifact.deterministic_match,
                    artifact_hash=artifact.artifact_hash,
                    payload_json=cast(
                        dict[str, Any],
                        canonical_data(payload),
                    ),
                    created_at=artifact.created_at,
                )
            )
            if not artifact.deterministic_match:
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.REPLAY_FAILED,
                    reason_code="DETERMINISTIC_REPLAY_HASH_MISMATCH",
                    artifact_hash=artifact.artifact_hash,
                    idempotency_key=f"replay:{artifact.artifact_hash}",
                    created_at=artifact.created_at,
                )
            return True

    def has_passed_replay(
        self,
        *,
        challenger_id: str,
        candidate_artifact_hash: str,
    ) -> bool:
        with self._session_factory() as session:
            candidate = session.scalar(
                select(ResearchCandidateArtifactRow).where(
                    ResearchCandidateArtifactRow.challenger_id == challenger_id,
                    ResearchCandidateArtifactRow.bundle_hash == candidate_artifact_hash,
                )
            )
            if candidate is None:
                return False
            artifact = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id == challenger_id,
                    ResearchReplayArtifactRow.candidate_artifact_hash == candidate_artifact_hash,
                    ResearchReplayArtifactRow.deterministic_match.is_(True),
                )
            )
            return artifact is not None

    def replay_artifact(
        self,
        challenger_id: str,
    ) -> DeterministicReplayArtifactV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id
                    == challenger_id
                )
            )
        if row is None:
            return None
        try:
            artifact = DeterministicReplayArtifactV1(
                challenger_id=row.challenger_id,
                candidate_artifact_hash=row.candidate_artifact_hash,
                config_hash=row.config_hash,
                code_hash=row.code_hash,
                data_manifest_hash=row.data_manifest_hash,
                first_replay_hash=row.first_replay_hash,
                second_replay_hash=row.second_replay_hash,
                created_at=_stored_time(row.created_at),
            )
        except ValueError as exc:
            raise ResearchPersistenceError(
                "stored replay artifact is invalid"
            ) from exc
        if (
            row.artifact_hash != artifact.artifact_hash
            or row.candidate_artifact_hash
            != artifact.candidate_artifact_hash
            or row.data_manifest_hash != artifact.data_manifest_hash
            or row.deterministic_match != artifact.deterministic_match
            or _stored_time(row.created_at) != artifact.created_at
        ):
            raise ResearchPersistenceError(
                "stored replay artifact binding mismatch"
            )
        return artifact

    def append_budget_event(
        self,
        *,
        experiment_family: str,
        event_type: str,
        submission_delta: int,
        oos_budget_delta: int,
        hypothesis_delta: int,
        failure_delta: int,
        idempotency_key: str,
        created_at: datetime,
    ) -> bool:
        deltas = (
            submission_delta,
            oos_budget_delta,
            hypothesis_delta,
            failure_delta,
        )
        if any(value < 0 for value in deltas):
            raise ResearchPersistenceError("experiment budget deltas cannot be negative")
        timestamp = require_aware_utc(created_at)
        payload = {
            "experiment_family": experiment_family,
            "event_type": event_type,
            "submission_delta": submission_delta,
            "oos_budget_delta": oos_budget_delta,
            "hypothesis_delta": hypothesis_delta,
            "failure_delta": failure_delta,
            "idempotency_key": idempotency_key,
            "created_at": timestamp,
        }
        with self._session_factory.begin() as session:
            existing = session.scalar(
                select(ExperimentBudgetEventRow).where(
                    ExperimentBudgetEventRow.experiment_family == experiment_family,
                    ExperimentBudgetEventRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.event_hash != canonical_hash(payload):
                    raise ResearchPersistenceError("experiment budget idempotency conflict")
                return False
            row = ExperimentBudgetEventRow(
                budget_event_id=stable_id(
                    "experiment-budget",
                    experiment_family,
                    idempotency_key,
                ),
                experiment_family=experiment_family,
                event_type=event_type,
                submission_delta=submission_delta,
                oos_budget_delta=oos_budget_delta,
                hypothesis_delta=hypothesis_delta,
                failure_delta=failure_delta,
                idempotency_key=idempotency_key,
                event_hash=canonical_hash(payload),
                payload_json=canonical_data(payload),
                created_at=timestamp,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                session.rollback()
                with self._session_factory() as replay_session:
                    existing = replay_session.scalar(
                        select(ExperimentBudgetEventRow).where(
                            ExperimentBudgetEventRow.experiment_family == experiment_family,
                            ExperimentBudgetEventRow.idempotency_key == idempotency_key,
                        )
                    )
                    if existing is None or existing.event_hash != row.event_hash:
                        raise ResearchPersistenceError(
                            "experiment budget idempotency conflict"
                        ) from None
                    return False
            return True

    def budget_totals(self, experiment_family: str) -> dict[str, int]:
        with self._session_factory() as session:
            row = session.execute(
                select(
                    func.coalesce(
                        func.sum(ExperimentBudgetEventRow.submission_delta),
                        0,
                    ),
                    func.coalesce(
                        func.sum(ExperimentBudgetEventRow.oos_budget_delta),
                        0,
                    ),
                    func.coalesce(
                        func.sum(ExperimentBudgetEventRow.hypothesis_delta),
                        0,
                    ),
                    func.coalesce(
                        func.sum(ExperimentBudgetEventRow.failure_delta),
                        0,
                    ),
                ).where(ExperimentBudgetEventRow.experiment_family == experiment_family)
            ).one()
        return {
            "submissions": int(row[0]),
            "oos_budget_used": int(row[1]),
            "hypotheses": int(row[2]),
            "failures": int(row[3]),
        }

    def reserve_oos_budget(
        self,
        *,
        request: OosEvaluationRequest,
        maximum_submissions: int,
        maximum_oos_uses: int,
        idempotency_key: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> OosBudgetReservationV1:
        if maximum_submissions <= 0 or maximum_oos_uses <= 0:
            raise ResearchPersistenceError("OOS budget limits must be positive")
        timestamp = require_aware_utc(created_at)
        expiry = require_aware_utc(expires_at)
        if expiry <= timestamp:
            raise ResearchPersistenceError("OOS budget reservation expiry must follow creation")
        for attempt in range(3):
            try:
                return self._reserve_oos_budget_once(
                    request=request,
                    maximum_submissions=maximum_submissions,
                    maximum_oos_uses=maximum_oos_uses,
                    idempotency_key=idempotency_key,
                    created_at=timestamp,
                    expires_at=expiry,
                )
            except IntegrityError as exc:
                if attempt == 2:
                    raise ResearchPersistenceError(
                        "concurrent OOS budget reservation conflict"
                    ) from exc
        raise ResearchPersistenceError("OOS budget reservation failed")

    def _reserve_oos_budget_once(
        self,
        *,
        request: OosEvaluationRequest,
        maximum_submissions: int,
        maximum_oos_uses: int,
        idempotency_key: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> OosBudgetReservationV1:
        with self._session_factory.begin() as session:
            self._oos_budget_lock(
                session,
                experiment_family=request.experiment_family,
            )
            existing = session.scalar(
                select(OosBudgetReservationRow).where(
                    OosBudgetReservationRow.experiment_family == request.experiment_family,
                    OosBudgetReservationRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                reservation = OosBudgetReservationV1.model_validate(existing.payload_json)
                if (
                    existing.reservation_id != reservation.reservation_id
                    or existing.reservation_hash != reservation.reservation_hash
                    or existing.challenger_id != reservation.challenger_id
                    or existing.submission_ordinal != reservation.submission_ordinal
                    or existing.oos_budget_ordinal != reservation.oos_budget_ordinal
                ):
                    raise ResearchPersistenceError("stored OOS budget reservation binding mismatch")
                self._require_oos_reservation_binding(
                    reservation,
                    request=request,
                )
                return reservation
            conflicting_submission = session.scalar(
                select(OosBudgetReservationRow).where(
                    OosBudgetReservationRow.experiment_family == request.experiment_family,
                    OosBudgetReservationRow.submission_number == request.submission_number,
                )
            )
            if conflicting_submission is not None:
                raise ResearchPersistenceError(
                    "OOS submission number already has a different reservation"
                )
            totals = session.execute(
                select(
                    func.coalesce(
                        func.sum(ExperimentBudgetEventRow.submission_delta),
                        0,
                    ),
                    func.coalesce(
                        func.sum(ExperimentBudgetEventRow.oos_budget_delta),
                        0,
                    ),
                ).where(ExperimentBudgetEventRow.experiment_family == request.experiment_family)
            ).one()
            submission_ordinal = int(totals[0]) + 1
            oos_budget_ordinal = int(totals[1]) + 1
            if request.submission_number != submission_ordinal:
                raise ResearchPersistenceError(
                    "OOS submission number is not the next family submission"
                )
            if submission_ordinal > maximum_submissions:
                raise ResearchPersistenceError("experiment-family submission budget exhausted")
            if oos_budget_ordinal > maximum_oos_uses:
                raise ResearchPersistenceError("experiment-family OOS budget exhausted")
            reservation_payload = {
                "schema_version": "oos_budget_reservation_v1",
                "reservation_id": stable_id(
                    "oos-budget-reservation",
                    request.experiment_family,
                    request.submission_number,
                    request.challenger_id,
                    request.candidate_artifact_hash,
                    request.evaluation_contract_hash,
                ),
                "challenger_id": request.challenger_id,
                "experiment_family": request.experiment_family,
                "submission_number": request.submission_number,
                "submission_ordinal": submission_ordinal,
                "oos_budget_ordinal": oos_budget_ordinal,
                "candidate_artifact_hash": request.candidate_artifact_hash,
                "evaluation_contract_hash": request.evaluation_contract_hash,
                "idempotency_key": idempotency_key,
                "created_at": created_at,
                "expires_at": expires_at,
            }
            reservation = OosBudgetReservationV1.model_validate(
                {
                    **reservation_payload,
                    "reservation_hash": canonical_hash(reservation_payload),
                }
            )
            event_idempotency_key = idempotency_key
            event_payload = {
                "experiment_family": request.experiment_family,
                "event_type": "OOS_CONSUMED",
                "submission_delta": 1,
                "oos_budget_delta": 1,
                "hypothesis_delta": 0,
                "failure_delta": 0,
                "idempotency_key": event_idempotency_key,
                "created_at": created_at,
            }
            session.add(
                OosBudgetReservationRow(
                    reservation_id=reservation.reservation_id,
                    challenger_id=reservation.challenger_id,
                    experiment_family=reservation.experiment_family,
                    submission_number=reservation.submission_number,
                    submission_ordinal=reservation.submission_ordinal,
                    oos_budget_ordinal=reservation.oos_budget_ordinal,
                    candidate_artifact_hash=reservation.candidate_artifact_hash,
                    evaluation_contract_hash=reservation.evaluation_contract_hash,
                    idempotency_key=reservation.idempotency_key,
                    reservation_hash=reservation.reservation_hash,
                    payload_json=model_payload(reservation),
                    created_at=reservation.created_at,
                    expires_at=reservation.expires_at,
                )
            )
            session.add(
                ExperimentBudgetEventRow(
                    budget_event_id=stable_id(
                        "experiment-budget",
                        request.experiment_family,
                        event_idempotency_key,
                    ),
                    experiment_family=request.experiment_family,
                    event_type="OOS_CONSUMED",
                    submission_delta=1,
                    oos_budget_delta=1,
                    hypothesis_delta=0,
                    failure_delta=0,
                    idempotency_key=event_idempotency_key,
                    event_hash=canonical_hash(event_payload),
                    payload_json=cast(
                        dict[str, Any],
                        canonical_data(event_payload),
                    ),
                    created_at=created_at,
                )
            )
            session.flush()
            return reservation

    def store_oos_result(
        self,
        result: OosLockboxResultV1,
        *,
        created_at: datetime,
        candidate_artifact_hash: str,
        champion_shadow: ShadowArmIdentity | None = None,
        challenger_shadow: ShadowArmIdentity | None = None,
    ) -> bool:
        timestamp = require_aware_utc(created_at)
        if result.candidate_artifact_hash != candidate_artifact_hash:
            raise ResearchPersistenceError("OOS result candidate artifact binding mismatch")
        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(session, result.challenger_id)
            candidate = self._require_registered_candidate_artifact(
                session,
                challenger_id=result.challenger_id,
                candidate_artifact_hash=candidate_artifact_hash,
            )
            passed_report = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id == result.challenger_id,
                    FalsificationReportRow.mandatory_passed.is_(True),
                )
            )
            if passed_report is None:
                raise ResearchPersistenceError(
                    "OOS requires a passed mandatory falsification report"
                )
            passed_replay = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id == result.challenger_id,
                    ResearchReplayArtifactRow.candidate_artifact_hash == candidate_artifact_hash,
                    ResearchReplayArtifactRow.deterministic_match.is_(True),
                )
            )
            if passed_replay is None:
                raise ResearchPersistenceError(
                    "OOS requires a matching deterministic replay artifact"
                )
            parsed_report = FalsificationReportV1.model_validate(passed_report.payload_json)
            self._require_falsification_bindings(
                parsed_report,
                candidate_artifact_hash=candidate.bundle_hash,
                evaluation_contract_hash=result.evaluation_contract_hash,
                data_manifest_hash=passed_replay.data_manifest_hash,
                replay_hash=passed_replay.artifact_hash,
            )
            if result.experiment_family != manifest.experiment_family:
                raise ResearchPersistenceError("OOS experiment family does not match Challenger")
            existing = session.scalar(
                select(OosLockboxResultRow).where(
                    OosLockboxResultRow.challenger_id == result.challenger_id,
                    OosLockboxResultRow.submission_number == result.submission_number,
                )
            )
            if existing is not None:
                if existing.result_hash != result.result_hash:
                    raise ResearchPersistenceError("OOS submission hash conflict")
                return False
            current = self._current_challenger_status(session, manifest)
            if current is not ChallengerStatus.PROPOSED:
                raise ResearchPersistenceError(f"OOS requires PROPOSED, got {current.value}")
            if result.verdict is OosVerdict.PASS:
                if champion_shadow is None or challenger_shadow is None:
                    raise ResearchPersistenceError(
                        "passed OOS requires matched Champion and Challenger shadow arms"
                    )
                self._validate_shadow_pair(
                    session,
                    manifest=manifest,
                    champion=champion_shadow,
                    challenger=challenger_shadow,
                )
            elif champion_shadow is not None or challenger_shadow is not None:
                raise ResearchPersistenceError("failed OOS cannot register shadow arms")
            oos_result_id = stable_id(
                "oos-result",
                result.challenger_id,
                result.submission_number,
            )
            session.add(
                OosLockboxResultRow(
                    oos_result_id=oos_result_id,
                    challenger_id=result.challenger_id,
                    experiment_family=result.experiment_family,
                    submission_number=result.submission_number,
                    candidate_artifact_hash=result.candidate_artifact_hash,
                    evaluation_contract_hash=result.evaluation_contract_hash,
                    verdict=result.verdict.value,
                    common_sessions=result.common_sessions,
                    result_hash=result.result_hash,
                    payload_json=model_payload(result),
                    evaluated_at=result.evaluated_at,
                    created_at=timestamp,
                )
            )
            if result.verdict is OosVerdict.FAIL:
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.OOS_REJECTED,
                    reason_code="OOS_LOCKBOX_REJECTED",
                    artifact_hash=result.result_hash,
                    idempotency_key=f"oos-rejected:{result.result_hash}",
                    created_at=timestamp,
                )
                return True
            assert champion_shadow is not None
            assert challenger_shadow is not None
            self._register_shadow_pair(
                session,
                manifest=manifest,
                oos_result_id=oos_result_id,
                result_hash=result.result_hash,
                champion=champion_shadow,
                challenger=challenger_shadow,
                created_at=timestamp,
            )
            self._append_challenger_transition(
                session,
                manifest=manifest,
                to_status=ChallengerStatus.SHADOW_PENDING,
                reason_code="OOS_LOCKBOX_PASSED",
                artifact_hash=result.result_hash,
                idempotency_key=f"shadow-pending:{result.result_hash}",
                created_at=timestamp,
            )
            return True

    def store_oos_result_v2(
        self,
        result: OosLockboxResultV2,
        *,
        created_at: datetime,
        candidate_artifact_hash: str,
        champion_shadow: ShadowArmIdentity | None = None,
        challenger_shadow: ShadowArmIdentity | None = None,
    ) -> bool:
        """Persist one portfolio-level OOS result and atomically apply its gate."""

        timestamp = require_aware_utc(created_at)
        if result.candidate_artifact_hash != candidate_artifact_hash:
            raise ResearchPersistenceError(
                "OOS V2 result Candidate artifact binding mismatch"
            )
        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(
                session,
                result.challenger_id,
            )
            candidate = self._require_registered_candidate_artifact(
                session,
                challenger_id=result.challenger_id,
                candidate_artifact_hash=candidate_artifact_hash,
            )
            comparison_row = session.scalar(
                select(PortfolioComparisonContractRow).where(
                    PortfolioComparisonContractRow.challenger_id
                    == result.challenger_id,
                    PortfolioComparisonContractRow.candidate_artifact_hash
                    == candidate_artifact_hash,
                    PortfolioComparisonContractRow.contract_hash
                    == result.portfolio_comparison_contract_hash,
                )
            )
            if comparison_row is None:
                raise ResearchPersistenceError(
                    "OOS V2 requires its immutable portfolio contract"
                )
            comparison = PortfolioComparisonContractV1.model_validate(
                comparison_row.payload_json
            )
            if (
                comparison.candidate_artifact_hash != candidate.bundle_hash
                or comparison.created_at > result.evaluated_at
                or not result.portfolio_contract_binding_valid
                or not result.allocation_policy_fixed_before_oos
            ):
                raise ResearchPersistenceError(
                    "OOS V2 portfolio contract binding is invalid"
                )
            passed_report = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id
                    == result.challenger_id,
                    FalsificationReportRow.mandatory_passed.is_(True),
                )
            )
            passed_replay = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id
                    == result.challenger_id,
                    ResearchReplayArtifactRow.candidate_artifact_hash
                    == candidate_artifact_hash,
                    ResearchReplayArtifactRow.deterministic_match.is_(True),
                )
            )
            if passed_report is None or passed_replay is None:
                raise ResearchPersistenceError(
                    "OOS V2 requires falsification and replay"
                )
            parsed_report = FalsificationReportV1.model_validate(
                passed_report.payload_json
            )
            self._require_falsification_bindings(
                parsed_report,
                candidate_artifact_hash=candidate.bundle_hash,
                evaluation_contract_hash=result.evaluation_contract_hash,
                data_manifest_hash=passed_replay.data_manifest_hash,
                replay_hash=passed_replay.artifact_hash,
            )
            if result.experiment_family != manifest.experiment_family:
                raise ResearchPersistenceError(
                    "OOS V2 experiment family does not match Challenger"
                )
            existing = session.scalar(
                select(OosLockboxResultRow).where(
                    OosLockboxResultRow.challenger_id
                    == result.challenger_id,
                    OosLockboxResultRow.submission_number
                    == result.submission_number,
                )
            )
            if existing is not None:
                if existing.result_hash != result.result_hash:
                    raise ResearchPersistenceError(
                        "OOS V2 submission hash conflict"
                    )
                return False
            current = self._current_challenger_status(session, manifest)
            if current is not ChallengerStatus.PROPOSED:
                raise ResearchPersistenceError(
                    f"OOS V2 requires PROPOSED, got {current.value}"
                )
            if result.verdict is OosVerdict.PASS:
                if champion_shadow is None or challenger_shadow is None:
                    raise ResearchPersistenceError(
                        "passed OOS V2 requires matched shadow arms"
                    )
                self._validate_shadow_pair(
                    session,
                    manifest=manifest,
                    champion=champion_shadow,
                    challenger=challenger_shadow,
                )
            elif champion_shadow is not None or challenger_shadow is not None:
                raise ResearchPersistenceError(
                    "failed OOS V2 cannot register shadow arms"
                )
            oos_result_id = stable_id(
                "oos-result-v2",
                result.challenger_id,
                result.submission_number,
                result.portfolio_comparison_contract_hash,
            )
            session.add(
                OosLockboxResultRow(
                    oos_result_id=oos_result_id,
                    challenger_id=result.challenger_id,
                    experiment_family=result.experiment_family,
                    submission_number=result.submission_number,
                    candidate_artifact_hash=result.candidate_artifact_hash,
                    evaluation_contract_hash=result.evaluation_contract_hash,
                    verdict=result.verdict.value,
                    common_sessions=result.common_sessions,
                    result_hash=result.result_hash,
                    payload_json=model_payload(result),
                    evaluated_at=result.evaluated_at,
                    created_at=timestamp,
                )
            )
            if result.verdict is OosVerdict.FAIL:
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.OOS_REJECTED,
                    reason_code="PORTFOLIO_DELTA_SHARPE_OOS_REJECTED",
                    artifact_hash=result.result_hash,
                    idempotency_key=f"oos-v2-rejected:{result.result_hash}",
                    created_at=timestamp,
                )
                return True
            assert champion_shadow is not None
            assert challenger_shadow is not None
            self._register_shadow_pair(
                session,
                manifest=manifest,
                oos_result_id=oos_result_id,
                result_hash=result.result_hash,
                champion=champion_shadow,
                challenger=challenger_shadow,
                created_at=timestamp,
            )
            self._append_challenger_transition(
                session,
                manifest=manifest,
                to_status=ChallengerStatus.SHADOW_PENDING,
                reason_code="PORTFOLIO_DELTA_SHARPE_OOS_PASSED",
                artifact_hash=result.result_hash,
                idempotency_key=f"shadow-v2-pending:{result.result_hash}",
                created_at=timestamp,
            )
            return True

    def start_shadow_evaluation(
        self,
        *,
        challenger_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> bool:
        timestamp = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(session, challenger_id)
            registrations = list(
                session.scalars(
                    select(ResearchShadowArmRegistrationRow).where(
                        ResearchShadowArmRegistrationRow.challenger_id == challenger_id
                    )
                )
            )
            roles = {row.arm_role for row in registrations}
            pair_ids = {row.shadow_pair_id for row in registrations}
            contract_hashes = {row.execution_contract_hash for row in registrations}
            if (
                len(registrations) != 2
                or roles != {"CHAMPION", "CHALLENGER"}
                or len(pair_ids) != 1
                or len(contract_hashes) != 1
                or any(row.real_order_routing for row in registrations)
            ):
                raise ResearchPersistenceError(
                    "shadow start requires one matched paper-only arm pair"
                )
            return self._append_challenger_transition(
                session,
                manifest=manifest,
                to_status=ChallengerStatus.SHADOW_RUNNING,
                reason_code="EXPLICIT_SHADOW_START",
                artifact_hash=canonical_hash(next(iter(pair_ids))),
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )

    def oos_result(
        self,
        *,
        challenger_id: str,
        submission_number: int,
    ) -> OosLockboxResultV1 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(OosLockboxResultRow).where(
                    OosLockboxResultRow.challenger_id == challenger_id,
                    OosLockboxResultRow.submission_number == submission_number,
                )
            )
            return None if row is None else OosLockboxResultV1.model_validate(row.payload_json)

    def oos_result_v2(
        self,
        *,
        challenger_id: str,
        submission_number: int,
    ) -> OosLockboxResultV2 | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(OosLockboxResultRow).where(
                    OosLockboxResultRow.challenger_id == challenger_id,
                    OosLockboxResultRow.submission_number == submission_number,
                )
            )
            if row is None:
                return None
            try:
                return OosLockboxResultV2.model_validate(row.payload_json)
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored OOS result is not V2"
                ) from exc

    def shadow_pair(self, challenger_id: str) -> tuple[dict[str, Any], ...]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(ResearchShadowArmRegistrationRow)
                    .where(ResearchShadowArmRegistrationRow.challenger_id == challenger_id)
                    .order_by(ResearchShadowArmRegistrationRow.arm_role)
                )
            )
            return tuple(row.payload_json for row in rows)

    def shadow_start_event(
        self,
        challenger_id: str,
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ChallengerEventRow)
                .where(
                    ChallengerEventRow.challenger_id == challenger_id,
                    ChallengerEventRow.reason_code
                    == "EXPLICIT_SHADOW_START",
                )
                .order_by(desc(ChallengerEventRow.sequence))
                .limit(1)
            )
            return None if row is None else row.payload_json

    def validate_shadow_pair(
        self,
        *,
        challenger_id: str,
        champion: ShadowArmIdentity,
        challenger: ShadowArmIdentity,
    ) -> None:
        with self._session_factory() as session:
            manifest = session.get(ChallengerManifestRow, challenger_id)
            if manifest is None:
                raise ResearchPersistenceError("unknown Challenger")
            self._validate_shadow_pair(
                session,
                manifest=manifest,
                champion=champion,
                challenger=challenger,
            )

    def record_shadow_performance_summary(
        self,
        summary: TrustedShadowPerformanceSummaryV1,
    ) -> bool:
        """Persist one immutable promotion-facing shadow evidence snapshot."""

        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(
                session,
                summary.challenger_id,
            )
            existing = session.get(
                ResearchShadowPerformanceSummaryRow,
                summary.summary_id,
            )
            if existing is not None:
                if existing.summary_hash != summary.summary_hash:
                    raise ResearchPersistenceError("shadow performance summary hash conflict")
                return False
            current = self._current_challenger_status(session, manifest)
            if current is not ChallengerStatus.SHADOW_RUNNING:
                raise ResearchPersistenceError("shadow summary requires SHADOW_RUNNING")
            self._validate_shadow_performance_summary(
                session,
                manifest=manifest,
                summary=summary,
            )
            session.add(
                ResearchShadowPerformanceSummaryRow(
                    summary_id=summary.summary_id,
                    challenger_id=summary.challenger_id,
                    shadow_pair_id=summary.shadow_pair_id,
                    run_id=summary.run_id,
                    source_summary_hash=summary.source_summary.summary_hash,
                    materialized_evidence_hash=(summary.materialized_evidence_hash),
                    summary_hash=summary.summary_hash,
                    common_sessions=summary.forward_sessions,
                    data_available_cutoff=summary.data_available_cutoff,
                    payload_json=model_payload(summary),
                    created_at=summary.created_at,
                )
            )
            return True

    def record_shadow_performance_summary_v2(
        self,
        summary: TrustedShadowPerformanceSummaryV2,
    ) -> bool:
        """Persist aggregate-only paired portfolio Sharpe shadow evidence."""

        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(
                session,
                summary.challenger_id,
            )
            existing = session.get(
                ResearchShadowPerformanceSummaryRow,
                summary.summary_id,
            )
            if existing is not None:
                if existing.summary_hash != summary.summary_hash:
                    raise ResearchPersistenceError(
                        "shadow V2 performance summary hash conflict"
                    )
                return False
            if (
                self._current_challenger_status(session, manifest)
                is not ChallengerStatus.SHADOW_RUNNING
            ):
                raise ResearchPersistenceError(
                    "shadow V2 summary requires SHADOW_RUNNING"
                )
            comparison_row = session.scalar(
                select(PortfolioComparisonContractRow).where(
                    PortfolioComparisonContractRow.challenger_id
                    == summary.challenger_id,
                    PortfolioComparisonContractRow.contract_hash
                    == summary.portfolio_comparison_contract_hash,
                    PortfolioComparisonContractRow.candidate_artifact_hash
                    == summary.candidate_artifact_hash,
                )
            )
            if comparison_row is None:
                raise ResearchPersistenceError(
                    "shadow V2 summary lost its portfolio contract"
                )
            comparison = PortfolioComparisonContractV1.model_validate(
                comparison_row.payload_json
            )
            if (
                comparison.champion_portfolio_manifest_hash
                != summary.champion_portfolio_manifest_hash
                or comparison.candidate_portfolio_manifest_hash
                != summary.candidate_portfolio_manifest_hash
            ):
                raise ResearchPersistenceError(
                    "shadow V2 portfolio manifest binding mismatch"
                )
            registrations = tuple(
                session.scalars(
                    select(ResearchShadowArmRegistrationRow).where(
                        ResearchShadowArmRegistrationRow.challenger_id
                        == summary.challenger_id
                    )
                )
            )
            if (
                len(registrations) != 2
                or {item.shadow_pair_id for item in registrations}
                != {summary.shadow_pair_id}
                or {item.execution_contract_hash for item in registrations}
                != {summary.execution_contract_hash}
                or any(item.real_order_routing for item in registrations)
            ):
                raise ResearchPersistenceError(
                    "shadow V2 matched execution binding mismatch"
                )
            oos_row = session.scalar(
                select(OosLockboxResultRow).where(
                    OosLockboxResultRow.challenger_id
                    == summary.challenger_id,
                    OosLockboxResultRow.candidate_artifact_hash
                    == summary.candidate_artifact_hash,
                    OosLockboxResultRow.verdict == OosVerdict.PASS.value,
                )
            )
            if oos_row is None:
                raise ResearchPersistenceError(
                    "shadow V2 summary requires passed OOS V2"
                )
            try:
                oos = OosLockboxResultV2.model_validate(
                    oos_row.payload_json
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "shadow V2 summary requires an OOS V2 result"
                ) from exc
            if (
                oos.portfolio_comparison_contract_hash
                != summary.portfolio_comparison_contract_hash
            ):
                raise ResearchPersistenceError(
                    "shadow and OOS portfolio contracts differ"
                )
            session.add(
                ResearchShadowPerformanceSummaryRow(
                    summary_id=summary.summary_id,
                    challenger_id=summary.challenger_id,
                    shadow_pair_id=summary.shadow_pair_id,
                    run_id=summary.run_id,
                    source_summary_hash=(
                        summary.materialized_evidence_hash
                    ),
                    materialized_evidence_hash=(
                        summary.materialized_evidence_hash
                    ),
                    summary_hash=summary.summary_hash,
                    common_sessions=summary.forward_sessions,
                    data_available_cutoff=summary.data_available_cutoff,
                    payload_json=model_payload(summary),
                    created_at=summary.created_at,
                )
            )
            return True

    def build_trusted_promotion_evidence(
        self,
        *,
        challenger_id: str,
        created_at: datetime,
    ) -> PromotionEvidenceV1:
        timestamp = require_aware_utc(created_at)
        with self._session_factory() as session:
            manifest = session.get(ChallengerManifestRow, challenger_id)
            if manifest is None:
                raise ResearchPersistenceError("unknown Challenger")
            summary_row = session.scalar(
                select(ResearchShadowPerformanceSummaryRow)
                .where(ResearchShadowPerformanceSummaryRow.challenger_id == challenger_id)
                .order_by(
                    desc(ResearchShadowPerformanceSummaryRow.data_available_cutoff),
                    desc(ResearchShadowPerformanceSummaryRow.created_at),
                    desc(ResearchShadowPerformanceSummaryRow.summary_id),
                )
                .limit(1)
            )
            if summary_row is None:
                raise ResearchPersistenceError("promotion requires a persisted shadow summary")
            summary = TrustedShadowPerformanceSummaryV1.model_validate(summary_row.payload_json)
            self._validate_shadow_performance_summary(
                session,
                manifest=manifest,
                summary=summary,
            )
            falsification_row = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id == challenger_id,
                    FalsificationReportRow.mandatory_passed.is_(True),
                )
            )
            replay_row = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id == challenger_id,
                    ResearchReplayArtifactRow.candidate_artifact_hash
                    == summary.candidate_artifact_hash,
                    ResearchReplayArtifactRow.deterministic_match.is_(True),
                )
            )
            oos_row = session.scalar(
                select(OosLockboxResultRow)
                .where(
                    OosLockboxResultRow.challenger_id == challenger_id,
                    OosLockboxResultRow.candidate_artifact_hash == summary.candidate_artifact_hash,
                    OosLockboxResultRow.verdict == OosVerdict.PASS.value,
                )
                .order_by(
                    OosLockboxResultRow.submission_number,
                    OosLockboxResultRow.evaluated_at,
                    OosLockboxResultRow.oos_result_id,
                )
                .limit(1)
            )
            if falsification_row is None or replay_row is None or oos_row is None:
                raise ResearchPersistenceError("promotion evidence prerequisites are incomplete")
            latest_designation = self._latest_champion_designation(session)
            current_champion_version = (
                summary.current_champion_version
                if latest_designation is None
                else latest_designation.strategy_version
            )
            if current_champion_version != summary.current_champion_version:
                raise ResearchPersistenceError(
                    "shadow summary is stale against the current Champion"
                )
            cutoff = max(
                summary.data_available_cutoff,
                FalsificationReportV1.model_validate(falsification_row.payload_json).created_at,
                _payload_timestamp(
                    replay_row.payload_json,
                    "created_at",
                ),
                OosLockboxResultV1.model_validate(oos_row.payload_json).evaluated_at,
            )
            if timestamp < cutoff:
                raise ResearchPersistenceError("promotion evidence predates its source cutoff")
            evidence_id = stable_id(
                "promotion-evidence",
                challenger_id,
                summary.summary_hash,
                falsification_row.report_hash,
                oos_row.result_hash,
                replay_row.first_replay_hash,
                current_champion_version,
            )
            return build_promotion_evidence(
                evidence_id=evidence_id,
                challenger_id=challenger_id,
                current_champion_version=current_champion_version,
                candidate_version=manifest.strategy_version,
                candidate_artifact_hash=summary.candidate_artifact_hash,
                falsification_report_hash=falsification_row.report_hash,
                oos_result_hash=oos_row.result_hash,
                shadow_summary_hash=summary.summary_hash,
                replay_hash=replay_row.first_replay_hash,
                common_oos_sessions=oos_row.common_sessions,
                forward_sessions=summary.forward_sessions,
                independent_trades=summary.independent_trades,
                annualized_net_excess_return_after_cost=(
                    summary.annualized_net_excess_return_after_cost
                ),
                matched_annualized_difference=(summary.matched_annualized_difference),
                economic_effect=summary.economic_effect,
                maximum_drawdown=summary.maximum_drawdown,
                tail_loss=summary.tail_loss,
                annualized_turnover=summary.annualized_turnover,
                estimated_capacity_usd=summary.estimated_capacity_usd,
                regime_pass_fraction=summary.regime_pass_fraction,
                runtime_error_rate=summary.runtime_error_rate,
                replay_reproducible=True,
                mandatory_tests_passed=True,
                data_available_cutoff=cutoff,
                created_at=timestamp,
            )

    def build_trusted_promotion_evidence_v2(
        self,
        *,
        challenger_id: str,
        created_at: datetime,
    ) -> PromotionEvidenceV2:
        timestamp = require_aware_utc(created_at)
        with self._session_factory() as session:
            manifest = session.get(ChallengerManifestRow, challenger_id)
            if manifest is None:
                raise ResearchPersistenceError("unknown Challenger")
            summary_rows = tuple(
                session.scalars(
                    select(ResearchShadowPerformanceSummaryRow)
                    .where(
                        ResearchShadowPerformanceSummaryRow.challenger_id
                        == challenger_id
                    )
                    .order_by(
                        desc(
                            ResearchShadowPerformanceSummaryRow.data_available_cutoff
                        ),
                        desc(
                            ResearchShadowPerformanceSummaryRow.created_at
                        ),
                        desc(
                            ResearchShadowPerformanceSummaryRow.summary_id
                        ),
                    )
                )
            )
            summary: TrustedShadowPerformanceSummaryV2 | None = None
            summary_row: ResearchShadowPerformanceSummaryRow | None = None
            for candidate_row in summary_rows:
                try:
                    candidate_summary = (
                        TrustedShadowPerformanceSummaryV2.model_validate(
                            candidate_row.payload_json
                        )
                    )
                except ValueError:
                    continue
                summary = candidate_summary
                summary_row = candidate_row
                break
            if summary is None or summary_row is None:
                raise ResearchPersistenceError(
                    "promotion V2 requires a shadow V2 summary"
                )
            oos_rows = tuple(
                session.scalars(
                    select(OosLockboxResultRow)
                    .where(
                        OosLockboxResultRow.challenger_id == challenger_id,
                        OosLockboxResultRow.candidate_artifact_hash
                        == summary.candidate_artifact_hash,
                        OosLockboxResultRow.verdict
                        == OosVerdict.PASS.value,
                    )
                    .order_by(
                        OosLockboxResultRow.submission_number,
                        OosLockboxResultRow.evaluated_at,
                        OosLockboxResultRow.oos_result_id,
                    )
                )
            )
            oos: OosLockboxResultV2 | None = None
            oos_row: OosLockboxResultRow | None = None
            for candidate_row in oos_rows:
                try:
                    candidate_oos = OosLockboxResultV2.model_validate(
                        candidate_row.payload_json
                    )
                except ValueError:
                    continue
                oos = candidate_oos
                oos_row = candidate_row
                break
            falsification_row = session.scalar(
                select(FalsificationReportRow).where(
                    FalsificationReportRow.challenger_id == challenger_id,
                    FalsificationReportRow.mandatory_passed.is_(True),
                )
            )
            replay_row = session.scalar(
                select(ResearchReplayArtifactRow).where(
                    ResearchReplayArtifactRow.challenger_id == challenger_id,
                    ResearchReplayArtifactRow.candidate_artifact_hash
                    == summary.candidate_artifact_hash,
                    ResearchReplayArtifactRow.deterministic_match.is_(True),
                )
            )
            comparison_row = session.scalar(
                select(PortfolioComparisonContractRow).where(
                    PortfolioComparisonContractRow.challenger_id
                    == challenger_id,
                    PortfolioComparisonContractRow.contract_hash
                    == summary.portfolio_comparison_contract_hash,
                )
            )
            if (
                oos is None
                or oos_row is None
                or falsification_row is None
                or replay_row is None
                or comparison_row is None
            ):
                raise ResearchPersistenceError(
                    "promotion V2 prerequisites are incomplete"
                )
            comparison = PortfolioComparisonContractV1.model_validate(
                comparison_row.payload_json
            )
            latest_designation = self._latest_champion_designation(session)
            current_champion_version = (
                summary.current_champion_version
                if latest_designation is None
                else latest_designation.strategy_version
            )
            if current_champion_version != summary.current_champion_version:
                raise ResearchPersistenceError(
                    "shadow V2 summary is stale against current Champion"
                )
            cutoff = max(
                summary.data_available_cutoff,
                FalsificationReportV1.model_validate(
                    falsification_row.payload_json
                ).created_at,
                _payload_timestamp(replay_row.payload_json, "created_at"),
                oos.evaluated_at,
            )
            if timestamp < cutoff:
                raise ResearchPersistenceError(
                    "promotion V2 evidence predates source cutoff"
                )
            evidence_id = stable_id(
                "promotion-evidence-v2",
                challenger_id,
                summary.summary_hash,
                falsification_row.report_hash,
                oos.result_hash,
                replay_row.first_replay_hash,
                comparison.contract_hash,
                current_champion_version,
            )
            return build_promotion_evidence_v2(
                evidence_id=evidence_id,
                challenger_id=challenger_id,
                current_champion_version=current_champion_version,
                candidate_version=manifest.strategy_version,
                candidate_artifact_hash=summary.candidate_artifact_hash,
                comparison_contract=comparison,
                falsification_report_hash=falsification_row.report_hash,
                oos_result=oos,
                shadow_summary=summary,
                replay_hash=replay_row.first_replay_hash,
                annualized_net_excess_return_after_cost=(
                    summary.annualized_net_excess_return_after_cost
                ),
                matched_annualized_difference=(
                    summary.matched_annualized_difference
                ),
                economic_effect=summary.economic_effect,
                maximum_drawdown=summary.maximum_drawdown,
                tail_loss=summary.tail_loss,
                annualized_turnover=summary.annualized_turnover,
                estimated_capacity_usd=summary.estimated_capacity_usd,
                regime_pass_fraction=summary.regime_pass_fraction,
                runtime_error_rate=summary.runtime_error_rate,
                replay_reproducible=True,
                mandatory_tests_passed=True,
                data_available_cutoff=cutoff,
                created_at=timestamp,
            )

    def trusted_promotion_evaluation(
        self,
        *,
        evidence_id: str,
        contract_hash: str,
    ) -> tuple[PromotionEvidenceV1, TrustedPromotionEvaluationV1] | None:
        """Return the one persisted evaluation for a source/contract pair."""

        with self._session_factory() as session:
            row = session.scalar(
                select(TrustedPromotionEvaluationRow)
                .where(
                    TrustedPromotionEvaluationRow.evidence_id == evidence_id,
                    TrustedPromotionEvaluationRow.contract_hash == contract_hash,
                )
                .order_by(
                    TrustedPromotionEvaluationRow.created_at,
                    TrustedPromotionEvaluationRow.evaluation_id,
                )
                .limit(1)
            )
            if row is None:
                return None
            evidence_row = session.get(
                ResearchPromotionEvidenceRow,
                row.evidence_id,
            )
            if evidence_row is None:
                raise ResearchPersistenceError("trusted evaluation lost its promotion evidence")
            return (
                PromotionEvidenceV1.model_validate(evidence_row.payload_json),
                TrustedPromotionEvaluationV1.model_validate(row.payload_json),
            )

    def trusted_promotion_evaluation_v2(
        self,
        *,
        evidence_id: str,
        contract_hash: str,
    ) -> tuple[PromotionEvidenceV2, TrustedPromotionEvaluationV2] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(TrustedPromotionEvaluationRow)
                .where(
                    TrustedPromotionEvaluationRow.evidence_id == evidence_id,
                    TrustedPromotionEvaluationRow.contract_hash
                    == contract_hash,
                )
                .order_by(
                    TrustedPromotionEvaluationRow.created_at,
                    TrustedPromotionEvaluationRow.evaluation_id,
                )
                .limit(1)
            )
            if row is None:
                return None
            evidence_row = session.get(
                ResearchPromotionEvidenceRow,
                row.evidence_id,
            )
            if evidence_row is None:
                raise ResearchPersistenceError(
                    "trusted V2 evaluation lost its promotion evidence"
                )
            try:
                return (
                    PromotionEvidenceV2.model_validate(
                        evidence_row.payload_json
                    ),
                    TrustedPromotionEvaluationV2.model_validate(
                        row.payload_json
                    ),
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "stored trusted evaluation is not V2"
                ) from exc

    def record_trusted_promotion_evaluation(
        self,
        *,
        evidence: PromotionEvidenceV1,
        contract: PromotionEvaluationContractV1,
        evaluation: TrustedPromotionEvaluationV1,
    ) -> bool:
        expected = evaluate_trusted_promotion_evidence(
            evidence=evidence,
            contract=contract,
            created_at=evaluation.created_at,
        )
        if expected.evaluation_hash != evaluation.evaluation_hash:
            raise ResearchPersistenceError(
                "trusted promotion evaluation does not match its contract"
            )
        decision = evaluation.decision
        with self._session_factory.begin() as session:
            self._champion_designation_lock(session)
            manifest = self._challenger_for_update(
                session,
                evidence.challenger_id,
            )
            existing = session.get(
                TrustedPromotionEvaluationRow,
                evaluation.evaluation_id,
            )
            if existing is not None:
                if existing.evaluation_hash != evaluation.evaluation_hash:
                    raise ResearchPersistenceError("trusted promotion evaluation hash conflict")
                return False
            if (
                self._current_challenger_status(session, manifest)
                is not ChallengerStatus.SHADOW_RUNNING
            ):
                raise ResearchPersistenceError(
                    "trusted promotion evaluation requires SHADOW_RUNNING"
                )
            summary_row = self._validate_promotion_evidence_binding(
                session,
                manifest=manifest,
                evidence=evidence,
            )
            if (
                evaluation.evidence_hash != evidence.evidence_hash
                or decision.challenger_id != evidence.challenger_id
                or decision.current_champion_version != evidence.current_champion_version
                or decision.candidate_version != evidence.candidate_version
                or decision.replay_hash != evidence.replay_hash
            ):
                raise ResearchPersistenceError("trusted promotion evaluation binding mismatch")
            evidence_row = session.get(
                ResearchPromotionEvidenceRow,
                evidence.evidence_id,
            )
            if evidence_row is not None:
                if evidence_row.evidence_hash != evidence.evidence_hash:
                    raise ResearchPersistenceError("promotion evidence hash conflict")
            else:
                session.add(
                    ResearchPromotionEvidenceRow(
                        evidence_id=evidence.evidence_id,
                        challenger_id=evidence.challenger_id,
                        shadow_summary_id=summary_row.summary_id,
                        evidence_hash=evidence.evidence_hash,
                        payload_json=model_payload(evidence),
                        created_at=evidence.created_at,
                    )
                )
            self._add_promotion_decision(session, decision)
            session.add(
                TrustedPromotionEvaluationRow(
                    evaluation_id=evaluation.evaluation_id,
                    evidence_id=evidence.evidence_id,
                    challenger_id=evidence.challenger_id,
                    promotion_decision_id=decision.promotion_decision_id,
                    evidence_hash=evidence.evidence_hash,
                    contract_hash=evaluation.contract_hash,
                    verdict=decision.verdict.value,
                    evaluation_hash=evaluation.evaluation_hash,
                    payload_json=model_payload(evaluation),
                    created_at=evaluation.created_at,
                )
            )
            if decision.verdict is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL:
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.PROMOTION_ELIGIBLE,
                    reason_code="TRUSTED_PROMOTION_CRITERIA_SATISFIED",
                    artifact_hash=evaluation.evaluation_hash,
                    idempotency_key=(f"trusted-promotion:{evaluation.evaluation_hash}"),
                    created_at=evaluation.created_at,
                )
            return True

    def record_trusted_promotion_evaluation_v2(
        self,
        *,
        evidence: PromotionEvidenceV2,
        contract: PromotionEvaluationContractV2,
        evaluation: TrustedPromotionEvaluationV2,
    ) -> bool:
        expected = evaluate_trusted_promotion_evidence_v2(
            evidence=evidence,
            contract=contract,
            created_at=evaluation.created_at,
        )
        if expected.evaluation_hash != evaluation.evaluation_hash:
            raise ResearchPersistenceError(
                "trusted promotion V2 evaluation does not match its contract"
            )
        decision = evaluation.decision
        with self._session_factory.begin() as session:
            self._champion_designation_lock(session)
            manifest = self._challenger_for_update(
                session,
                evidence.challenger_id,
            )
            existing = session.get(
                TrustedPromotionEvaluationRow,
                evaluation.evaluation_id,
            )
            if existing is not None:
                if existing.evaluation_hash != evaluation.evaluation_hash:
                    raise ResearchPersistenceError(
                        "trusted promotion V2 evaluation hash conflict"
                    )
                return False
            if (
                self._current_challenger_status(session, manifest)
                is not ChallengerStatus.SHADOW_RUNNING
            ):
                raise ResearchPersistenceError(
                    "trusted promotion V2 evaluation requires SHADOW_RUNNING"
                )
            summary_row = session.get(
                ResearchShadowPerformanceSummaryRow,
                evidence.shadow_summary.summary_id,
            )
            comparison_row = session.scalar(
                select(PortfolioComparisonContractRow).where(
                    PortfolioComparisonContractRow.challenger_id
                    == evidence.challenger_id,
                    PortfolioComparisonContractRow.contract_hash
                    == evidence.portfolio_comparison_contract_hash,
                )
            )
            oos_row = session.scalar(
                select(OosLockboxResultRow).where(
                    OosLockboxResultRow.challenger_id
                    == evidence.challenger_id,
                    OosLockboxResultRow.result_hash
                    == evidence.oos_result.result_hash,
                    OosLockboxResultRow.verdict == OosVerdict.PASS.value,
                )
            )
            if (
                summary_row is None
                or summary_row.summary_hash
                != evidence.shadow_summary.summary_hash
                or comparison_row is None
                or oos_row is None
                or not evidence.portfolio_contract_binding_valid
                or not evidence.allocation_policy_fixed_before_oos
            ):
                raise ResearchPersistenceError(
                    "trusted promotion V2 evidence binding mismatch"
                )
            if (
                evaluation.evidence_hash != evidence.evidence_hash
                or evaluation.portfolio_comparison_contract_hash
                != evidence.portfolio_comparison_contract_hash
                or decision.challenger_id != evidence.challenger_id
                or decision.current_champion_version
                != evidence.current_champion_version
                or decision.candidate_version != evidence.candidate_version
                or decision.replay_hash != evidence.replay_hash
            ):
                raise ResearchPersistenceError(
                    "trusted promotion V2 decision binding mismatch"
                )
            evidence_row = session.get(
                ResearchPromotionEvidenceRow,
                evidence.evidence_id,
            )
            if evidence_row is not None:
                if evidence_row.evidence_hash != evidence.evidence_hash:
                    raise ResearchPersistenceError(
                        "promotion V2 evidence hash conflict"
                    )
            else:
                session.add(
                    ResearchPromotionEvidenceRow(
                        evidence_id=evidence.evidence_id,
                        challenger_id=evidence.challenger_id,
                        shadow_summary_id=summary_row.summary_id,
                        evidence_hash=evidence.evidence_hash,
                        payload_json=model_payload(evidence),
                        created_at=evidence.created_at,
                    )
                )
            self._add_promotion_decision(session, decision)
            session.add(
                TrustedPromotionEvaluationRow(
                    evaluation_id=evaluation.evaluation_id,
                    evidence_id=evidence.evidence_id,
                    challenger_id=evidence.challenger_id,
                    promotion_decision_id=decision.promotion_decision_id,
                    evidence_hash=evidence.evidence_hash,
                    contract_hash=evaluation.contract_hash,
                    verdict=decision.verdict.value,
                    evaluation_hash=evaluation.evaluation_hash,
                    payload_json=model_payload(evaluation),
                    created_at=evaluation.created_at,
                )
            )
            if (
                decision.verdict
                is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
            ):
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.PROMOTION_ELIGIBLE,
                    reason_code=(
                        "PORTFOLIO_DELTA_SHARPE_PROMOTION_CRITERIA_SATISFIED"
                    ),
                    artifact_hash=evaluation.evaluation_hash,
                    idempotency_key=(
                        f"trusted-promotion-v2:{evaluation.evaluation_hash}"
                    ),
                    created_at=evaluation.created_at,
                )
            return True

    def record_trusted_manual_promotion_approval(
        self,
        *,
        challenger_id: str,
        approved_by: str,
        created_at: datetime,
    ) -> tuple[PromotionDecisionV1, bool]:
        approver = approved_by.strip()
        if not approver:
            raise ResearchPersistenceError("manual approver is required")
        timestamp = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            self._champion_designation_lock(session)
            manifest = self._challenger_for_update(session, challenger_id)
            if (
                self._current_challenger_status(session, manifest)
                is not ChallengerStatus.PROMOTION_ELIGIBLE
            ):
                raise ResearchPersistenceError("manual approval requires PROMOTION_ELIGIBLE")
            evaluation_row = self._latest_trusted_eligible_evaluation(
                session,
                challenger_id=challenger_id,
            )
            evaluation = _trusted_promotion_evaluation_from_payload(
                evaluation_row.payload_json
            )
            eligible = evaluation.decision
            latest_designation = self._latest_champion_designation(session)
            if (
                latest_designation is not None
                and latest_designation.strategy_version != eligible.current_champion_version
            ):
                raise ResearchPersistenceError(
                    "manual approval is stale against the current Champion"
                )
            existing = session.scalar(
                select(ResearchPromotionDecisionRow).where(
                    ResearchPromotionDecisionRow.challenger_id == challenger_id,
                    ResearchPromotionDecisionRow.verdict
                    == PromotionVerdict.MANUALLY_APPROVED.value,
                )
            )
            if existing is not None:
                decision = PromotionDecisionV1.model_validate(existing.payload_json)
                if (
                    decision.approved_by != approver
                    or decision.current_champion_version != eligible.current_champion_version
                    or decision.candidate_version != eligible.candidate_version
                ):
                    raise ResearchPersistenceError(
                        "Challenger already has a different manual approval"
                    )
                return decision, False
            if timestamp < evaluation.created_at:
                raise ResearchPersistenceError("manual approval predates trusted evaluation")
            payload: dict[str, Any] = {
                "schema_version": "promotion_decision_v1",
                "promotion_decision_id": stable_id(
                    "trusted-manual-promotion-approval",
                    evaluation.evaluation_hash,
                    approver,
                ),
                "challenger_id": challenger_id,
                "current_champion_version": (eligible.current_champion_version),
                "candidate_version": eligible.candidate_version,
                "verdict": PromotionVerdict.MANUALLY_APPROVED,
                "criteria": eligible.criteria,
                "failed_reason_codes": [],
                "replay_hash": eligible.replay_hash,
                "automatic_promotion_enabled": False,
                "approved_by": approver,
                "created_at": timestamp,
            }
            decision = PromotionDecisionV1.model_validate(
                {**payload, "decision_hash": canonical_hash(payload)}
            )
            self._add_promotion_decision(session, decision)
            return decision, True

    def designate_champion(
        self,
        *,
        challenger_id: str,
        expected_current_version: str,
        designated_by: str,
        idempotency_key: str,
        designated_at: datetime,
    ) -> tuple[ChampionDesignationV1, bool]:
        operator = designated_by.strip()
        if not operator:
            raise ResearchPersistenceError("Champion designator is required")
        timestamp = require_aware_utc(designated_at)
        for attempt in range(3):
            try:
                return self._designate_champion_once(
                    challenger_id=challenger_id,
                    expected_current_version=expected_current_version,
                    designated_by=operator,
                    idempotency_key=idempotency_key,
                    designated_at=timestamp,
                )
            except IntegrityError as exc:
                if attempt == 2:
                    raise ResearchPersistenceError(
                        "concurrent Champion designation conflict"
                    ) from exc
            except OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise ResearchPersistenceError("Champion designation database failure") from exc
                if attempt == 2:
                    raise ResearchPersistenceError(
                        "concurrent Champion designation conflict"
                    ) from exc
        raise ResearchPersistenceError("Champion designation failed")

    def current_champion_designation(
        self,
    ) -> ChampionDesignationV1 | None:
        with self._session_factory() as session:
            row = self._latest_champion_designation(session)
            return None if row is None else ChampionDesignationV1.model_validate(row.payload_json)

    def store_promotion_decision(self, decision: PromotionDecisionV1) -> bool:
        del decision
        raise ResearchPersistenceError(
            "promotion decisions require the trusted Research Lifecycle gate"
        )

    def record_promotion_eligibility(
        self,
        decision: PromotionDecisionV1,
    ) -> bool:
        """Legacy/test-only boolean gate; production uses trusted evaluations."""
        if decision.verdict not in {
            PromotionVerdict.INELIGIBLE,
            PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL,
        }:
            raise ResearchPersistenceError(
                "eligibility gate accepts only INELIGIBLE or ELIGIBLE_REQUIRES_MANUAL_APPROVAL"
            )
        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(
                session,
                decision.challenger_id,
            )
            existing = session.get(
                ResearchPromotionDecisionRow,
                decision.promotion_decision_id,
            )
            if existing is not None:
                if existing.decision_hash != decision.decision_hash:
                    raise ResearchPersistenceError("promotion decision hash conflict")
                return False
            self._validate_promotion_binding(
                session,
                manifest=manifest,
                decision=decision,
                expected_status=ChallengerStatus.SHADOW_RUNNING,
            )
            self._add_promotion_decision(session, decision)
            if decision.verdict is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL:
                self._append_challenger_transition(
                    session,
                    manifest=manifest,
                    to_status=ChallengerStatus.PROMOTION_ELIGIBLE,
                    reason_code="PROMOTION_CRITERIA_SATISFIED",
                    artifact_hash=decision.decision_hash,
                    idempotency_key=f"promotion-eligible:{decision.decision_hash}",
                    created_at=decision.created_at,
                )
            return True

    def record_manual_promotion_approval(
        self,
        decision: PromotionDecisionV1,
    ) -> bool:
        if decision.verdict is not PromotionVerdict.MANUALLY_APPROVED:
            raise ResearchPersistenceError("manual approval path requires MANUALLY_APPROVED")
        with self._session_factory.begin() as session:
            manifest = self._challenger_for_update(
                session,
                decision.challenger_id,
            )
            existing = session.get(
                ResearchPromotionDecisionRow,
                decision.promotion_decision_id,
            )
            if existing is not None:
                if existing.decision_hash != decision.decision_hash:
                    raise ResearchPersistenceError("promotion decision hash conflict")
                return False
            previous_manual = session.scalar(
                select(ResearchPromotionDecisionRow).where(
                    ResearchPromotionDecisionRow.challenger_id == decision.challenger_id,
                    ResearchPromotionDecisionRow.verdict
                    == PromotionVerdict.MANUALLY_APPROVED.value,
                )
            )
            if previous_manual is not None:
                raise ResearchPersistenceError("Challenger already has a manual promotion approval")
            self._validate_promotion_binding(
                session,
                manifest=manifest,
                decision=decision,
                expected_status=ChallengerStatus.PROMOTION_ELIGIBLE,
            )
            eligible = session.scalar(
                select(ResearchPromotionDecisionRow)
                .where(
                    ResearchPromotionDecisionRow.challenger_id == decision.challenger_id,
                    ResearchPromotionDecisionRow.verdict
                    == PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL.value,
                )
                .order_by(desc(ResearchPromotionDecisionRow.created_at))
                .limit(1)
            )
            if eligible is None:
                raise ResearchPersistenceError(
                    "manual approval requires a persisted eligibility decision"
                )
            eligible_decision = PromotionDecisionV1.model_validate(eligible.payload_json)
            if (
                eligible_decision.current_champion_version != decision.current_champion_version
                or eligible_decision.candidate_version != decision.candidate_version
                or eligible_decision.criteria != decision.criteria
                or eligible_decision.replay_hash != decision.replay_hash
            ):
                raise ResearchPersistenceError(
                    "manual approval does not match eligibility decision"
                )
            self._add_promotion_decision(session, decision)
            return True

    def status(self, *, history_limit: int = 20) -> dict[str, Any]:
        selection = self.current_selection()
        with self._session_factory() as session:
            cycles = list(
                session.scalars(
                    select(ResearchCycleRow)
                    .order_by(desc(ResearchCycleRow.created_at))
                    .limit(history_limit)
                )
            )
            manifests = list(
                session.scalars(
                    select(ChallengerManifestRow)
                    .order_by(desc(ChallengerManifestRow.created_at))
                    .limit(history_limit)
                )
            )
            evidence = list(
                session.scalars(
                    select(ResearchEvidenceSourceRow)
                    .order_by(desc(ResearchEvidenceSourceRow.created_at))
                    .limit(history_limit)
                )
            )
            proposals = list(
                session.scalars(
                    select(AlgorithmProposalRow)
                    .order_by(desc(AlgorithmProposalRow.created_at))
                    .limit(history_limit)
                )
            )
            candidate_artifacts = list(
                session.scalars(
                    select(ResearchCandidateArtifactRow)
                    .order_by(desc(ResearchCandidateArtifactRow.created_at))
                    .limit(history_limit)
                )
            )
            oos_results = list(
                session.scalars(
                    select(OosLockboxResultRow)
                    .order_by(desc(OosLockboxResultRow.created_at))
                    .limit(history_limit)
                )
            )
            falsification_reports = list(
                session.scalars(
                    select(FalsificationReportRow)
                    .order_by(desc(FalsificationReportRow.created_at))
                    .limit(history_limit)
                )
            )
            replay_artifacts = list(
                session.scalars(
                    select(ResearchReplayArtifactRow)
                    .order_by(desc(ResearchReplayArtifactRow.created_at))
                    .limit(history_limit)
                )
            )
            shadow_registrations = list(
                session.scalars(
                    select(ResearchShadowArmRegistrationRow)
                    .order_by(desc(ResearchShadowArmRegistrationRow.created_at))
                    .limit(history_limit * 2)
                )
            )
            shadow_summaries = list(
                session.scalars(
                    select(ResearchShadowPerformanceSummaryRow)
                    .order_by(desc(ResearchShadowPerformanceSummaryRow.created_at))
                    .limit(history_limit)
                )
            )
            promotion_evidence = list(
                session.scalars(
                    select(ResearchPromotionEvidenceRow)
                    .order_by(desc(ResearchPromotionEvidenceRow.created_at))
                    .limit(history_limit)
                )
            )
            trusted_evaluations = list(
                session.scalars(
                    select(TrustedPromotionEvaluationRow)
                    .order_by(desc(TrustedPromotionEvaluationRow.created_at))
                    .limit(history_limit)
                )
            )
            promotions = list(
                session.scalars(
                    select(ResearchPromotionDecisionRow)
                    .order_by(desc(ResearchPromotionDecisionRow.created_at))
                    .limit(history_limit)
                )
            )
            champion_designations = list(
                session.scalars(
                    select(ResearchChampionDesignationRow)
                    .order_by(desc(ResearchChampionDesignationRow.sequence))
                    .limit(history_limit)
                )
            )
            trusted_eligible_challenger_ids = sorted(
                set(
                    session.scalars(
                        select(TrustedPromotionEvaluationRow.challenger_id).where(
                            TrustedPromotionEvaluationRow.verdict
                            == PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL.value
                        )
                    )
                )
            )
            manually_approved_challenger_ids = sorted(
                set(trusted_eligible_challenger_ids).intersection(
                    session.scalars(
                        select(ResearchPromotionDecisionRow.challenger_id).where(
                            ResearchPromotionDecisionRow.verdict
                            == PromotionVerdict.MANUALLY_APPROVED.value
                        )
                    )
                )
            )
            challenger_events = list(
                session.scalars(
                    select(ChallengerEventRow)
                    .where(
                        ChallengerEventRow.challenger_id.in_(
                            [
                                manifest.challenger_id
                                for manifest in manifests
                            ]
                        )
                    )
                    .order_by(
                        ChallengerEventRow.challenger_id,
                        desc(ChallengerEventRow.sequence),
                    )
                )
            )
            latest_event_by_challenger: dict[
                str,
                ChallengerEventRow,
            ] = {}
            for event in challenger_events:
                latest_event_by_challenger.setdefault(
                    event.challenger_id,
                    event,
                )
            challengers = [
                {
                    **manifest.payload_json,
                    "current_status": (
                        manifest.initial_status
                        if (
                            latest_event_by_challenger.get(
                                manifest.challenger_id
                            )
                            is None
                        )
                        else latest_event_by_challenger[
                            manifest.challenger_id
                        ].to_status
                    ),
                    "latest_status_reason": (
                        None
                        if (
                            latest_event_by_challenger.get(
                                manifest.challenger_id
                            )
                            is None
                        )
                        else latest_event_by_challenger[
                            manifest.challenger_id
                        ].reason_code
                    ),
                }
                for manifest in manifests
            ]
        return {
            "selected_commander": (
                None if selection is None else selection.model_dump(mode="json")
            ),
            "recent_cycles": [row.payload_json for row in cycles],
            "evidence_sources": [row.payload_json for row in evidence],
            "algorithm_proposals": [row.payload_json for row in proposals],
            "challengers": challengers,
            "candidate_artifacts": [row.payload_json for row in candidate_artifacts],
            "falsification_reports": [row.payload_json for row in falsification_reports],
            "replay_artifacts": [row.payload_json for row in replay_artifacts],
            "oos_results": [row.payload_json for row in oos_results],
            "shadow_arm_registrations": [row.payload_json for row in shadow_registrations],
            "shadow_performance_summaries": [row.payload_json for row in shadow_summaries],
            "trusted_promotion_evidence": [row.payload_json for row in promotion_evidence],
            "trusted_promotion_evaluations": [row.payload_json for row in trusted_evaluations],
            "promotion_decisions": [row.payload_json for row in promotions],
            "champion_designations": [row.payload_json for row in champion_designations],
            "current_champion": (
                None if not champion_designations else champion_designations[0].payload_json
            ),
            "promotion_gate": {
                "eligible_challenger_ids": (trusted_eligible_challenger_ids),
                "manually_approved_challenger_ids": (manually_approved_challenger_ids),
                "explicit_human_designation_available": bool(
                    set(manually_approved_challenger_ids)
                    - {row.source_challenger_id for row in champion_designations}
                ),
                "automatic_promotion_enabled": False,
                "champion_mutation_available": False,
                "real_order_routing": False,
            },
            "experiment_outcome_ledger": self.experiment_outcomes().status(),
            "publications": [],
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    @staticmethod
    def _add_cycle_event(
        session: Session,
        *,
        cycle_id: str,
        event_type: str,
        actor_role: str,
        artifact_hash: str | None,
        idempotency_key: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> str:
        existing = session.scalar(
            select(ResearchCycleEventRow).where(
                ResearchCycleEventRow.research_cycle_id == cycle_id,
                ResearchCycleEventRow.idempotency_key == idempotency_key,
            )
        )
        event_payload = {
            "research_cycle_id": cycle_id,
            "event_type": event_type,
            "actor_role": actor_role,
            "artifact_hash": artifact_hash,
            "idempotency_key": idempotency_key,
            "payload": payload,
            "created_at": created_at,
        }
        event_hash = canonical_hash(event_payload)
        if existing is not None:
            if existing.event_hash != event_hash:
                raise ResearchPersistenceError("cycle-event idempotency conflict")
            return existing.event_id
        event_id = stable_id("research-cycle-event", cycle_id, idempotency_key)
        session.add(
            ResearchCycleEventRow(
                event_id=event_id,
                research_cycle_id=cycle_id,
                event_type=event_type,
                actor_role=actor_role,
                artifact_hash=artifact_hash,
                idempotency_key=idempotency_key,
                event_hash=event_hash,
                payload_json=canonical_data(event_payload),
                created_at=created_at,
            )
        )
        return event_id

    @staticmethod
    def _current_challenger_status(
        session: Session,
        manifest: ChallengerManifestRow,
    ) -> ChallengerStatus:
        latest = session.scalar(
            select(ChallengerEventRow)
            .where(ChallengerEventRow.challenger_id == manifest.challenger_id)
            .order_by(desc(ChallengerEventRow.sequence))
            .limit(1)
        )
        return ChallengerStatus(manifest.initial_status if latest is None else latest.to_status)

    @staticmethod
    def _challenger_for_update(
        session: Session,
        challenger_id: str,
    ) -> ChallengerManifestRow:
        statement = select(ChallengerManifestRow).where(
            ChallengerManifestRow.challenger_id == challenger_id
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        manifest = session.scalar(statement)
        if manifest is None:
            raise ResearchPersistenceError("unknown Challenger")
        return manifest

    @staticmethod
    def _challenger_registration_lock(
        session: Session,
        *,
        strategy_id: str,
        strategy_version: str,
    ) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:registration_key, 0))"),
                {"registration_key": (f"research-challenger:{strategy_id}:{strategy_version}")},
            )

    @staticmethod
    def _validate_stored_proposal(
        proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
        proposal_row: AlgorithmProposalRow,
    ) -> None:
        stored_bindings: tuple[tuple[str, object, object], ...] = (
            ("proposal_id", proposal.proposal_id, proposal_row.proposal_id),
            ("proposal_hash", proposal.proposal_hash, proposal_row.proposal_hash),
            ("hypothesis_id", proposal.hypothesis_id, proposal_row.hypothesis_id),
            (
                "parent_strategy_id",
                proposal.parent_strategy_id,
                proposal_row.parent_strategy_id,
            ),
            (
                "parent_strategy_version",
                proposal.parent_strategy_version,
                proposal_row.parent_strategy_version,
            ),
            (
                "proposed_strategy_id",
                proposal.proposed_strategy_id,
                proposal_row.proposed_strategy_id,
            ),
            (
                "proposed_strategy_version",
                proposal.proposed_strategy_version,
                proposal_row.proposed_strategy_version,
            ),
        )
        mismatches = [name for name, actual, expected in stored_bindings if actual != expected]
        if mismatches:
            raise ResearchPersistenceError(
                "stored proposal binding mismatch: " + ",".join(mismatches)
            )
        if proposal_row.evidence_manifest_hash != canonical_hash(
            sorted(proposal.evidence_source_ids)
        ):
            raise ResearchPersistenceError("stored proposal evidence manifest hash mismatch")

    @staticmethod
    def _validate_stored_candidate_artifact(
        bundle: CandidateArtifactBundleV1,
        row: ResearchCandidateArtifactRow,
    ) -> None:
        bindings: tuple[tuple[str, object, object], ...] = (
            ("bundle_id", bundle.bundle_id, row.bundle_id),
            ("challenger_id", bundle.challenger_id, row.challenger_id),
            (
                "research_cycle_id",
                bundle.request_binding.research_cycle_id,
                row.research_cycle_id,
            ),
            (
                "candidate_tree_hash",
                bundle.candidate_tree_hash,
                row.candidate_tree_hash,
            ),
            ("code_hash", bundle.code_hash, row.code_hash),
            ("config_hash", bundle.config_hash, row.config_hash),
            (
                "test_manifest_hash",
                bundle.test_manifest_hash,
                row.test_manifest_hash,
            ),
            (
                "declared_entrypoint",
                bundle.declared_entrypoint,
                row.declared_entrypoint,
            ),
            ("bundle_hash", bundle.bundle_hash, row.bundle_hash),
        )
        mismatches = [name for name, actual, expected in bindings if actual != expected]
        if mismatches:
            raise ResearchPersistenceError(
                "stored candidate artifact binding mismatch: " + ",".join(mismatches)
            )

    @classmethod
    def _require_registered_candidate_artifact(
        cls,
        session: Session,
        *,
        challenger_id: str,
        candidate_artifact_hash: str | None = None,
    ) -> CandidateArtifactBundleV1:
        predicates = [ResearchCandidateArtifactRow.challenger_id == challenger_id]
        if candidate_artifact_hash is not None:
            predicates.append(ResearchCandidateArtifactRow.bundle_hash == candidate_artifact_hash)
        row = session.scalar(select(ResearchCandidateArtifactRow).where(*predicates))
        if row is None:
            raise ResearchPersistenceError("registered candidate artifact is required")
        try:
            bundle = CandidateArtifactBundleV1.model_validate(row.payload_json)
        except ValueError as exc:
            raise ResearchPersistenceError("stored candidate artifact payload is invalid") from exc
        cls._validate_stored_candidate_artifact(bundle, row)
        return bundle

    @staticmethod
    def _falsification_bindings(
        report: FalsificationReportV1,
    ) -> dict[str, str | int]:
        required_hashes = (
            "candidate_artifact_hash",
            "evaluation_contract_hash",
            "data_manifest_hash",
            "replay_hash",
        )
        expected: dict[str, str | int] | None = None
        for result in report.results:
            values: dict[str, str | int] = {}
            for field_name in required_hashes:
                value = result.metrics.get(field_name)
                if (
                    not isinstance(value, str)
                    or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)
                ):
                    raise ResearchPersistenceError(
                        f"falsification result is missing a valid {field_name} binding"
                    )
                values[field_name] = value
            deterministic_seed = result.metrics.get("deterministic_seed")
            if type(deterministic_seed) is not int or deterministic_seed < 0:
                raise ResearchPersistenceError(
                    "falsification result is missing a valid deterministic_seed"
                )
            values["deterministic_seed"] = deterministic_seed
            if expected is None:
                expected = values
            elif values != expected:
                raise ResearchPersistenceError("falsification result bindings are inconsistent")
        if expected is None:
            raise ResearchPersistenceError("falsification report has no binding-bearing results")
        return expected

    @classmethod
    def _require_falsification_bindings(
        cls,
        report: FalsificationReportV1,
        *,
        candidate_artifact_hash: str,
        data_manifest_hash: str,
        replay_hash: str,
        evaluation_contract_hash: str | None = None,
    ) -> None:
        bindings = cls._falsification_bindings(report)
        expected: dict[str, str] = {
            "candidate_artifact_hash": candidate_artifact_hash,
            "data_manifest_hash": data_manifest_hash,
            "replay_hash": replay_hash,
        }
        if evaluation_contract_hash is not None:
            expected["evaluation_contract_hash"] = evaluation_contract_hash
        mismatches = [
            field_name
            for field_name, expected_value in expected.items()
            if bindings[field_name] != expected_value
        ]
        if mismatches:
            raise ResearchPersistenceError(
                "falsification binding mismatch: " + ",".join(mismatches)
            )

    @staticmethod
    def _validate_challenger_registration(
        *,
        manifest: ChallengerManifestV1,
        proposal: AlgorithmProposalV1 | AlgorithmProposalV2,
        proposal_row: AlgorithmProposalRow,
        cycle: ResearchCycleRow,
    ) -> None:
        ResearchRepository._validate_stored_proposal(proposal, proposal_row)
        bindings: tuple[tuple[str, object, object], ...] = (
            ("proposal_hash", manifest.proposal_hash, proposal.proposal_hash),
            ("strategy_id", manifest.strategy_id, proposal.proposed_strategy_id),
            (
                "strategy_version",
                manifest.strategy_version,
                proposal.proposed_strategy_version,
            ),
            (
                "parent_version",
                manifest.parent_version,
                proposal.parent_strategy_version,
            ),
            ("hypothesis_id", manifest.hypothesis_id, proposal.hypothesis_id),
            (
                "experiment_family",
                manifest.experiment_family,
                cycle.experiment_family,
            ),
            (
                "created_by_commander",
                manifest.created_by_commander.value,
                cycle.selected_commander,
            ),
            (
                "evidence_source_ids",
                sorted(manifest.evidence_source_ids),
                sorted(proposal.evidence_source_ids),
            ),
            ("required_data", manifest.required_data, proposal.required_data),
            (
                "decision_horizon",
                manifest.decision_horizon,
                proposal.target_horizon,
            ),
            (
                "execution_universe",
                manifest.execution_universe,
                proposal.target_universe,
            ),
            (
                "estimated_turnover",
                manifest.estimated_turnover,
                proposal.estimated_turnover,
            ),
            (
                "estimated_capacity",
                manifest.estimated_capacity,
                proposal.estimated_capacity,
            ),
        )
        mismatches = [name for name, actual, expected in bindings if actual != expected]
        if mismatches:
            raise ResearchPersistenceError(
                "Challenger manifest does not match its accepted proposal: " + ",".join(mismatches)
            )

    @staticmethod
    def _append_challenger_transition(
        session: Session,
        *,
        manifest: ChallengerManifestRow,
        to_status: ChallengerStatus,
        reason_code: str,
        artifact_hash: str | None,
        idempotency_key: str,
        created_at: datetime,
        artifact_payload: dict[str, Any] | None = None,
    ) -> bool:
        existing = session.scalar(
            select(ChallengerEventRow).where(
                ChallengerEventRow.challenger_id == manifest.challenger_id,
                ChallengerEventRow.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.to_status != to_status.value:
                raise ResearchPersistenceError("idempotency key status conflict")
            return False
        current = ResearchRepository._current_challenger_status(session, manifest)
        if to_status not in ALLOWED_TRANSITIONS[current]:
            raise ResearchPersistenceError(
                f"invalid Challenger transition {current.value}->{to_status.value}"
            )
        latest_sequence = session.scalar(
            select(func.max(ChallengerEventRow.sequence)).where(
                ChallengerEventRow.challenger_id == manifest.challenger_id
            )
        )
        sequence = 1 if latest_sequence is None else latest_sequence + 1
        payload: dict[str, Any] = {
            "challenger_id": manifest.challenger_id,
            "sequence": sequence,
            "from_status": current.value,
            "to_status": to_status.value,
            "reason_code": reason_code,
            "artifact_hash": artifact_hash,
            "idempotency_key": idempotency_key,
            "created_at": created_at,
        }
        if artifact_payload is not None:
            payload["artifact_payload"] = artifact_payload
        session.add(
            ChallengerEventRow(
                challenger_event_id=stable_id(
                    "challenger-event",
                    manifest.challenger_id,
                    idempotency_key,
                ),
                challenger_id=manifest.challenger_id,
                sequence=sequence,
                from_status=current.value,
                to_status=to_status.value,
                reason_code=reason_code,
                artifact_hash=artifact_hash,
                idempotency_key=idempotency_key,
                event_hash=canonical_hash(payload),
                payload_json=cast(dict[str, Any], canonical_data(payload)),
                created_at=created_at,
            )
        )
        return True

    @staticmethod
    def _validate_shadow_pair(
        session: Session,
        *,
        manifest: ChallengerManifestRow,
        champion: ShadowArmIdentity,
        challenger: ShadowArmIdentity,
    ) -> None:
        try:
            require_matched_shadow_contract(champion, challenger)
        except ValueError as exc:
            raise ResearchPersistenceError(str(exc)) from exc
        if (
            challenger.strategy_id != manifest.strategy_id
            or challenger.strategy_version != manifest.strategy_version
        ):
            raise ResearchPersistenceError("Challenger shadow identity does not match manifest")
        proposal = session.get(AlgorithmProposalRow, manifest.proposal_id)
        if proposal is None:
            raise ResearchPersistenceError("Challenger proposal is missing")
        if (
            champion.strategy_id != proposal.parent_strategy_id
            or champion.strategy_version != proposal.parent_strategy_version
        ):
            raise ResearchPersistenceError(
                "Champion shadow identity does not match parent strategy"
            )

    @staticmethod
    def _validate_shadow_performance_summary(
        session: Session,
        *,
        manifest: ChallengerManifestRow,
        summary: TrustedShadowPerformanceSummaryV1,
    ) -> None:
        if (
            summary.challenger_id != manifest.challenger_id
            or summary.candidate_version != manifest.strategy_version
        ):
            raise ResearchPersistenceError("shadow summary does not match Challenger manifest")
        registrations = list(
            session.scalars(
                select(ResearchShadowArmRegistrationRow)
                .where(ResearchShadowArmRegistrationRow.challenger_id == manifest.challenger_id)
                .order_by(ResearchShadowArmRegistrationRow.arm_role)
            )
        )
        if (
            len(registrations) != 2
            or {row.arm_role for row in registrations} != {"CHAMPION", "CHALLENGER"}
            or {row.shadow_pair_id for row in registrations} != {summary.shadow_pair_id}
            or any(row.real_order_routing for row in registrations)
        ):
            raise ResearchPersistenceError(
                "shadow summary requires its registered matched paper pair"
            )
        by_role = {row.arm_role: row for row in registrations}
        champion = by_role["CHAMPION"]
        challenger = by_role["CHALLENGER"]
        if (
            champion.strategy_version != summary.current_champion_version
            or challenger.strategy_version != summary.candidate_version
            or champion.execution_contract_hash != summary.execution_contract_hash
            or challenger.execution_contract_hash != summary.execution_contract_hash
            or canonical_hash(champion.payload_json) != summary.champion_registration_hash
            or canonical_hash(challenger.payload_json) != summary.challenger_registration_hash
        ):
            raise ResearchPersistenceError("shadow summary registration binding mismatch")
        oos = session.get(OosLockboxResultRow, challenger.oos_result_id)
        if (
            oos is None
            or oos.verdict != OosVerdict.PASS.value
            or oos.candidate_artifact_hash != summary.candidate_artifact_hash
        ):
            raise ResearchPersistenceError("shadow summary candidate artifact is not OOS-approved")
        events = list(
            session.scalars(
                select(DomainEventRow)
                .where(
                    DomainEventRow.aggregate_type == "RESEARCH_MATCHED_SHADOW_CYCLE",
                    DomainEventRow.aggregate_id == summary.run_id,
                    DomainEventRow.correlation_id == summary.shadow_pair_id,
                    DomainEventRow.available_at <= summary.data_available_cutoff,
                )
                .order_by(
                    DomainEventRow.available_at,
                    DomainEventRow.event_id,
                )
            )
        )
        event_hashes = tuple(row.payload_hash for row in events)
        provenance_ids: list[str] = []
        cycle_results: list[MatchedShadowCycleResultV1] = []
        for event in events:
            if (
                event.event_type != TRUSTED_SHADOW_CYCLE_EVENT
                or event.causation_id is None
            ):
                raise ResearchPersistenceError(
                    "shadow summary contains unattested daily evidence"
                )
            source_row = session.get(DomainEventRow, event.causation_id)
            if (
                source_row is None
                or source_row.aggregate_type
                != PROSPECTIVE_SHADOW_SOURCE_AGGREGATE
                or source_row.aggregate_id != summary.run_id
                or source_row.event_type != "PROSPECTIVE_SOURCE_VERIFIED"
                or _stored_time(source_row.available_at)
                > summary.data_available_cutoff
            ):
                raise ResearchPersistenceError(
                    "shadow summary lost its prospective source evidence"
                )
            try:
                source = ProspectiveShadowCycleSourceV1.model_validate(
                    source_row.payload_json
                )
                cycle = MatchedShadowCycleResultV1.model_validate(
                    event.payload_json
                )
            except ValueError as exc:
                raise ResearchPersistenceError(
                    "shadow summary source evidence is invalid"
                ) from exc
            if (
                source.provenance_id != source_row.event_id
                or source.provenance_hash != source_row.payload_hash
                or source.run_id != summary.run_id
                or source.shadow_pair_id != summary.shadow_pair_id
                or source.challenger_id != summary.challenger_id
                or source.prospective_request_id
                != source_row.correlation_id
                or source.prospective_execution_id
                != source_row.causation_id
                or source.champion_target_hash
                != cycle.champion.target.target_hash
                or source.challenger_target_hash
                != cycle.challenger.target.target_hash
                or source.quote_bundle_hash != cycle.quote_bundle.bundle_hash
                or source.quote_manifest_hash
                != cycle.quote_bundle.quote_manifest_hash
                or cycle.result_hash != event.payload_hash
            ):
                raise ResearchPersistenceError(
                    "shadow summary prospective source binding mismatch"
                )
            provenance_ids.append(source.provenance_id)
            cycle_results.append(cycle)
        if (
            event_hashes != summary.daily_evidence_hashes
            or len(events) != summary.forward_sessions
            or len(provenance_ids) != len(set(provenance_ids))
        ):
            raise ResearchPersistenceError("shadow summary does not match immutable daily evidence")
        run = session.get(RunRow, summary.run_id)
        if (
            run is None
            or run.experiment_version != SHADOW_RUNTIME_VERSION
            or run.result_manifest is None
        ):
            raise ResearchPersistenceError(
                "shadow summary lacks its versioned runtime contract"
            )
        try:
            spec = ShadowPairRuntimeSpecV1.model_validate(run.result_manifest)
            if run.result_hash != spec.spec_hash:
                raise ValueError("shadow runtime spec row hash mismatch")
            replay_hash = canonical_hash(
                {
                    "schema_version": "research_shadow_replay_v1",
                    "run_id": summary.run_id,
                    "spec_hash": spec.spec_hash,
                    "cycle_hashes": [
                        item.result_hash for item in cycle_results
                    ],
                }
            )
            expected_summary = summarize_matched_shadow_results(
                spec=spec,
                results=tuple(cycle_results),
                replay_hash=replay_hash,
            )
        except ValueError as exc:
            raise ResearchPersistenceError(
                "shadow summary runtime evidence is invalid"
            ) from exc
        if expected_summary.summary_hash != summary.source_summary.summary_hash:
            raise ResearchPersistenceError(
                "shadow summary metrics do not match immutable cycles"
            )

    @staticmethod
    def _validate_promotion_evidence_binding(
        session: Session,
        *,
        manifest: ChallengerManifestRow,
        evidence: PromotionEvidenceV1,
    ) -> ResearchShadowPerformanceSummaryRow:
        if (
            evidence.challenger_id != manifest.challenger_id
            or evidence.candidate_version != manifest.strategy_version
        ):
            raise ResearchPersistenceError("promotion evidence does not match Challenger")
        summary_row = session.scalar(
            select(ResearchShadowPerformanceSummaryRow).where(
                ResearchShadowPerformanceSummaryRow.challenger_id == manifest.challenger_id,
                ResearchShadowPerformanceSummaryRow.summary_hash == evidence.shadow_summary_hash,
            )
        )
        if summary_row is None:
            raise ResearchPersistenceError("promotion evidence lacks its persisted shadow summary")
        summary = TrustedShadowPerformanceSummaryV1.model_validate(summary_row.payload_json)
        ResearchRepository._validate_shadow_performance_summary(
            session,
            manifest=manifest,
            summary=summary,
        )
        metric_bindings = (
            (
                evidence.forward_sessions,
                summary.forward_sessions,
            ),
            (
                evidence.independent_trades,
                summary.independent_trades,
            ),
            (
                evidence.annualized_net_excess_return_after_cost,
                summary.annualized_net_excess_return_after_cost,
            ),
            (
                evidence.matched_annualized_difference,
                summary.matched_annualized_difference,
            ),
            (evidence.economic_effect, summary.economic_effect),
            (evidence.maximum_drawdown, summary.maximum_drawdown),
            (evidence.tail_loss, summary.tail_loss),
            (evidence.annualized_turnover, summary.annualized_turnover),
            (
                evidence.estimated_capacity_usd,
                summary.estimated_capacity_usd,
            ),
            (
                evidence.regime_pass_fraction,
                summary.regime_pass_fraction,
            ),
            (evidence.runtime_error_rate, summary.runtime_error_rate),
        )
        if any(actual != expected for actual, expected in metric_bindings):
            raise ResearchPersistenceError("promotion metrics do not match shadow summary")
        falsification = session.scalar(
            select(FalsificationReportRow).where(
                FalsificationReportRow.challenger_id == manifest.challenger_id,
                FalsificationReportRow.report_hash == evidence.falsification_report_hash,
                FalsificationReportRow.mandatory_passed.is_(True),
            )
        )
        oos = session.scalar(
            select(OosLockboxResultRow).where(
                OosLockboxResultRow.challenger_id == manifest.challenger_id,
                OosLockboxResultRow.result_hash == evidence.oos_result_hash,
                OosLockboxResultRow.candidate_artifact_hash == evidence.candidate_artifact_hash,
                OosLockboxResultRow.verdict == OosVerdict.PASS.value,
            )
        )
        replay = session.scalar(
            select(ResearchReplayArtifactRow).where(
                ResearchReplayArtifactRow.challenger_id == manifest.challenger_id,
                ResearchReplayArtifactRow.candidate_artifact_hash
                == evidence.candidate_artifact_hash,
                ResearchReplayArtifactRow.first_replay_hash == evidence.replay_hash,
                ResearchReplayArtifactRow.second_replay_hash == evidence.replay_hash,
                ResearchReplayArtifactRow.deterministic_match.is_(True),
            )
        )
        if falsification is None or oos is None or replay is None:
            raise ResearchPersistenceError("promotion evidence artifact binding failed")
        if (
            evidence.common_oos_sessions != oos.common_sessions
            or not evidence.mandatory_tests_passed
            or not evidence.replay_reproducible
            or evidence.candidate_version != summary.candidate_version
            or evidence.current_champion_version != summary.current_champion_version
        ):
            raise ResearchPersistenceError("promotion evidence prerequisite flags are invalid")
        latest_designation = ResearchRepository._latest_champion_designation(session)
        if (
            latest_designation is not None
            and latest_designation.strategy_version != evidence.current_champion_version
        ):
            raise ResearchPersistenceError("promotion evidence is stale against current Champion")
        return summary_row

    @staticmethod
    def _latest_trusted_eligible_evaluation(
        session: Session,
        *,
        challenger_id: str,
    ) -> TrustedPromotionEvaluationRow:
        row = session.scalar(
            select(TrustedPromotionEvaluationRow)
            .where(
                TrustedPromotionEvaluationRow.challenger_id == challenger_id,
                TrustedPromotionEvaluationRow.verdict
                == PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL.value,
            )
            .order_by(
                desc(TrustedPromotionEvaluationRow.created_at),
                desc(TrustedPromotionEvaluationRow.evaluation_id),
            )
            .limit(1)
        )
        if row is None:
            raise ResearchPersistenceError("manual action requires trusted promotion eligibility")
        return row

    @staticmethod
    def _latest_champion_designation(
        session: Session,
    ) -> ResearchChampionDesignationRow | None:
        statement = (
            select(ResearchChampionDesignationRow)
            .order_by(desc(ResearchChampionDesignationRow.sequence))
            .limit(1)
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        return session.scalar(statement)

    def _designate_champion_once(
        self,
        *,
        challenger_id: str,
        expected_current_version: str,
        designated_by: str,
        idempotency_key: str,
        designated_at: datetime,
    ) -> tuple[ChampionDesignationV1, bool]:
        with self._session_factory.begin() as session:
            self._champion_designation_lock(session)
            existing = session.scalar(
                select(ResearchChampionDesignationRow).where(
                    ResearchChampionDesignationRow.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                designation = ChampionDesignationV1.model_validate(existing.payload_json)
                if (
                    designation.source_challenger_id != challenger_id
                    or designation.expected_current_version != expected_current_version
                    or designation.designated_by != designated_by
                ):
                    raise ResearchPersistenceError("Champion designation idempotency conflict")
                return designation, False
            manifest = self._challenger_for_update(session, challenger_id)
            if (
                self._current_challenger_status(session, manifest)
                is not ChallengerStatus.PROMOTION_ELIGIBLE
            ):
                raise ResearchPersistenceError("Champion designation requires PROMOTION_ELIGIBLE")
            evaluation_row = self._latest_trusted_eligible_evaluation(
                session,
                challenger_id=challenger_id,
            )
            evaluation = _trusted_promotion_evaluation_from_payload(
                evaluation_row.payload_json
            )
            eligible = evaluation.decision
            manual_row = session.scalar(
                select(ResearchPromotionDecisionRow)
                .where(
                    ResearchPromotionDecisionRow.challenger_id == challenger_id,
                    ResearchPromotionDecisionRow.verdict
                    == PromotionVerdict.MANUALLY_APPROVED.value,
                )
                .order_by(
                    desc(ResearchPromotionDecisionRow.created_at),
                    desc(ResearchPromotionDecisionRow.promotion_decision_id),
                )
                .limit(1)
            )
            if manual_row is None:
                raise ResearchPersistenceError(
                    "Champion designation requires explicit manual approval"
                )
            manual = PromotionDecisionV1.model_validate(manual_row.payload_json)
            if (
                manual.current_champion_version != eligible.current_champion_version
                or manual.candidate_version != eligible.candidate_version
                or manual.criteria != eligible.criteria
                or manual.replay_hash != eligible.replay_hash
                or manual.created_at < evaluation.created_at
            ):
                raise ResearchPersistenceError("manual approval does not match trusted evaluation")
            latest = self._latest_champion_designation(session)
            actual_current_version = (
                eligible.current_champion_version if latest is None else latest.strategy_version
            )
            if expected_current_version != actual_current_version:
                raise ResearchPersistenceError("Champion expected-current-version conflict")
            if eligible.current_champion_version != actual_current_version:
                raise ResearchPersistenceError(
                    "trusted evaluation is stale against current Champion"
                )
            if designated_at < manual.created_at:
                raise ResearchPersistenceError("Champion designation predates manual approval")
            evidence_row = session.get(
                ResearchPromotionEvidenceRow,
                evaluation_row.evidence_id,
            )
            if evidence_row is None:
                raise ResearchPersistenceError("trusted evaluation lost its promotion evidence")
            evidence = _promotion_evidence_from_payload(
                evidence_row.payload_json
            )
            sequence = 1 if latest is None else latest.sequence + 1
            payload = {
                "schema_version": "champion_designation_v1",
                "designation_id": stable_id(
                    "champion-designation",
                    sequence,
                    challenger_id,
                    evaluation.evaluation_hash,
                    manual.decision_hash,
                ),
                "sequence": sequence,
                "strategy_id": manifest.strategy_id,
                "strategy_version": manifest.strategy_version,
                "candidate_artifact_hash": (evidence.candidate_artifact_hash),
                "source_challenger_id": challenger_id,
                "trusted_evaluation_id": evaluation.evaluation_id,
                "trusted_evaluation_hash": evaluation.evaluation_hash,
                "manual_approval_decision_id": (manual.promotion_decision_id),
                "manual_approval_decision_hash": manual.decision_hash,
                "previous_designation_id": (None if latest is None else latest.designation_id),
                "expected_current_version": expected_current_version,
                "designated_by": designated_by,
                "idempotency_key": idempotency_key,
                "designated_at": designated_at,
                "automatic_promotion_enabled": False,
                "real_order_routing": False,
            }
            designation = ChampionDesignationV1.model_validate(
                {**payload, "designation_hash": canonical_hash(payload)}
            )
            session.add(
                ResearchChampionDesignationRow(
                    designation_id=designation.designation_id,
                    sequence=designation.sequence,
                    strategy_id=designation.strategy_id,
                    strategy_version=designation.strategy_version,
                    candidate_artifact_hash=(designation.candidate_artifact_hash),
                    source_challenger_id=(designation.source_challenger_id),
                    trusted_evaluation_id=(designation.trusted_evaluation_id),
                    manual_approval_decision_id=(designation.manual_approval_decision_id),
                    previous_designation_id=(designation.previous_designation_id),
                    expected_current_version=(designation.expected_current_version),
                    designated_by=designation.designated_by,
                    idempotency_key=designation.idempotency_key,
                    automatic_promotion_enabled=False,
                    real_order_routing=False,
                    designation_hash=designation.designation_hash,
                    payload_json=model_payload(designation),
                    designated_at=designation.designated_at,
                )
            )
            self._append_challenger_transition(
                session,
                manifest=manifest,
                to_status=ChallengerStatus.PROMOTED,
                reason_code="EXPLICIT_HUMAN_CHAMPION_DESIGNATION",
                artifact_hash=designation.designation_hash,
                idempotency_key=(f"champion-designation:{designation.designation_hash}"),
                created_at=designated_at,
            )
            session.flush()
            return designation, True

    @staticmethod
    def _validate_promotion_binding(
        session: Session,
        *,
        manifest: ChallengerManifestRow,
        decision: PromotionDecisionV1,
        expected_status: ChallengerStatus,
    ) -> None:
        current = ResearchRepository._current_challenger_status(session, manifest)
        if current is not expected_status:
            raise ResearchPersistenceError(
                f"promotion decision requires {expected_status.value}, got {current.value}"
            )
        required = set(REQUIRED_PROMOTION_CRITERIA)
        provided = set(decision.criteria)
        if provided != required:
            missing = sorted(required - provided)
            unknown = sorted(provided - required)
            raise ResearchPersistenceError(
                f"promotion criteria mismatch missing={missing} unknown={unknown}"
            )
        expected_failures = {
            name.upper() for name in REQUIRED_PROMOTION_CRITERIA if not decision.criteria[name]
        }
        if set(decision.failed_reason_codes) != expected_failures:
            raise ResearchPersistenceError("promotion failed_reason_codes do not match criteria")
        if decision.candidate_version != manifest.strategy_version:
            raise ResearchPersistenceError("promotion candidate version does not match Challenger")
        if decision.current_champion_version != manifest.parent_version:
            raise ResearchPersistenceError(
                "promotion Champion version does not match Challenger parent"
            )
        replay = session.scalar(
            select(ResearchReplayArtifactRow).where(
                ResearchReplayArtifactRow.challenger_id == decision.challenger_id,
                ResearchReplayArtifactRow.deterministic_match.is_(True),
                ResearchReplayArtifactRow.first_replay_hash == decision.replay_hash,
                ResearchReplayArtifactRow.second_replay_hash == decision.replay_hash,
            )
        )
        if replay is None:
            raise ResearchPersistenceError(
                "promotion replay hash does not match deterministic replay"
            )
        falsification = session.scalar(
            select(FalsificationReportRow).where(
                FalsificationReportRow.challenger_id == decision.challenger_id,
                FalsificationReportRow.mandatory_passed.is_(True),
            )
        )
        if falsification is None:
            raise ResearchPersistenceError("promotion requires passed mandatory falsification")
        oos = session.scalar(
            select(OosLockboxResultRow).where(
                OosLockboxResultRow.challenger_id == decision.challenger_id,
                OosLockboxResultRow.verdict == OosVerdict.PASS.value,
                OosLockboxResultRow.candidate_artifact_hash == replay.candidate_artifact_hash,
            )
        )
        if oos is None:
            raise ResearchPersistenceError("promotion requires candidate-bound passed OOS")
        registrations = list(
            session.scalars(
                select(ResearchShadowArmRegistrationRow).where(
                    ResearchShadowArmRegistrationRow.challenger_id == decision.challenger_id
                )
            )
        )
        roles = {row.arm_role for row in registrations}
        pair_ids = {row.shadow_pair_id for row in registrations}
        contracts = {row.execution_contract_hash for row in registrations}
        if (
            len(registrations) != 2
            or roles != {"CHAMPION", "CHALLENGER"}
            or len(pair_ids) != 1
            or len(contracts) != 1
            or any(row.real_order_routing for row in registrations)
        ):
            raise ResearchPersistenceError("promotion requires a matched paper-only shadow pair")
        champion = next(row for row in registrations if row.arm_role == "CHAMPION")
        challenger = next(row for row in registrations if row.arm_role == "CHALLENGER")
        if (
            champion.strategy_version != decision.current_champion_version
            or challenger.strategy_version != decision.candidate_version
        ):
            raise ResearchPersistenceError("promotion versions do not match shadow registrations")
        if decision.verdict is PromotionVerdict.MANUALLY_APPROVED:
            if not decision.approved_by or expected_failures:
                raise ResearchPersistenceError("manual approval requires approver and all criteria")
        elif decision.approved_by is not None:
            raise ResearchPersistenceError("eligibility decisions cannot contain an approver")

    @staticmethod
    def _add_promotion_decision(
        session: Session,
        decision: PromotionDecisionV1,
    ) -> None:
        session.add(
            ResearchPromotionDecisionRow(
                promotion_decision_id=decision.promotion_decision_id,
                challenger_id=decision.challenger_id,
                verdict=decision.verdict.value,
                automatic_promotion_enabled=decision.automatic_promotion_enabled,
                replay_hash=decision.replay_hash,
                decision_hash=decision.decision_hash,
                payload_json=model_payload(decision),
                created_at=decision.created_at,
            )
        )

    @staticmethod
    def _register_shadow_pair(
        session: Session,
        *,
        manifest: ChallengerManifestRow,
        oos_result_id: str,
        result_hash: str,
        champion: ShadowArmIdentity,
        challenger: ShadowArmIdentity,
        created_at: datetime,
    ) -> str:
        contract_payload = asdict(champion.contract)
        contract_hash = canonical_hash(contract_payload)
        shadow_pair_id = stable_id(
            "research-shadow-pair",
            manifest.challenger_id,
            result_hash,
            contract_hash,
        )
        for arm_role, identity in (
            ("CHAMPION", champion),
            ("CHALLENGER", challenger),
        ):
            payload = {
                "schema_version": "research_shadow_arm_registration_v1",
                "shadow_pair_id": shadow_pair_id,
                "challenger_id": manifest.challenger_id,
                "oos_result_id": oos_result_id,
                "arm_role": arm_role,
                "arm_id": identity.arm_id,
                "strategy_id": identity.strategy_id,
                "strategy_version": identity.strategy_version,
                "execution_contract": contract_payload,
                "execution_contract_hash": contract_hash,
                "real_order_routing": False,
                "created_at": created_at,
            }
            session.add(
                ResearchShadowArmRegistrationRow(
                    shadow_registration_id=stable_id(
                        "research-shadow-arm",
                        shadow_pair_id,
                        arm_role,
                    ),
                    shadow_pair_id=shadow_pair_id,
                    challenger_id=manifest.challenger_id,
                    oos_result_id=oos_result_id,
                    arm_role=arm_role,
                    arm_id=identity.arm_id,
                    strategy_id=identity.strategy_id,
                    strategy_version=identity.strategy_version,
                    execution_contract_hash=contract_hash,
                    real_order_routing=False,
                    payload_json=cast(
                        dict[str, Any],
                        canonical_data(payload),
                    ),
                    created_at=created_at,
                )
            )
        return shadow_pair_id

    @staticmethod
    def _selection_lock(session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(84531202)"))

    @staticmethod
    def _oos_budget_lock(
        session: Session,
        *,
        experiment_family: str,
    ) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:budget_key, 0))"),
                {"budget_key": f"research-oos-budget:{experiment_family}"},
            )

    @staticmethod
    def _champion_designation_lock(session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(84531203)"))

    @staticmethod
    def _require_oos_reservation_binding(
        reservation: OosBudgetReservationV1,
        *,
        request: OosEvaluationRequest,
    ) -> None:
        if (
            reservation.challenger_id != request.challenger_id
            or reservation.experiment_family != request.experiment_family
            or reservation.submission_number != request.submission_number
            or reservation.candidate_artifact_hash != request.candidate_artifact_hash
            or reservation.evaluation_contract_hash != request.evaluation_contract_hash
        ):
            raise ResearchPersistenceError("OOS budget reservation idempotency conflict")


def _trusted_promotion_evaluation_from_payload(
    payload: dict[str, Any],
) -> TrustedPromotionEvaluationV1 | TrustedPromotionEvaluationV2:
    if payload.get("schema_version") == "trusted_promotion_evaluation_v2":
        return TrustedPromotionEvaluationV2.model_validate(payload)
    return TrustedPromotionEvaluationV1.model_validate(payload)


def _promotion_evidence_from_payload(
    payload: dict[str, Any],
) -> PromotionEvidenceV1 | PromotionEvidenceV2:
    if payload.get("schema_version") == "promotion_evidence_v2":
        return PromotionEvidenceV2.model_validate(payload)
    return PromotionEvidenceV1.model_validate(payload)


def _algorithm_proposal_from_payload(
    payload: dict[str, Any],
) -> AlgorithmProposalV1 | AlgorithmProposalV2:
    if payload.get("schema_version") == "algorithm_proposal_v2":
        return AlgorithmProposalV2.model_validate(payload)
    return AlgorithmProposalV1.model_validate(payload)


def _research_request_from_payload(
    payload: dict[str, Any],
) -> ResearchRequestV1 | ResearchRequestV2:
    if payload.get("schema_version") == "research_request_v2":
        return ResearchRequestV2.model_validate(payload)
    return ResearchRequestV1.model_validate(payload)


def _payload_timestamp(payload: dict[str, Any], field_name: str) -> datetime:
    raw = payload.get(field_name)
    if not isinstance(raw, str):
        raise ResearchPersistenceError(f"stored payload is missing {field_name}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchPersistenceError(f"stored payload has invalid {field_name}") from exc
    return require_aware_utc(parsed)


def _stored_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)
