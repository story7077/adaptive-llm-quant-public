from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    HASH_PATTERN,
    IDENTIFIER_PATTERN,
    OosVerdict,
)
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
    PortfolioCostStressResultV1,
    PortfolioDeltaSharpeError,
    PortfolioDeltaSharpeResultV1,
    PortfolioReturnObservationV1,
    evaluate_portfolio_delta_sharpe,
)

PRIVATE_DATASET_SCHEMA_VERSION_V2 = "oos_private_dataset_v2"
TRUSTED_DATASET_PRODUCER_VERSION_V2 = "trusted_candidate_evaluation_v2"
MAX_PRIVATE_DATASET_BYTES_V2 = 64 * 1024 * 1024

OOS_V2_REASON_CODES = frozenset(
    {
        "PREDECLARED_PORTFOLIO_OOS_CRITERIA_PASSED",
        "INSUFFICIENT_COMMON_SESSIONS",
        "INSUFFICIENT_INDEPENDENT_TRADES",
        "DELTA_SHARPE_LCB_NOT_MET",
        "WORST_COST_DELTA_SHARPE_LCB_NOT_MET",
        "DEGENERATE_VARIANCE",
        "DEGENERATE_BOOTSTRAP_DISTRIBUTION",
        "NONFINITE_PORTFOLIO_METRIC",
        "ABNORMAL_PORTFOLIO_RETURN",
        "RISK_FREE_SERIES_MISMATCH",
        "COMMON_SESSION_ORDER_INVALID",
        "COMMON_SESSION_DUPLICATE",
        "LOCKBOX_DATA_UNAVAILABLE",
        "LOCKBOX_DATA_INCOMPLETE",
        "LOCKBOX_DATA_INVALID",
        "LOCKBOX_DATA_DUPLICATE",
        "LOCKBOX_DATA_HASH_MISMATCH",
        "LOCKBOX_DATA_PIT_INVALID",
        "PORTFOLIO_CONTRACT_BINDING_INVALID",
        "ALLOCATION_POLICY_NOT_FIXED_BEFORE_OOS",
    }
)


class PrivateOosDatasetManifestV2(DomainModel):
    schema_version: Literal["private_oos_dataset_manifest_v2"] = (
        "private_oos_dataset_manifest_v2"
    )
    dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    portfolio_comparison_contract_hash: str = Field(pattern=HASH_PATTERN)
    source_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    candidate_replay_hash: str = Field(pattern=HASH_PATTERN)
    trusted_producer_version: Literal["trusted_candidate_evaluation_v2"]
    common_session_count: int = Field(gt=0)
    independent_trade_count: int = Field(ge=0)
    private_file_hash: str = Field(pattern=HASH_PATTERN)
    dataset_hash: str = Field(pattern=HASH_PATTERN)
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"manifest_hash"})
        if canonical_hash(payload) != self.manifest_hash:
            raise ValueError("private OOS V2 manifest hash mismatch")
        return self


class OosWorkerRequestV2(DomainModel):
    schema_version: Literal["oos_worker_request_v2"] = "oos_worker_request_v2"
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
        "trusted_candidate_evaluation_v2"
    ]
    portfolio_comparison_contract: PortfolioComparisonContractV1
    data_available_cutoff: datetime
    minimum_common_sessions: int = Field(ge=2)
    minimum_independent_trades: int = Field(ge=0)
    minimum_delta_sharpe_lcb: float
    minimum_worst_cost_delta_sharpe_lcb: float
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

    @field_validator(
        "minimum_delta_sharpe_lcb",
        "minimum_worst_cost_delta_sharpe_lcb",
        mode="after",
    )
    @classmethod
    def validate_finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("OOS V2 threshold must be finite")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.data_available_cutoff > self.evaluated_at:
            raise ValueError("OOS V2 data cutoff cannot follow evaluation time")
        if self.expires_at <= self.evaluated_at:
            raise ValueError("OOS V2 request is expired at evaluation time")
        if (
            self.candidate_artifact_hash
            != self.portfolio_comparison_contract.candidate_artifact_hash
        ):
            raise ValueError("OOS V2 Candidate artifact binding mismatch")
        payload = self.model_dump(mode="python", exclude={"request_hash"})
        if canonical_hash(payload) != self.request_hash:
            raise ValueError("OOS V2 request hash mismatch")
        return self


