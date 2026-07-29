from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc

HASH_PATTERN = r"^[a-f0-9]{64}$"
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$"
IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$"
SYMBOL_PATTERN = r"^[A-Z0-9][A-Z0-9._-]{0,15}$"


class ResearchCommanderKind(StrEnum):
    CODEX_SOL_MAX = "CODEX_SOL_MAX"
    WEBGPT_SOL_PRO = "WEBGPT_SOL_PRO"


class ResearchRole(StrEnum):
    WEB_SCOUT = "WEB_SCOUT"
    RESEARCH_COMMANDER = "RESEARCH_COMMANDER"
    CANDIDATE_BUILDER = "CANDIDATE_BUILDER"


class ResearchDecisionKind(StrEnum):
    NO_RESEARCH_CHANGE = "NO_RESEARCH_CHANGE"
    PROPOSE_NEW_STRATEGY = "PROPOSE_NEW_STRATEGY"
    PROPOSE_STRATEGY_REVISION = "PROPOSE_STRATEGY_REVISION"
    PROPOSE_FEATURE_REVISION = "PROPOSE_FEATURE_REVISION"
    PROPOSE_CALIBRATION_REVISION = "PROPOSE_CALIBRATION_REVISION"
    RETIRE_STRATEGY = "RETIRE_STRATEGY"
    REQUEST_MORE_EVIDENCE = "REQUEST_MORE_EVIDENCE"


class ChallengerStatus(StrEnum):
    PROPOSED = "PROPOSED"
    BUILD_FAILED = "BUILD_FAILED"
    TEST_FAILED = "TEST_FAILED"
    REPLAY_FAILED = "REPLAY_FAILED"
    OOS_REJECTED = "OOS_REJECTED"
    SHADOW_PENDING = "SHADOW_PENDING"
    SHADOW_RUNNING = "SHADOW_RUNNING"
    PROMOTION_ELIGIBLE = "PROMOTION_ELIGIBLE"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    RETIRED = "RETIRED"


class FalsificationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


class OosVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


OOS_REASON_CODES = frozenset(
    {
        "PREDECLARED_OOS_CRITERIA_PASSED",
        "INSUFFICIENT_COMMON_SESSIONS",
        "MINIMUM_ECONOMIC_EFFECT_NOT_MET",
        "COST_ADJUSTED_EFFECT_NOT_MET",
        "LOCKBOX_DATA_UNAVAILABLE",
        "LOCKBOX_DATA_HASH_MISMATCH",
        "LOCKBOX_DATA_INVALID",
        "LOCKBOX_DATA_INCOMPLETE",
        "LOCKBOX_DATA_DUPLICATE",
        "LOCKBOX_DATA_PIT_INVALID",
    }
)


class PromotionVerdict(StrEnum):
    INELIGIBLE = "INELIGIBLE"
    ELIGIBLE_REQUIRES_MANUAL_APPROVAL = "ELIGIBLE_REQUIRES_MANUAL_APPROVAL"
    MANUALLY_APPROVED = "MANUALLY_APPROVED"
    REJECTED = "REJECTED"


