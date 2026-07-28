from __future__ import annotations

import math
import random
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN


class PortfolioIntegrationMode(StrEnum):
    ADD_SLEEVE = "ADD_SLEEVE"
    REPLACE_SLEEVE = "REPLACE_SLEEVE"


class RiskFreeSeriesMode(StrEnum):
    SERIES = "SERIES"
    EXPLICIT_ZERO = "EXPLICIT_ZERO"


class StationaryBootstrapContractV1(DomainModel):
    schema_version: str = Field(default="stationary_bootstrap_contract_v1")
    configured_seed: int = Field(ge=0)
    samples: int = Field(ge=100, le=100_000)
    expected_block_sessions: int = Field(ge=1, le=252)
    lower_quantile: float = Field(gt=0, lt=0.5)
    variance_epsilon: float = Field(gt=0)

    @field_validator("lower_quantile", "variance_epsilon", mode="after")
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("bootstrap parameter must be finite")
        return value


class PortfolioComparisonContractV1(DomainModel):
    schema_version: str = Field(default="portfolio_comparison_contract_v1")
    comparison_contract_id: str = Field(pattern=IDENTIFIER_PATTERN)
    champion_portfolio_manifest_hash: str = Field(pattern=HASH_PATTERN)
    candidate_portfolio_manifest_hash: str = Field(pattern=HASH_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    allocation_policy_version: str = Field(pattern=IDENTIFIER_PATTERN)
    allocation_policy_hash: str = Field(pattern=HASH_PATTERN)
    integration_mode: PortfolioIntegrationMode
    sleeve_replaced_or_added: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_risk_budget: float = Field(gt=0, le=1)
    weight_selection_data_cutoff: datetime
    allocation_policy_created_at: datetime
    starting_nav: float = Field(gt=0)
    market_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    execution_contract_hash: str = Field(pattern=HASH_PATTERN)
    cost_model_hash: str = Field(pattern=HASH_PATTERN)
    risk_free_series_manifest_hash: str = Field(pattern=HASH_PATTERN)
    risk_free_series_mode: RiskFreeSeriesMode
    common_session_policy: str = Field(pattern=IDENTIFIER_PATTERN)
    annualization_sessions: int = Field(ge=1, le=366)
    bootstrap_contract: StationaryBootstrapContractV1
    cost_stress_multipliers: tuple[float, ...] = Field(min_length=3, max_length=3)
    maximum_absolute_daily_return: float = Field(gt=0)
    created_at: datetime
    contract_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "weight_selection_data_cutoff",
        "allocation_policy_created_at",
        "created_at",
        mode="after",
    )
    @classmethod
    def validate_times(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "candidate_risk_budget",
        "starting_nav",
        "maximum_absolute_daily_return",
        mode="after",
    )
    @classmethod
    def validate_finite_number(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("portfolio comparison number must be finite")
        return value

    @field_validator("cost_stress_multipliers", mode="after")
    @classmethod
    def validate_cost_stresses(
        cls,
        value: tuple[float, ...],
    ) -> tuple[float, ...]:
        if value != (1.0, 2.0, 3.0):
            raise ValueError("portfolio cost stress must be exactly 1x/2x/3x")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        if self.weight_selection_data_cutoff > self.allocation_policy_created_at:
            raise ValueError("allocation policy uses data unavailable at creation")
        if self.allocation_policy_created_at > self.created_at:
            raise ValueError("portfolio contract predates its allocation policy")
        if (
            self.champion_portfolio_manifest_hash
            == self.candidate_portfolio_manifest_hash
        ):
            raise ValueError("portfolio manifests must identify distinct portfolios")
        if self.common_session_policy != "INTERSECTION_NO_INTERPOLATION":
            raise ValueError("unsupported common-session policy")
        payload = self.model_dump(mode="python", exclude={"contract_hash"})
        if canonical_hash(payload) != self.contract_hash:
            raise ValueError("portfolio comparison contract hash mismatch")
        return self


class PortfolioReturnObservationV1(DomainModel):
    session_index: int = Field(ge=0)
    session_key: str = Field(pattern=IDENTIFIER_PATTERN)
    available_at: datetime
    candidate_portfolio_return_before_cost: float
    champion_portfolio_return_before_cost: float
    candidate_base_cost_return: float = Field(ge=0)
    champion_base_cost_return: float = Field(ge=0)
    risk_free_daily_return: float

    @field_validator("available_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

    @field_validator(
        "candidate_portfolio_return_before_cost",
        "champion_portfolio_return_before_cost",
        "candidate_base_cost_return",
        "champion_base_cost_return",
        "risk_free_daily_return",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("portfolio observation must be finite")
        return value


class PortfolioCostStressResultV1(DomainModel):
    cost_multiplier: float = Field(gt=0)
    candidate_portfolio_sharpe: float
    champion_portfolio_sharpe: float
    delta_sharpe_point: float
    delta_sharpe_lcb: float
    delta_sharpe_ucb: float

    @field_validator(
        "cost_multiplier",
        "candidate_portfolio_sharpe",
        "champion_portfolio_sharpe",
        "delta_sharpe_point",
        "delta_sharpe_lcb",
        "delta_sharpe_ucb",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("portfolio Sharpe aggregate must be finite")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if not self.delta_sharpe_lcb <= self.delta_sharpe_ucb:
            raise ValueError("delta-Sharpe interval is inverted")
        return self


class PortfolioDeltaSharpeResultV1(DomainModel):
    schema_version: str = Field(default="portfolio_delta_sharpe_result_v1")
    portfolio_comparison_contract_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    derived_bootstrap_seed_hash: str = Field(pattern=HASH_PATTERN)
    common_sessions: int = Field(gt=1)
    candidate_portfolio_sharpe: float
    champion_portfolio_sharpe: float
    delta_sharpe_point: float
    delta_sharpe_lcb: float
    delta_sharpe_ucb: float
    worst_cost_delta_sharpe_lcb: float
    cost_stress_results: tuple[PortfolioCostStressResultV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    result_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator(
        "candidate_portfolio_sharpe",
        "champion_portfolio_sharpe",
        "delta_sharpe_point",
        "delta_sharpe_lcb",
        "delta_sharpe_ucb",
        "worst_cost_delta_sharpe_lcb",
        mode="after",
    )
    @classmethod
    def validate_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("portfolio delta-Sharpe result must be finite")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if not self.delta_sharpe_lcb <= self.delta_sharpe_ucb:
            raise ValueError("delta-Sharpe interval is inverted")
        if tuple(item.cost_multiplier for item in self.cost_stress_results) != (
            1.0,
            2.0,
            3.0,
        ):
            raise ValueError("cost stress results are incomplete or unordered")
        base = self.cost_stress_results[0]
        if (
            self.candidate_portfolio_sharpe != base.candidate_portfolio_sharpe
            or self.champion_portfolio_sharpe != base.champion_portfolio_sharpe
            or self.delta_sharpe_point != base.delta_sharpe_point
            or self.delta_sharpe_lcb != base.delta_sharpe_lcb
            or self.delta_sharpe_ucb != base.delta_sharpe_ucb
        ):
            raise ValueError("base delta-Sharpe result does not match 1x cost")
        if self.worst_cost_delta_sharpe_lcb != min(
            item.delta_sharpe_lcb for item in self.cost_stress_results
        ):
            raise ValueError("worst-cost delta-Sharpe LCB mismatch")
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("portfolio delta-Sharpe result hash mismatch")
        return self


class PortfolioDeltaSharpeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def build_portfolio_comparison_contract(
    *,
    champion_portfolio_manifest_hash: str,
    candidate_portfolio_manifest_hash: str,
    candidate_artifact_hash: str,
    allocation_policy_version: str,
    allocation_policy_hash: str,
    integration_mode: PortfolioIntegrationMode,
    sleeve_replaced_or_added: str,
    candidate_risk_budget: float,
    weight_selection_data_cutoff: datetime,
    allocation_policy_created_at: datetime,
    starting_nav: float,
    market_data_manifest_hash: str,
    execution_contract_hash: str,
    cost_model_hash: str,
    risk_free_series_manifest_hash: str,
    risk_free_series_mode: RiskFreeSeriesMode,
    common_session_policy: str,
    annualization_sessions: int,
    bootstrap_contract: StationaryBootstrapContractV1,
    cost_stress_multipliers: tuple[float, ...],
    maximum_absolute_daily_return: float,
    created_at: datetime,
) -> PortfolioComparisonContractV1:
    timestamp = require_aware_utc(created_at)
    identity = stable_id(
        "portfolio-comparison-contract",
        champion_portfolio_manifest_hash,
        candidate_portfolio_manifest_hash,
        candidate_artifact_hash,
        allocation_policy_hash,
        timestamp,
    )
    payload = {
        "schema_version": "portfolio_comparison_contract_v1",
        "comparison_contract_id": identity,
        "champion_portfolio_manifest_hash": champion_portfolio_manifest_hash,
        "candidate_portfolio_manifest_hash": candidate_portfolio_manifest_hash,
        "candidate_artifact_hash": candidate_artifact_hash,
        "allocation_policy_version": allocation_policy_version,
        "allocation_policy_hash": allocation_policy_hash,
        "integration_mode": integration_mode,
        "sleeve_replaced_or_added": sleeve_replaced_or_added,
        "candidate_risk_budget": float(candidate_risk_budget),
        "weight_selection_data_cutoff": require_aware_utc(
            weight_selection_data_cutoff
        ),
        "allocation_policy_created_at": require_aware_utc(
            allocation_policy_created_at
        ),
        "starting_nav": float(starting_nav),
        "market_data_manifest_hash": market_data_manifest_hash,
        "execution_contract_hash": execution_contract_hash,
        "cost_model_hash": cost_model_hash,
        "risk_free_series_manifest_hash": risk_free_series_manifest_hash,
        "risk_free_series_mode": risk_free_series_mode,
        "common_session_policy": common_session_policy,
        "annualization_sessions": annualization_sessions,
        "bootstrap_contract": bootstrap_contract,
        "cost_stress_multipliers": tuple(
            float(value) for value in cost_stress_multipliers
        ),
        "maximum_absolute_daily_return": float(
            maximum_absolute_daily_return
        ),
        "created_at": timestamp,
    }
    return PortfolioComparisonContractV1.model_validate(
        {**payload, "contract_hash": canonical_hash(payload)}
    )


def evaluate_portfolio_delta_sharpe(
    *,
    observations: Sequence[PortfolioReturnObservationV1],
    comparison_contract: PortfolioComparisonContractV1,
    evaluation_contract_hash: str,
) -> PortfolioDeltaSharpeResultV1:
    rows = tuple(observations)
    _validate_observations(rows, comparison_contract)
    seed_hash = canonical_hash(
        {
            "configured_bootstrap_seed": (
                comparison_contract.bootstrap_contract.configured_seed
            ),
            "candidate_artifact_hash": (
                comparison_contract.candidate_artifact_hash
            ),
            "evaluation_contract_hash": evaluation_contract_hash,
            "portfolio_comparison_contract_hash": (
                comparison_contract.contract_hash
            ),
        }
    )
    seed = int(seed_hash[:16], 16)
    results = tuple(
        _evaluate_cost_stress(
            rows,
            multiplier=multiplier,
            annualization_sessions=comparison_contract.annualization_sessions,
            bootstrap=comparison_contract.bootstrap_contract,
            seed=seed,
        )
        for multiplier in comparison_contract.cost_stress_multipliers
    )
    base = results[0]
    payload = {
        "schema_version": "portfolio_delta_sharpe_result_v1",
        "portfolio_comparison_contract_hash": comparison_contract.contract_hash,
        "evaluation_contract_hash": evaluation_contract_hash,
        "derived_bootstrap_seed_hash": seed_hash,
        "common_sessions": len(rows),
        "candidate_portfolio_sharpe": base.candidate_portfolio_sharpe,
        "champion_portfolio_sharpe": base.champion_portfolio_sharpe,
        "delta_sharpe_point": base.delta_sharpe_point,
        "delta_sharpe_lcb": base.delta_sharpe_lcb,
        "delta_sharpe_ucb": base.delta_sharpe_ucb,
        "worst_cost_delta_sharpe_lcb": min(
            item.delta_sharpe_lcb for item in results
        ),
        "cost_stress_results": results,
    }
    return PortfolioDeltaSharpeResultV1.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )


def _validate_observations(
    rows: tuple[PortfolioReturnObservationV1, ...],
    contract: PortfolioComparisonContractV1,
) -> None:
    if len(rows) < 2:
        raise PortfolioDeltaSharpeError("INSUFFICIENT_COMMON_SESSIONS")
    expected_indices = tuple(range(len(rows)))
    if tuple(item.session_index for item in rows) != expected_indices:
        raise PortfolioDeltaSharpeError("COMMON_SESSION_ORDER_INVALID")
    keys = tuple(item.session_key for item in rows)
    if len(keys) != len(set(keys)):
        raise PortfolioDeltaSharpeError("COMMON_SESSION_DUPLICATE")
    for row in rows:
        numbers = (
            row.candidate_portfolio_return_before_cost,
            row.champion_portfolio_return_before_cost,
            row.candidate_base_cost_return,
            row.champion_base_cost_return,
            row.risk_free_daily_return,
        )
        if not all(math.isfinite(value) for value in numbers):
            raise PortfolioDeltaSharpeError("NONFINITE_PORTFOLIO_METRIC")
        if any(
            abs(value) > contract.maximum_absolute_daily_return
            for value in (
                row.candidate_portfolio_return_before_cost,
                row.champion_portfolio_return_before_cost,
                row.risk_free_daily_return,
            )
        ):
            raise PortfolioDeltaSharpeError("ABNORMAL_PORTFOLIO_RETURN")
        if (
            row.candidate_portfolio_return_before_cost <= -1
            or row.champion_portfolio_return_before_cost <= -1
            or row.risk_free_daily_return <= -1
        ):
            raise PortfolioDeltaSharpeError("ABNORMAL_PORTFOLIO_RETURN")
        if (
            contract.risk_free_series_mode is RiskFreeSeriesMode.EXPLICIT_ZERO
            and row.risk_free_daily_return != 0
        ):
            raise PortfolioDeltaSharpeError("RISK_FREE_SERIES_MISMATCH")


def _evaluate_cost_stress(
    rows: tuple[PortfolioReturnObservationV1, ...],
    *,
    multiplier: float,
    annualization_sessions: int,
    bootstrap: StationaryBootstrapContractV1,
    seed: int,
) -> PortfolioCostStressResultV1:
    candidate = tuple(
        item.candidate_portfolio_return_before_cost
        - multiplier * item.candidate_base_cost_return
        for item in rows
    )
    champion = tuple(
        item.champion_portfolio_return_before_cost
        - multiplier * item.champion_base_cost_return
        for item in rows
    )
    risk_free = tuple(item.risk_free_daily_return for item in rows)
    candidate_sharpe = _sample_sharpe(
        candidate,
        risk_free,
        annualization_sessions=annualization_sessions,
        variance_epsilon=bootstrap.variance_epsilon,
    )
    champion_sharpe = _sample_sharpe(
        champion,
        risk_free,
        annualization_sessions=annualization_sessions,
        variance_epsilon=bootstrap.variance_epsilon,
    )
    point = candidate_sharpe - champion_sharpe
    deltas: list[float] = []
    rng = random.Random(seed)
    for _ in range(bootstrap.samples):
        indices = _stationary_indices(
            count=len(rows),
            expected_block_sessions=bootstrap.expected_block_sessions,
            rng=rng,
        )
        sampled_candidate = tuple(candidate[index] for index in indices)
        sampled_champion = tuple(champion[index] for index in indices)
        sampled_risk_free = tuple(risk_free[index] for index in indices)
        try:
            delta = _sample_sharpe(
                sampled_candidate,
                sampled_risk_free,
                annualization_sessions=annualization_sessions,
                variance_epsilon=bootstrap.variance_epsilon,
            ) - _sample_sharpe(
                sampled_champion,
                sampled_risk_free,
                annualization_sessions=annualization_sessions,
                variance_epsilon=bootstrap.variance_epsilon,
            )
        except PortfolioDeltaSharpeError:
            continue
        deltas.append(delta)
    if len(deltas) < bootstrap.samples // 2:
        raise PortfolioDeltaSharpeError("DEGENERATE_BOOTSTRAP_DISTRIBUTION")
    deltas.sort()
    lower = _percentile(deltas, bootstrap.lower_quantile)
    upper = _percentile(deltas, 1.0 - bootstrap.lower_quantile)
    return PortfolioCostStressResultV1(
        cost_multiplier=multiplier,
        candidate_portfolio_sharpe=candidate_sharpe,
        champion_portfolio_sharpe=champion_sharpe,
        delta_sharpe_point=point,
        delta_sharpe_lcb=lower,
        delta_sharpe_ucb=upper,
    )


def _sample_sharpe(
    returns: Sequence[float],
    risk_free: Sequence[float],
    *,
    annualization_sessions: int,
    variance_epsilon: float,
) -> float:
    if len(returns) != len(risk_free) or len(returns) < 2:
        raise PortfolioDeltaSharpeError("INSUFFICIENT_COMMON_SESSIONS")
    excess = tuple(
        value - risk_free_value
        for value, risk_free_value in zip(returns, risk_free, strict=True)
    )
    if not all(math.isfinite(value) for value in excess):
        raise PortfolioDeltaSharpeError("NONFINITE_PORTFOLIO_METRIC")
    mean = sum(excess) / len(excess)
    sample_variance = sum((value - mean) ** 2 for value in excess) / (
        len(excess) - 1
    )
    if not math.isfinite(sample_variance) or sample_variance <= variance_epsilon:
        raise PortfolioDeltaSharpeError("DEGENERATE_VARIANCE")
    return math.sqrt(annualization_sessions) * mean / math.sqrt(sample_variance)


def _stationary_indices(
    *,
    count: int,
    expected_block_sessions: int,
    rng: random.Random,
) -> tuple[int, ...]:
    restart_probability = 1.0 / expected_block_sessions
    index = rng.randrange(count)
    values: list[int] = []
    for _ in range(count):
        values.append(index)
        index = (
            rng.randrange(count)
            if rng.random() < restart_probability
            else (index + 1) % count
        )
    return tuple(values)


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise PortfolioDeltaSharpeError("DEGENERATE_BOOTSTRAP_DISTRIBUTION")
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return (
        sorted_values[lower] * (1.0 - fraction)
        + sorted_values[upper] * fraction
    )


__all__ = [
    "PortfolioComparisonContractV1",
    "PortfolioCostStressResultV1",
    "PortfolioDeltaSharpeError",
    "PortfolioDeltaSharpeResultV1",
    "PortfolioIntegrationMode",
    "PortfolioReturnObservationV1",
    "RiskFreeSeriesMode",
    "StationaryBootstrapContractV1",
    "build_portfolio_comparison_contract",
    "evaluate_portfolio_delta_sharpe",
]