class OosLockboxResultV2(DomainModel):
    schema_version: Literal["oos_lockbox_result_v2"] = "oos_lockbox_result_v2"
    challenger_id: str = Field(pattern=IDENTIFIER_PATTERN)
    experiment_family: str = Field(pattern=IDENTIFIER_PATTERN)
    submission_number: int = Field(ge=1)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    portfolio_comparison_contract_hash: str = Field(pattern=HASH_PATTERN)
    verdict: OosVerdict
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=20)
    common_sessions: int = Field(ge=0)
    independent_trades: int = Field(ge=0)
    candidate_portfolio_sharpe: float | None = None
    champion_portfolio_sharpe: float | None = None
    delta_sharpe_point: float | None = None
    delta_sharpe_lcb: float | None = None
    delta_sharpe_ucb: float | None = None
    worst_cost_delta_sharpe_lcb: float | None = None
    cost_stress_results: tuple[PortfolioCostStressResultV1, ...] = ()
    no_degenerate_variance: bool
    portfolio_contract_binding_valid: bool
    allocation_policy_fixed_before_oos: bool
    all_metrics_finite: bool
    budget_consumed: int = Field(ge=1)
    evaluated_at: datetime
    result_hash: str = Field(pattern=HASH_PATTERN)

    @field_validator("evaluated_at", mode="after")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return require_aware_utc(value)

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
    def validate_optional_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("OOS V2 aggregate must be finite")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("OOS V2 reason codes must be unique")
        if not set(self.reason_codes).issubset(OOS_V2_REASON_CODES):
            raise ValueError("OOS V2 result contains an unknown reason code")
        pass_code = "PREDECLARED_PORTFOLIO_OOS_CRITERIA_PASSED"
        metrics = (
            self.candidate_portfolio_sharpe,
            self.champion_portfolio_sharpe,
            self.delta_sharpe_point,
            self.delta_sharpe_lcb,
            self.delta_sharpe_ucb,
            self.worst_cost_delta_sharpe_lcb,
        )
        if self.verdict is OosVerdict.PASS:
            if self.reason_codes != (pass_code,):
                raise ValueError("passing OOS V2 result has invalid reasons")
            if (
                any(value is None for value in metrics)
                or len(self.cost_stress_results) != 3
                or not self.no_degenerate_variance
                or not self.portfolio_contract_binding_valid
                or not self.allocation_policy_fixed_before_oos
                or not self.all_metrics_finite
            ):
                raise ValueError("passing OOS V2 result is incomplete")
        elif pass_code in self.reason_codes:
            raise ValueError("failed OOS V2 result cannot contain pass reason")
        if self.cost_stress_results and len(self.cost_stress_results) != 3:
            raise ValueError("OOS V2 cost stress output is incomplete")
        if self.cost_stress_results:
            base = self.cost_stress_results[0]
            if (
                self.candidate_portfolio_sharpe
                != base.candidate_portfolio_sharpe
                or self.champion_portfolio_sharpe
                != base.champion_portfolio_sharpe
                or self.delta_sharpe_point != base.delta_sharpe_point
                or self.delta_sharpe_lcb != base.delta_sharpe_lcb
                or self.delta_sharpe_ucb != base.delta_sharpe_ucb
                or self.worst_cost_delta_sharpe_lcb
                != min(
                    item.delta_sharpe_lcb
                    for item in self.cost_stress_results
                )
            ):
                raise ValueError("OOS V2 cost stress aggregate mismatch")
        payload = self.model_dump(mode="python", exclude={"result_hash"})
        if canonical_hash(payload) != self.result_hash:
            raise ValueError("OOS V2 result hash mismatch")
        return self


