from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from zoneinfo import ZoneInfo

import yaml
from pydantic import Field, field_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.research.evaluation_contracts import (
    FalsificationEvaluationContractV1,
)
from trading.research.falsification import MANDATORY_FALSIFICATION_TESTS
from trading.research.promotion_evidence import PromotionEvaluationContractV1
from trading.research.sandbox_contract import (
    CANDIDATE_PATCH_POLICY_V2,
    CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
    DEFAULT_ALLOWED_PREFIXES,
    DEFAULT_FORBIDDEN_EXACT,
    DEFAULT_FORBIDDEN_PREFIXES,
)

if TYPE_CHECKING:
    from trading.research.candidate_process import (
        CandidateExecutionSecurityV1,
        CandidateProcessLimitsV1,
    )
    from trading.research.chronological_meta_oos import (
        MetaOosEvaluationContractV1,
    )
    from trading.research.meta_controller import MetaControllerParametersV1
    from trading.research.oos_lockbox import (
        OosProcessEvaluationConfig,
        OosProcessEvaluationConfigV2,
    )
    from trading.research.portfolio_delta_sharpe import (
        PortfolioComparisonContractV1,
        StationaryBootstrapContractV1,
    )
    from trading.research.promotion_v2 import PromotionEvaluationContractV2
    from trading.research.shadow_runtime import ShadowPaperParametersV1

RESEARCH_CONFIG_FILE = "research/research-plane.yaml"
FACTORIAL_CONFIG_FILE = "research/ai-guard-factorial.yaml"


class ResearchSafetyConfig(DomainModel):
    real_order_routing: bool
    automatic_promotion_enabled: bool
    champion_in_place_mutation: bool
    raw_oos_access: bool
    credential_access: bool


