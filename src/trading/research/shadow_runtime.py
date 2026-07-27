from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import (
    DomainModel,
    Fill,
    LedgerEntry,
    NavSnapshot,
    OrderIntent,
)
from trading.domain.enums import (
    OrderSide,
)
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.ledger.journal import fill_entry, validate_entries
from trading.ledger.nav import calculate_nav
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    VERSION_PATTERN,
)
from trading.research.shadow import ShadowExecutionContract

USD_CASH = "USD_CASH"
SHADOW_RUNTIME_VERSION = "research_shadow_runtime_v1"


class ShadowArmRole(StrEnum):
    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"


class ShadowPaperParametersV1(DomainModel):
    schema_version: str = Field(default="shadow_paper_parameters_v1")
    contract_version: str = Field(pattern=VERSION_PATTERN)
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
    real_order_routing: Literal[False] = False

    @model_validator(mode="after")
    def validate_sensitivities(self) -> Self:
        if self.sensitivity_10_bps <= self.sensitivity_5_bps:
            raise ValueError("10 bp sensitivity must exceed 5 bp sensitivity")
        if (
            self.delay_penalty_bps >= self.basis_points_per_unit_return
            or self.sensitivity_10_bps >= self.basis_points_per_unit_return
        ):
            raise ValueError("paper cost basis points must remain below one unit")
        return self


class ShadowStrategyBindingV1(DomainModel):
    role: ShadowArmRole
    arm_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=30)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    artifact_hash: str = Field(pattern=HASH_PATTERN)


class MatchedShadowExecutionContractV1(DomainModel):
    market_input_manifest_hash: str = Field(pattern=HASH_PATTERN)
    decision_schedule_version: str = Field(pattern=VERSION_PATTERN)
    execution_scenario_version: str = Field(pattern=VERSION_PATTERN)
    cost_model_version: str = Field(pattern=VERSION_PATTERN)
    starting_capital_usd: Decimal = Field(gt=0)
    liquidity_policy_version: str = Field(pattern=VERSION_PATTERN)


class ShadowPairRuntimeSpecV1(DomainModel):
    schema_version: str = Field(default="shadow_pair_runtime_spec_v1")
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    shadow_pair_id: str = Field(pattern=IDENTIFIER_PATTERN)
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    champion: ShadowStrategyBindingV1
    challenger: ShadowStrategyBindingV1
    execution_contract: MatchedShadowExecutionContractV1
    execution_contract_hash: str = Field(pattern=HASH_PATTERN)
    paper_parameters: ShadowPaperParametersV1
    runtime_contract_hash: str = Field(pattern=HASH_PATTERN)
    code_version: str = Field(pattern=VERSION_PATTERN)
    created_at: datetime
    real_order_routing: Literal[False] = False
    spec_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("created_at", mode="after")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_spec(self) -> Self:
        if self.champion.role is not ShadowArmRole.CHAMPION:
            raise ValueError("Champion binding has wrong role")
        if self.challenger.role is not ShadowArmRole.CHALLENGER:
            raise ValueError("Challenger binding has wrong role")
        if self.champion.arm_id == self.challenger.arm_id:
            raise ValueError("matched shadow arms must be independent")
        if self.champion.strategy_version == self.challenger.strategy_version:
            raise ValueError("Challenger must use a distinct strategy version")
        if self.champion.artifact_hash == self.challenger.artifact_hash:
            raise ValueError("Challenger must use a distinct strategy artifact")
        if canonical_hash(self.execution_contract) != self.execution_contract_hash:
            raise ValueError("shadow execution contract hash mismatch")
        expected_runtime_hash = canonical_hash(
            {
                "execution_contract": self.execution_contract,
                "paper_parameters": self.paper_parameters,
            }
        )
        if expected_runtime_hash != self.runtime_contract_hash:
            raise ValueError("shadow runtime contract hash mismatch")
        payload = self.model_dump(mode="python", exclude={"spec_hash"})
        if canonical_hash(payload) != self.spec_hash:
            raise ValueError("shadow runtime spec hash mismatch")
        return self

    @property
    def starting_capital_usd(self) -> Decimal:
        return self.execution_contract.starting_capital_usd

    @property
    def market_input_manifest_hash(self) -> str:
        return self.execution_contract.market_input_manifest_hash

    def binding_for(self, role: ShadowArmRole) -> ShadowStrategyBindingV1:
        return self.champion if role is ShadowArmRole.CHAMPION else self.challenger


class ShadowPositionV1(DomainModel):
    instrument_id: str = Field(pattern=IDENTIFIER_PATTERN)
    quantity: Decimal = Field(gt=0)