class OosWorkerResponseV2(DomainModel):
    schema_version: Literal["oos_worker_response_v2"] = "oos_worker_response_v2"
    request_id: str = Field(pattern=IDENTIFIER_PATTERN)
    request_hash: str = Field(pattern=HASH_PATTERN)
    reservation_id: str = Field(pattern=IDENTIFIER_PATTERN)
    reservation_hash: str = Field(pattern=HASH_PATTERN)
    result: OosLockboxResultV2
    response_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        payload = self.model_dump(mode="python", exclude={"response_hash"})
        if canonical_hash(payload) != self.response_hash:
            raise ValueError("OOS V2 response hash mismatch")
        return self


class OosV2PrivateDatasetError(RuntimeError):
    def __init__(self, reason_code: str, *, binding_valid: bool = True) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.binding_valid = binding_valid


def evaluate_private_request_v2(
    request: OosWorkerRequestV2,
    *,
    private_root: Path,
) -> OosWorkerResponseV2:
    rows: tuple[PortfolioReturnObservationV1, ...] = ()
    independent_trades = 0
    try:
        rows, independent_trades = _load_private_dataset_v2(request, private_root)
        metric = evaluate_portfolio_delta_sharpe(
            observations=rows,
            comparison_contract=request.portfolio_comparison_contract,
            evaluation_contract_hash=request.evaluation_contract_hash,
        )
    except OosV2PrivateDatasetError as exc:
        result = _failure_result(
            request,
            reason_codes=(exc.reason_code,),
            portfolio_contract_binding_valid=exc.binding_valid,
        )
    except PortfolioDeltaSharpeError as exc:
        result = _failure_result(
            request,
            reason_codes=(exc.reason_code,),
            common_sessions=len(rows),
            independent_trades=independent_trades,
            no_degenerate_variance=exc.reason_code
            not in {"DEGENERATE_VARIANCE", "DEGENERATE_BOOTSTRAP_DISTRIBUTION"},
        )
    else:
        reason_codes: list[str] = []
        if metric.common_sessions < request.minimum_common_sessions:
            reason_codes.append("INSUFFICIENT_COMMON_SESSIONS")
        if independent_trades < request.minimum_independent_trades:
            reason_codes.append("INSUFFICIENT_INDEPENDENT_TRADES")
        if not metric.delta_sharpe_lcb > request.minimum_delta_sharpe_lcb:
            reason_codes.append("DELTA_SHARPE_LCB_NOT_MET")
        if not (
            metric.worst_cost_delta_sharpe_lcb
            >= request.minimum_worst_cost_delta_sharpe_lcb
        ):
            reason_codes.append("WORST_COST_DELTA_SHARPE_LCB_NOT_MET")
        verdict = OosVerdict.FAIL if reason_codes else OosVerdict.PASS
        if not reason_codes:
            reason_codes.append("PREDECLARED_PORTFOLIO_OOS_CRITERIA_PASSED")
        result = _result_from_metric(
            request,
            metric=metric,
            independent_trades=independent_trades,
            verdict=verdict,
            reason_codes=tuple(reason_codes),
        )
    response_payload = {
        "schema_version": "oos_worker_response_v2",
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "reservation_id": request.reservation_id,
        "reservation_hash": request.reservation_hash,
        "result": result,
    }
    return OosWorkerResponseV2.model_validate(
        {**response_payload, "response_hash": canonical_hash(response_payload)}
    )


