from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
    OosVerdict,
    PromotionDecisionV1,
)
from trading.research.oos_v2 import OosLockboxResultV2
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
    PortfolioCostStressResultV1,
    PortfolioReturnObservationV1,
    evaluate_portfolio_delta_sharpe,
)
from trading.research.promotion import evaluate_promotion_eligibility

REQUIRED_PROMOTION_CRITERIA_V2 = (
    "minimum_independent_trades",
    "minimum_forward_period",
    "net_excess_return_after_cost",
    "matched_baseline_improvement",
    "minimum_economic_effect",
    "maximum_drawdown",
    "tail_risk",
    "turnover",
    "capacity",
    "regime_robustness",
    "error_rate",
    "replay_reproducible",
    "mandatory_tests",
    "oos_portfolio_delta_sharpe_lcb",
    "shadow_portfolio_delta_sharpe_lcb",
    "worst_cost_portfolio_delta_sharpe_lcb",
    "portfolio_comparison_contract_binding",
    "allocation_policy_fixed_before_oos",
)


class TrustedShadowPerformanceSummaryV2(DomainModel):
    schema_version: Literal["trusted_shadow_performance_summary_v2"] = (
        "trusted_shadow_performance_summary_v2"
    )
    summary_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    shadow_pair_id: str = Field(pattern=IDENTIFIER_PATTERN)
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    current_champion_version: str = Field(pattern=VERSION_PATTERN)
    candidate_version: str = Field(pattern=VERSION_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    champion_portfolio_manifest_hash: str = Field(pattern=HASH_PATTERN)
    candidate_portfolio_manifest_hash: str = Field(pattern=HASH_PATTERN)
    portfolio_comparison_contract_hash: str = Field(pattern=HASH_PATTERN)
    execution_contract_hash: str = Field(pattern=HASH_PATTERN)
    daily_evidence_hashes: tuple[str, ...] = Field(min_length=1)
    materialized_evidence_hash: str = Field(pattern=HASH_PATTERN)
    forward_sessions: int = Field(gt=1)
    independent_trades: int = Field(ge=0)
    shadow_candidate_portfolio_sharpe: float
    shadow_champion_portfolio_sharpe: float
    shadow_delta_sharpe_point: float
    shadow_delta_sharpe_lcb: float
    shadow_delta_sharpe_ucb: float
    shadow_worst_cost_delta_sharpe_lcb: float
    cost_stress_results: tuple[PortfolioCostStressResultV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    annualized_net_excess_return_after_cost: float
    matched_annualized_difference: float
    economic_effect: float
    maximum_drawdown: float = Field(ge=0, le=1)
    tail_loss: float = Field(ge=0, le=1)
    annualized_turnover: float = Field(ge=0)
    estimated_capacity_usd: float = Field(ge=0)
    regime_pass_fraction: float = Field(ge=0, le=1)
    runtime_error_rate: float = Field(ge=0, le=1)
    data_available_cutoff: datetime
    created_at: datetime
    real_order_routing: Literal[False] = False
    summary_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("data_available_cutoff", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "shadow_candidate_portfolio_sharpe",
        "shadow_champion_portfolio_sharpe",
        "shadow_delta_sharpe_point",
        "shadow_delta_sharpe_lcb",
        "shadow_delta_sharpe_ucb",
        "shadow_worst_cost_delta_sharpe_lcb",
        "annualized_net_excess_return_after_cost",
        "matched_annualized_difference",
        "economic_effect",
        "maximum_drawdown",
        "tail_loss",
        "annualized_turnover",
        "estimated_capacity_usd",
        "regime_pass_fraction",
        "runtime_error_rate",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("trusted shadow V2 metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.created_at < self.data_available_cutoff:
            raise ValueError("shadow V2 summary predates its data cutoff")
        if self.current_champion_version == self.candidate_version:
            raise ValueError("shadow V2 versions must differ")
        if self.forward_sessions != len(self.daily_evidence_hashes):
            raise ValueError("shadow V2 evidence count mismatch")
        if len(self.daily_evidence_hashes) != len(set(self.daily_evidence_hashes)):
            raise ValueError("shadow V2 evidence hashes must be unique")
        if any(
            re.fullmatch(HASH_PATTERN, item) is None
            for item in self.daily_evidence_hashes
        ):
            raise ValueError("shadow V2 evidence hash is invalid")
        if canonical_hash(self.daily_evidence_hashes) != self.materialized_evidence_hash:
            raise ValueError("shadow V2 materialized evidence hash mismatch")
        if self.shadow_worst_cost_delta_sharpe_lcb != min(
            item.delta_sharpe_lcb for item in self.cost_stress_results
        ):
            raise ValueError("shadow V2 worst-cost LCB mismatch")
        payload = self.model_dump(mode="python", exclude={"summary_hash"})
        if canonical_hash(payload) != self.summary_hash:
            raise ValueError("trusted shadow V2 summary hash mismatch")
        return self


class PromotionEvaluationContractV2(DomainModel):
    schema_version: Literal["promotion_evaluation_contract_v2"] = (
        "promotion_evaluation_contract_v2"
    )
    contract_version: str = Field(pattern=IDENTIFIER_PATTERN)
    minimum_common_oos_sessions: int = Field(ge=2)
    minimum_forward_sessions: int = Field(gt=1)
    minimum_independent_trades: int = Field(gt=0)
    minimum_annualized_net_excess_return_after_cost: float
    minimum_matched_annualized_difference: float
    minimum_economic_effect: float
    maximum_drawdown: float = Field(ge=0, le=1)
    maximum_tail_loss: float = Field(ge=0, le=1)
    maximum_annualized_turnover: float = Field(gt=0)
    minimum_capacity_usd: float = Field(gt=0)
    minimum_regime_pass_fraction: float = Field(ge=0, le=1)
    maximum_runtime_error_rate: float = Field(ge=0, le=1)
    minimum_oos_delta_sharpe_lcb: float
    minimum_shadow_delta_sharpe_lcb: float
    minimum_worst_cost_delta_sharpe_lcb: float

    @field_validator(
        "minimum_annualized_net_excess_return_after_cost",
        "minimum_matched_annualized_difference",
        "minimum_economic_effect",
        "maximum_drawdown",
        "maximum_tail_loss",
        "maximum_annualized_turnover",
        "minimum_capacity_usd",
        "minimum_regime_pass_fraction",
        "maximum_runtime_error_rate",
        "minimum_oos_delta_sharpe_lcb",
        "minimum_shadow_delta_sharpe_lcb",
        "minimum_worst_cost_delta_sharpe_lcb",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("promotion V2 threshold must be finite")
        return value


class PromotionEvidenceV2(DomainModel):
    schema_version: Literal["promotion_evidence_v2"] = "promotion_evidence_v2"
    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    current_champion_version: str = Field(pattern=VERSION_PATTERN)
    candidate_version: str = Field(pattern=VERSION_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    portfolio_comparison_contract_hash: str = Field(pattern=HASH_PATTERN)
    falsification_report_hash: str = Field(pattern=HASH_PATTERN)
    oos_result: OosLockboxResultV2
    shadow_summary: TrustedShadowPerformanceSummaryV2
    replay_hash: str = Field(pattern=HASH_PATTERN)
    annualized_net_excess_return_after_cost: float
    matched_annualized_difference: float
    economic_effect: float
    maximum_drawdown: float = Field(ge=0, le=1)
    tail_loss: float = Field(ge=0, le=1)
    annualized_turnover: float = Field(ge=0)
    estimated_capacity_usd: float = Field(ge=0)
    regime_pass_fraction: float = Field(ge=0, le=1)
    runtime_error_rate: float = Field(ge=0, le=1)
    replay_reproducible: bool
    mandatory_tests_passed: bool
    allocation_policy_fixed_before_oos: bool
    portfolio_contract_binding_valid: bool
    data_available_cutoff: datetime
    created_at: datetime
    evidence_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("data_available_cutoff", "created_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "annualized_net_excess_return_after_cost",
        "matched_annualized_difference",
        "economic_effect",
        "maximum_drawdown",
        "tail_loss",
        "annualized_turnover",
        "estimated_capacity_usd",
        "regime_pass_fraction",
        "runtime_error_rate",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("promotion V2 evidence metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.created_at < self.data_available_cutoff:
            raise ValueError("promotion V2 evidence predates its data cutoff")
        if self.current_champion_version == self.candidate_version:
            raise ValueError("promotion V2 versions must differ")
        if (
            self.oos_result.challenger_id != self.challenger_id
            or self.shadow_summary.challenger_id != self.challenger_id
            or self.oos_result.candidate_artifact_hash
            != self.candidate_artifact_hash
            or self.shadow_summary.candidate_artifact_hash
            != self.candidate_artifact_hash
            or self.oos_result.portfolio_comparison_contract_hash
            != self.portfolio_comparison_contract_hash
            or self.shadow_summary.portfolio_comparison_contract_hash
            != self.portfolio_comparison_contract_hash
        ):
            raise ValueError("promotion V2 portfolio evidence binding mismatch")
        payload = self.model_dump(mode="python", exclude={"evidence_hash"})
        if canonical_hash(payload) != self.evidence_hash:
            raise ValueError("promotion V2 evidence hash mismatch")
        return self


class TrustedPromotionEvaluationV2(DomainModel):
    schema_version: Literal["trusted_promotion_evaluation_v2"] = (
        "trusted_promotion_evaluation_v2"
    )
    evaluation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evidence_hash: str = Field(pattern=HASH_PATTERN)
    contract_hash: str = Field(pattern=HASH_PATTERN)
    portfolio_comparison_contract_hash: str = Field(pattern=HASH_PATTERN)
    decision: PromotionDecisionV1
    created_at: datetime
    automatic_promotion_enabled: Literal[False] = False
    evaluation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_evaluation(self) -> Self:
        if self.decision.automatic_promotion_enabled:
            raise ValueError("automatic Champion promotion is unavailable")
        payload = self.model_dump(mode="python", exclude={"evaluation_hash"})
        if canonical_hash(payload) != self.evaluation_hash:
            raise ValueError("trusted promotion V2 evaluation hash mismatch")
        return self


def build_trusted_shadow_performance_summary_v2(
    *,
    summary_id: str,
    challenger_id: str,
    shadow_pair_id: str,
    run_id: str,
    current_champion_version: str,
    candidate_version: str,
    candidate_artifact_hash: str,
    comparison_contract: PortfolioComparisonContractV1,
    execution_contract_hash: str,
    observations: Sequence[PortfolioReturnObservationV1],
    daily_evidence_hashes: tuple[str, ...],
    independent_trades: int,
    annualized_net_excess_return_after_cost: float,
    matched_annualized_difference: float,
    economic_effect: float,
    maximum_drawdown: float,
    tail_loss: float,
    annualized_turnover: float,
    estimated_capacity_usd: float,
    regime_pass_fraction: float,
    runtime_error_rate: float,
    data_available_cutoff: datetime,
    created_at: datetime,
) -> TrustedShadowPerformanceSummaryV2:
    rows = tuple(observations)
    cutoff = require_aware_utc(data_available_cutoff)
    if candidate_artifact_hash != comparison_contract.candidate_artifact_hash:
        raise ValueError("shadow V2 candidate artifact binding mismatch")
    if execution_contract_hash != comparison_contract.execution_contract_hash:
        raise ValueError("shadow V2 execution contract binding mismatch")
    if len(rows) != len(daily_evidence_hashes):
        raise ValueError("shadow V2 rows and evidence hashes differ")
    if any(item.available_at > cutoff for item in rows):
        raise ValueError("shadow V2 observation exceeds data cutoff")
    metric = evaluate_portfolio_delta_sharpe(
        observations=rows,
        comparison_contract=comparison_contract,
        evaluation_contract_hash=execution_contract_hash,
    )
    payload = {
        "schema_version": "trusted_shadow_performance_summary_v2",
        "summary_id": summary_id,
        "challenger_id": challenger_id,
        "shadow_pair_id": shadow_pair_id,
        "run_id": run_id,
        "current_champion_version": current_champion_version,
        "candidate_version": candidate_version,
        "candidate_artifact_hash": candidate_artifact_hash,
        "champion_portfolio_manifest_hash": (
            comparison_contract.champion_portfolio_manifest_hash
        ),
        "candidate_portfolio_manifest_hash": (
            comparison_contract.candidate_portfolio_manifest_hash
        ),
        "portfolio_comparison_contract_hash": comparison_contract.contract_hash,
        "execution_contract_hash": execution_contract_hash,
        "daily_evidence_hashes": daily_evidence_hashes,
        "materialized_evidence_hash": canonical_hash(daily_evidence_hashes),
        "forward_sessions": metric.common_sessions,
        "independent_trades": independent_trades,
        "shadow_candidate_portfolio_sharpe": metric.candidate_portfolio_sharpe,
        "shadow_champion_portfolio_sharpe": metric.champion_portfolio_sharpe,
        "shadow_delta_sharpe_point": metric.delta_sharpe_point,
        "shadow_delta_sharpe_lcb": metric.delta_sharpe_lcb,
        "shadow_delta_sharpe_ucb": metric.delta_sharpe_ucb,
        "shadow_worst_cost_delta_sharpe_lcb": (
            metric.worst_cost_delta_sharpe_lcb
        ),
        "cost_stress_results": metric.cost_stress_results,
        "annualized_net_excess_return_after_cost": float(
            annualized_net_excess_return_after_cost
        ),
        "matched_annualized_difference": float(matched_annualized_difference),
        "economic_effect": float(economic_effect),
        "maximum_drawdown": float(maximum_drawdown),
        "tail_loss": float(tail_loss),
        "annualized_turnover": float(annualized_turnover),
        "estimated_capacity_usd": float(estimated_capacity_usd),
        "regime_pass_fraction": float(regime_pass_fraction),
        "runtime_error_rate": float(runtime_error_rate),
        "data_available_cutoff": cutoff,
        "created_at": require_aware_utc(created_at),
        "real_order_routing": False,
    }
    return TrustedShadowPerformanceSummaryV2.model_validate(
        {**payload, "summary_hash": canonical_hash(payload)}
    )


def build_promotion_evidence_v2(
    *,
    evidence_id: str,
    challenger_id: str,
    current_champion_version: str,
    candidate_version: str,
    candidate_artifact_hash: str,
    comparison_contract: PortfolioComparisonContractV1,
    falsification_report_hash: str,
    oos_result: OosLockboxResultV2,
    shadow_summary: TrustedShadowPerformanceSummaryV2,
    replay_hash: str,
    annualized_net_excess_return_after_cost: float,
    matched_annualized_difference: float,
    economic_effect: float,
    maximum_drawdown: float,
    tail_loss: float,
    annualized_turnover: float,
    estimated_capacity_usd: float,
    regime_pass_fraction: float,
    runtime_error_rate: float,
    replay_reproducible: bool,
    mandatory_tests_passed: bool,
    data_available_cutoff: datetime,
    created_at: datetime,
) -> PromotionEvidenceV2:
    binding_valid = (
        oos_result.portfolio_contract_binding_valid
        and oos_result.portfolio_comparison_contract_hash
        == comparison_contract.contract_hash
        and shadow_summary.portfolio_comparison_contract_hash
        == comparison_contract.contract_hash
        and shadow_summary.champion_portfolio_manifest_hash
        == comparison_contract.champion_portfolio_manifest_hash
        and shadow_summary.candidate_portfolio_manifest_hash
        == comparison_contract.candidate_portfolio_manifest_hash
    )
    payload = {
        "schema_version": "promotion_evidence_v2",
        "evidence_id": evidence_id,
        "challenger_id": challenger_id,
        "current_champion_version": current_champion_version,
        "candidate_version": candidate_version,
        "candidate_artifact_hash": candidate_artifact_hash,
        "portfolio_comparison_contract_hash": comparison_contract.contract_hash,
        "falsification_report_hash": falsification_report_hash,
        "oos_result": oos_result,
        "shadow_summary": shadow_summary,
        "replay_hash": replay_hash,
        "annualized_net_excess_return_after_cost": float(
            annualized_net_excess_return_after_cost
        ),
        "matched_annualized_difference": float(matched_annualized_difference),
        "economic_effect": float(economic_effect),
        "maximum_drawdown": float(maximum_drawdown),
        "tail_loss": float(tail_loss),
        "annualized_turnover": float(annualized_turnover),
        "estimated_capacity_usd": float(estimated_capacity_usd),
        "regime_pass_fraction": float(regime_pass_fraction),
        "runtime_error_rate": float(runtime_error_rate),
        "replay_reproducible": replay_reproducible,
        "mandatory_tests_passed": mandatory_tests_passed,
        "allocation_policy_fixed_before_oos": (
            oos_result.allocation_policy_fixed_before_oos
            and comparison_contract.allocation_policy_created_at
            <= comparison_contract.created_at
        ),
        "portfolio_contract_binding_valid": binding_valid,
        "data_available_cutoff": require_aware_utc(data_available_cutoff),
        "created_at": require_aware_utc(created_at),
    }
    return PromotionEvidenceV2.model_validate(
        {**payload, "evidence_hash": canonical_hash(payload)}
    )


def evaluate_trusted_promotion_evidence_v2(
    *,
    evidence: PromotionEvidenceV2,
    contract: PromotionEvaluationContractV2,
    created_at: datetime,
) -> TrustedPromotionEvaluationV2:
    timestamp = require_aware_utc(created_at)
    if timestamp < evidence.created_at:
        raise ValueError("promotion V2 evaluation predates its evidence")
    oos = evidence.oos_result
    shadow = evidence.shadow_summary
    criteria = {
        "minimum_independent_trades": (
            min(oos.independent_trades, shadow.independent_trades)
            >= contract.minimum_independent_trades
        ),
        "minimum_forward_period": (
            oos.common_sessions >= contract.minimum_common_oos_sessions
            and shadow.forward_sessions >= contract.minimum_forward_sessions
        ),
        "net_excess_return_after_cost": (
            evidence.annualized_net_excess_return_after_cost
            >= contract.minimum_annualized_net_excess_return_after_cost
        ),
        "matched_baseline_improvement": (
            evidence.matched_annualized_difference
            >= contract.minimum_matched_annualized_difference
        ),
        "minimum_economic_effect": (
            evidence.economic_effect >= contract.minimum_economic_effect
        ),
        "maximum_drawdown": (
            evidence.maximum_drawdown <= contract.maximum_drawdown
        ),
        "tail_risk": evidence.tail_loss <= contract.maximum_tail_loss,
        "turnover": (
            evidence.annualized_turnover
            <= contract.maximum_annualized_turnover
        ),
        "capacity": (
            evidence.estimated_capacity_usd >= contract.minimum_capacity_usd
        ),
        "regime_robustness": (
            evidence.regime_pass_fraction
            >= contract.minimum_regime_pass_fraction
        ),
        "error_rate": (
            evidence.runtime_error_rate <= contract.maximum_runtime_error_rate
        ),
        "replay_reproducible": evidence.replay_reproducible,
        "mandatory_tests": evidence.mandatory_tests_passed,
        "oos_portfolio_delta_sharpe_lcb": (
            oos.verdict is OosVerdict.PASS
            and oos.delta_sharpe_lcb is not None
            and oos.delta_sharpe_lcb
            > contract.minimum_oos_delta_sharpe_lcb
        ),
        "shadow_portfolio_delta_sharpe_lcb": (
            shadow.shadow_delta_sharpe_lcb
            > contract.minimum_shadow_delta_sharpe_lcb
        ),
        "worst_cost_portfolio_delta_sharpe_lcb": (
            oos.worst_cost_delta_sharpe_lcb is not None
            and min(
                oos.worst_cost_delta_sharpe_lcb,
                shadow.shadow_worst_cost_delta_sharpe_lcb,
            )
            >= contract.minimum_worst_cost_delta_sharpe_lcb
        ),
        "portfolio_comparison_contract_binding": (
            evidence.portfolio_contract_binding_valid
        ),
        "allocation_policy_fixed_before_oos": (
            evidence.allocation_policy_fixed_before_oos
        ),
    }
    if tuple(criteria) != REQUIRED_PROMOTION_CRITERIA_V2:
        raise RuntimeError("promotion V2 criteria order changed")
    contract_hash = canonical_hash(contract)
    evaluation_id = stable_id(
        "trusted-promotion-evaluation-v2",
        evidence.challenger_id,
        evidence.evidence_hash,
        contract_hash,
    )
    decision = evaluate_promotion_eligibility(
        promotion_decision_id=stable_id(
            "promotion-decision-v2",
            evaluation_id,
            evidence.replay_hash,
        ),
        challenger_id=evidence.challenger_id,
        current_champion_version=evidence.current_champion_version,
        candidate_version=evidence.candidate_version,
        criteria=criteria,
        replay_hash=evidence.replay_hash,
        created_at=timestamp,
        required_criteria=REQUIRED_PROMOTION_CRITERIA_V2,
    )
    payload = {
        "schema_version": "trusted_promotion_evaluation_v2",
        "evaluation_id": evaluation_id,
        "evidence_hash": evidence.evidence_hash,
        "contract_hash": contract_hash,
        "portfolio_comparison_contract_hash": (
            evidence.portfolio_comparison_contract_hash
        ),
        "decision": decision,
        "created_at": timestamp,
        "automatic_promotion_enabled": False,
    }
    return TrustedPromotionEvaluationV2.model_validate(
        {**payload, "evaluation_hash": canonical_hash(payload)}
    )


__all__ = [
    "REQUIRED_PROMOTION_CRITERIA_V2",
    "PromotionEvaluationContractV2",
    "PromotionEvidenceV2",
    "TrustedPromotionEvaluationV2",
    "TrustedShadowPerformanceSummaryV2",
    "build_promotion_evidence_v2",
    "build_trusted_shadow_performance_summary_v2",
    "evaluate_trusted_promotion_evidence_v2",
]