class ShadowArmStateV1(DomainModel):
    schema_version: str = Field(default="shadow_arm_state_v1")
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    role: ShadowArmRole
    arm_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=30)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    artifact_hash: str = Field(pattern=HASH_PATTERN)
    sequence: int = Field(ge=0)
    cash_usd: Decimal = Field(ge=0)
    positions: tuple[ShadowPositionV1, ...]
    last_nav_usd: Decimal = Field(gt=0)
    cumulative_turnover_usd: Decimal = Field(ge=0)
    cumulative_commission_usd: Decimal = Field(ge=0)
    cumulative_execution_cost_usd: Decimal = Field(ge=0)
    cumulative_sensitivity_5bp_usd: Decimal = Field(ge=0)
    cumulative_sensitivity_10bp_usd: Decimal = Field(ge=0)
    as_of: datetime
    source_cycle_hash: str = Field(pattern=HASH_PATTERN)
    state_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("as_of", mode="after")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        instruments = tuple(item.instrument_id for item in self.positions)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("shadow positions must be unique and sorted")
        payload = self.model_dump(mode="python", exclude={"state_hash"})
        if canonical_hash(payload) != self.state_hash:
            raise ValueError("shadow state hash mismatch")
        return self

    def position_map(self) -> dict[str, Decimal]:
        return {item.instrument_id: item.quantity for item in self.positions}


class ShadowTargetWeightV1(DomainModel):
    instrument_id: str = Field(pattern=IDENTIFIER_PATTERN)
    weight: Decimal = Field(ge=0, le=1)


class ShadowTargetDecisionV1(DomainModel):
    schema_version: str = Field(default="shadow_target_decision_v1")
    target_id: str = Field(pattern=IDENTIFIER_PATTERN)
    run_id: str = Field(pattern=IDENTIFIER_PATTERN)
    role: ShadowArmRole
    arm_id: str = Field(pattern=IDENTIFIER_PATTERN, max_length=30)
    strategy_id: str = Field(pattern=IDENTIFIER_PATTERN)
    strategy_version: str = Field(pattern=VERSION_PATTERN)
    artifact_hash: str = Field(pattern=HASH_PATTERN)
    decision_time: datetime
    signal_data_cutoff: datetime
    valid_until: datetime
    market_input_manifest_hash: str = Field(pattern=HASH_PATTERN)
    quote_manifest_hash: str = Field(pattern=HASH_PATTERN)
    runtime_contract_hash: str = Field(pattern=HASH_PATTERN)
    target_weights: tuple[ShadowTargetWeightV1, ...] = Field(min_length=1)
    target_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "decision_time",
        "signal_data_cutoff",
        "valid_until",
        mode="after",
    )
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.signal_data_cutoff > self.decision_time:
            raise ValueError("shadow target uses future signal data")
        if self.valid_until <= self.decision_time:
            raise ValueError("shadow target validity must follow decision time")
        instruments = tuple(item.instrument_id for item in self.target_weights)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("shadow target instruments must be unique and sorted")
        if USD_CASH not in instruments:
            raise ValueError("shadow target must include USD_CASH")
        if sum(item.weight for item in self.target_weights) != Decimal("1"):
            raise ValueError("shadow target weights must sum to one")
        payload = self.model_dump(mode="python", exclude={"target_hash"})
        if canonical_hash(payload) != self.target_hash:
            raise ValueError("shadow target hash mismatch")
        return self

    def weight_map(self) -> dict[str, Decimal]:
        return {item.instrument_id: item.weight for item in self.target_weights}


