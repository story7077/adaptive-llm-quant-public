from __future__ import annotations

import math
import re
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
    PromotionDecisionV1,
)
from trading.research.promotion import (
    REQUIRED_PROMOTION_CRITERIA,
    evaluate_promotion_eligibility,
)
from trading.research.shadow_runtime import MatchedShadowPerformanceSummaryV1


class PromotionEvaluationContractV1(DomainModel):
    schema_version: str = Field(default="promotion_evaluation_contract_v1")
    contract_version: str = Field(pattern=IDENTIFIER_PATTERN)
    minimum_common_oos_sessions: int = Field(ge=126)
    minimum_forward_sessions: int = Field(gt=0)
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
        mode="after",
    )
    @classmethod
    def validate_finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("promotion threshold must be finite")
        return value


class TrustedShadowPerformanceSummaryV1(DomainModel):
    """Promotion-facing snapshot bound to matched immutable shadow evidence."""

    schema_version: str = Field(default="trusted_shadow_performance_summary_v1")
    summary_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    shadow_pair_id: str = Field(pattern=IDENTIFIER_PATTERN)
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    current_champion_version: str = Field(pattern=VERSION_PATTERN)
    candidate_version: str = Field(pattern=VERSION_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    champion_registration_hash: str = Field(pattern=HASH_PATTERN)
    challenger_registration_hash: str = Field(pattern=HASH_PATTERN)
    execution_contract_hash: str = Field(pattern=HASH_PATTERN)
    source_summary: MatchedShadowPerformanceSummaryV1
    daily_evidence_hashes: tuple[str, ...] = Field(min_length=1)
    materialized_evidence_hash: str = Field(pattern=HASH_PATTERN)
    forward_sessions: int = Field(gt=0)
    independent_trades: int = Field(ge=0)
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
    def validate_time(cls, value: datetime) -> datetime:
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
    def validate_finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("shadow promotion metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.created_at < self.data_available_cutoff:
            raise ValueError("shadow summary predates its data cutoff")
        if self.current_champion_version == self.candidate_version:
            raise ValueError("shadow summary versions must differ")
        if (
            self.source_summary.run_id != self.run_id
            or self.source_summary.shadow_pair_id != self.shadow_pair_id
            or self.source_summary.common_sessions != self.forward_sessions
        ):
            raise ValueError("source shadow summary binding mismatch")
        if self.forward_sessions != len(self.daily_evidence_hashes):
            raise ValueError("shadow session count does not match daily evidence")
        if len(self.daily_evidence_hashes) != len(
            set(self.daily_evidence_hashes)
        ):
            raise ValueError("shadow daily evidence hashes must be unique")
        if any(
            re.fullmatch(HASH_PATTERN, item) is None
            for item in self.daily_evidence_hashes
        ):
            raise ValueError("invalid shadow daily evidence hash")
        if (
            canonical_hash(self.daily_evidence_hashes)
            != self.materialized_evidence_hash
        ):
            raise ValueError("shadow materialized evidence hash mismatch")
        payload = self.model_dump(mode="python", exclude={"summary_hash"})
        if canonical_hash(payload) != self.summary_hash:
            raise ValueError("trusted shadow summary hash mismatch")
        return self


class PromotionEvidenceV1(DomainModel):
    """Host-produced immutable evidence; AI confidence is intentionally absent."""

    schema_version: str = Field(default="promotion_evidence_v1")
    evidence_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    current_champion_version: str = Field(pattern=VERSION_PATTERN)
    candidate_version: str = Field(pattern=VERSION_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    falsification_report_hash: str = Field(pattern=HASH_PATTERN)
    oos_result_hash: str = Field(pattern=HASH_PATTERN)
    shadow_summary_hash: str = Field(pattern=HASH_PATTERN)
    replay_hash: str = Field(pattern=HASH_PATTERN)
    common_oos_sessions: int = Field(ge=0)
    forward_sessions: int = Field(ge=0)
    independent_trades: int = Field(ge=0)
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
    data_available_cutoff: datetime
    created_at: datetime
    evidence_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("data_available_cutoff", "created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
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
    def validate_finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("promotion evidence metric must be finite")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.created_at < self.data_available_cutoff:
            raise ValueError("promotion evidence predates its data cutoff")
        if self.current_champion_version == self.candidate_version:
            raise ValueError("promotion evidence cannot compare one version to itself")
        payload = self.model_dump(mode="python", exclude={"evidence_hash"})
        if canonical_hash(payload) != self.evidence_hash:
            raise ValueError("promotion evidence hash mismatch")
        return self


class TrustedPromotionEvaluationV1(DomainModel):
    schema_version: str = Field(default="trusted_promotion_evaluation_v1")
    evaluation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    evidence_hash: str = Field(pattern=HASH_PATTERN)
    contract_hash: str = Field(pattern=HASH_PATTERN)
    decision: PromotionDecisionV1
    created_at: datetime
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
            raise ValueError("trusted promotion evaluation hash mismatch")
        return self


class ChampionDesignationV1(DomainModel):
    """One immutable human designation; it has no broker-routing authority."""

    schema_version: str = Field(default="champion_designation_v1")
    designation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    sequence: int = Field(ge=1)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    source_challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    trusted_evaluation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    trusted_evaluation_hash: str = Field(pattern=HASH_PATTERN)
    manual_approval_decision_id: str = Field(pattern=IDENTIFIER_PATTERN)
    manual_approval_decision_hash: str = Field(pattern=HASH_PATTERN)
    previous_designation_id: str | None = Field(
        default=None,
        pattern=IDENTIFIER_PATTERN,
    )
    expected_current_version: str = Field(pattern=VERSION_PATTERN)
    designated_by: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    designated_at: datetime
    automatic_promotion_enabled: Literal[False] = False
    real_order_routing: Literal[False] = False
    designation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("designated_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_designation(self) -> Self:
        if self.strategy_version == self.expected_current_version:
            raise ValueError("Champion designation must select a new version")
        if self.sequence == 1 and self.previous_designation_id is not None:
            raise ValueError("first Champion designation cannot have a predecessor")
        if self.sequence > 1 and self.previous_designation_id is None:
            raise ValueError("later Champion designation requires a predecessor")
        payload = self.model_dump(mode="python", exclude={"designation_hash"})
        if canonical_hash(payload) != self.designation_hash:
            raise ValueError("Champion designation hash mismatch")
        return self


def build_trusted_shadow_performance_summary(
    *,
    summary_id: str,
    challenger_id: str,
    current_champion_version: str,
    candidate_version: str,
    candidate_artifact_hash: str,
    champion_registration_hash: str,
    challenger_registration_hash: str,
    execution_contract_hash: str,
    source_summary: MatchedShadowPerformanceSummaryV1,
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
) -> TrustedShadowPerformanceSummaryV1:
    materialized_evidence_hash = canonical_hash(daily_evidence_hashes)
    payload = {
        "schema_version": "trusted_shadow_performance_summary_v1",
        "summary_id": summary_id,
        "challenger_id": challenger_id,
        "shadow_pair_id": source_summary.shadow_pair_id,
        "run_id": source_summary.run_id,
        "current_champion_version": current_champion_version,
        "candidate_version": candidate_version,
        "candidate_artifact_hash": candidate_artifact_hash,
        "champion_registration_hash": champion_registration_hash,
        "challenger_registration_hash": challenger_registration_hash,
        "execution_contract_hash": execution_contract_hash,
        "source_summary": source_summary,
        "daily_evidence_hashes": daily_evidence_hashes,
        "materialized_evidence_hash": materialized_evidence_hash,
        "forward_sessions": source_summary.common_sessions,
        "independent_trades": independent_trades,
        "annualized_net_excess_return_after_cost": (
            annualized_net_excess_return_after_cost
        ),
        "matched_annualized_difference": matched_annualized_difference,
        "economic_effect": economic_effect,
        "maximum_drawdown": maximum_drawdown,
        "tail_loss": tail_loss,
        "annualized_turnover": annualized_turnover,
        "estimated_capacity_usd": estimated_capacity_usd,
        "regime_pass_fraction": regime_pass_fraction,
        "runtime_error_rate": runtime_error_rate,
        "data_available_cutoff": require_aware_utc(data_available_cutoff),
        "created_at": require_aware_utc(created_at),
        "real_order_routing": False,
    }
    return TrustedShadowPerformanceSummaryV1.model_validate(
        {**payload, "summary_hash": canonical_hash(payload)}
    )


def build_promotion_evidence(
    *,
    evidence_id: str,
    challenger_id: str,
    current_champion_version: str,
    candidate_version: str,
    candidate_artifact_hash: str,
    falsification_report_hash: str,
    oos_result_hash: str,
    shadow_summary_hash: str,
    replay_hash: str,
    common_oos_sessions: int,
    forward_sessions: int,
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
    replay_reproducible: bool,
    mandatory_tests_passed: bool,
    data_available_cutoff: datetime,
    created_at: datetime,
) -> PromotionEvidenceV1:
    payload = {
        "schema_version": "promotion_evidence_v1",
        "evidence_id": evidence_id,
        "challenger_id": challenger_id,
        "current_champion_version": current_champion_version,
        "candidate_version": candidate_version,
        "candidate_artifact_hash": candidate_artifact_hash,
        "falsification_report_hash": falsification_report_hash,
        "oos_result_hash": oos_result_hash,
        "shadow_summary_hash": shadow_summary_hash,
        "replay_hash": replay_hash,
        "common_oos_sessions": common_oos_sessions,
        "forward_sessions": forward_sessions,
        "independent_trades": independent_trades,
        "annualized_net_excess_return_after_cost": (
            annualized_net_excess_return_after_cost
        ),
        "matched_annualized_difference": matched_annualized_difference,
        "economic_effect": economic_effect,
        "maximum_drawdown": maximum_drawdown,
        "tail_loss": tail_loss,
        "annualized_turnover": annualized_turnover,
        "estimated_capacity_usd": estimated_capacity_usd,
        "regime_pass_fraction": regime_pass_fraction,
        "runtime_error_rate": runtime_error_rate,
        "replay_reproducible": replay_reproducible,
        "mandatory_tests_passed": mandatory_tests_passed,
        "data_available_cutoff": require_aware_utc(data_available_cutoff),
        "created_at": require_aware_utc(created_at),
    }
    return PromotionEvidenceV1.model_validate(
        {**payload, "evidence_hash": canonical_hash(payload)}
    )


def evaluate_trusted_promotion_evidence(
    *,
    evidence: PromotionEvidenceV1,
    contract: PromotionEvaluationContractV1,
    created_at: datetime,
) -> TrustedPromotionEvaluationV1:
    timestamp = require_aware_utc(created_at)
    if timestamp < evidence.created_at:
        raise ValueError("promotion evaluation predates its evidence")
    criteria = {
        "minimum_independent_trades": (
            evidence.independent_trades >= contract.minimum_independent_trades
        ),
        "minimum_forward_period": (
            evidence.forward_sessions >= contract.minimum_forward_sessions
            and evidence.common_oos_sessions
            >= contract.minimum_common_oos_sessions
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
    }
    if tuple(criteria) != REQUIRED_PROMOTION_CRITERIA:
        raise RuntimeError("trusted promotion criteria order changed")
    contract_hash = canonical_hash(contract)
    evaluation_id = stable_id(
        "trusted-promotion-evaluation",
        evidence.challenger_id,
        evidence.evidence_hash,
        contract_hash,
    )
    decision = evaluate_promotion_eligibility(
        promotion_decision_id=stable_id(
            "promotion-decision",
            evaluation_id,
            evidence.replay_hash,
        ),
        challenger_id=evidence.challenger_id,
        current_champion_version=evidence.current_champion_version,
        candidate_version=evidence.candidate_version,
        criteria=criteria,
        replay_hash=evidence.replay_hash,
        created_at=timestamp,
    )
    payload = {
        "schema_version": "trusted_promotion_evaluation_v1",
        "evaluation_id": evaluation_id,
        "evidence_hash": evidence.evidence_hash,
        "contract_hash": contract_hash,
        "decision": decision,
        "created_at": timestamp,
    }
    return TrustedPromotionEvaluationV1.model_validate(
        {**payload, "evaluation_hash": canonical_hash(payload)}
    )


__all__ = [
    "ChampionDesignationV1",
    "PromotionEvaluationContractV1",
    "PromotionEvidenceV1",
    "TrustedPromotionEvaluationV1",
    "TrustedShadowPerformanceSummaryV1",
    "build_promotion_evidence",
    "build_trusted_shadow_performance_summary",
    "evaluate_trusted_promotion_evidence",
]
