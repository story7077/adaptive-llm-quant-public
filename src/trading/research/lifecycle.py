from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading.domain.hashing import canonical_hash
from trading.persistence.research import (
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.research.candidate_artifact import CandidateArtifactBundleV1
from trading.research.contracts import (
    ChallengerStatus,
    FalsificationReportV1,
    OosLockboxResultV1,
    OosVerdict,
    PromotionDecisionV1,
    PromotionVerdict,
)
from trading.research.oos_lockbox import (
    OosEvaluationRequest,
    OosLockboxService,
    OosLockboxServiceV2,
)
from trading.research.oos_v2 import OosLockboxResultV2
from trading.research.promotion_evidence import (
    ChampionDesignationV1,
    PromotionEvaluationContractV1,
    PromotionEvidenceV1,
    TrustedPromotionEvaluationV1,
    TrustedShadowPerformanceSummaryV1,
    evaluate_trusted_promotion_evidence,
)
from trading.research.promotion_v2 import (
    PromotionEvaluationContractV2,
    PromotionEvidenceV2,
    TrustedPromotionEvaluationV2,
    TrustedShadowPerformanceSummaryV2,
    evaluate_trusted_promotion_evidence_v2,
)
from trading.research.replay import DeterministicReplayArtifactV1
from trading.research.shadow import (
    ShadowArmIdentity,
    require_matched_shadow_contract,
)


class ResearchLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    challenger_id: str
    created: bool
    status: ChallengerStatus
    artifact_hash: str


@dataclass(frozen=True, slots=True)
class OosLifecycleResult:
    result: OosLockboxResultV1
    created: bool
    status: ChallengerStatus


@dataclass(frozen=True, slots=True)
class OosLifecycleResultV2:
    result: OosLockboxResultV2
    created: bool
    status: ChallengerStatus


@dataclass(frozen=True, slots=True)
class TrustedPromotionLifecycleResult:
    evidence: PromotionEvidenceV1
    evaluation: TrustedPromotionEvaluationV1
    created: bool
    status: ChallengerStatus


@dataclass(frozen=True, slots=True)
class TrustedPromotionLifecycleResultV2:
    evidence: PromotionEvidenceV2
    evaluation: TrustedPromotionEvaluationV2
    created: bool
    status: ChallengerStatus


@dataclass(frozen=True, slots=True)
class ManualPromotionApprovalResult:
    decision: PromotionDecisionV1
    created: bool
    status: ChallengerStatus


@dataclass(frozen=True, slots=True)
class ChampionDesignationResult:
    designation: ChampionDesignationV1
    created: bool
    status: ChallengerStatus


class ResearchLifecycleService:
    """Trusted host gate for falsification, OOS, and paper-only shadow entry."""

    real_order_routing = False
    automatic_promotion_enabled = False

    def __init__(
        self,
        *,
        repository: ResearchRepository,
        oos_lockbox: OosLockboxService | None = None,
        oos_lockbox_v2: OosLockboxServiceV2 | None = None,
    ) -> None:
        self._repository = repository
        self._oos_lockbox = oos_lockbox
        self._oos_lockbox_v2 = oos_lockbox_v2

    def record_falsification(
        self,
        report: FalsificationReportV1,
    ) -> LifecycleResult:
        try:
            created = self._repository.record_falsification_report(report)
            status = self._repository.challenger_status(report.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.PROPOSED if report.mandatory_passed else ChallengerStatus.TEST_FAILED
        )
        if status is not expected:
            raise ResearchLifecycleError(f"unexpected post-falsification status {status.value}")
        return LifecycleResult(
            challenger_id=report.challenger_id,
            created=created,
            status=status,
            artifact_hash=report.report_hash,
        )

    def register_candidate_artifact(
        self,
        bundle: CandidateArtifactBundleV1,
        *,
        created_at: datetime,
    ) -> LifecycleResult:
        try:
            created = self._repository.register_candidate_artifact(
                bundle,
                created_at=created_at,
            )
            status = self._repository.challenger_status(bundle.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.PROPOSED:
            raise ResearchLifecycleError("candidate artifact registration requires PROPOSED")
        return LifecycleResult(
            challenger_id=bundle.challenger_id,
            created=created,
            status=status,
            artifact_hash=bundle.bundle_hash,
        )

    def evaluate_oos_and_register_shadow(
        self,
        request: OosEvaluationRequest,
        *,
        champion_shadow: ShadowArmIdentity,
        challenger_shadow: ShadowArmIdentity,
        evaluated_at: datetime,
        persisted_at: datetime,
    ) -> OosLifecycleResult:
        existing = self._repository.oos_result(
            challenger_id=request.challenger_id,
            submission_number=request.submission_number,
        )
        if existing is not None:
            if (
                existing.experiment_family != request.experiment_family
                or existing.candidate_artifact_hash != request.candidate_artifact_hash
                or existing.evaluation_contract_hash != request.evaluation_contract_hash
            ):
                raise ResearchLifecycleError("OOS idempotency binding mismatch")
            return OosLifecycleResult(
                result=existing,
                created=False,
                status=self._repository.challenger_status(request.challenger_id),
            )
        if not self._repository.has_passed_falsification(
            challenger_id=request.challenger_id,
            candidate_artifact_hash=request.candidate_artifact_hash,
            evaluation_contract_hash=request.evaluation_contract_hash,
        ):
            raise ResearchLifecycleError("OOS cannot run before mandatory falsification passes")
        if not self._repository.has_passed_replay(
            challenger_id=request.challenger_id,
            candidate_artifact_hash=request.candidate_artifact_hash,
        ):
            raise ResearchLifecycleError("OOS cannot run before deterministic replay passes")
        if (
            self._repository.challenger_status(request.challenger_id)
            is not ChallengerStatus.PROPOSED
        ):
            raise ResearchLifecycleError("OOS requires a PROPOSED Challenger")
        try:
            require_matched_shadow_contract(champion_shadow, challenger_shadow)
            self._repository.validate_shadow_pair(
                challenger_id=request.challenger_id,
                champion=champion_shadow,
                challenger=challenger_shadow,
            )
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        except ValueError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if self._oos_lockbox is None:
            raise ResearchLifecycleError("OOS lockbox is not configured")
        result = self._oos_lockbox.evaluate(
            request,
            evaluated_at=evaluated_at,
        )
        if (
            result.challenger_id != request.challenger_id
            or result.experiment_family != request.experiment_family
            or result.submission_number != request.submission_number
            or result.candidate_artifact_hash != request.candidate_artifact_hash
            or result.evaluation_contract_hash != request.evaluation_contract_hash
        ):
            raise ResearchLifecycleError("OOS result is not bound to its request")
        try:
            created = self._repository.store_oos_result(
                result,
                created_at=persisted_at,
                candidate_artifact_hash=request.candidate_artifact_hash,
                champion_shadow=(champion_shadow if result.verdict is OosVerdict.PASS else None),
                challenger_shadow=(
                    challenger_shadow if result.verdict is OosVerdict.PASS else None
                ),
            )
            status = self._repository.challenger_status(request.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.SHADOW_PENDING
            if result.verdict is OosVerdict.PASS
            else ChallengerStatus.OOS_REJECTED
        )
        if status is not expected:
            raise ResearchLifecycleError(f"unexpected post-OOS status {status.value}")
        return OosLifecycleResult(
            result=result,
            created=created,
            status=status,
        )

    def evaluate_oos_and_register_shadow_v2(
        self,
        request: OosEvaluationRequest,
        *,
        champion_shadow: ShadowArmIdentity,
        challenger_shadow: ShadowArmIdentity,
        evaluated_at: datetime,
        persisted_at: datetime,
    ) -> OosLifecycleResultV2:
        existing = self._repository.oos_result_v2(
            challenger_id=request.challenger_id,
            submission_number=request.submission_number,
        )
        if existing is not None:
            if (
                existing.experiment_family != request.experiment_family
                or existing.candidate_artifact_hash
                != request.candidate_artifact_hash
                or existing.evaluation_contract_hash
                != request.evaluation_contract_hash
            ):
                raise ResearchLifecycleError(
                    "OOS V2 idempotency binding mismatch"
                )
            return OosLifecycleResultV2(
                result=existing,
                created=False,
                status=self._repository.challenger_status(
                    request.challenger_id
                ),
            )
        if self._repository.portfolio_sharpe().comparison_contract(
            challenger_id=request.challenger_id
        ) is None:
            raise ResearchLifecycleError(
                "OOS V2 requires a predeclared portfolio contract"
            )
        if not self._repository.has_passed_falsification(
            challenger_id=request.challenger_id,
            candidate_artifact_hash=request.candidate_artifact_hash,
            evaluation_contract_hash=request.evaluation_contract_hash,
        ):
            raise ResearchLifecycleError(
                "OOS V2 cannot run before falsification passes"
            )
        if not self._repository.has_passed_replay(
            challenger_id=request.challenger_id,
            candidate_artifact_hash=request.candidate_artifact_hash,
        ):
            raise ResearchLifecycleError(
                "OOS V2 cannot run before deterministic replay passes"
            )
        if (
            self._repository.challenger_status(request.challenger_id)
            is not ChallengerStatus.PROPOSED
        ):
            raise ResearchLifecycleError("OOS V2 requires PROPOSED")
        try:
            require_matched_shadow_contract(
                champion_shadow,
                challenger_shadow,
            )
            self._repository.validate_shadow_pair(
                challenger_id=request.challenger_id,
                champion=champion_shadow,
                challenger=challenger_shadow,
            )
        except (ResearchPersistenceError, ValueError) as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if self._oos_lockbox_v2 is None:
            raise ResearchLifecycleError("OOS V2 lockbox is not configured")
        result = self._oos_lockbox_v2.evaluate(
            request,
            evaluated_at=evaluated_at,
        )
        if (
            result.challenger_id != request.challenger_id
            or result.experiment_family != request.experiment_family
            or result.submission_number != request.submission_number
            or result.candidate_artifact_hash
            != request.candidate_artifact_hash
            or result.evaluation_contract_hash
            != request.evaluation_contract_hash
        ):
            raise ResearchLifecycleError(
                "OOS V2 result is not bound to request"
            )
        try:
            created = self._repository.store_oos_result_v2(
                result,
                created_at=persisted_at,
                candidate_artifact_hash=request.candidate_artifact_hash,
                champion_shadow=(
                    champion_shadow
                    if result.verdict is OosVerdict.PASS
                    else None
                ),
                challenger_shadow=(
                    challenger_shadow
                    if result.verdict is OosVerdict.PASS
                    else None
                ),
            )
            status = self._repository.challenger_status(
                request.challenger_id
            )
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.SHADOW_PENDING
            if result.verdict is OosVerdict.PASS
            else ChallengerStatus.OOS_REJECTED
        )
        if status is not expected:
            raise ResearchLifecycleError(
                f"unexpected post-OOS V2 status {status.value}"
            )
        return OosLifecycleResultV2(
            result=result,
            created=created,
            status=status,
        )

    def record_deterministic_replay(
        self,
        artifact: DeterministicReplayArtifactV1,
    ) -> LifecycleResult:
        try:
            created = self._repository.record_replay_artifact(artifact)
            status = self._repository.challenger_status(artifact.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.PROPOSED
            if artifact.deterministic_match
            else ChallengerStatus.REPLAY_FAILED
        )
        if status is not expected:
            raise ResearchLifecycleError(f"unexpected replay status {status.value}")
        return LifecycleResult(
            challenger_id=artifact.challenger_id,
            created=created,
            status=status,
            artifact_hash=artifact.artifact_hash,
        )

    def record_shadow_performance_summary(
        self,
        summary: TrustedShadowPerformanceSummaryV1,
    ) -> LifecycleResult:
        try:
            created = self._repository.record_shadow_performance_summary(summary)
            status = self._repository.challenger_status(summary.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.SHADOW_RUNNING:
            raise ResearchLifecycleError("shadow summary cannot alter Challenger status")
        return LifecycleResult(
            challenger_id=summary.challenger_id,
            created=created,
            status=status,
            artifact_hash=summary.summary_hash,
        )

    def record_shadow_performance_summary_v2(
        self,
        summary: TrustedShadowPerformanceSummaryV2,
    ) -> LifecycleResult:
        try:
            created = self._repository.record_shadow_performance_summary_v2(
                summary
            )
            status = self._repository.challenger_status(
                summary.challenger_id
            )
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.SHADOW_RUNNING:
            raise ResearchLifecycleError(
                "shadow V2 summary cannot alter Challenger status"
            )
        return LifecycleResult(
            challenger_id=summary.challenger_id,
            created=created,
            status=status,
            artifact_hash=summary.summary_hash,
        )

    def evaluate_trusted_promotion(
        self,
        *,
        challenger_id: str,
        contract: PromotionEvaluationContractV1,
        created_at: datetime,
    ) -> TrustedPromotionLifecycleResult:
        try:
            evidence = self._repository.build_trusted_promotion_evidence(
                challenger_id=challenger_id,
                created_at=created_at,
            )
            persisted = self._repository.trusted_promotion_evaluation(
                evidence_id=evidence.evidence_id,
                contract_hash=canonical_hash(contract),
            )
            if persisted is not None:
                stored_evidence, stored_evaluation = persisted
                return TrustedPromotionLifecycleResult(
                    evidence=stored_evidence,
                    evaluation=stored_evaluation,
                    created=False,
                    status=self._repository.challenger_status(challenger_id),
                )
            evaluation = evaluate_trusted_promotion_evidence(
                evidence=evidence,
                contract=contract,
                created_at=created_at,
            )
            created = self._repository.record_trusted_promotion_evaluation(
                evidence=evidence,
                contract=contract,
                evaluation=evaluation,
            )
            status = self._repository.challenger_status(challenger_id)
        except (ResearchPersistenceError, ValueError) as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.PROMOTION_ELIGIBLE
            if evaluation.decision.verdict is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
            else ChallengerStatus.SHADOW_RUNNING
        )
        if status is not expected:
            raise ResearchLifecycleError(f"unexpected trusted promotion status {status.value}")
        return TrustedPromotionLifecycleResult(
            evidence=evidence,
            evaluation=evaluation,
            created=created,
            status=status,
        )

    def evaluate_trusted_promotion_v2(
        self,
        *,
        challenger_id: str,
        contract: PromotionEvaluationContractV2,
        created_at: datetime,
    ) -> TrustedPromotionLifecycleResultV2:
        try:
            evidence = (
                self._repository.build_trusted_promotion_evidence_v2(
                    challenger_id=challenger_id,
                    created_at=created_at,
                )
            )
            persisted = self._repository.trusted_promotion_evaluation_v2(
                evidence_id=evidence.evidence_id,
                contract_hash=canonical_hash(contract),
            )
            if persisted is not None:
                stored_evidence, stored_evaluation = persisted
                return TrustedPromotionLifecycleResultV2(
                    evidence=stored_evidence,
                    evaluation=stored_evaluation,
                    created=False,
                    status=self._repository.challenger_status(
                        challenger_id
                    ),
                )
            evaluation = evaluate_trusted_promotion_evidence_v2(
                evidence=evidence,
                contract=contract,
                created_at=created_at,
            )
            created = (
                self._repository.record_trusted_promotion_evaluation_v2(
                    evidence=evidence,
                    contract=contract,
                    evaluation=evaluation,
                )
            )
            status = self._repository.challenger_status(challenger_id)
        except (ResearchPersistenceError, ValueError) as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.PROMOTION_ELIGIBLE
            if evaluation.decision.verdict
            is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
            else ChallengerStatus.SHADOW_RUNNING
        )
        if status is not expected:
            raise ResearchLifecycleError(
                f"unexpected trusted promotion V2 status {status.value}"
            )
        return TrustedPromotionLifecycleResultV2(
            evidence=evidence,
            evaluation=evaluation,
            created=created,
            status=status,
        )

    def approve_trusted_promotion(
        self,
        *,
        challenger_id: str,
        approved_by: str,
        created_at: datetime,
    ) -> ManualPromotionApprovalResult:
        try:
            decision, created = self._repository.record_trusted_manual_promotion_approval(
                challenger_id=challenger_id,
                approved_by=approved_by,
                created_at=created_at,
            )
            status = self._repository.challenger_status(challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.PROMOTION_ELIGIBLE:
            raise ResearchLifecycleError("manual approval must not designate the Champion")
        return ManualPromotionApprovalResult(
            decision=decision,
            created=created,
            status=status,
        )

    def designate_champion(
        self,
        *,
        challenger_id: str,
        expected_current_version: str,
        designated_by: str,
        idempotency_key: str,
        designated_at: datetime,
    ) -> ChampionDesignationResult:
        try:
            designation, created = self._repository.designate_champion(
                challenger_id=challenger_id,
                expected_current_version=expected_current_version,
                designated_by=designated_by,
                idempotency_key=idempotency_key,
                designated_at=designated_at,
            )
            status = self._repository.challenger_status(challenger_id)
        except (ResearchPersistenceError, ValueError) as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.PROMOTED:
            raise ResearchLifecycleError(
                "Champion designation did not atomically promote Challenger"
            )
        return ChampionDesignationResult(
            designation=designation,
            created=created,
            status=status,
        )

    def record_promotion_eligibility(
        self,
        decision: PromotionDecisionV1,
    ) -> LifecycleResult:
        """Legacy/test-only helper; production uses trusted evidence."""
        if decision.verdict is PromotionVerdict.MANUALLY_APPROVED:
            raise ResearchLifecycleError(
                "MANUALLY_APPROVED requires the explicit manual approval path"
            )
        try:
            created = self._repository.record_promotion_eligibility(decision)
            status = self._repository.challenger_status(decision.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        expected = (
            ChallengerStatus.PROMOTION_ELIGIBLE
            if decision.verdict is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
            else ChallengerStatus.SHADOW_RUNNING
        )
        if status is not expected:
            raise ResearchLifecycleError(f"unexpected promotion eligibility status {status.value}")
        return LifecycleResult(
            challenger_id=decision.challenger_id,
            created=created,
            status=status,
            artifact_hash=decision.decision_hash,
        )

    def record_manual_promotion_approval(
        self,
        decision: PromotionDecisionV1,
    ) -> LifecycleResult:
        if decision.verdict is not PromotionVerdict.MANUALLY_APPROVED:
            raise ResearchLifecycleError("manual approval path requires MANUALLY_APPROVED")
        try:
            created = self._repository.record_manual_promotion_approval(decision)
            status = self._repository.challenger_status(decision.challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.PROMOTION_ELIGIBLE:
            raise ResearchLifecycleError("manual approval cannot mutate Champion or mark PROMOTED")
        return LifecycleResult(
            challenger_id=decision.challenger_id,
            created=created,
            status=status,
            artifact_hash=decision.decision_hash,
        )

    def start_shadow(
        self,
        *,
        challenger_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> LifecycleResult:
        try:
            created = self._repository.start_shadow_evaluation(
                challenger_id=challenger_id,
                idempotency_key=idempotency_key,
                created_at=created_at,
            )
            status = self._repository.challenger_status(challenger_id)
        except ResearchPersistenceError as exc:
            raise ResearchLifecycleError(str(exc)) from exc
        if status is not ChallengerStatus.SHADOW_RUNNING:
            raise ResearchLifecycleError(f"unexpected shadow status {status.value}")
        return LifecycleResult(
            challenger_id=challenger_id,
            created=created,
            status=status,
            artifact_hash=canonical_hash(idempotency_key),
        )