def _load_private_dataset_v2(
    request: OosWorkerRequestV2,
    private_root: Path,
) -> tuple[tuple[PortfolioReturnObservationV1, ...], int]:
    try:
        root = private_root.resolve(strict=True)
        dataset_path = (root / f"{request.dataset_id}.json").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_UNAVAILABLE") from exc
    if not dataset_path.is_relative_to(root) or not dataset_path.is_file():
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_UNAVAILABLE")
    try:
        if dataset_path.stat().st_size > MAX_PRIVATE_DATASET_BYTES_V2:
            raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID")
        decoded_object: object = json.loads(dataset_path.read_text(encoding="utf-8"))
    except OosV2PrivateDatasetError:
        raise
    except (OSError, UnicodeError) as exc:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_UNAVAILABLE") from exc
    except json.JSONDecodeError as exc:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID") from exc
    if not isinstance(decoded_object, dict):
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID")
    decoded = cast(dict[str, object], decoded_object)
    required = {
        "schema_version",
        "dataset_id",
        "candidate_artifact_hash",
        "evaluation_contract_hash",
        "portfolio_comparison_contract_hash",
        "champion_portfolio_manifest_hash",
        "candidate_portfolio_manifest_hash",
        "allocation_policy_hash",
        "market_data_manifest_hash",
        "execution_contract_hash",
        "cost_model_hash",
        "risk_free_series_manifest_hash",
        "source_data_manifest_hash",
        "candidate_replay_hash",
        "trusted_producer_version",
        "independent_trade_count",
        "observations",
        "dataset_hash",
    }
    if set(decoded) != required:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
    if (
        decoded["schema_version"] != PRIVATE_DATASET_SCHEMA_VERSION_V2
        or decoded["trusted_producer_version"]
        != TRUSTED_DATASET_PRODUCER_VERSION_V2
        or decoded["trusted_producer_version"]
        != request.expected_trusted_producer_version
    ):
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID")
    hash_payload = {key: value for key, value in decoded.items() if key != "dataset_hash"}
    try:
        dataset_hash = _parse_hash(decoded["dataset_hash"])
        computed_hash = canonical_hash(hash_payload)
    except (ValueError, OosV2PrivateDatasetError) as exc:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID") from exc
    if computed_hash != dataset_hash or dataset_hash != request.dataset_manifest_hash:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_HASH_MISMATCH")
    contract = request.portfolio_comparison_contract
    bindings = (
        (decoded["dataset_id"], request.dataset_id),
        (decoded["candidate_artifact_hash"], request.candidate_artifact_hash),
        (decoded["evaluation_contract_hash"], request.evaluation_contract_hash),
        (decoded["portfolio_comparison_contract_hash"], contract.contract_hash),
        (
            decoded["champion_portfolio_manifest_hash"],
            contract.champion_portfolio_manifest_hash,
        ),
        (
            decoded["candidate_portfolio_manifest_hash"],
            contract.candidate_portfolio_manifest_hash,
        ),
        (decoded["allocation_policy_hash"], contract.allocation_policy_hash),
        (decoded["market_data_manifest_hash"], contract.market_data_manifest_hash),
        (decoded["execution_contract_hash"], contract.execution_contract_hash),
        (decoded["cost_model_hash"], contract.cost_model_hash),
        (
            decoded["risk_free_series_manifest_hash"],
            contract.risk_free_series_manifest_hash,
        ),
        (
            decoded["source_data_manifest_hash"],
            request.expected_source_data_manifest_hash,
        ),
        (decoded["candidate_replay_hash"], request.expected_candidate_replay_hash),
    )
    if any(actual != expected for actual, expected in bindings):
        raise OosV2PrivateDatasetError(
            "PORTFOLIO_CONTRACT_BINDING_INVALID",
            binding_valid=False,
        )
    raw_observations_object = decoded["observations"]
    if not isinstance(raw_observations_object, list):
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
    raw_observations = cast(list[object], raw_observations_object)
    try:
        rows = tuple(
            PortfolioReturnObservationV1.model_validate(item)
            for item in raw_observations
        )
    except ValueError as exc:
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID") from exc
    if any(item.available_at > request.data_available_cutoff for item in rows):
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_PIT_INVALID")
    if rows and min(item.available_at for item in rows) <= contract.created_at:
        raise OosV2PrivateDatasetError(
            "ALLOCATION_POLICY_NOT_FIXED_BEFORE_OOS"
        )
    independent_trades = decoded["independent_trade_count"]
    if (
        isinstance(independent_trades, bool)
        or not isinstance(independent_trades, int)
        or independent_trades < 0
    ):
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID")
    return rows, independent_trades