class ResearchScheduleConfig(DomainModel):
    schedule_version: str = Field(min_length=1)
    timezone: str
    market_calendar_version: str = Field(min_length=1)
    daily_aggregation_time: str
    daily_post_close_delay_minutes: int = Field(ge=0)
    weekly_research_day: str
    weekly_research_time: str
    evidence_trigger_minimum_new_sources: int = Field(gt=0)
    planning_lookback_days: int = Field(gt=0)
    dispatch_lease_seconds: int = Field(gt=0)
    maximum_dispatch_attempts: int = Field(gt=0)
    worker_poll_seconds: int = Field(gt=0)
    status_history_limit: int = Field(gt=0)

    @field_validator("timezone", mode="after")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator(
        "daily_aggregation_time",
        "weekly_research_time",
        mode="after",
    )
    @classmethod
    def validate_schedule_time(cls, value: str) -> str:
        parsed = time.fromisoformat(value)
        if parsed.second or parsed.microsecond:
            raise ValueError("research schedule times must have minute precision")
        return value

    @field_validator("weekly_research_day", mode="after")
    @classmethod
    def validate_weekday(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in {
            "MONDAY",
            "TUESDAY",
            "WEDNESDAY",
            "THURSDAY",
            "FRIDAY",
            "SATURDAY",
            "SUNDAY",
        }:
            raise ValueError("weekly_research_day must be an English weekday")
        return normalized


class ModelInvocationConfig(DomainModel):
    model: str | None = None
    family: str | None = None
    reasoning_profile: str
    access_path: str | None = None
    api_fallback_allowed: bool | None = None
    conversation_reuse_allowed: bool | None = None
    session_resume_allowed: bool | None = None


class ResearchModelsConfig(DomainModel):
    web_scout: ModelInvocationConfig
    codex_commander: ModelInvocationConfig
    candidate_builder: ModelInvocationConfig


class ResearchUniverseConfig(DomainModel):
    asset_classes: list[str] = Field(min_length=1)
    source: str
    hardcoded_symbol_allowlist: bool
    leveraged_and_inverse_products_require_explicit_proposal: bool


class ExperimentBudgetConfig(DomainModel):
    maximum_submissions_per_family: int = Field(gt=0)
    maximum_oos_uses_per_family: int = Field(gt=0)
    maximum_hypotheses_per_family: int = Field(gt=0)


class ResearchOosConfig(DomainModel):
    minimum_common_sessions: int = Field(gt=0)
    minimum_mean_daily_difference: float
    detail_return_policy: str
    annualization_sessions: int = Field(gt=0)
    newey_west_lag: int = Field(ge=0)
    bootstrap_seed: int = Field(ge=0)
    bootstrap_block_length: int = Field(gt=0)
    bootstrap_samples: int = Field(gt=0)
    base_cost_bps: int = Field(ge=0)
    request_ttl_seconds: int = Field(gt=0)
    worker_timeout_seconds: int = Field(gt=0)
    cost_sensitivity_bps: tuple[int, ...] = Field(min_length=1)
    trusted_producer_version: str = Field(min_length=1)


class ResearchShadowPaperParametersConfig(DomainModel):
    contract_version: str = Field(min_length=1)
    commission_rate: Decimal = Field(ge=0, le=1)
    commission_waiver_threshold_usd: Decimal = Field(ge=0)
    delay_penalty_bps: Decimal = Field(ge=0)
    displayed_participation_rate: Decimal = Field(gt=0, le=1)
    adv_participation_rate: Decimal = Field(gt=0, le=1)
    minimum_order_notional_usd: Decimal = Field(gt=0)
    quantity_quantum: Decimal = Field(gt=0)
    price_quantum: Decimal = Field(gt=0)
    sensitivity_5_bps: Decimal = Field(gt=0)
    sensitivity_10_bps: Decimal = Field(gt=0)
    basis_points_per_unit_return: Decimal = Field(gt=0)
    maximum_quote_age_seconds: int = Field(gt=0)
    weight_tolerance: Decimal = Field(gt=0)
    real_order_routing: Literal[False]


class ResearchShadowConfig(DomainModel):
    minimum_forward_sessions: int = Field(gt=0)
    minimum_independent_trades: int = Field(gt=0)
    starting_capital_usd: str
    matched_execution_required: bool
    paper_parameters: ResearchShadowPaperParametersConfig


class ResearchPromotionConfig(DomainModel):
    automatic_promotion_enabled: bool
    require_manual_approval: bool
    minimum_common_oos_sessions: int = Field(gt=0)
    minimum_forward_sessions: int = Field(gt=0)
    minimum_independent_trades: int = Field(gt=0)
    require_positive_net_excess_return: bool
    require_replay_reproducibility: bool
    require_all_mandatory_falsification: bool
    evaluation_contract: PromotionEvaluationContractV1


class CandidatePatchConfig(DomainModel):
    allowed_prefixes: list[str] = Field(min_length=1)
    forbidden_prefixes: list[str] = Field(min_length=1)
    forbidden_exact: list[str] = Field(min_length=1)


class CandidateExecutionConfig(DomainModel):
    isolation_kind: str = Field(min_length=1)
    isolation_version: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    maximum_stdout_bytes: int = Field(gt=0)
    maximum_stderr_bytes: int = Field(gt=0)
    maximum_memory_bytes: int = Field(gt=0)
    maximum_processes: int = Field(gt=0)


class ResearchFalsificationConfig(DomainModel):
    mandatory: list[str] = Field(min_length=1)
    evaluation_contract: FalsificationEvaluationContractV1


class RecursiveOutcomeLedgerConfig(DomainModel):
    learning_forward_horizon_sessions: int = Field(gt=0)
    reject_unmatured_outcomes: Literal[True]
    exclude_promotion_oos_from_training: Literal[True]
    exclude_meta_audit_from_training: Literal[True]


class RecursiveMetaControllerConfig(DomainModel):
    policy_version: str = Field(min_length=1)
    maximum_actions_per_cycle: int = Field(gt=0)
    prior_strength: float = Field(gt=0)
    exploration_coefficient: float = Field(ge=0)
    exploration_floor: float = Field(ge=0)
    technical_failure_weight: float = Field(ge=0)
    reward_clip: float = Field(gt=0)
    turnover_penalty_weight: float = Field(ge=0)
    turnover_scale: float = Field(gt=0)
    drawdown_penalty_weight: float = Field(ge=0)
    drawdown_scale: float = Field(gt=0)
    cost_penalty_weight: float = Field(ge=0)
    cost_scale_bps: float = Field(gt=0)
    complexity_penalty_weight: float = Field(ge=0)
    complexity_scale: float = Field(gt=0)


class RecursivePortfolioSharpeConfig(DomainModel):
    annualization_sessions: int = Field(gt=0)
    minimum_common_sessions: int = Field(gt=0)
    minimum_independent_trades: int = Field(gt=0)
    configured_bootstrap_seed: int = Field(ge=0)
    bootstrap_samples: int = Field(gt=0)
    bootstrap_block_length: int = Field(gt=0)
    lower_quantile: float = Field(gt=0, lt=0.5)
    variance_epsilon: float = Field(gt=0)
    maximum_absolute_daily_return: float = Field(gt=0)
    minimum_delta_sharpe_lcb: float
    minimum_worst_cost_delta_sharpe_lcb: float
    cost_stress_multipliers: tuple[float, ...] = Field(min_length=1)
    trusted_producer_version: Literal["trusted_candidate_evaluation_v2"]


class RecursiveMetaOosConfig(DomainModel):
    enabled: Literal[False]
    require_outer_audit_reservation: Literal[True]
    prohibit_best_seed_selection: Literal[True]
    plan_version: Literal["chronological-meta-oos-v1"]
    maximum_outer_audit_uses_per_dataset: int = Field(ge=1)
    reservation_ttl_hours: int = Field(ge=1)
    minimum_epochs: int = Field(ge=2)
    maximum_epochs: int = Field(ge=2)
    maximum_candidate_generation_budget_per_epoch: int = Field(ge=1)
    maximum_oos_budget_per_epoch: int = Field(ge=1)
    minimum_adaptive_delta_sharpe_lcb: float
    minimum_research_efficiency: float = Field(ge=0)
    maximum_allowed_drawdown: float = Field(ge=0, le=1)
    tail_quantile: float = Field(gt=0, lt=0.5)
    maximum_absolute_daily_return: float = Field(gt=0)


class RecursiveImprovementConfig(DomainModel):
    enabled: Literal[False]
    contract_version: Literal["recursive-improvement-v1"]
    candidate_patch_policy_version: Literal["candidate_patch_policy_v2"]
    candidate_patch_policy_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcome_ledger: RecursiveOutcomeLedgerConfig
    meta_controller: RecursiveMetaControllerConfig
    portfolio_sharpe: RecursivePortfolioSharpeConfig
    meta_oos: RecursiveMetaOosConfig


class FactorialArmConfig(DomainModel):
    deterministic_loss_guard: bool
    operational_risk_commander: bool


class FactorialMatchedConditionsConfig(DomainModel):
    independent_state: Literal[True]
    common_market_input: Literal[True]
    common_decision_time: Literal[True]
    common_execution_scenario: Literal[True]
    common_cost_model: Literal[True]
    common_starting_capital: Literal[True]
    decision_schedule_version: str = Field(min_length=1)
    execution_scenario_version: str = Field(min_length=1)
    cost_model_version: str = Field(min_length=1)


class FactorialReportConfig(DomainModel):
    guard_main_effect: Literal[True]
    ai_main_effect: Literal[True]
    ai_guard_interaction_effect: Literal[True]
    prohibit_ai_alpha_label_without_factorial_attribution: Literal[True]


class FactorialExperimentConfig(DomainModel):
    schema_version: Literal["ai-guard-factorial-config-v1"]
    experiment_version: Literal["ai_guard_factorial_v1"]
    base_core: Literal["B0-VOL"]
    arms: dict[str, FactorialArmConfig]
    matched_conditions: FactorialMatchedConditionsConfig
    report: FactorialReportConfig


class ResearchPlaneConfig(DomainModel):
    schema_version: str
    algorithm_version: str
    safety: ResearchSafetyConfig
    schedule: ResearchScheduleConfig
    models: ResearchModelsConfig
    universe: ResearchUniverseConfig
    experiment_budget: ExperimentBudgetConfig
    oos: ResearchOosConfig
    shadow: ResearchShadowConfig
    promotion: ResearchPromotionConfig
    candidate_patch: CandidatePatchConfig
    candidate_execution: CandidateExecutionConfig
    falsification: ResearchFalsificationConfig
    recursive_improvement: RecursiveImprovementConfig


@dataclass(frozen=True, slots=True)
class ResearchConfigBundle:
    config: ResearchPlaneConfig
    factorial: FactorialExperimentConfig
    factorial_document: dict[str, object]
    manifest_hash: str
    path: Path
    factorial_path: Path


def load_research_config(config_dir: Path) -> ResearchConfigBundle:
    path = (config_dir / RESEARCH_CONFIG_FILE).resolve()
    factorial_path = (config_dir / FACTORIAL_CONFIG_FILE).resolve()
    loaded_document: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    loaded_factorial: object = yaml.safe_load(
        factorial_path.read_text(encoding="utf-8")
    )
    if not isinstance(loaded_document, dict):
        raise ValueError("research-plane config must be a YAML object")
    if not isinstance(loaded_factorial, dict):
        raise ValueError("AI/Guard factorial config must be a YAML object")
    document = cast(dict[str, object], loaded_document)
    factorial_document = cast(dict[str, object], loaded_factorial)
    config = ResearchPlaneConfig.model_validate(document)
    factorial = FactorialExperimentConfig.model_validate(factorial_document)
    _validate_invariants(config)
    _validate_factorial(factorial)
    return ResearchConfigBundle(
        config=config,
        factorial=factorial,
        factorial_document=factorial_document,
        manifest_hash=canonical_hash(
            {
                RESEARCH_CONFIG_FILE: config,
                FACTORIAL_CONFIG_FILE: factorial_document,
            }
        ),
        path=path,
        factorial_path=factorial_path,
    )


def recursive_improvement_status(
    bundle: ResearchConfigBundle,
    *,
    experiment_outcome_ledger: dict[str, object],
    meta_controller_ledger: dict[str, object] | None = None,
    portfolio_sharpe_ledger: dict[str, object] | None = None,
    meta_oos_ledger: dict[str, object] | None = None,
) -> dict[str, object]:
    recursive = bundle.config.recursive_improvement
    return {
        "schema_version": "recursive_improvement_status_v1",
        "status": "DISABLED_RESEARCH_ONLY_PR4",
        "enabled": recursive.enabled,
        "audit_only": True,
        "implementation_scope": "PHASE_0_PR_1_PR_2_PR_3_PR_4",
        "contract_version": recursive.contract_version,
        "config_manifest_hash": bundle.manifest_hash,
        "candidate_patch_policy": {
            "version": recursive.candidate_patch_policy_version,
            "contract_hash": recursive.candidate_patch_policy_hash,
        },
        "automatic_outcome_maintenance_enabled": recursive.enabled,
        "experiment_outcome_ledger": dict(experiment_outcome_ledger),
        "meta_controller": {
            "status": "IMPLEMENTED_DISABLED",
            "policy_version": recursive.meta_controller.policy_version,
            **dict(meta_controller_ledger or {}),
        },
        "portfolio_delta_sharpe": {
            "status": "IMPLEMENTED_DISABLED",
            "ledger": dict(portfolio_sharpe_ledger or {}),
            "minimum_common_sessions": (
                recursive.portfolio_sharpe.minimum_common_sessions
            ),
            "minimum_delta_sharpe_lcb": (
                recursive.portfolio_sharpe.minimum_delta_sharpe_lcb
            ),
            "minimum_worst_cost_delta_sharpe_lcb": (
                recursive.portfolio_sharpe.minimum_worst_cost_delta_sharpe_lcb
            ),
            "trusted_producer_version": (
                recursive.portfolio_sharpe.trusted_producer_version
            ),
        },
        "chronological_meta_oos": {
            "status": "IMPLEMENTED_DISABLED",
            "enabled": recursive.meta_oos.enabled,
            "plan_version": recursive.meta_oos.plan_version,
            "minimum_epochs": recursive.meta_oos.minimum_epochs,
            "maximum_outer_audit_uses_per_dataset": (
                recursive.meta_oos.maximum_outer_audit_uses_per_dataset
            ),
            "minimum_adaptive_delta_sharpe_lcb": (
                recursive.meta_oos.minimum_adaptive_delta_sharpe_lcb
            ),
            "minimum_research_efficiency": (
                recursive.meta_oos.minimum_research_efficiency
            ),
            "maximum_allowed_drawdown": (
                recursive.meta_oos.maximum_allowed_drawdown
            ),
            "ledger": dict(meta_oos_ledger or {}),
        },
        "automatic_promotion_enabled": (
            bundle.config.promotion.automatic_promotion_enabled
        ),
        "real_order_routing": bundle.config.safety.real_order_routing,
    }


def meta_controller_parameters(
    bundle: ResearchConfigBundle,
) -> MetaControllerParametersV1:
    """Build the immutable domain parameters without importing config in math."""

    from trading.research.meta_controller import (
        MetaControllerParametersV1,
    )

    configured = bundle.config.recursive_improvement.meta_controller
    return MetaControllerParametersV1(
        schema_version="meta_controller_parameters_v1",
        **configured.model_dump(mode="python"),
    )


def portfolio_bootstrap_contract(
    bundle: ResearchConfigBundle,
) -> StationaryBootstrapContractV1:
    from trading.research.portfolio_delta_sharpe import (
        StationaryBootstrapContractV1,
    )

    configured = bundle.config.recursive_improvement.portfolio_sharpe
    return StationaryBootstrapContractV1(
        configured_seed=configured.configured_bootstrap_seed,
        samples=configured.bootstrap_samples,
        expected_block_sessions=configured.bootstrap_block_length,
        lower_quantile=configured.lower_quantile,
        variance_epsilon=configured.variance_epsilon,
    )


def promotion_evaluation_contract_v2(
    bundle: ResearchConfigBundle,
) -> PromotionEvaluationContractV2:
    from trading.research.promotion_v2 import PromotionEvaluationContractV2

    base = bundle.config.promotion.evaluation_contract
    sharpe = bundle.config.recursive_improvement.portfolio_sharpe
    return PromotionEvaluationContractV2(
        contract_version="research-promotion-thresholds-v2",
        minimum_common_oos_sessions=base.minimum_common_oos_sessions,
        minimum_forward_sessions=base.minimum_forward_sessions,
        minimum_independent_trades=base.minimum_independent_trades,
        minimum_annualized_net_excess_return_after_cost=(
            base.minimum_annualized_net_excess_return_after_cost
        ),
        minimum_matched_annualized_difference=(
            base.minimum_matched_annualized_difference
        ),
        minimum_economic_effect=base.minimum_economic_effect,
        maximum_drawdown=base.maximum_drawdown,
        maximum_tail_loss=base.maximum_tail_loss,
        maximum_annualized_turnover=base.maximum_annualized_turnover,
        minimum_capacity_usd=base.minimum_capacity_usd,
        minimum_regime_pass_fraction=base.minimum_regime_pass_fraction,
        maximum_runtime_error_rate=base.maximum_runtime_error_rate,
        minimum_oos_delta_sharpe_lcb=sharpe.minimum_delta_sharpe_lcb,
        minimum_shadow_delta_sharpe_lcb=sharpe.minimum_delta_sharpe_lcb,
        minimum_worst_cost_delta_sharpe_lcb=(
            sharpe.minimum_worst_cost_delta_sharpe_lcb
        ),
    )


def meta_oos_evaluation_contract(
    bundle: ResearchConfigBundle,
) -> MetaOosEvaluationContractV1:
    from trading.research.chronological_meta_oos import (
        build_meta_oos_evaluation_contract,
    )

    configured = bundle.config.recursive_improvement.meta_oos
    return build_meta_oos_evaluation_contract(
        contract_version="chronological-meta-oos-thresholds-v1",
        annualization_sessions=(
            bundle.config.recursive_improvement.portfolio_sharpe
            .annualization_sessions
        ),
        minimum_epochs=configured.minimum_epochs,
        maximum_epochs=configured.maximum_epochs,
        maximum_candidate_generation_budget_per_epoch=(
            configured.maximum_candidate_generation_budget_per_epoch
        ),
        maximum_oos_budget_per_epoch=(
            configured.maximum_oos_budget_per_epoch
        ),
        maximum_outer_audit_uses_per_dataset=(
            configured.maximum_outer_audit_uses_per_dataset
        ),
        reservation_ttl_hours=configured.reservation_ttl_hours,
        minimum_adaptive_delta_sharpe_lcb=(
            configured.minimum_adaptive_delta_sharpe_lcb
        ),
        minimum_research_efficiency=(
            configured.minimum_research_efficiency
        ),
        maximum_allowed_drawdown=configured.maximum_allowed_drawdown,
        tail_quantile=configured.tail_quantile,
        maximum_absolute_daily_return=(
            configured.maximum_absolute_daily_return
        ),
    )


def oos_process_evaluation_config(
    bundle: ResearchConfigBundle,
    *,
    dataset_id: str,
    dataset_manifest_hash: str,
    data_available_cutoff: datetime,
    expected_source_data_manifest_hash: str,
    expected_candidate_replay_hash: str,
) -> OosProcessEvaluationConfig:
    from trading.research.oos_lockbox import OosProcessEvaluationConfig

    oos = bundle.config.oos
    budgets = bundle.config.experiment_budget
    return OosProcessEvaluationConfig(
        dataset_id=dataset_id,
        dataset_manifest_hash=dataset_manifest_hash,
        data_available_cutoff=data_available_cutoff,
        expected_source_data_manifest_hash=(
            expected_source_data_manifest_hash
        ),
        expected_candidate_replay_hash=expected_candidate_replay_hash,
        expected_trusted_producer_version=oos.trusted_producer_version,
        minimum_common_sessions=oos.minimum_common_sessions,
        minimum_mean_daily_difference=oos.minimum_mean_daily_difference,
        annualization_sessions=oos.annualization_sessions,
        newey_west_lag=oos.newey_west_lag,
        bootstrap_seed=oos.bootstrap_seed,
        bootstrap_block_length=oos.bootstrap_block_length,
        bootstrap_samples=oos.bootstrap_samples,
        base_cost_bps=oos.base_cost_bps,
        request_ttl_seconds=oos.request_ttl_seconds,
        worker_timeout_seconds=oos.worker_timeout_seconds,
        cost_sensitivity_bps=oos.cost_sensitivity_bps,
        maximum_submissions=budgets.maximum_submissions_per_family,
        maximum_oos_uses=budgets.maximum_oos_uses_per_family,
    )


def oos_process_evaluation_config_v2(
    bundle: ResearchConfigBundle,
    *,
    dataset_id: str,
    dataset_manifest_hash: str,
    data_available_cutoff: datetime,
    expected_source_data_manifest_hash: str,
    expected_candidate_replay_hash: str,
    portfolio_comparison_contract: PortfolioComparisonContractV1,
) -> OosProcessEvaluationConfigV2:
    from trading.research.oos_lockbox import OosProcessEvaluationConfigV2

    recursive = bundle.config.recursive_improvement.portfolio_sharpe
    budgets = bundle.config.experiment_budget
    return OosProcessEvaluationConfigV2(
        dataset_id=dataset_id,
        dataset_manifest_hash=dataset_manifest_hash,
        data_available_cutoff=data_available_cutoff,
        expected_source_data_manifest_hash=expected_source_data_manifest_hash,
        expected_candidate_replay_hash=expected_candidate_replay_hash,
        portfolio_comparison_contract=portfolio_comparison_contract,
        minimum_common_sessions=recursive.minimum_common_sessions,
        minimum_independent_trades=recursive.minimum_independent_trades,
        minimum_delta_sharpe_lcb=recursive.minimum_delta_sharpe_lcb,
        minimum_worst_cost_delta_sharpe_lcb=(
            recursive.minimum_worst_cost_delta_sharpe_lcb
        ),
        request_ttl_seconds=bundle.config.oos.request_ttl_seconds,
        worker_timeout_seconds=bundle.config.oos.worker_timeout_seconds,
        maximum_submissions=budgets.maximum_submissions_per_family,
        maximum_oos_uses=budgets.maximum_oos_uses_per_family,
    )


def shadow_paper_parameters(
    bundle: ResearchConfigBundle,
) -> ShadowPaperParametersV1:
    from trading.research.shadow_runtime import ShadowPaperParametersV1

    payload = bundle.config.shadow.paper_parameters.model_dump(mode="python")
    return ShadowPaperParametersV1.model_validate(payload)


def candidate_process_limits(
    bundle: ResearchConfigBundle,
) -> CandidateProcessLimitsV1:
    from trading.research.candidate_process import CandidateProcessLimitsV1

    execution = bundle.config.candidate_execution
    return CandidateProcessLimitsV1.model_validate(
        execution.model_dump(
            mode="python",
            exclude={"isolation_kind", "isolation_version"},
        )
    )


def candidate_execution_security(
    bundle: ResearchConfigBundle,
    *,
    candidate_artifact_hash: str,
    candidate_tree_hash: str,
    runtime_executable_hash: str,
    worker_code_hash: str,
    declared_entrypoint: str,
) -> CandidateExecutionSecurityV1:
    from trading.research.candidate_process import (
        build_candidate_execution_security,
    )

    execution = bundle.config.candidate_execution
    return build_candidate_execution_security(
        isolation_kind=execution.isolation_kind,
        isolation_version=execution.isolation_version,
        candidate_artifact_hash=candidate_artifact_hash,
        candidate_tree_hash=candidate_tree_hash,
        runtime_executable_hash=runtime_executable_hash,
        worker_code_hash=worker_code_hash,
        declared_entrypoint=declared_entrypoint,
        limits=candidate_process_limits(bundle),
    )


def _validate_invariants(config: ResearchPlaneConfig) -> None:
    if any(
        (
            config.safety.real_order_routing,
            config.safety.automatic_promotion_enabled,
            config.safety.champion_in_place_mutation,
            config.safety.raw_oos_access,
            config.safety.credential_access,
            config.promotion.automatic_promotion_enabled,
        )
    ):
        raise ValueError("Research Plane safety switches must remain false")
    if not config.promotion.require_manual_approval:
        raise ValueError("Research promotion must require manual approval")
    scout = config.models.web_scout
    if (
        scout.family != "GPT-5.6 Sol Pro"
        or scout.reasoning_profile != "xhigh"
        or scout.access_path != "CHATGPT_WEB_AGBROWSE"
        or scout.api_fallback_allowed is not False
        or scout.conversation_reuse_allowed is not False
    ):
        raise ValueError("Web Scout must be exact GPT-5.6 Sol Pro/xhigh via AGBrowse")
    for invocation in (
        config.models.codex_commander,
        config.models.candidate_builder,
    ):
        if (
            invocation.model != "gpt-5.6-sol"
            or invocation.reasoning_profile != "max"
            or invocation.session_resume_allowed is not False
        ):
            raise ValueError("Codex roles must use fresh gpt-5.6-sol/max invocations")
    if config.universe.asset_classes != ["US_EQUITY", "US_ETF"]:
        raise ValueError("Research universe must cover US equities and ETFs")
    if (
        config.universe.source != "VERSIONED_AVAILABLE_DATA_CATALOG"
        or config.universe.hardcoded_symbol_allowlist
    ):
        raise ValueError("Research universe must come from a versioned data catalog")
    if tuple(config.candidate_patch.allowed_prefixes) != DEFAULT_ALLOWED_PREFIXES:
        raise ValueError("candidate allowed path contract changed")
    if tuple(config.candidate_patch.forbidden_prefixes) != DEFAULT_FORBIDDEN_PREFIXES:
        raise ValueError("candidate forbidden prefix contract changed")
    if tuple(config.candidate_patch.forbidden_exact) != DEFAULT_FORBIDDEN_EXACT:
        raise ValueError("candidate forbidden exact-path contract changed")
    recursive = config.recursive_improvement
    if (
        recursive.candidate_patch_policy_version
        != CANDIDATE_PATCH_POLICY_V2
        or recursive.candidate_patch_policy_hash
        != CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH
    ):
        raise ValueError("recursive Candidate patch policy binding changed")
    portfolio_sharpe = recursive.portfolio_sharpe
    if portfolio_sharpe.cost_stress_multipliers != (1.0, 2.0, 3.0):
        raise ValueError("portfolio Sharpe cost stress must remain 1x/2x/3x")
    if portfolio_sharpe.minimum_common_sessions < 126:
        raise ValueError("portfolio Sharpe OOS requires at least 126 sessions")
    if (
        portfolio_sharpe.trusted_producer_version
        != "trusted_candidate_evaluation_v2"
    ):
        raise ValueError("portfolio OOS trusted producer version changed")
    meta_oos = recursive.meta_oos
    if meta_oos.minimum_epochs > meta_oos.maximum_epochs:
        raise ValueError("meta-OOS minimum epochs exceed maximum")
    if meta_oos.maximum_outer_audit_uses_per_dataset != 1:
        raise ValueError(
            "initial meta-OOS dataset reuse budget must remain one"
        )
    if (
        meta_oos.maximum_absolute_daily_return
        != portfolio_sharpe.maximum_absolute_daily_return
    ):
        raise ValueError("meta-OOS and portfolio return bounds differ")
    candidate_execution = config.candidate_execution
    if (
        candidate_execution.isolation_kind
        != "CODEX_WINDOWS_RESTRICTED_TOKEN"
        or candidate_execution.isolation_version != "1.0.0"
    ):
        raise ValueError("candidate execution isolation contract changed")
    if tuple(config.falsification.mandatory) != MANDATORY_FALSIFICATION_TESTS:
        raise ValueError("mandatory falsification catalog changed")
    if config.falsification.evaluation_contract.cost_stress_multipliers != (
        1.0,
        2.0,
        3.0,
    ):
        raise ValueError("mandatory cost stress contract must remain 1x/2x/3x")
    if config.oos.detail_return_policy != "AGGREGATES_AND_REASON_CODES_ONLY":
        raise ValueError("OOS lockbox must not expose observation details")
    if config.oos.cost_sensitivity_bps != (0, 5, 10):
        raise ValueError("OOS cost sensitivity contract must remain 0/5/10 bp")
    if (
        config.oos.trusted_producer_version
        != "trusted_candidate_evaluation_v1"
    ):
        raise ValueError("OOS trusted dataset producer version changed")
    shadow_parameters = config.shadow.paper_parameters
    if shadow_parameters.real_order_routing:
        raise ValueError("shadow paper parameters cannot enable real routing")
    if shadow_parameters.sensitivity_10_bps <= shadow_parameters.sensitivity_5_bps:
        raise ValueError("shadow 10 bp sensitivity must exceed 5 bp")
    if (
        shadow_parameters.delay_penalty_bps
        >= shadow_parameters.basis_points_per_unit_return
        or shadow_parameters.sensitivity_10_bps
        >= shadow_parameters.basis_points_per_unit_return
    ):
        raise ValueError("shadow basis-point costs must stay below one unit")


def _validate_factorial(config: FactorialExperimentConfig) -> None:
    expected = {"B0-VOL", "B3-GUARD", "B3-AI", "B3-AI-GUARD"}
    if set(config.arms) != expected:
        raise ValueError("AI/Guard factorial arm set mismatch")
    treatment_pairs = {
        arm_id: (
            item.deterministic_loss_guard,
            item.operational_risk_commander,
        )
        for arm_id, item in config.arms.items()
    }
    if treatment_pairs != {
        "B0-VOL": (False, False),
        "B3-GUARD": (True, False),
        "B3-AI": (False, True),
        "B3-AI-GUARD": (True, True),
    }:
        raise ValueError("AI/Guard factorial treatment assignment changed")
