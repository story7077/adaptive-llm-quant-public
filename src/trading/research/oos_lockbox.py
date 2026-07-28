from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    OosBudgetReservationV1,
    OosLockboxResultV1,
    OosVerdict,
    OosWorkerRequestV1,
    OosWorkerResponseV1,
)
from trading.research.oos_v2 import (
    OosLockboxResultV2,
    OosWorkerRequestV2,
    OosWorkerResponseV2,
)
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
)


class OosLockboxError(RuntimeError):
    """Raised when the private OOS boundary rejects an evaluation."""


@dataclass(frozen=True, slots=True)
class OosEvaluationRequest:
    challenger_id: str
    experiment_family: str
    submission_number: int
    candidate_artifact_hash: str
    evaluation_contract_hash: str


@dataclass(frozen=True, slots=True)
class PrivateOosObservation:
    session_key: str
    candidate_return: float
    matched_baseline_return: float


class PrivateOosEvaluator(Protocol):
    def evaluate(
        self,
        request: OosEvaluationRequest,
    ) -> Sequence[PrivateOosObservation]: ...


class OosBudgetLedger(Protocol):
    def reserve(
        self,
        *,
        experiment_family: str,
        submission_number: int,
    ) -> int: ...


class OosReservationRepository(Protocol):
    def reserve_oos_budget(
        self,
        *,
        request: OosEvaluationRequest,
        maximum_submissions: int,
        maximum_oos_uses: int,
        idempotency_key: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> OosBudgetReservationV1: ...


@dataclass(frozen=True, slots=True)
class OosProcessEvaluationConfig:
    dataset_id: str
    dataset_manifest_hash: str
    data_available_cutoff: datetime
    expected_source_data_manifest_hash: str
    expected_candidate_replay_hash: str
    expected_trusted_producer_version: str
    minimum_common_sessions: int
    minimum_mean_daily_difference: float
    annualization_sessions: int
    newey_west_lag: int
    bootstrap_seed: int
    bootstrap_block_length: int
    bootstrap_samples: int
    base_cost_bps: int
    request_ttl_seconds: int
    worker_timeout_seconds: int
    cost_sensitivity_bps: tuple[int, ...]
    maximum_submissions: int
    maximum_oos_uses: int

    def __post_init__(self) -> None:
        require_aware_utc(self.data_available_cutoff)
        for name, value in (
            (
                "expected_source_data_manifest_hash",
                self.expected_source_data_manifest_hash,
            ),
            (
                "expected_candidate_replay_hash",
                self.expected_candidate_replay_hash,
            ),
        ):
            if (
                len(value) != 64
                or value.lower() != value
                or any(
                    character not in "0123456789abcdef"
                    for character in value
                )
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 hash")
        if (
            self.expected_trusted_producer_version
            != "trusted_candidate_evaluation_v1"
        ):
            raise ValueError("untrusted OOS dataset producer version")
        if self.minimum_common_sessions < 126:
            raise ValueError("production OOS requires at least 126 common sessions")
        if (
            self.annualization_sessions <= 0
            or self.newey_west_lag < 0
            or self.bootstrap_seed < 0
            or self.bootstrap_block_length <= 0
            or self.bootstrap_samples <= 0
            or self.base_cost_bps < 0
            or self.request_ttl_seconds <= 0
            or self.worker_timeout_seconds <= 0
        ):
            raise ValueError("OOS numeric evaluation parameters are invalid")
        if self.cost_sensitivity_bps != (0, 5, 10):
            raise ValueError("OOS cost sensitivity must remain 0/5/10 bp")
        if self.maximum_submissions <= 0 or self.maximum_oos_uses <= 0:
            raise ValueError("OOS experiment budgets must be positive")


@dataclass(frozen=True, slots=True)
class OosProcessEvaluationConfigV2:
    dataset_id: str
    dataset_manifest_hash: str
    data_available_cutoff: datetime
    expected_source_data_manifest_hash: str
    expected_candidate_replay_hash: str
    portfolio_comparison_contract: PortfolioComparisonContractV1
    minimum_common_sessions: int
    minimum_independent_trades: int
    minimum_delta_sharpe_lcb: float
    minimum_worst_cost_delta_sharpe_lcb: float
    request_ttl_seconds: int
    worker_timeout_seconds: int
    maximum_submissions: int
    maximum_oos_uses: int

    def __post_init__(self) -> None:
        require_aware_utc(self.data_available_cutoff)
        for name, value in (
            ("dataset_manifest_hash", self.dataset_manifest_hash),
            (
                "expected_source_data_manifest_hash",
                self.expected_source_data_manifest_hash,
            ),
            (
                "expected_candidate_replay_hash",
                self.expected_candidate_replay_hash,
            ),
        ):
            if (
                len(value) != 64
                or value.lower() != value
                or any(
                    character not in "0123456789abcdef"
                    for character in value
                )
            ):
                raise ValueError(f"{name} must be a lowercase SHA-256 hash")
        if self.minimum_common_sessions < 2:
            raise ValueError("portfolio OOS requires at least two common sessions")
        if (
            self.minimum_independent_trades < 0
            or self.request_ttl_seconds <= 0
            or self.worker_timeout_seconds <= 0
            or self.maximum_submissions <= 0
            or self.maximum_oos_uses <= 0
        ):
            raise ValueError("portfolio OOS numeric parameters are invalid")
        if not all(
            math.isfinite(value)
            for value in (
                self.minimum_delta_sharpe_lcb,
                self.minimum_worst_cost_delta_sharpe_lcb,
            )
        ):
            raise ValueError("portfolio OOS thresholds must be finite")


class OosProcessClient:
    """Invokes the trusted lockbox worker without exposing its private rows."""

    def __init__(
        self,
        *,
        private_root: Path,
        python_executable: Path | None = None,
        worker_module: str = "trading.research.oos_worker",
        timeout_seconds: float = 60.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("OOS worker timeout must be positive")
        self._private_root = private_root
        self._python_executable = (
            Path(sys.executable) if python_executable is None else python_executable
        )
        self._worker_module = worker_module
        self._timeout_seconds = timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate(self, request: OosWorkerRequestV1) -> OosWorkerResponseV1:
        if require_aware_utc(self._clock()) >= request.expires_at:
            raise OosLockboxError("OOS_WORKER_REQUEST_EXPIRED")
        environment = _minimal_worker_environment(self._private_root)
        try:
            completed = subprocess.run(
                [
                    str(self._python_executable),
                    "-I",
                    "-m",
                    self._worker_module,
                ],
                input=request.model_dump_json(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=tempfile.gettempdir(),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise OosLockboxError("OOS_WORKER_UNAVAILABLE") from exc
        if completed.returncode != 0 or len(completed.stdout) > 1_048_576:
            raise OosLockboxError("OOS_WORKER_FAILED")
        try:
            response = OosWorkerResponseV1.model_validate_json(completed.stdout)
        except ValidationError as exc:
            raise OosLockboxError("OOS_WORKER_RESPONSE_INVALID") from exc
        _require_response_binding(request, response)
        return response

    def evaluate_v2(self, request: OosWorkerRequestV2) -> OosWorkerResponseV2:
        if require_aware_utc(self._clock()) >= request.expires_at:
            raise OosLockboxError("OOS_WORKER_REQUEST_EXPIRED")
        environment = _minimal_worker_environment(self._private_root)
        try:
            completed = subprocess.run(
                [
                    str(self._python_executable),
                    "-I",
                    "-m",
                    self._worker_module,
                ],
                input=request.model_dump_json(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=tempfile.gettempdir(),
                env=environment,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
            raise OosLockboxError("OOS_WORKER_UNAVAILABLE") from exc
        if completed.returncode != 0 or len(completed.stdout) > 1_048_576:
            raise OosLockboxError("OOS_WORKER_FAILED")
        try:
            response = OosWorkerResponseV2.model_validate_json(completed.stdout)
        except ValidationError as exc:
            raise OosLockboxError("OOS_WORKER_RESPONSE_INVALID") from exc
        _require_response_binding_v2(request, response)
        return response


class PersistentOosBudgetAdapter:
    def __init__(
        self,
        repository: OosReservationRepository,
        *,
        maximum_submissions: int,
        maximum_oos_uses: int,
    ) -> None:
        self._repository = repository
        self._maximum_submissions = maximum_submissions
        self._maximum_oos_uses = maximum_oos_uses

    def reserve(
        self,
        request: OosEvaluationRequest,
        *,
        created_at: datetime,
        expires_at: datetime,
    ) -> OosBudgetReservationV1:
        idempotency_key = stable_id(
            "oos-reserve",
            request.challenger_id,
            request.experiment_family,
            request.submission_number,
            request.candidate_artifact_hash,
            request.evaluation_contract_hash,
        )
        return self._repository.reserve_oos_budget(
            request=request,
            maximum_submissions=self._maximum_submissions,
            maximum_oos_uses=self._maximum_oos_uses,
            idempotency_key=idempotency_key,
            created_at=created_at,
            expires_at=expires_at,
        )


@dataclass(frozen=True, slots=True)
class _ProductionBackend:
    budget: PersistentOosBudgetAdapter
    client: OosProcessClient
    config: OosProcessEvaluationConfig


@dataclass(frozen=True, slots=True)
class _ProductionBackendV2:
    budget: PersistentOosBudgetAdapter
    client: OosProcessClient
    config: OosProcessEvaluationConfigV2


class OosLockboxService:
    """Aggregate-only OOS boundary with legacy test fakes and a process backend."""

    def __init__(
        self,
        *,
        evaluator: PrivateOosEvaluator,
        budget_ledger: OosBudgetLedger,
        minimum_common_sessions: int,
        minimum_mean_daily_difference: float,
    ) -> None:
        self._evaluator: PrivateOosEvaluator | None = evaluator
        self._budget_ledger: OosBudgetLedger | None = budget_ledger
        self._minimum_common_sessions = minimum_common_sessions
        self._minimum_mean_daily_difference = minimum_mean_daily_difference
        self._production_backend: _ProductionBackend | None = None

    @classmethod
    def production(
        cls,
        *,
        repository: OosReservationRepository,
        private_root: Path,
        config: OosProcessEvaluationConfig,
        python_executable: Path | None = None,
        timeout_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> OosLockboxService:
        service = cls.__new__(cls)
        service._evaluator = None
        service._budget_ledger = None
        service._minimum_common_sessions = config.minimum_common_sessions
        service._minimum_mean_daily_difference = (
            config.minimum_mean_daily_difference
        )
        service._production_backend = _ProductionBackend(
            budget=PersistentOosBudgetAdapter(
                repository,
                maximum_submissions=config.maximum_submissions,
                maximum_oos_uses=config.maximum_oos_uses,
            ),
            client=OosProcessClient(
                private_root=private_root,
                python_executable=python_executable,
                timeout_seconds=(
                    config.worker_timeout_seconds
                    if timeout_seconds is None
                    else timeout_seconds
                ),
                clock=clock,
            ),
            config=config,
        )
        return service

    def evaluate(
        self,
        request: OosEvaluationRequest,
        *,
        evaluated_at: datetime,
    ) -> OosLockboxResultV1:
        timestamp = require_aware_utc(evaluated_at)
        if self._production_backend is not None:
            return self._evaluate_production(request, evaluated_at=timestamp)
        if self._budget_ledger is None or self._evaluator is None:
            raise OosLockboxError("OOS_LOCKBOX_NOT_CONFIGURED")
        consumed = self._budget_ledger.reserve(
            experiment_family=request.experiment_family,
            submission_number=request.submission_number,
        )
        observations = tuple(self._evaluator.evaluate(request))
        keys = [item.session_key for item in observations]
        if len(keys) != len(set(keys)):
            raise OosLockboxError("duplicate private OOS session keys")
        differences = [
            item.candidate_return - item.matched_baseline_return
            for item in observations
        ]
        count = len(differences)
        mean_difference = sum(differences) / count if count else 0.0
        reason_codes: list[str] = []
        if count < self._minimum_common_sessions:
            reason_codes.append("INSUFFICIENT_COMMON_SESSIONS")
        if mean_difference < self._minimum_mean_daily_difference:
            reason_codes.append("MINIMUM_ECONOMIC_EFFECT_NOT_MET")
        verdict = OosVerdict.FAIL if reason_codes else OosVerdict.PASS
        if not reason_codes:
            reason_codes.append("PREDECLARED_OOS_CRITERIA_PASSED")
        aggregate: Mapping[str, float] = {
            "mean_daily_difference": mean_difference,
        }
        payload = {
            "schema_version": "oos_lockbox_result_v1",
            "challenger_id": request.challenger_id,
            "experiment_family": request.experiment_family,
            "submission_number": request.submission_number,
            "candidate_artifact_hash": request.candidate_artifact_hash,
            "evaluation_contract_hash": request.evaluation_contract_hash,
            "verdict": verdict,
            "reason_codes": reason_codes,
            "aggregate_statistics": dict(aggregate),
            "common_sessions": count,
            "budget_consumed": consumed,
            "evaluated_at": timestamp,
        }
        return OosLockboxResultV1.model_validate(
            {
                **payload,
                "result_hash": canonical_hash(payload),
            }
        )

    def _evaluate_production(
        self,
        request: OosEvaluationRequest,
        *,
        evaluated_at: datetime,
    ) -> OosLockboxResultV1:
        backend = self._production_backend
        if backend is None:
            raise OosLockboxError("OOS_LOCKBOX_NOT_CONFIGURED")
        expires_at = evaluated_at + timedelta(
            seconds=backend.config.request_ttl_seconds
        )
        reservation = backend.budget.reserve(
            request,
            created_at=evaluated_at,
            expires_at=expires_at,
        )
        bound_evaluated_at = reservation.created_at
        bound_expires_at = reservation.expires_at
        worker_payload = {
            "schema_version": "oos_worker_request_v1",
            "request_id": stable_id(
                "oos-worker-request",
                reservation.reservation_id,
                reservation.reservation_hash,
                backend.config.dataset_manifest_hash,
            ),
            "challenger_id": request.challenger_id,
            "experiment_family": request.experiment_family,
            "submission_number": request.submission_number,
            "candidate_artifact_hash": request.candidate_artifact_hash,
            "evaluation_contract_hash": request.evaluation_contract_hash,
            "reservation_id": reservation.reservation_id,
            "reservation_hash": reservation.reservation_hash,
            "oos_budget_ordinal": reservation.oos_budget_ordinal,
            "dataset_id": backend.config.dataset_id,
            "dataset_manifest_hash": backend.config.dataset_manifest_hash,
            "expected_source_data_manifest_hash": (
                backend.config.expected_source_data_manifest_hash
            ),
            "expected_candidate_replay_hash": (
                backend.config.expected_candidate_replay_hash
            ),
            "expected_trusted_producer_version": (
                backend.config.expected_trusted_producer_version
            ),
            "data_available_cutoff": backend.config.data_available_cutoff,
            "minimum_common_sessions": backend.config.minimum_common_sessions,
            "minimum_mean_daily_difference": (
                backend.config.minimum_mean_daily_difference
            ),
            "annualization_sessions": backend.config.annualization_sessions,
            "newey_west_lag": backend.config.newey_west_lag,
            "bootstrap_seed": backend.config.bootstrap_seed,
            "bootstrap_block_length": backend.config.bootstrap_block_length,
            "bootstrap_samples": backend.config.bootstrap_samples,
            "base_cost_bps": backend.config.base_cost_bps,
            "cost_sensitivity_bps": backend.config.cost_sensitivity_bps,
            "evaluated_at": bound_evaluated_at,
            "expires_at": bound_expires_at,
        }
        worker_request = OosWorkerRequestV1.model_validate(
            {
                **worker_payload,
                "request_hash": canonical_hash(worker_payload),
            }
        )
        response = backend.client.evaluate(worker_request)
        result = response.result
        if (
            result.budget_consumed != reservation.oos_budget_ordinal
            or result.challenger_id != request.challenger_id
            or result.experiment_family != request.experiment_family
            or result.submission_number != request.submission_number
            or result.candidate_artifact_hash
            != request.candidate_artifact_hash
            or result.evaluation_contract_hash
            != request.evaluation_contract_hash
        ):
            raise OosLockboxError("OOS_WORKER_RESULT_BINDING_MISMATCH")
        return result


class OosLockboxServiceV2:
    """Production-only portfolio-level OOS boundary."""

    def __init__(self, backend: _ProductionBackendV2) -> None:
        self._backend = backend

    @classmethod
    def production(
        cls,
        *,
        repository: OosReservationRepository,
        private_root: Path,
        config: OosProcessEvaluationConfigV2,
        python_executable: Path | None = None,
        timeout_seconds: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> OosLockboxServiceV2:
        return cls(
            _ProductionBackendV2(
                budget=PersistentOosBudgetAdapter(
                    repository,
                    maximum_submissions=config.maximum_submissions,
                    maximum_oos_uses=config.maximum_oos_uses,
                ),
                client=OosProcessClient(
                    private_root=private_root,
                    python_executable=python_executable,
                    timeout_seconds=(
                        config.worker_timeout_seconds
                        if timeout_seconds is None
                        else timeout_seconds
                    ),
                    clock=clock,
                ),
                config=config,
            )
        )

    def evaluate(
        self,
        request: OosEvaluationRequest,
        *,
        evaluated_at: datetime,
    ) -> OosLockboxResultV2:
        timestamp = require_aware_utc(evaluated_at)
        config = self._backend.config
        if (
            request.candidate_artifact_hash
            != config.portfolio_comparison_contract.candidate_artifact_hash
        ):
            raise OosLockboxError("OOS_PORTFOLIO_CONTRACT_BINDING_MISMATCH")
        expires_at = timestamp + timedelta(seconds=config.request_ttl_seconds)
        reservation = self._backend.budget.reserve(
            request,
            created_at=timestamp,
            expires_at=expires_at,
        )
        worker_payload = {
            "schema_version": "oos_worker_request_v2",
            "request_id": stable_id(
                "oos-worker-request-v2",
                reservation.reservation_id,
                reservation.reservation_hash,
                config.dataset_manifest_hash,
                config.portfolio_comparison_contract.contract_hash,
            ),
            "challenger_id": request.challenger_id,
            "experiment_family": request.experiment_family,
            "submission_number": request.submission_number,
            "candidate_artifact_hash": request.candidate_artifact_hash,
            "evaluation_contract_hash": request.evaluation_contract_hash,
            "reservation_id": reservation.reservation_id,
            "reservation_hash": reservation.reservation_hash,
            "oos_budget_ordinal": reservation.oos_budget_ordinal,
            "dataset_id": config.dataset_id,
            "dataset_manifest_hash": config.dataset_manifest_hash,
            "expected_source_data_manifest_hash": (
                config.expected_source_data_manifest_hash
            ),
            "expected_candidate_replay_hash": (
                config.expected_candidate_replay_hash
            ),
            "expected_trusted_producer_version": (
                "trusted_candidate_evaluation_v2"
            ),
            "portfolio_comparison_contract": (
                config.portfolio_comparison_contract
            ),
            "data_available_cutoff": config.data_available_cutoff,
            "minimum_common_sessions": config.minimum_common_sessions,
            "minimum_independent_trades": config.minimum_independent_trades,
            "minimum_delta_sharpe_lcb": config.minimum_delta_sharpe_lcb,
            "minimum_worst_cost_delta_sharpe_lcb": (
                config.minimum_worst_cost_delta_sharpe_lcb
            ),
            "evaluated_at": reservation.created_at,
            "expires_at": reservation.expires_at,
        }
        worker_request = OosWorkerRequestV2.model_validate(
            {
                **worker_payload,
                "request_hash": canonical_hash(worker_payload),
            }
        )
        response = self._backend.client.evaluate_v2(worker_request)
        result = response.result
        if (
            result.budget_consumed != reservation.oos_budget_ordinal
            or result.challenger_id != request.challenger_id
            or result.experiment_family != request.experiment_family
            or result.submission_number != request.submission_number
            or result.candidate_artifact_hash
            != request.candidate_artifact_hash
            or result.evaluation_contract_hash
            != request.evaluation_contract_hash
            or result.portfolio_comparison_contract_hash
            != config.portfolio_comparison_contract.contract_hash
        ):
            raise OosLockboxError("OOS_WORKER_RESULT_BINDING_MISMATCH")
        return result


def _minimal_worker_environment(private_root: Path) -> dict[str, str]:
    environment = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "TRADING_OOS_PRIVATE_ROOT": str(private_root),
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _require_response_binding(
    request: OosWorkerRequestV1,
    response: OosWorkerResponseV1,
) -> None:
    if (
        response.request_id != request.request_id
        or response.request_hash != request.request_hash
        or response.reservation_id != request.reservation_id
        or response.reservation_hash != request.reservation_hash
    ):
        raise OosLockboxError("OOS_WORKER_RESPONSE_BINDING_MISMATCH")


def _require_response_binding_v2(
    request: OosWorkerRequestV2,
    response: OosWorkerResponseV2,
) -> None:
    if (
        response.request_id != request.request_id
        or response.request_hash != request.request_hash
        or response.reservation_id != request.reservation_id
        or response.reservation_hash != request.reservation_hash
    ):
        raise OosLockboxError("OOS_WORKER_RESPONSE_BINDING_MISMATCH")