class ShadowQuoteV1(DomainModel):
    quote_id: str = Field(pattern=IDENTIFIER_PATTERN)
    instrument_id: str = Field(pattern=IDENTIFIER_PATTERN)
    event_time: datetime
    available_at: datetime
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    bid_size_shares: Decimal = Field(gt=0)
    ask_size_shares: Decimal = Field(gt=0)
    adv_shares: Decimal = Field(gt=0)
    source_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("event_time", "available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_quote(self) -> Self:
        if self.ask_price < self.bid_price:
            raise ValueError("crossed quote is not executable")
        if self.available_at < self.event_time:
            raise ValueError("quote cannot be available before its event")
        return self

    @property
    def midpoint(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal("2")


class MatchedQuoteBundleV1(DomainModel):
    schema_version: str = Field(default="matched_shadow_quote_bundle_v1")
    market_input_manifest_hash: str = Field(pattern=HASH_PATTERN)
    quote_manifest_hash: str = Field(pattern=HASH_PATTERN)
    as_of: datetime
    quotes: tuple[ShadowQuoteV1, ...] = Field(min_length=1)
    bundle_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("as_of", mode="after")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        instruments = tuple(item.instrument_id for item in self.quotes)
        if instruments != tuple(sorted(set(instruments))):
            raise ValueError("matched quotes must be unique and sorted")
        if canonical_hash(self.quotes) != self.quote_manifest_hash:
            raise ValueError("quote manifest hash mismatch")
        payload = self.model_dump(mode="python", exclude={"bundle_hash"})
        if canonical_hash(payload) != self.bundle_hash:
            raise ValueError("quote bundle hash mismatch")
        return self

    def quote_map(self) -> dict[str, ShadowQuoteV1]:
        return {item.instrument_id: item for item in self.quotes}


class ShadowFillCostV1(DomainModel):
    fill_id: str = Field(pattern=IDENTIFIER_PATTERN)
    base_execution_cost_usd: Decimal = Field(ge=0)
    sensitivity_5bp_cost_usd: Decimal = Field(ge=0)
    sensitivity_10bp_cost_usd: Decimal = Field(ge=0)


class ShadowDailyArmSummaryV1(DomainModel):
    role: ShadowArmRole
    arm_id: str
    as_of: datetime
    start_nav_usd: Decimal = Field(gt=0)
    end_nav_usd: Decimal = Field(gt=0)
    net_return: Decimal
    turnover_usd: Decimal = Field(ge=0)
    turnover_ratio: Decimal = Field(ge=0)
    commission_usd: Decimal = Field(ge=0)
    execution_cost_usd: Decimal = Field(ge=0)
    sensitivity_5bp_cost_usd: Decimal = Field(ge=0)
    sensitivity_10bp_cost_usd: Decimal = Field(ge=0)
    cash_weight: Decimal = Field(ge=0, le=1)
    exposures: dict[str, Decimal]

    @field_validator("as_of", mode="after")
    @classmethod
    def validate_as_of(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @model_validator(mode="after")
    def validate_exposures(self) -> Self:
        if any(value < 0 for value in self.exposures.values()):
            raise ValueError("shadow exposures must be long-only")
        total = self.cash_weight + sum(self.exposures.values(), Decimal("0"))
        if abs(total - Decimal("1")) > Decimal("0.000001"):
            raise ValueError("shadow cash and risky exposures must sum to one")
        return self


class ShadowArmCycleResultV1(DomainModel):
    role: ShadowArmRole
    target: ShadowTargetDecisionV1
    orders: tuple[OrderIntent, ...]
    fills: tuple[Fill, ...]
    fill_costs: tuple[ShadowFillCostV1, ...]
    ledger_entries: tuple[LedgerEntry, ...]
    nav: NavSnapshot
    next_state: ShadowArmStateV1
    daily_summary: ShadowDailyArmSummaryV1


class MatchedShadowCycleResultV1(DomainModel):
    schema_version: str = Field(default="matched_shadow_cycle_result_v1")
    run_id: str
    shadow_pair_id: str
    runtime_contract_hash: str = Field(pattern=HASH_PATTERN)
    quote_bundle: MatchedQuoteBundleV1
    champion: ShadowArmCycleResultV1
    challenger: ShadowArmCycleResultV1
    matched_daily_return_difference: Decimal
    real_order_routing: Literal[False] = False
    result_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.champion.role is not ShadowArmRole.CHAMPION:
            raise ValueError("matched result Champion role mismatch")
        if self.challenger.role is not ShadowArmRole.CHALLENGER:
            raise ValueError("matched result Challenger role mismatch")
        expected = (
            self.challenger.daily_summary.net_return
            - self.champion.daily_summary.net_return
        )
        if self.matched_daily_return_difference != expected:
            raise ValueError("matched daily return difference mismatch")
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("matched shadow cycle hash mismatch")
        return self


class MatchedShadowPerformanceSummaryV1(DomainModel):
    schema_version: str = Field(default="matched_shadow_performance_summary_v1")
    run_id: str
    shadow_pair_id: str
    common_sessions: int = Field(gt=0)
    champion_cumulative_return: Decimal
    challenger_cumulative_return: Decimal
    mean_matched_daily_return_difference: Decimal
    champion_turnover_usd: Decimal = Field(ge=0)
    challenger_turnover_usd: Decimal = Field(ge=0)
    champion_commission_usd: Decimal = Field(ge=0)
    challenger_commission_usd: Decimal = Field(ge=0)
    champion_execution_cost_usd: Decimal = Field(ge=0)
    challenger_execution_cost_usd: Decimal = Field(ge=0)
    champion_sensitivity_5bp_cost_usd: Decimal = Field(ge=0)
    challenger_sensitivity_5bp_cost_usd: Decimal = Field(ge=0)
    champion_sensitivity_10bp_cost_usd: Decimal = Field(ge=0)
    challenger_sensitivity_10bp_cost_usd: Decimal = Field(ge=0)
    champion_average_exposures: dict[str, Decimal]
    challenger_average_exposures: dict[str, Decimal]
    replay_hash: str = Field(pattern=HASH_PATTERN)
    profitability_claimed: Literal[False] = False
    summary_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"summary_hash"})
        if canonical_hash(payload) != self.summary_hash:
            raise ValueError("matched shadow summary hash mismatch")
        return self


def build_shadow_pair_runtime_spec(
    *,
    shadow_pair_id: str,
    challenger_id: str,
    champion: ShadowStrategyBindingV1,
    challenger: ShadowStrategyBindingV1,
    execution_contract: ShadowExecutionContract,
    paper_parameters: ShadowPaperParametersV1,
    code_version: str,
    created_at: datetime,
) -> ShadowPairRuntimeSpecV1:
    contract_model = MatchedShadowExecutionContractV1(
        market_input_manifest_hash=execution_contract.market_input_manifest_hash,
        decision_schedule_version=execution_contract.decision_schedule_version,
        execution_scenario_version=execution_contract.execution_scenario_version,
        cost_model_version=execution_contract.cost_model_version,
        starting_capital_usd=Decimal(execution_contract.starting_capital_usd),
        liquidity_policy_version=execution_contract.liquidity_policy_version,
    )
    execution_contract_hash = canonical_hash(contract_model)
    runtime_contract_hash = canonical_hash(
        {
            "execution_contract": contract_model,
            "paper_parameters": paper_parameters,
        }
    )
    run_id = stable_id(
        "research-shadow-run",
        shadow_pair_id,
        champion.artifact_hash,
        challenger.artifact_hash,
        runtime_contract_hash,
    )
    payload = {
        "schema_version": "shadow_pair_runtime_spec_v1",
        "run_id": run_id,
        "shadow_pair_id": shadow_pair_id,
        "challenger_id": challenger_id,
        "champion": champion,
        "challenger": challenger,
        "execution_contract": contract_model,
        "execution_contract_hash": execution_contract_hash,
        "paper_parameters": paper_parameters,
        "runtime_contract_hash": runtime_contract_hash,
        "code_version": code_version,
        "created_at": require_aware_utc(created_at),
        "real_order_routing": False,
    }
    return ShadowPairRuntimeSpecV1.model_validate(
        {**payload, "spec_hash": canonical_hash(payload)}
    )


def build_shadow_target_decision(
    *,
    target_id: str,
    spec: ShadowPairRuntimeSpecV1,
    role: ShadowArmRole,
    decision_time: datetime,
    signal_data_cutoff: datetime,
    valid_until: datetime,
    quote_manifest_hash: str,
    target_weights: dict[str, Decimal],
) -> ShadowTargetDecisionV1:
    binding = spec.binding_for(role)
    weights = tuple(
        ShadowTargetWeightV1(instrument_id=symbol, weight=weight)
        for symbol, weight in sorted(target_weights.items())
    )
    payload = {
        "schema_version": "shadow_target_decision_v1",
        "target_id": target_id,
        "run_id": spec.run_id,
        "role": role,
        "arm_id": binding.arm_id,
        "strategy_id": binding.strategy_id,
        "strategy_version": binding.strategy_version,
        "artifact_hash": binding.artifact_hash,
        "decision_time": require_aware_utc(decision_time),
        "signal_data_cutoff": require_aware_utc(signal_data_cutoff),
        "valid_until": require_aware_utc(valid_until),
        "market_input_manifest_hash": spec.market_input_manifest_hash,
        "quote_manifest_hash": quote_manifest_hash,
        "runtime_contract_hash": spec.runtime_contract_hash,
        "target_weights": weights,
    }
    return ShadowTargetDecisionV1.model_validate(
        {**payload, "target_hash": canonical_hash(payload)}
    )


def build_matched_quote_bundle(
    *,
    market_input_manifest_hash: str,
    as_of: datetime,
    quotes: tuple[ShadowQuoteV1, ...],
) -> MatchedQuoteBundleV1:
    ordered = tuple(sorted(quotes, key=lambda item: item.instrument_id))
    quote_manifest_hash = canonical_hash(ordered)
    payload = {
        "schema_version": "matched_shadow_quote_bundle_v1",
        "market_input_manifest_hash": market_input_manifest_hash,
        "quote_manifest_hash": quote_manifest_hash,
        "as_of": require_aware_utc(as_of),
        "quotes": ordered,
    }
    return MatchedQuoteBundleV1.model_validate(
        {**payload, "bundle_hash": canonical_hash(payload)}
    )


def build_initial_shadow_state(
    *,
    spec: ShadowPairRuntimeSpecV1,
    role: ShadowArmRole,
) -> ShadowArmStateV1:
    binding = spec.binding_for(role)
    payload = {
        "schema_version": "shadow_arm_state_v1",
        "run_id": spec.run_id,
        "role": role,
        "arm_id": binding.arm_id,
        "strategy_id": binding.strategy_id,
        "strategy_version": binding.strategy_version,
        "artifact_hash": binding.artifact_hash,
        "sequence": 0,
        "cash_usd": spec.starting_capital_usd,
        "positions": (),
        "last_nav_usd": spec.starting_capital_usd,
        "cumulative_turnover_usd": Decimal("0"),
        "cumulative_commission_usd": Decimal("0"),
        "cumulative_execution_cost_usd": Decimal("0"),
        "cumulative_sensitivity_5bp_usd": Decimal("0"),
        "cumulative_sensitivity_10bp_usd": Decimal("0"),
        "as_of": spec.created_at,
        "source_cycle_hash": canonical_hash(
            {"event": "INITIAL_CAPITAL", "spec_hash": spec.spec_hash}
        ),
    }
    return ShadowArmStateV1.model_validate(
        {**payload, "state_hash": canonical_hash(payload)}
    )


def execute_matched_shadow_cycle(
    *,
    spec: ShadowPairRuntimeSpecV1,
    champion_state: ShadowArmStateV1,
    challenger_state: ShadowArmStateV1,
    champion_target: ShadowTargetDecisionV1,
    challenger_target: ShadowTargetDecisionV1,
    quote_bundle: MatchedQuoteBundleV1,
) -> MatchedShadowCycleResultV1:
    if spec.real_order_routing or spec.paper_parameters.real_order_routing:
        raise ValueError("real broker routing is unavailable")
    _validate_cycle_common_inputs(
        spec=spec,
        states=(champion_state, challenger_state),
        targets=(champion_target, challenger_target),
        quote_bundle=quote_bundle,
    )
    champion = _execute_arm_cycle(
        spec=spec,
        state=champion_state,
        target=champion_target,
        quote_bundle=quote_bundle,
    )
    challenger = _execute_arm_cycle(
        spec=spec,
        state=challenger_state,
        target=challenger_target,
        quote_bundle=quote_bundle,
    )
    payload = {
        "schema_version": "matched_shadow_cycle_result_v1",
        "run_id": spec.run_id,
        "shadow_pair_id": spec.shadow_pair_id,
        "runtime_contract_hash": spec.runtime_contract_hash,
        "quote_bundle": quote_bundle,
        "champion": champion,
        "challenger": challenger,
        "matched_daily_return_difference": (
            challenger.daily_summary.net_return
            - champion.daily_summary.net_return
        ),
        "real_order_routing": False,
    }
    return MatchedShadowCycleResultV1.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )


def summarize_matched_shadow_results(
    *,
    spec: ShadowPairRuntimeSpecV1,
    results: tuple[MatchedShadowCycleResultV1, ...],
    replay_hash: str,
) -> MatchedShadowPerformanceSummaryV1:
    if not results:
        raise ValueError("matched shadow summary requires at least one session")
    ordered = tuple(sorted(results, key=lambda item: item.quote_bundle.as_of))
    if any(
        item.run_id != spec.run_id
        or item.shadow_pair_id != spec.shadow_pair_id
        or item.runtime_contract_hash != spec.runtime_contract_hash
        for item in ordered
    ):
        raise ValueError("matched shadow result binding mismatch")
    champion_summaries = tuple(item.champion.daily_summary for item in ordered)
    challenger_summaries = tuple(item.challenger.daily_summary for item in ordered)
    payload = {
        "schema_version": "matched_shadow_performance_summary_v1",
        "run_id": spec.run_id,
        "shadow_pair_id": spec.shadow_pair_id,
        "common_sessions": len(ordered),
        "champion_cumulative_return": _cumulative_return(champion_summaries),
        "challenger_cumulative_return": _cumulative_return(challenger_summaries),
        "mean_matched_daily_return_difference": (
            sum(
                (
                    item.matched_daily_return_difference
                    for item in ordered
                ),
                Decimal("0"),
            )
            / Decimal(len(ordered))
        ),
        "champion_turnover_usd": sum(
            (item.turnover_usd for item in champion_summaries),
            Decimal("0"),
        ),
        "challenger_turnover_usd": sum(
            (item.turnover_usd for item in challenger_summaries),
            Decimal("0"),
        ),
        "champion_commission_usd": sum(
            (item.commission_usd for item in champion_summaries),
            Decimal("0"),
        ),
        "challenger_commission_usd": sum(
            (item.commission_usd for item in challenger_summaries),
            Decimal("0"),
        ),
        "champion_execution_cost_usd": sum(
            (item.execution_cost_usd for item in champion_summaries),
            Decimal("0"),
        ),
        "challenger_execution_cost_usd": sum(
            (item.execution_cost_usd for item in challenger_summaries),
            Decimal("0"),
        ),
        "champion_sensitivity_5bp_cost_usd": sum(
            (item.sensitivity_5bp_cost_usd for item in champion_summaries),
            Decimal("0"),
        ),
        "challenger_sensitivity_5bp_cost_usd": sum(
            (item.sensitivity_5bp_cost_usd for item in challenger_summaries),
            Decimal("0"),
        ),
        "champion_sensitivity_10bp_cost_usd": sum(
            (item.sensitivity_10bp_cost_usd for item in champion_summaries),
            Decimal("0"),
        ),
        "challenger_sensitivity_10bp_cost_usd": sum(
            (item.sensitivity_10bp_cost_usd for item in challenger_summaries),
            Decimal("0"),
        ),
        "champion_average_exposures": _average_exposures(champion_summaries),
        "challenger_average_exposures": _average_exposures(challenger_summaries),
        "replay_hash": replay_hash,
        "profitability_claimed": False,
    }
    return MatchedShadowPerformanceSummaryV1.model_validate(
        {**payload, "summary_hash": canonical_hash(payload)}
    )