def _parse_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OosV2PrivateDatasetError("LOCKBOX_DATA_INVALID")
    return value


def _result_from_metric(
    request: OosWorkerRequestV2,
    *,
    metric: PortfolioDeltaSharpeResultV1,
    independent_trades: int,
    verdict: OosVerdict,
    reason_codes: tuple[str, ...],
) -> OosLockboxResultV2:
    payload = _base_result_payload(
        request,
        verdict=verdict,
        reason_codes=reason_codes,
        common_sessions=metric.common_sessions,
        independent_trades=independent_trades,
        candidate_portfolio_sharpe=metric.candidate_portfolio_sharpe,
        champion_portfolio_sharpe=metric.champion_portfolio_sharpe,
        delta_sharpe_point=metric.delta_sharpe_point,
        delta_sharpe_lcb=metric.delta_sharpe_lcb,
        delta_sharpe_ucb=metric.delta_sharpe_ucb,
        worst_cost_delta_sharpe_lcb=metric.worst_cost_delta_sharpe_lcb,
        cost_stress_results=metric.cost_stress_results,
        no_degenerate_variance=True,
        portfolio_contract_binding_valid=True,
        allocation_policy_fixed_before_oos=True,
        all_metrics_finite=True,
    )
    return OosLockboxResultV2.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )


def _failure_result(
    request: OosWorkerRequestV2,
    *,
    reason_codes: Sequence[str],
    common_sessions: int = 0,
    independent_trades: int = 0,
    no_degenerate_variance: bool = True,
    portfolio_contract_binding_valid: bool = True,
) -> OosLockboxResultV2:
    payload = _base_result_payload(
        request,
        verdict=OosVerdict.FAIL,
        reason_codes=tuple(reason_codes),
        common_sessions=common_sessions,
        independent_trades=independent_trades,
        candidate_portfolio_sharpe=None,
        champion_portfolio_sharpe=None,
        delta_sharpe_point=None,
        delta_sharpe_lcb=None,
        delta_sharpe_ucb=None,
        worst_cost_delta_sharpe_lcb=None,
        cost_stress_results=(),
        no_degenerate_variance=no_degenerate_variance,
        portfolio_contract_binding_valid=portfolio_contract_binding_valid,
        allocation_policy_fixed_before_oos=(
            "ALLOCATION_POLICY_NOT_FIXED_BEFORE_OOS" not in reason_codes
        ),
        all_metrics_finite=(
            "NONFINITE_PORTFOLIO_METRIC" not in reason_codes
        ),
    )
    return OosLockboxResultV2.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )


def _base_result_payload(
    request: OosWorkerRequestV2,
    **values: object,
) -> dict[str, object]:
    return {
        "schema_version": "oos_lockbox_result_v2",
        "challenger_id": request.challenger_id,
        "experiment_family": request.experiment_family,
        "submission_number": request.submission_number,
        "candidate_artifact_hash": request.candidate_artifact_hash,
        "evaluation_contract_hash": request.evaluation_contract_hash,
        "portfolio_comparison_contract_hash": (
            request.portfolio_comparison_contract.contract_hash
        ),
        **values,
        "budget_consumed": request.oos_budget_ordinal,
        "evaluated_at": request.evaluated_at,
    }


__all__ = [
    "OOS_V2_REASON_CODES",
    "PRIVATE_DATASET_SCHEMA_VERSION_V2",
    "TRUSTED_DATASET_PRODUCER_VERSION_V2",
    "OosLockboxResultV2",
    "OosV2PrivateDatasetError",
    "OosWorkerRequestV2",
    "OosWorkerResponseV2",
    "PrivateOosDatasetManifestV2",
    "evaluate_private_request_v2",
]
