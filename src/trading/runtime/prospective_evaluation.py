from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from trading.domain.hashing import stable_id
from trading.persistence.prospective_evaluation import (
    ProspectiveEvaluationRepository,
)
from trading.persistence.prospective_outcomes import (
    ProspectiveOutcomeRepository,
)
from trading.persistence.research import ResearchRepository
from trading.research.candidate_evaluation import (
    CandidateEvaluationDatasetV2,
    CandidateEvaluationError,
    evaluate_candidate_twice,
)
from trading.research.commander_candidate import connect_candidate_runtime
from trading.research.config import ResearchConfigBundle
from trading.research.contracts import (
    ChallengerStatus,
    FalsificationReportV1,
)
from trading.research.evaluation_contracts import (
    CandidateEvaluationTraceV1,
)
from trading.research.falsification import ExperimentBudget
from trading.research.falsification_runner import (
    AutomatedFalsificationRunner,
    FalsificationRunContext,
)
from trading.research.lifecycle import ResearchLifecycleService
from trading.research.prospective_evaluation import (
    ProspectiveEvaluationBuildResult,
    ProspectiveEvaluationConfigBundle,
    build_prospective_evaluation_dataset,
)
from trading.research.prospective_outcomes import (
    ProspectiveOutcomeConfigBundle,
)
from trading.research.replay import DeterministicReplayArtifactV1


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluationRunResult:
    status: str
    challenger_status: ChallengerStatus
    successful_forward_sessions: int
    required_forward_sessions: int
    terminal_failure_count: int
    dataset: CandidateEvaluationDatasetV2 | None = None
    trace: CandidateEvaluationTraceV1 | None = None
    replay: DeterministicReplayArtifactV1 | None = None
    falsification_report: FalsificationReportV1 | None = None
    dataset_created: bool = False
    trace_created: bool = False
    replay_created: bool = False
    falsification_created: bool = False
    build_result: ProspectiveEvaluationBuildResult | None = None