def _validate_cycle_common_inputs(
    *,
    spec: ShadowPairRuntimeSpecV1,
    states: tuple[ShadowArmStateV1, ShadowArmStateV1],
    targets: tuple[ShadowTargetDecisionV1, ShadowTargetDecisionV1],
    quote_bundle: MatchedQuoteBundleV1,
) -> None:
    expected_roles = (ShadowArmRole.CHAMPION, ShadowArmRole.CHALLENGER)
    if tuple(state.role for state in states) != expected_roles:
        raise ValueError("matched shadow states must be Champion then Challenger")
    if tuple(target.role for target in targets) != expected_roles:
        raise ValueError("matched shadow targets must be Champion then Challenger")
    if targets[0].decision_time != targets[1].decision_time:
        raise ValueError("matched shadow decisions require a common timestamp")
    if targets[0].signal_data_cutoff != targets[1].signal_data_cutoff:
        raise ValueError("matched shadow decisions require a common signal cutoff")
    if targets[0].valid_until != targets[1].valid_until:
        raise ValueError("matched shadow decisions require common validity")
    if quote_bundle.as_of >= targets[0].valid_until:
        raise ValueError("matched shadow quote is outside decision validity")
    if quote_bundle.market_input_manifest_hash != spec.market_input_manifest_hash:
        raise ValueError("market input manifest differs from registered contract")
    for state, target, role in zip(states, targets, expected_roles, strict=True):
        binding = spec.binding_for(role)
        if (
            state.run_id != spec.run_id
            or state.role is not role
            or state.arm_id != binding.arm_id
            or state.strategy_id != binding.strategy_id
            or state.strategy_version != binding.strategy_version
            or state.artifact_hash != binding.artifact_hash
        ):
            raise ValueError("shadow state is not bound to its exact strategy artifact")
        if target.decision_time < state.as_of or quote_bundle.as_of <= state.as_of:
            raise ValueError("shadow cycle cannot predate its persisted arm state")
        if (
            target.run_id != spec.run_id
            or target.role is not role
            or target.arm_id != binding.arm_id
            or target.strategy_id != binding.strategy_id
            or target.strategy_version != binding.strategy_version
            or target.artifact_hash != binding.artifact_hash
            or target.runtime_contract_hash != spec.runtime_contract_hash
            or target.market_input_manifest_hash
            != quote_bundle.market_input_manifest_hash
            or target.quote_manifest_hash != quote_bundle.quote_manifest_hash
        ):
            raise ValueError("shadow target is not host-bound to its strategy and inputs")