class CommanderSelectionV1(DomainModel):
    schema_version: str = Field(default="research_commander_selection_v1")
    selection_id: str = Field(pattern=IDENTIFIER_PATTERN)
    version: int = Field(ge=1)
    selected_commander: ResearchCommanderKind
    effective_at: datetime
    created_at: datetime
    config_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("effective_at", "created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)


class AvailableInstrumentV1(DomainModel):
    symbol: str = Field(pattern=SYMBOL_PATTERN)
    asset_class: str = Field(pattern=r"^(US_EQUITY|US_ETF)$")
    first_available_at: datetime
    last_available_at: datetime | None = None
    point_in_time_membership_available: bool
    daily_history_sessions: int = Field(ge=0)
    intraday_history_sessions: int = Field(ge=0)
    execution_supported: bool
    research_tags: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("first_available_at", "last_available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware_utc(value)


class AvailableDataCatalogV1(DomainModel):
    schema_version: str = Field(default="available_data_catalog_v1")
    catalog_id: str = Field(pattern=IDENTIFIER_PATTERN)
    as_of: datetime
    data_available_cutoff: datetime
    instruments: list[AvailableInstrumentV1] = Field(min_length=1)
    dataset_versions: dict[str, str]
    catalog_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("as_of", "data_available_cutoff", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_catalog(self) -> Self:
        if self.data_available_cutoff > self.as_of:
            raise ValueError("data_available_cutoff cannot exceed as_of")
        symbols = [item.symbol for item in self.instruments]
        if len(symbols) != len(set(symbols)):
            raise ValueError("available-data symbols must be unique")
        payload = self.model_dump(mode="python", exclude={"catalog_hash"})
        if canonical_hash(payload) != self.catalog_hash:
            raise ValueError("catalog_hash mismatch")
        return self


class AlgorithmProposalV1(DomainModel):
    schema_version: str = Field(default="algorithm_proposal_v1")
    proposal_id: str = Field(pattern=IDENTIFIER_PATTERN)
    hypothesis_id: str = Field(pattern=IDENTIFIER_PATTERN)
    hypothesis: str = Field(min_length=1, max_length=4000)
    economic_mechanism: str = Field(min_length=1, max_length=4000)
    why_current_model_failed: str = Field(min_length=1, max_length=4000)
    parent_strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    parent_strategy_version: str = Field(pattern=VERSION_PATTERN)
    proposed_strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    proposed_strategy_version: str = Field(pattern=VERSION_PATTERN)
    target_horizon: str = Field(min_length=1, max_length=120)
    target_universe: list[str] = Field(min_length=1, max_length=2000)
    required_data: list[str] = Field(min_length=1, max_length=200)
    feature_changes: list[str] = Field(default_factory=list, max_length=200)
    signal_formula_changes: list[str] = Field(default_factory=list, max_length=200)
    entry_rule_changes: list[str] = Field(default_factory=list, max_length=100)
    exit_rule_changes: list[str] = Field(default_factory=list, max_length=100)
    position_sizing_changes: list[str] = Field(default_factory=list, max_length=100)
    regime_activation_changes: list[str] = Field(default_factory=list, max_length=100)
    calibration_changes: list[str] = Field(default_factory=list, max_length=100)
    expected_edge_source: str = Field(min_length=1, max_length=3000)
    expected_failure_modes: list[str] = Field(min_length=1, max_length=100)
    invalidation_conditions: list[str] = Field(min_length=1, max_length=100)
    placebo_tests: list[str] = Field(min_length=1, max_length=100)
    stress_tests: list[str] = Field(min_length=1, max_length=100)
    minimum_economic_effect: dict[str, JsonValue]
    estimated_capacity: dict[str, JsonValue]
    estimated_turnover: dict[str, JsonValue]
    estimated_cost_sensitivity: dict[str, JsonValue]
    files_allowed_to_change: list[str] = Field(min_length=1, max_length=100)
    tests_required: list[str] = Field(min_length=1, max_length=200)
    evidence_source_ids: list[str] = Field(min_length=1, max_length=500)
    raw_confidence: float = Field(ge=0, le=1)
    proposal_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("target_universe", mode="after")
    @classmethod
    def normalize_universe(cls, value: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in value]
        if any(re.fullmatch(SYMBOL_PATTERN, symbol) is None for symbol in normalized):
            raise ValueError("target_universe contains an invalid US market symbol")
        if len(normalized) != len(set(normalized)):
            raise ValueError("target_universe symbols must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_proposal_hash(self) -> Self:
        if self.proposed_strategy_version == self.parent_strategy_version:
            raise ValueError(
                "proposed strategy version must differ from parent strategy version"
            )
        payload = self.model_dump(mode="python", exclude={"proposal_hash"})
        if canonical_hash(payload) != self.proposal_hash:
            raise ValueError("proposal_hash mismatch")
        return self


class ResearchRequestV1(DomainModel):
    schema_version: str = Field(default="research_request_v1")
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    selected_commander: ResearchCommanderKind
    commander_selection_id: str = Field(pattern=IDENTIFIER_PATTERN)
    commander_selection_version: int = Field(ge=1)
    created_at: datetime
    as_of: datetime
    data_available_cutoff: datetime
    expires_at: datetime
    source_snapshot_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    champion_version: str = Field(pattern=VERSION_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    champion_manifest: dict[str, JsonValue]
    active_challenger_manifests: list[dict[str, JsonValue]]
    strategy_performance_summary: dict[str, JsonValue]
    failure_case_clusters: list[dict[str, JsonValue]]
    regime_summary: dict[str, JsonValue]
    execution_cost_summary: dict[str, JsonValue]
    capacity_summary: dict[str, JsonValue]
    recent_market_evidence: list[dict[str, JsonValue]]
    recent_web_research: list[dict[str, JsonValue]]
    available_data_catalog: dict[str, JsonValue]
    allowed_change_scope: list[str] = Field(min_length=1)
    forbidden_change_scope: list[str] = Field(min_length=1)
    experiment_budget: dict[str, JsonValue]
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "created_at",
        "as_of",
        "data_available_cutoff",
        "expires_at",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if not self.created_at <= self.data_available_cutoff <= self.as_of:
            raise ValueError("request time ordering is invalid")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must follow created_at")
        payload = self.model_dump(mode="python", exclude={"context_manifest_hash"})
        if canonical_hash(payload) != self.context_manifest_hash:
            raise ValueError("context_manifest_hash mismatch")
        return self

    def assert_current_selection(
        self,
        current_selection: CommanderSelectionV1,
    ) -> None:
        if (
            current_selection.selection_id != self.commander_selection_id
            or current_selection.version != self.commander_selection_version
            or current_selection.selected_commander is not self.selected_commander
            or current_selection.created_at > self.created_at
            or current_selection.effective_at > self.created_at
        ):
            raise ValueError("STALE_SELECTION")


class ResearchDecisionV1(DomainModel):
    schema_version: str = Field(default="research_decision_v1")
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    research_cycle_id: str = Field(pattern=IDENTIFIER_PATTERN)
    selected_commander: ResearchCommanderKind
    commander_selection_id: str = Field(pattern=IDENTIFIER_PATTERN)
    commander_selection_version: int = Field(ge=1)
    source_snapshot_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    champion_version: str = Field(pattern=VERSION_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    context_manifest_hash: str = Field(pattern=HASH_PATTERN)
    request_schema_version: str = Field(default="research_request_v1")
    request_expires_at: datetime
    decision: ResearchDecisionKind
    rationale: str = Field(min_length=1, max_length=6000)
    proposal: AlgorithmProposalV1 | None = None
    requested_evidence: list[str] = Field(default_factory=list, max_length=100)
    created_at: datetime
    output_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("request_expires_at", "created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        proposal_kinds = {
            ResearchDecisionKind.PROPOSE_NEW_STRATEGY,
            ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
            ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
            ResearchDecisionKind.PROPOSE_CALIBRATION_REVISION,
        }
        if (self.decision in proposal_kinds) != (self.proposal is not None):
            raise ValueError("proposal presence does not match decision kind")
        if self.decision is ResearchDecisionKind.REQUEST_MORE_EVIDENCE:
            if not self.requested_evidence:
                raise ValueError("REQUEST_MORE_EVIDENCE requires requested_evidence")
        elif self.requested_evidence:
            raise ValueError("requested_evidence is limited to REQUEST_MORE_EVIDENCE")
        payload = self.model_dump(mode="python", exclude={"output_hash"})
        if canonical_hash(payload) != self.output_hash:
            raise ValueError("output_hash mismatch")
        return self

    def assert_bound_to(
        self,
        request: ResearchRequestV1,
        *,
        received_at: datetime,
        current_selection: CommanderSelectionV1,
    ) -> None:
        now = require_aware_utc(received_at)
        expected = (
            self.request_id == request.request_id
            and self.research_cycle_id == request.research_cycle_id
            and self.selected_commander is request.selected_commander
            and self.commander_selection_id == request.commander_selection_id
            and (
                self.commander_selection_version
                == request.commander_selection_version
            )
            and self.source_snapshot_commit == request.source_snapshot_commit
            and self.champion_version == request.champion_version
            and self.experiment_family == request.experiment_family
            and self.context_manifest_hash == request.context_manifest_hash
            and self.request_schema_version == request.schema_version
            and self.request_expires_at == request.expires_at
        )
        if not expected:
            raise ValueError("research decision binding mismatch")
        if now >= request.expires_at:
            raise ValueError("research request expired")
        request.assert_current_selection(current_selection)


class ChallengerManifestV1(DomainModel):
    schema_version: str = Field(default="challenger_manifest_v1")
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    parent_version: str = Field(pattern=VERSION_PATTERN)
    hypothesis_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    patch_hash: str = Field(pattern=HASH_PATTERN)
    proposal_hash: str = Field(pattern=HASH_PATTERN)
    code_hash: str = Field(pattern=HASH_PATTERN)
    config_hash: str = Field(pattern=HASH_PATTERN)
    test_manifest_hash: str = Field(pattern=HASH_PATTERN)
    created_by_commander: ResearchCommanderKind
    implemented_by_builder: str = Field(min_length=1, max_length=120)
    evidence_source_ids: list[str] = Field(min_length=1)
    required_data: list[str] = Field(min_length=1)
    decision_horizon: str = Field(min_length=1, max_length=120)
    execution_universe: list[str] = Field(min_length=1, max_length=2000)
    estimated_turnover: dict[str, JsonValue]
    estimated_capacity: dict[str, JsonValue]
    status: ChallengerStatus
    created_at: datetime
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("execution_universe", mode="after")
    @classmethod
    def normalize_universe(cls, value: list[str]) -> list[str]:
        normalized = [symbol.strip().upper() for symbol in value]
        if any(re.fullmatch(SYMBOL_PATTERN, symbol) is None for symbol in normalized):
            raise ValueError("execution_universe contains an invalid symbol")
        if len(normalized) != len(set(normalized)):
            raise ValueError("execution_universe symbols must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_manifest_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"manifest_hash"})
        if canonical_hash(payload) != self.manifest_hash:
            raise ValueError("manifest_hash mismatch")
        if self.strategy_version == self.parent_version:
            raise ValueError("a Challenger cannot overwrite its parent version")
        return self


class FalsificationTestResultV1(DomainModel):
    test_id: str = Field(pattern=IDENTIFIER_PATTERN)
    mandatory: bool
    status: FalsificationStatus
    reason_code: str = Field(pattern=IDENTIFIER_PATTERN)
    metrics: dict[str, JsonValue]
    result_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("falsification result hash mismatch")
        return self


class FalsificationReportV1(DomainModel):
    schema_version: str = Field(default="falsification_report_v1")
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    results: list[FalsificationTestResultV1] = Field(min_length=1)
    mandatory_passed: bool
    created_at: datetime
    report_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        expected = all(
            (not result.mandatory) or result.status is FalsificationStatus.PASS
            for result in self.results
        )
        if self.mandatory_passed != expected:
            raise ValueError("mandatory_passed does not match result set")
        ids = [result.test_id for result in self.results]
        if len(ids) != len(set(ids)):
            raise ValueError("falsification test IDs must be unique")
        payload = self.model_dump(mode="python", exclude={"report_hash"})
        if canonical_hash(payload) != self.report_hash:
            raise ValueError("falsification report hash mismatch")
        return self


class OosLockboxResultV1(DomainModel):
    schema_version: str = Field(default="oos_lockbox_result_v1")
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    submission_number: int = Field(ge=1)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    verdict: OosVerdict
    reason_codes: list[str] = Field(min_length=1, max_length=20)
    aggregate_statistics: dict[str, float] = Field(default_factory=dict, max_length=20)
    common_sessions: int = Field(ge=0)
    budget_consumed: int = Field(ge=1)
    evaluated_at: datetime
    result_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator("reason_codes", mode="after")
    @classmethod
    def validate_reason_codes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("OOS reason codes must be unique")
        if not set(value).issubset(OOS_REASON_CODES):
            raise ValueError("OOS result contains a non-approved reason code")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        allowed_aggregate_keys = {
            "mean_daily_difference",
            "annualized_difference",
            "newey_west_standard_error",
            "bootstrap_ci_lower",
            "bootstrap_ci_upper",
            "cost_sensitivity_0_bps_annualized_difference",
            "cost_sensitivity_5_bps_annualized_difference",
            "cost_sensitivity_10_bps_annualized_difference",
        }
        if not set(self.aggregate_statistics).issubset(allowed_aggregate_keys):
            raise ValueError("OOS result contains a non-approved aggregate")
        passed_code = "PREDECLARED_OOS_CRITERIA_PASSED"
        if self.verdict is OosVerdict.PASS and self.reason_codes != [passed_code]:
            raise ValueError("passed OOS result has invalid reason codes")
        if self.verdict is OosVerdict.FAIL and passed_code in self.reason_codes:
            raise ValueError("failed OOS result cannot contain the pass reason")
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("OOS result hash mismatch")
        return self


class OosBudgetReservationV1(DomainModel):
    schema_version: str = Field(default="oos_budget_reservation_v1")
    reservation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    submission_number: int = Field(ge=1)
    submission_ordinal: int = Field(ge=1)
    oos_budget_ordinal: int = Field(ge=1)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    idempotency_key: str = Field(pattern=IDENTIFIER_PATTERN)
    created_at: datetime
    expires_at: datetime
    reservation_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", "expires_at", mode="after")
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_reservation(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("OOS budget reservation must expire after creation")
        if self.submission_number != self.submission_ordinal:
            raise ValueError("OOS submission number must equal its reserved ordinal")
        payload = self.model_dump(mode="python", exclude={"reservation_hash"})
        if canonical_hash(payload) != self.reservation_hash:
            raise ValueError("OOS budget reservation hash mismatch")
        return self


class OosWorkerRequestV1(DomainModel):
    schema_version: str = Field(default="oos_worker_request_v1")
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    submission_number: int = Field(ge=1)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    reservation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    reservation_hash: str = Field(pattern=HASH_PATTERN)
    oos_budget_ordinal: int = Field(ge=1)
    dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    dataset_manifest_hash: str = Field(pattern=HASH_PATTERN)
    expected_source_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    expected_candidate_replay_hash: str = Field(pattern=HASH_PATTERN)
    expected_trusted_producer_version: Literal[
        "trusted_candidate_evaluation_v1"
    ]
    data_available_cutoff: datetime
    minimum_common_sessions: int = Field(ge=126)
    minimum_mean_daily_difference: float
    annualization_sessions: int = Field(ge=1, le=366)
    newey_west_lag: int = Field(ge=0, le=64)
    bootstrap_seed: int = Field(ge=0)
    bootstrap_block_length: int = Field(ge=1, le=252)
    bootstrap_samples: int = Field(ge=100, le=100_000)
    base_cost_bps: int = Field(ge=0, le=100)
    cost_sensitivity_bps: tuple[int, int, int] = (0, 5, 10)
    evaluated_at: datetime
    expires_at: datetime
    request_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "data_available_cutoff",
        "evaluated_at",
        "expires_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.data_available_cutoff > self.evaluated_at:
            raise ValueError("OOS data cutoff cannot follow evaluation time")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("OOS worker request is expired at evaluation time")
        if self.cost_sensitivity_bps != (0, 5, 10):
            raise ValueError("OOS cost sensitivity contract must be 0/5/10 bps")
        payload = self.model_dump(mode="python", exclude={"request_hash"})
        if canonical_hash(payload) != self.request_hash:
            raise ValueError("OOS worker request hash mismatch")
        return self


class OosWorkerResponseV1(DomainModel):
    schema_version: str = Field(default="oos_worker_response_v1")
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    reservation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    reservation_hash: str = Field(pattern=HASH_PATTERN)
    result: OosLockboxResultV1
    response_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"response_hash"})
        if canonical_hash(payload) != self.response_hash:
            raise ValueError("OOS worker response hash mismatch")
        return self


class PromotionDecisionV1(DomainModel):
    schema_version: str = Field(default="promotion_decision_v1")
    promotion_decision_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    current_champion_version: str = Field(pattern=VERSION_PATTERN)
    candidate_version: str = Field(pattern=VERSION_PATTERN)
    verdict: PromotionVerdict
    criteria: dict[str, bool]
    failed_reason_codes: list[str]
    replay_hash: str = Field(pattern=HASH_PATTERN)
    automatic_promotion_enabled: bool = False
    approved_by: str | None = Field(default=None, max_length=120)
    created_at: datetime
    decision_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.automatic_promotion_enabled:
            raise ValueError("automatic Champion promotion is unavailable")
        eligible = all(self.criteria.values())
        if (
            self.verdict is PromotionVerdict.ELIGIBLE_REQUIRES_MANUAL_APPROVAL
            and (not eligible or self.failed_reason_codes)
        ):
            raise ValueError("eligible promotion has unmet criteria")
        if (
            self.verdict is PromotionVerdict.MANUALLY_APPROVED
            and (not eligible or not self.approved_by)
        ):
            raise ValueError("manual promotion requires approver and all criteria")
        if self.verdict is PromotionVerdict.INELIGIBLE and eligible:
            raise ValueError("ineligible verdict requires a failed criterion")
        payload = self.model_dump(mode="python", exclude={"decision_hash"})
        if canonical_hash(payload) != self.decision_hash:
            raise ValueError("promotion decision hash mismatch")
        return self