class ProspectiveEvaluationService:
    """Turn the frozen forward cohort into one replayed falsification trace."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        evaluation_config: ProspectiveEvaluationConfigBundle,
        outcome_config: ProspectiveOutcomeConfigBundle,
        research_config: ResearchConfigBundle,
    ) -> None:
        self._evaluation = ProspectiveEvaluationRepository(
            session_factory
        )
        self._outcomes = ProspectiveOutcomeRepository(session_factory)
        self._research = ResearchRepository(session_factory)
        self._lifecycle = ResearchLifecycleService(
            repository=self._research
        )
        if (
            outcome_config.manifest_hash
            != evaluation_config.outcome_manifest_hash
        ):
            raise ValueError(
                "prospective evaluation outcome config binding mismatch"
            )
        self._evaluation_config = evaluation_config
        self._research_config = research_config

    def run(
        self,
        *,
        challenger_id: str,
        commander_root: Path,
        commander_run: Path,
    ) -> ProspectiveEvaluationRunResult:
        required = (
            self._evaluation_config.config.source_selection
            .required_common_sessions
        )
        outcome_status = self._outcomes.status(
            challenger_id=challenger_id,
            minimum_common_sessions=required,
            minimum_observations=(
                self._research_config.config.falsification
                .evaluation_contract.minimum_observation_count
            ),
        )
        successful = int(outcome_status["outcome_count"])
        failures = int(outcome_status["terminal_failure_count"])
        challenger_status = self._research.challenger_status(
            challenger_id
        )
        existing_report = self._research.falsification_report(
            challenger_id
        )
        if existing_report is not None:
            return self._result(
                status="FALSIFICATION_ALREADY_RECORDED",
                challenger_status=challenger_status,
                successful=successful,
                required=required,
                failures=failures,
                dataset=self._evaluation.dataset(
                    challenger_id=challenger_id
                ),
                trace=self._evaluation.trace(
                    challenger_id=challenger_id
                ),
                replay=self._research.replay_artifact(challenger_id),
                report=existing_report,
            )
        if challenger_status is not ChallengerStatus.PROPOSED:
            return self._result(
                status="CHALLENGER_NOT_ELIGIBLE_FOR_EVALUATION",
                challenger_status=challenger_status,
                successful=successful,
                required=required,
                failures=failures,
            )
        if successful < required:
            return self._result(
                status="WAITING_FOR_FORWARD_OUTCOMES",
                challenger_status=challenger_status,
                successful=successful,
                required=required,
                failures=failures,
            )

        context = self._research.candidate_experiment_context(
            challenger_id
        )
        dataset = self._evaluation.dataset(
            challenger_id=challenger_id
        )
        connection = None
        build_result = None
        dataset_created = False
        if dataset is None:
            source = self._evaluation.source_records(
                challenger_id=challenger_id,
                maximum_records=required,
            )
            connection = connect_candidate_runtime(
                bundle=context.artifact,
                commander_root=commander_root,
                run_root=commander_run,
                research_config=self._research_config,
            )
            build_result = build_prospective_evaluation_dataset(
                config_bundle=self._evaluation_config,
                records=source.records,
                terminal_failures=source.terminal_failures,
                state_executor=connection.primary_executor,
            )
            dataset = build_result.dataset
            dataset_created = self._evaluation.store_dataset(
                dataset=dataset,
                config_manifest_hash=(
                    self._evaluation_config.manifest_hash
                ),
                created_at=build_result.logical_created_at,
            )

        logical_created_at = (
            dataset.source_manifest.cohort_manifest
            .selection_data_cutoff
        )
        trace = self._evaluation.trace(challenger_id=challenger_id)
        replay = self._research.replay_artifact(challenger_id)
        replay_created = False
        trace_created = False
        if trace is None:
            if connection is None:
                connection = connect_candidate_runtime(
                    bundle=context.artifact,
                    commander_root=commander_root,
                    run_root=commander_run,
                    research_config=self._research_config,
                )
            try:
                evaluated = evaluate_candidate_twice(
                    dataset=dataset,
                    executor=connection.primary_executor,
                    replay_executor=connection.replay_executor,
                    evaluation_contract=(
                        self._research_config.config.falsification
                        .evaluation_contract
                    ),
                    trace_id=stable_id(
                        "candidate-prospective-evaluation-trace-v2",
                        challenger_id,
                        dataset.dataset_hash,
                        self._research_config.config.falsification
                        .evaluation_contract.contract_version,
                    ),
                    config_hash=context.manifest.config_hash,
                    code_hash=context.manifest.code_hash,
                    created_at=logical_created_at,
                )
            except CandidateEvaluationError as exc:
                if exc.replay is None:
                    raise
                lifecycle = self._lifecycle.record_deterministic_replay(
                    exc.replay
                )
                return self._result(
                    status="DETERMINISTIC_REPLAY_FAILED",
                    challenger_status=lifecycle.status,
                    successful=successful,
                    required=required,
                    failures=failures,
                    dataset=dataset,
                    replay=exc.replay,
                    dataset_created=dataset_created,
                    replay_created=lifecycle.created,
                    build_result=build_result,
                )
            replay_result = self._lifecycle.record_deterministic_replay(
                evaluated.replay
            )
            replay = evaluated.replay
            replay_created = replay_result.created
            trace = evaluated.trace
            trace_created = self._evaluation.store_trace(
                dataset=dataset,
                trace=trace,
                replay_artifact_hash=replay.artifact_hash,
            )
        if replay is None or not replay.deterministic_match:
            raise RuntimeError(
                "prospective evaluation deterministic replay is unavailable"
            )

        report = self._research.falsification_report(challenger_id)
        falsification_created = False
        if report is None:
            totals = self._research.budget_totals(
                context.manifest.experiment_family
            )
            budget_config = (
                self._research_config.config.experiment_budget
            )
            report = AutomatedFalsificationRunner.from_host_trace(
                trace
            ).run(
                context=FalsificationRunContext(
                    challenger_id=challenger_id,
                    candidate_artifact_hash=(
                        context.artifact.bundle_hash
                    ),
                    evaluation_contract_hash=(
                        trace.evaluation_contract_hash
                    ),
                    data_manifest_hash=trace.data_manifest_hash,
                    replay_hash=replay.artifact_hash,
                    deterministic_seed=(
                        self._evaluation_config.config
                        .deterministic_seed
                    ),
                ),
                budget=ExperimentBudget(
                    experiment_family=(
                        context.manifest.experiment_family
                    ),
                    submission_count=totals["submissions"],
                    maximum_submissions=(
                        budget_config.maximum_submissions_per_family
                    ),
                    oos_budget_used=totals["oos_budget_used"],
                    maximum_oos_budget=(
                        budget_config.maximum_oos_uses_per_family
                    ),
                ),
                created_at=logical_created_at,
            )
            lifecycle = self._lifecycle.record_falsification(report)
            challenger_status = lifecycle.status
            falsification_created = lifecycle.created
        else:
            challenger_status = self._research.challenger_status(
                challenger_id
            )
        return self._result(
            status="FALSIFICATION_RECORDED",
            challenger_status=challenger_status,
            successful=successful,
            required=required,
            failures=failures,
            dataset=dataset,
            trace=trace,
            replay=replay,
            report=report,
            dataset_created=dataset_created,
            trace_created=trace_created,
            replay_created=replay_created,
            falsification_created=falsification_created,
            build_result=build_result,
        )

    @staticmethod
    def _result(
        *,
        status: str,
        challenger_status: ChallengerStatus,
        successful: int,
        required: int,
        failures: int,
        dataset: CandidateEvaluationDatasetV2 | None = None,
        trace: CandidateEvaluationTraceV1 | None = None,
        replay: DeterministicReplayArtifactV1 | None = None,
        report: FalsificationReportV1 | None = None,
        dataset_created: bool = False,
        trace_created: bool = False,
        replay_created: bool = False,
        falsification_created: bool = False,
        build_result: ProspectiveEvaluationBuildResult | None = None,
    ) -> ProspectiveEvaluationRunResult:
        return ProspectiveEvaluationRunResult(
            status=status,
            challenger_status=challenger_status,
            successful_forward_sessions=successful,
            required_forward_sessions=required,
            terminal_failure_count=failures,
            dataset=dataset,
            trace=trace,
            replay=replay,
            falsification_report=report,
            dataset_created=dataset_created,
            trace_created=trace_created,
            replay_created=replay_created,
            falsification_created=falsification_created,
            build_result=build_result,
        )


def prospective_evaluation_status(
    repository: ProspectiveEvaluationRepository,
    *,
    config: ProspectiveEvaluationConfigBundle,
    challenger_id: str | None,
    research_repository: ResearchRepository | None = None,
) -> dict[str, object]:
    if challenger_id is None:
        persisted: dict[str, object] = {
            "schema_version": (
                "candidate_prospective_evaluation_status_v1"
            ),
            "challenger_id": None,
            "status": "WAITING_FOR_CHALLENGER",
            "dataset": None,
            "trace": None,
            "oos_started": False,
            "shadow_started": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    else:
        persisted = repository.status(challenger_id=challenger_id)
    report = (
        None
        if challenger_id is None or research_repository is None
        else research_repository.falsification_report(challenger_id)
    )
    return {
        **persisted,
        "status": (
            persisted["status"]
            if report is None
            else (
                "MANDATORY_FALSIFICATION_PASSED"
                if report.mandatory_passed
                else "MANDATORY_FALSIFICATION_FAILED"
            )
        ),
        "producer_version": config.config.producer_version,
        "config_manifest_hash": config.manifest_hash,
        "selection_policy": (
            config.config.source_selection.policy
        ),
        "required_successful_sessions": (
            config.config.source_selection.required_common_sessions
        ),
        "minimum_request_coverage_ratio": (
            config.config.source_selection
            .minimum_request_coverage_ratio
        ),
        "minimum_variant_session_coverage_ratio": (
            config.config.source_selection
            .minimum_variant_session_coverage_ratio
        ),
        "variant_count": (
            len(config.config.parameter_neighborhoods)
            + len(config.config.data_ablations)
            + len(config.config.date_shifts)
            + len(config.config.placebos)
            + len(config.config.symbol_shuffles)
        ),
        "falsification": (
            None
            if report is None
            else {
                "report_hash": report.report_hash,
                "mandatory_passed": report.mandatory_passed,
                "passed_count": sum(
                    item.status.value == "PASS"
                    for item in report.results
                ),
                "failed_count": sum(
                    item.status.value == "FAIL"
                    for item in report.results
                ),
                "blocked_count": sum(
                    item.status.value == "BLOCKED"
                    for item in report.results
                ),
            }
        ),
        "falsification_started": (
            persisted.get("trace") is not None or report is not None
        ),
        "oos_started": False,
        "shadow_started": False,
        "automatic_promotion_enabled": False,
        "broker_access_permitted": False,
        "real_order_routing": False,
    }
