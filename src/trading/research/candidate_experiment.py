from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any, cast

from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.persistence.experiment_outcomes import (
    ExperimentOutcomePersistenceError,
    ExperimentOutcomeRepository,
)
from trading.persistence.research import (
    CandidateExperimentContext,
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.persistence.research_scheduler import (
    ResearchSchedulerRepository,
)
from trading.research.candidate_artifact import CandidateArtifactBundleV1
from trading.research.config import ResearchConfigBundle
from trading.research.experiment_outcomes import (
    ExperimentInformationRole,
    ExperimentMaturityStatus,
    ExperimentOutcomeEventKind,
    ExperimentOutcomeEventV1,
    ExperimentOutcomeMaturationInputV1,
    ExperimentStage,
    ResearchExperimentActionV1,
    build_experiment_action,
)
from trading.research.scheduler import VersionedResearchMarketSession


class CandidateExperimentRegistrationError(RuntimeError):
    """Raised when a Candidate cannot enter the immutable outcome ledger."""


@dataclass(frozen=True, slots=True)
class VerifiedCandidateTestAttestation:
    manifest_hash: str
    passed_test_count: int
    execution_contract_version: str


@dataclass(frozen=True, slots=True)
class CandidateDiscoveryRegistration:
    challenger_id: str
    experiment_id: str
    action: ResearchExperimentActionV1
    action_created: bool
    registration_event: ExperimentOutcomeEventV1
    registration_event_created: bool
    technical_event: ExperimentOutcomeEventV1
    technical_event_created: bool
    maturity_calendar_session_ids: tuple[str, ...]
    test_manifest_hash: str

    def status_payload(self) -> dict[str, object]:
        return {
            "schema_version": "candidate_discovery_registration_v1",
            "challenger_id": self.challenger_id,
            "experiment_id": self.experiment_id,
            "information_role": ExperimentInformationRole.DISCOVERY.value,
            "action_hash": self.action.action_hash,
            "action_created": self.action_created,
            "registration_event_hash": self.registration_event.event_hash,
            "registration_event_created": self.registration_event_created,
            "technical_event_hash": self.technical_event.event_hash,
            "technical_event_created": self.technical_event_created,
            "technical_success": self.technical_event.technical_success,
            "maturity_due_at": self.action.maturity_due_at.isoformat(),
            "maturity_session_count": len(
                self.maturity_calendar_session_ids
            ),
            "maturity_calendar_session_ids": list(
                self.maturity_calendar_session_ids
            ),
            "candidate_artifact_hash": (
                self.action.candidate_artifact_hash
            ),
            "test_manifest_hash": self.test_manifest_hash,
            "meta_training_permitted": self.action.meta_training_permitted,
            "eligible_for_meta_training": (
                self.technical_event.eligible_for_meta_training
            ),
            "challenger_status_advanced": False,
            "shadow_started": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }


def verify_candidate_test_manifest(
    payload: Mapping[str, Any],
    *,
    artifact: CandidateArtifactBundleV1,
) -> VerifiedCandidateTestAttestation:
    """Verify the sealed trusted-host test result without retaining raw output."""

    manifest = dict(payload)
    manifest_hash = canonical_hash(manifest)
    if manifest_hash != artifact.test_manifest_hash:
        raise CandidateExperimentRegistrationError(
            "candidate test manifest hash does not match the sealed artifact"
        )
    required_equal: tuple[tuple[str, object], ...] = (
        ("schema_version", "candidate_test_manifest_v1"),
        ("status", "PASSED"),
        ("exit_code", 0),
        ("source_snapshot_hash", artifact.source_snapshot_hash),
        ("candidate_tree_hash_before", artifact.candidate_tree_hash),
        ("candidate_tree_hash_after", artifact.candidate_tree_hash),
        ("patch_hash", artifact.patch_hash),
        ("proposal_hash", artifact.proposal_hash),
        ("builder_result_hash", artifact.builder_result_hash),
        ("declared_entrypoint", artifact.declared_entrypoint),
        ("output_limit_exceeded", False),
        ("candidate_tree_unchanged", True),
        ("candidate_source_projection_unchanged", True),
        ("candidate_test_projection_unchanged", True),
        ("host_abi_test_unchanged", True),
        ("host_principal_persisted", False),
        ("raw_output_persisted", False),
        ("broker_access_permitted", False),
        ("credential_access_permitted", False),
        ("network_access_permitted", False),
        ("real_order_routing", False),
    )
    mismatches = [
        key
        for key, expected in required_equal
        if manifest.get(key) != expected
    ]
    if mismatches:
        raise CandidateExperimentRegistrationError(
            "candidate test attestation mismatch: " + ",".join(mismatches)
        )
    runtime = _require_mapping(manifest, "runtime")
    expected_runtime = artifact.runtime.model_dump(mode="python")
    if dict(runtime) != expected_runtime:
        raise CandidateExperimentRegistrationError(
            "candidate test runtime differs from the sealed artifact"
        )
    test_count = _require_mapping(manifest, "test_count")
    passed = _require_nonnegative_int(test_count, "passed")
    failed = _require_nonnegative_int(test_count, "failed")
    errors = _require_nonnegative_int(test_count, "errors")
    if passed < 1 or failed != 0 or errors != 0:
        raise CandidateExperimentRegistrationError(
            "candidate test result is not a clean passing attestation"
        )
    contract_version = manifest.get("execution_contract_version")
    if not isinstance(contract_version, str) or not contract_version:
        raise CandidateExperimentRegistrationError(
            "candidate test execution contract is missing"
        )
    return VerifiedCandidateTestAttestation(
        manifest_hash=manifest_hash,
        passed_test_count=passed,
        execution_contract_version=contract_version,
    )


def select_forward_maturity_sessions(
    sessions: Sequence[VersionedResearchMarketSession],
    *,
    decision_at: datetime,
    horizon_sessions: int,
) -> tuple[VersionedResearchMarketSession, ...]:
    """Select full future sessions from the latest PIT calendar revision."""

    decision = require_aware_utc(decision_at)
    if horizon_sessions <= 0:
        raise CandidateExperimentRegistrationError(
            "experiment maturity horizon must be positive"
        )
    latest_by_date: dict[object, VersionedResearchMarketSession] = {}
    for session in sessions:
        if session.available_at > decision:
            continue
        current = latest_by_date.get(session.session_date)
        if current is None or (
            session.available_at,
            session.calendar_session_id,
        ) > (
            current.available_at,
            current.calendar_session_id,
        ):
            latest_by_date[session.session_date] = session
    eligible = tuple(
        session
        for session in sorted(
            latest_by_date.values(),
            key=lambda item: (
                item.session_date,
                item.open_at,
                item.calendar_session_id,
            ),
        )
        if session.open_at >= decision
    )
    if len(eligible) < horizon_sessions:
        raise CandidateExperimentRegistrationError(
            "versioned market calendar does not contain enough full future "
            f"sessions: required={horizon_sessions}, available={len(eligible)}"
        )
    selected = eligible[:horizon_sessions]
    if any(
        previous.close_at >= current.close_at
        for previous, current in pairwise(selected)
    ):
        raise CandidateExperimentRegistrationError(
            "versioned market calendar has non-increasing session closes"
        )
    return selected


class CandidateExperimentRegistrationService:
    """Map one sealed V2 Candidate into DISCOVERY-only audit evidence."""

    def __init__(
        self,
        *,
        research_repository: ResearchRepository,
        outcome_repository: ExperimentOutcomeRepository,
        scheduler_repository: ResearchSchedulerRepository,
        config: ResearchConfigBundle,
    ) -> None:
        self._research = research_repository
        self._outcomes = outcome_repository
        self._scheduler = scheduler_repository
        self._config = config

    def register_discovery(
        self,
        *,
        challenger_id: str,
        test_manifest: Mapping[str, Any],
    ) -> CandidateDiscoveryRegistration:
        context = self._research.candidate_experiment_context(
            challenger_id
        )
        attestation = verify_candidate_test_manifest(
            test_manifest,
            artifact=context.artifact,
        )
        sessions, _, _ = self._scheduler.planning_inputs(
            as_of=self._outcomes.database_now(),
            calendar_version=(
                self._config.config.schedule.market_calendar_version
            ),
        )
        horizon_sessions = (
            self._config.config.recursive_improvement.outcome_ledger
            .learning_forward_horizon_sessions
        )
        experiment_id = stable_id(
            "candidate-discovery-experiment",
            challenger_id,
            context.artifact.bundle_hash,
        )
        action_idempotency_key = stable_id(
            "candidate-discovery-action",
            challenger_id,
            context.artifact.bundle_hash,
        )

        def build_action(created_at: datetime) -> ResearchExperimentActionV1:
            maturity_sessions = select_forward_maturity_sessions(
                sessions,
                decision_at=created_at,
                horizon_sessions=horizon_sessions,
            )
            source_hashes, source_times = _action_source_provenance(
                context,
                maturity_sessions=maturity_sessions,
                decision_at=created_at,
            )
            return build_experiment_action(
                proposal=context.proposal,
                experiment_id=experiment_id,
                research_cycle_id=context.request.research_cycle_id,
                challenger_id=challenger_id,
                information_role=ExperimentInformationRole.DISCOVERY,
                decision_at=created_at,
                maturity_due_at=maturity_sessions[-1].close_at,
                candidate_artifact_hash=context.artifact.bundle_hash,
                evaluation_contract_hash=(
                    context.artifact.test_manifest_hash
                ),
                source_artifact_hashes=source_hashes,
                source_data_available_at=source_times,
                idempotency_key=action_idempotency_key,
                created_at=created_at,
            )

        try:
            action, action_created = (
                self._outcomes.register_action_at_database_clock(
                    experiment_id=experiment_id,
                    build=build_action,
                )
            )
            maturity_sessions = select_forward_maturity_sessions(
                sessions,
                decision_at=action.decision_at,
                horizon_sessions=horizon_sessions,
            )
            registration_key = stable_id(
                "candidate-discovery-registered",
                experiment_id,
            )

            def build_registration(
                created_at: datetime,
            ) -> ExperimentOutcomeMaturationInputV1:
                hashes, times = _canonical_provenance(
                    (
                        (action.action_hash, action.created_at),
                        (
                            context.artifact.bundle_hash,
                            context.artifact_created_at,
                        ),
                    )
                )
                return ExperimentOutcomeMaturationInputV1(
                    experiment_id=experiment_id,
                    event_kind=(
                        ExperimentOutcomeEventKind.EXPERIMENT_REGISTERED
                    ),
                    experiment_stage=ExperimentStage.TEST,
                    available_at=created_at,
                    maturity_status=ExperimentMaturityStatus.PENDING,
                    evaluation_contract_hash=(
                        context.artifact.test_manifest_hash
                    ),
                    source_artifact_hashes=hashes,
                    source_data_available_at=times,
                    idempotency_key=registration_key,
                    created_at=created_at,
                )

            registration_event, registration_created = (
                self._outcomes.append_outcome_at_database_clock(
                    experiment_id=experiment_id,
                    idempotency_key=registration_key,
                    build=build_registration,
                )
            )
            technical_key = stable_id(
                "candidate-tests-passed",
                experiment_id,
                attestation.manifest_hash,
            )

            def build_technical(
                created_at: datetime,
            ) -> ExperimentOutcomeMaturationInputV1:
                hashes, times = _canonical_provenance(
                    (
                        (action.action_hash, action.created_at),
                        (
                            context.artifact.bundle_hash,
                            context.artifact_created_at,
                        ),
                        (
                            attestation.manifest_hash,
                            context.artifact_created_at,
                        ),
                    )
                )
                return ExperimentOutcomeMaturationInputV1(
                    experiment_id=experiment_id,
                    event_kind=(
                        ExperimentOutcomeEventKind
                        .TECHNICAL_OUTCOME_RECORDED
                    ),
                    experiment_stage=ExperimentStage.TEST,
                    available_at=created_at,
                    maturity_status=ExperimentMaturityStatus.MATURED,
                    technical_success=True,
                    technical_failure_codes=(),
                    evaluation_contract_hash=(
                        context.artifact.test_manifest_hash
                    ),
                    source_artifact_hashes=hashes,
                    source_data_available_at=times,
                    idempotency_key=technical_key,
                    created_at=created_at,
                )

            technical_event, technical_created = (
                self._outcomes.append_outcome_at_database_clock(
                    experiment_id=experiment_id,
                    idempotency_key=technical_key,
                    build=build_technical,
                )
            )
        except (
            ExperimentOutcomePersistenceError,
            ResearchPersistenceError,
            ValueError,
        ) as exc:
            raise CandidateExperimentRegistrationError(str(exc)) from exc
        return CandidateDiscoveryRegistration(
            challenger_id=challenger_id,
            experiment_id=experiment_id,
            action=action,
            action_created=action_created,
            registration_event=registration_event,
            registration_event_created=registration_created,
            technical_event=technical_event,
            technical_event_created=technical_created,
            maturity_calendar_session_ids=tuple(
                session.calendar_session_id for session in maturity_sessions
            ),
            test_manifest_hash=attestation.manifest_hash,
        )


def _action_source_provenance(
    context: CandidateExperimentContext,
    *,
    maturity_sessions: Sequence[VersionedResearchMarketSession],
    decision_at: datetime,
) -> tuple[tuple[str, ...], tuple[datetime, ...]]:
    request = context.request
    pairs: list[tuple[str, datetime]] = [
        (request.context_manifest_hash, context.cycle_created_at),
        (
            request.research_memory_snapshot.snapshot_hash,
            request.research_memory_snapshot.created_at,
        ),
        (
            request.research_action_plan.plan_hash,
            request.research_action_plan.generated_at,
        ),
        (context.proposal.proposal_hash, context.proposal_created_at),
        (context.manifest.manifest_hash, context.manifest_created_at),
        (
            context.artifact.bundle_hash,
            context.artifact_created_at,
        ),
        (
            context.artifact.test_manifest_hash,
            context.artifact_created_at,
        ),
    ]
    pairs.extend(
        (session.session_hash, session.available_at)
        for session in maturity_sessions
    )
    if any(
        available_at > decision_at
        for _, available_at in pairs
    ):
        raise CandidateExperimentRegistrationError(
            "candidate experiment source was unavailable at decision time"
        )
    return _canonical_provenance(pairs)


def _canonical_provenance(
    pairs: Sequence[tuple[str, datetime]],
) -> tuple[tuple[str, ...], tuple[datetime, ...]]:
    earliest_by_hash: dict[str, datetime] = {}
    for artifact_hash, available_at in pairs:
        available = require_aware_utc(available_at)
        current = earliest_by_hash.get(artifact_hash)
        if current is None or available < current:
            earliest_by_hash[artifact_hash] = available
    ordered = tuple(
        sorted(
            earliest_by_hash.items(),
            key=lambda item: (item[1], item[0]),
        )
    )
    return (
        tuple(artifact_hash for artifact_hash, _ in ordered),
        tuple(available_at for _, available_at in ordered),
    )


def _require_mapping(
    payload: Mapping[str, Any],
    key: str,
) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise CandidateExperimentRegistrationError(
            f"candidate test manifest requires object field {key}"
        )
    return cast(Mapping[str, Any], value)


def _require_nonnegative_int(
    payload: Mapping[str, Any],
    key: str,
) -> int:
    value = payload.get(key)
    if type(value) is not int or value < 0:
        raise CandidateExperimentRegistrationError(
            f"candidate test manifest requires nonnegative integer {key}"
        )
    return value