def _execute_arm_cycle(
    *,
    spec: ShadowPairRuntimeSpecV1,
    state: ShadowArmStateV1,
    target: ShadowTargetDecisionV1,
    quote_bundle: MatchedQuoteBundleV1,
) -> ShadowArmCycleResultV1:
    parameters = spec.paper_parameters
    quotes = quote_bundle.quote_map()
    positions = state.position_map()
    weights = target.weight_map()
    risky_symbols = (set(positions) | (set(weights) - {USD_CASH}))
    missing_quotes = sorted(risky_symbols - set(quotes))
    if missing_quotes:
        raise ValueError(f"missing matched quotes: {missing_quotes}")
    for symbol in sorted(risky_symbols):
        quote = quotes[symbol]
        if quote.available_at <= target.decision_time:
            raise ValueError("execution quote must follow target creation")
        if quote.available_at > quote_bundle.as_of:
            raise ValueError("execution quote was unavailable at execution time")
        age = (quote_bundle.as_of - quote.event_time).total_seconds()
        if age < 0 or age > parameters.maximum_quote_age_seconds:
            raise ValueError("execution quote is stale")

    decision_nav = state.cash_usd + sum(
        (quantity * quotes[symbol].midpoint for symbol, quantity in positions.items()),
        Decimal("0"),
    )
    if decision_nav <= 0:
        raise ValueError("shadow arm NAV must remain positive")
    desired_quantities = {
        symbol: _round_down(
            decision_nav * weight / quotes[symbol].midpoint,
            parameters.quantity_quantum,
        )
        for symbol, weight in weights.items()
        if symbol != USD_CASH and weight > 0
    }
    orders: list[OrderIntent] = []
    fills: list[Fill] = []
    fill_costs: list[ShadowFillCostV1] = []
    entries: list[LedgerEntry] = []
    cash = state.cash_usd
    mutable_positions = dict(positions)

    planned: list[tuple[OrderSide, str, Decimal]] = []
    for symbol in sorted(risky_symbols):
        current = mutable_positions.get(symbol, Decimal("0"))
        desired = desired_quantities.get(symbol, Decimal("0"))
        delta = desired - current
        if delta == 0:
            continue
        side = OrderSide.BUY if delta > 0 else OrderSide.SELL
        quantity = abs(delta)
        if quantity * quotes[symbol].midpoint < parameters.minimum_order_notional_usd:
            continue
        planned.append((side, symbol, quantity))
    planned.sort(key=lambda item: (item[0] is OrderSide.BUY, item[1]))

    for side, symbol, requested_quantity in planned:
        quote = quotes[symbol]
        order = _build_order_intent(
            spec=spec,
            target=target,
            side=side,
            symbol=symbol,
            quantity=requested_quantity,
        )
        orders.append(order)
        fill_quantity = _fillable_quantity(
            requested_quantity=requested_quantity,
            side=side,
            quote=quote,
            cash_usd=cash,
            parameters=parameters,
        )
        if fill_quantity <= 0:
            continue
        fill_price = _fill_price(
            side=side,
            quote=quote,
            parameters=parameters,
        )
        notional = fill_quantity * fill_price
        commission = _commission(
            notional=notional,
            parameters=parameters,
        )
        if side is OrderSide.BUY and notional + commission > cash:
            raise ValueError("paper BUY would make cash negative")
        if side is OrderSide.SELL and fill_quantity > mutable_positions.get(
            symbol,
            Decimal("0"),
        ):
            raise ValueError("paper SELL would create a short position")
        fill = Fill(
            fill_id=stable_id(
                "research-shadow-fill",
                spec.run_id,
                target.target_hash,
                order.order_intent_id,
                quote.quote_id,
                fill_quantity,
                fill_price,
            ),
            order_intent_id=order.order_intent_id,
            arm_id=target.arm_id,
            symbol=symbol,
            side=side,
            quantity=fill_quantity,
            price=fill_price,
            commission_usd=commission,
            execution_scenario_id=(
                spec.execution_contract.execution_scenario_version
            ),
            effective_at=quote_bundle.as_of,
            created_at=quote_bundle.as_of,
        )
        fills.append(fill)
        entries.append(fill_entry(fill))
        if side is OrderSide.BUY:
            cash -= notional + commission
            mutable_positions[symbol] = (
                mutable_positions.get(symbol, Decimal("0")) + fill_quantity
            )
        else:
            cash += notional - commission
            residual = mutable_positions[symbol] - fill_quantity
            if residual < 0:
                raise ValueError("paper fill created a short position")
            if residual == 0:
                mutable_positions.pop(symbol)
            else:
                mutable_positions[symbol] = residual
        fill_costs.append(
            _fill_cost(
                fill=fill,
                midpoint=quote.midpoint,
                parameters=parameters,
            )
        )

    validate_entries(entries)
    if cash < 0 or any(quantity < 0 for quantity in mutable_positions.values()):
        raise ValueError("shadow execution violated long-only cash constraints")
    nav = calculate_nav(
        arm_id=target.arm_id,
        as_of=quote_bundle.as_of,
        cash_usd=cash,
        positions=mutable_positions,
        prices={
            symbol: quotes[symbol].midpoint for symbol in mutable_positions
        },
    )
    turnover = sum(
        (fill.quantity * fill.price for fill in fills),
        Decimal("0"),
    )
    commission = sum(
        (fill.commission_usd for fill in fills),
        Decimal("0"),
    )
    execution_cost = sum(
        (item.base_execution_cost_usd for item in fill_costs),
        Decimal("0"),
    )
    sensitivity_5 = sum(
        (item.sensitivity_5bp_cost_usd for item in fill_costs),
        Decimal("0"),
    )
    sensitivity_10 = sum(
        (item.sensitivity_10bp_cost_usd for item in fill_costs),
        Decimal("0"),
    )
    next_state = _build_next_state(
        state=state,
        cash_usd=cash,
        positions=mutable_positions,
        nav_usd=nav.nav_usd,
        turnover_usd=turnover,
        commission_usd=commission,
        execution_cost_usd=execution_cost,
        sensitivity_5bp_usd=sensitivity_5,
        sensitivity_10bp_usd=sensitivity_10,
        as_of=quote_bundle.as_of,
        source_cycle_hash=canonical_hash(
            {
                "target_hash": target.target_hash,
                "quote_bundle_hash": quote_bundle.bundle_hash,
            }
        ),
    )
    exposures = {
        symbol: quantity * quotes[symbol].midpoint / nav.nav_usd
        for symbol, quantity in sorted(mutable_positions.items())
    }
    cash_weight = nav.cash_usd / nav.nav_usd
    if (
        abs(cash_weight + sum(exposures.values(), Decimal("0")) - Decimal("1"))
        > parameters.weight_tolerance
    ):
        raise ValueError("shadow NAV exposures do not reconcile")
    daily = ShadowDailyArmSummaryV1(
        role=target.role,
        arm_id=target.arm_id,
        as_of=quote_bundle.as_of,
        start_nav_usd=state.last_nav_usd,
        end_nav_usd=nav.nav_usd,
        net_return=nav.nav_usd / state.last_nav_usd - Decimal("1"),
        turnover_usd=turnover,
        turnover_ratio=turnover / state.last_nav_usd,
        commission_usd=commission,
        execution_cost_usd=execution_cost,
        sensitivity_5bp_cost_usd=sensitivity_5,
        sensitivity_10bp_cost_usd=sensitivity_10,
        cash_weight=cash_weight,
        exposures=exposures,
    )
    return ShadowArmCycleResultV1(
        role=target.role,
        target=target,
        orders=tuple(orders),
        fills=tuple(fills),
        fill_costs=tuple(fill_costs),
        ledger_entries=tuple(entries),
        nav=nav,
        next_state=next_state,
        daily_summary=daily,
    )


def _build_order_intent(
    *,
    spec: ShadowPairRuntimeSpecV1,
    target: ShadowTargetDecisionV1,
    side: OrderSide,
    symbol: str,
    quantity: Decimal,
) -> OrderIntent:
    order_id = stable_id(
        "research-shadow-order",
        spec.run_id,
        target.target_hash,
        symbol,
        side.value,
        quantity,
    )
    return OrderIntent(
        order_intent_id=order_id,
        arm_id=target.arm_id,
        portfolio_decision_id=stable_id(
            "research-shadow-decision",
            target.target_hash,
        ),
        risk_decision_id=stable_id(
            "research-shadow-paper-risk",
            target.target_hash,
        ),
        symbol=symbol,
        side=side,
        order_type="MARKET",
        quantity=quantity,
        limit_price=None,
        time_in_force="DAY",
        session="REGULAR",
        client_order_id=stable_id("research-shadow-client", order_id),
        idempotency_key=stable_id("research-shadow-order-idem", order_id),
        created_at=target.decision_time,
    )


def _fillable_quantity(
    *,
    requested_quantity: Decimal,
    side: OrderSide,
    quote: ShadowQuoteV1,
    cash_usd: Decimal,
    parameters: ShadowPaperParametersV1,
) -> Decimal:
    displayed = (
        quote.ask_size_shares if side is OrderSide.BUY else quote.bid_size_shares
    )
    cap = min(
        requested_quantity,
        displayed * parameters.displayed_participation_rate,
        quote.adv_shares * parameters.adv_participation_rate,
    )
    if side is OrderSide.BUY:
        price = _fill_price(side=side, quote=quote, parameters=parameters)
        conservative_unit_cost = price * (
            Decimal("1") + parameters.commission_rate
        )
        cap = min(cap, cash_usd / conservative_unit_cost)
    return _round_down(cap, parameters.quantity_quantum)


def _fill_price(
    *,
    side: OrderSide,
    quote: ShadowQuoteV1,
    parameters: ShadowPaperParametersV1,
) -> Decimal:
    delay = (
        parameters.delay_penalty_bps
        / parameters.basis_points_per_unit_return
    )
    raw = (
        quote.ask_price * (Decimal("1") + delay)
        if side is OrderSide.BUY
        else quote.bid_price * (Decimal("1") - delay)
    )
    return raw.quantize(parameters.price_quantum, rounding=ROUND_HALF_EVEN)


def _commission(
    *,
    notional: Decimal,
    parameters: ShadowPaperParametersV1,
) -> Decimal:
    if notional <= parameters.commission_waiver_threshold_usd:
        return Decimal("0")
    return (notional * parameters.commission_rate).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_EVEN,
    )


def _fill_cost(
    *,
    fill: Fill,
    midpoint: Decimal,
    parameters: ShadowPaperParametersV1,
) -> ShadowFillCostV1:
    slippage = (
        (fill.price - midpoint) * fill.quantity
        if fill.side is OrderSide.BUY
        else (midpoint - fill.price) * fill.quantity
    )
    base = max(slippage, Decimal("0")) + fill.commission_usd
    notional = fill.quantity * fill.price
    return ShadowFillCostV1(
        fill_id=fill.fill_id,
        base_execution_cost_usd=base,
        sensitivity_5bp_cost_usd=(
            base
            + notional
            * parameters.sensitivity_5_bps
            / parameters.basis_points_per_unit_return
        ),
        sensitivity_10bp_cost_usd=(
            base
            + notional
            * parameters.sensitivity_10_bps
            / parameters.basis_points_per_unit_return
        ),
    )


def _build_next_state(
    *,
    state: ShadowArmStateV1,
    cash_usd: Decimal,
    positions: dict[str, Decimal],
    nav_usd: Decimal,
    turnover_usd: Decimal,
    commission_usd: Decimal,
    execution_cost_usd: Decimal,
    sensitivity_5bp_usd: Decimal,
    sensitivity_10bp_usd: Decimal,
    as_of: datetime,
    source_cycle_hash: str,
) -> ShadowArmStateV1:
    position_records = tuple(
        ShadowPositionV1(instrument_id=symbol, quantity=quantity)
        for symbol, quantity in sorted(positions.items())
        if quantity > 0
    )
    payload = {
        "schema_version": "shadow_arm_state_v1",
        "run_id": state.run_id,
        "role": state.role,
        "arm_id": state.arm_id,
        "strategy_id": state.strategy_id,
        "strategy_version": state.strategy_version,
        "artifact_hash": state.artifact_hash,
        "sequence": state.sequence + 1,
        "cash_usd": cash_usd,
        "positions": position_records,
        "last_nav_usd": nav_usd,
        "cumulative_turnover_usd": state.cumulative_turnover_usd + turnover_usd,
        "cumulative_commission_usd": (
            state.cumulative_commission_usd + commission_usd
        ),
        "cumulative_execution_cost_usd": (
            state.cumulative_execution_cost_usd + execution_cost_usd
        ),
        "cumulative_sensitivity_5bp_usd": (
            state.cumulative_sensitivity_5bp_usd + sensitivity_5bp_usd
        ),
        "cumulative_sensitivity_10bp_usd": (
            state.cumulative_sensitivity_10bp_usd + sensitivity_10bp_usd
        ),
        "as_of": require_aware_utc(as_of),
        "source_cycle_hash": source_cycle_hash,
    }
    return ShadowArmStateV1.model_validate(
        {**payload, "state_hash": canonical_hash(payload)}
    )


def _round_down(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).to_integral_value(rounding=ROUND_DOWN)
    return units * quantum


def _cumulative_return(
    summaries: tuple[ShadowDailyArmSummaryV1, ...],
) -> Decimal:
    growth = Decimal("1")
    for item in summaries:
        growth *= Decimal("1") + item.net_return
    return growth - Decimal("1")


def _average_exposures(
    summaries: tuple[ShadowDailyArmSummaryV1, ...],
) -> dict[str, Decimal]:
    totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    for summary in summaries:
        totals[USD_CASH] += summary.cash_weight
        for symbol, weight in summary.exposures.items():
            totals[symbol] += weight
    count = Decimal(len(summaries))
    return {symbol: value / count for symbol, value in sorted(totals.items())}
