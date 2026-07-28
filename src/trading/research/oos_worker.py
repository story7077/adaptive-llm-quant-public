from __future__ import annotations

import json
import math
import os
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from pydantic import ValidationError

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.research.contracts import (
    OosLockboxResultV1,
    OosVerdict,
    OosWorkerRequestV1,
    OosWorkerResponseV1,
)

PRIVATE_ROOT_ENV = "TRADING_OOS_PRIVATE_ROOT"
MAX_DATASET_BYTES = 32 * 1024 * 1024
PRIVATE_DATASET_SCHEMA_VERSION = "oos_private_dataset_v1"
TRUSTED_DATASET_PRODUCER_VERSION = "trusted_candidate_evaluation_v1"


class PrivateDatasetError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class _Observation:
    session_key: str
    available_at: datetime
    candidate_return: float
    matched_baseline_return: float
    candidate_turnover: float
    matched_baseline_turnover: float


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
    try:
        return require_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError) as exc:
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID") from exc


def _parse_number(value: object, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
    parsed = float(value)
    if not math.isfinite(parsed) or (nonnegative and parsed < 0):
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
    return parsed


def _parse_sha256(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
    return value


def _load_private_dataset(
    request: OosWorkerRequestV1,
    private_root: Path,
) -> tuple[_Observation, ...]:
    try:
        root = private_root.resolve(strict=True)
        dataset_path = (root / f"{request.dataset_id}.json").resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PrivateDatasetError("LOCKBOX_DATA_UNAVAILABLE") from exc
    if not dataset_path.is_relative_to(root) or not dataset_path.is_file():
        raise PrivateDatasetError("LOCKBOX_DATA_UNAVAILABLE")
    try:
        if dataset_path.stat().st_size > MAX_DATASET_BYTES:
            raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
        decoded_object: object = json.loads(
            dataset_path.read_text(encoding="utf-8")
        )
    except PrivateDatasetError:
        raise
    except (OSError, UnicodeError) as exc:
        raise PrivateDatasetError("LOCKBOX_DATA_UNAVAILABLE") from exc
    except json.JSONDecodeError as exc:
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID") from exc
    if not isinstance(decoded_object, dict):
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
    decoded = cast(dict[str, object], decoded_object)
    required_root = {
        "schema_version",
        "dataset_id",
        "candidate_artifact_hash",
        "evaluation_contract_hash",
        "source_data_manifest_hash",
        "candidate_replay_hash",
        "trusted_producer_version",
        "observations",
        "dataset_hash",
    }
    if set(decoded) != required_root:
        raise PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
    if decoded["schema_version"] != PRIVATE_DATASET_SCHEMA_VERSION:
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
    if decoded["dataset_id"] != request.dataset_id:
        raise PrivateDatasetError("LOCKBOX_DATA_HASH_MISMATCH")
    if (
        decoded["trusted_producer_version"]
        != TRUSTED_DATASET_PRODUCER_VERSION
        or decoded["trusted_producer_version"]
        != request.expected_trusted_producer_version
    ):
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
    candidate_artifact_hash = _parse_sha256(
        decoded["candidate_artifact_hash"]
    )
    evaluation_contract_hash = _parse_sha256(
        decoded["evaluation_contract_hash"]
    )
    source_data_manifest_hash = _parse_sha256(
        decoded["source_data_manifest_hash"]
    )
    candidate_replay_hash = _parse_sha256(
        decoded["candidate_replay_hash"]
    )
    dataset_hash = _parse_sha256(decoded["dataset_hash"])
    hash_payload: dict[str, object] = {
        key: value for key, value in decoded.items() if key != "dataset_hash"
    }
    try:
        computed_hash = canonical_hash(hash_payload)
    except ValueError as exc:
        raise PrivateDatasetError("LOCKBOX_DATA_INVALID") from exc
    if (
        computed_hash != dataset_hash
        or dataset_hash != request.dataset_manifest_hash
        or candidate_artifact_hash != request.candidate_artifact_hash
        or evaluation_contract_hash != request.evaluation_contract_hash
        or source_data_manifest_hash
        != request.expected_source_data_manifest_hash
        or candidate_replay_hash != request.expected_candidate_replay_hash
    ):
        raise PrivateDatasetError("LOCKBOX_DATA_HASH_MISMATCH")
    raw_observations_object = decoded["observations"]
    if not isinstance(raw_observations_object, list):
        raise PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
    raw_observations = cast(list[object], raw_observations_object)
    observations: list[_Observation] = []
    seen: set[str] = set()
    required_row = {
        "session_key",
        "available_at",
        "candidate_return",
        "matched_baseline_return",
        "candidate_turnover",
        "matched_baseline_turnover",
    }
    for raw_object in raw_observations:
        if not isinstance(raw_object, dict):
            raise PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
        raw = cast(dict[str, object], raw_object)
        if set(raw) != required_row:
            raise PrivateDatasetError("LOCKBOX_DATA_INCOMPLETE")
        session_key = raw["session_key"]
        if not isinstance(session_key, str) or not session_key:
            raise PrivateDatasetError("LOCKBOX_DATA_INVALID")
        if session_key in seen:
            raise PrivateDatasetError("LOCKBOX_DATA_DUPLICATE")
        seen.add(session_key)
        available_at = _parse_time(raw["available_at"])
        if available_at > request.data_available_cutoff:
            raise PrivateDatasetError("LOCKBOX_DATA_PIT_INVALID")
        observations.append(
            _Observation(
                session_key=session_key,
                available_at=available_at,
                candidate_return=_parse_number(raw["candidate_return"]),
                matched_baseline_return=_parse_number(
                    raw["matched_baseline_return"]
                ),
                candidate_turnover=_parse_number(
                    raw["candidate_turnover"],
                    nonnegative=True,
                ),
                matched_baseline_turnover=_parse_number(
                    raw["matched_baseline_turnover"],
                    nonnegative=True,
                ),
            )
        )
    return tuple(observations)


def _newey_west_standard_error(values: Sequence[float], lag: int) -> float:
    count = len(values)
    if count < 2:
        return 0.0
    mean = sum(values) / count
    centered = [value - mean for value in values]
    long_run_variance = sum(value * value for value in centered) / count
    bounded_lag = min(lag, count - 1)
    for offset in range(1, bounded_lag + 1):
        covariance = (
            sum(
                centered[index] * centered[index - offset]
                for index in range(offset, count)
            )
            / count
        )
        weight = 1.0 - (offset / (bounded_lag + 1))
        long_run_variance += 2.0 * weight * covariance
    return math.sqrt(max(0.0, long_run_variance) / count)


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
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


def _stationary_bootstrap_interval(
    values: Sequence[float],
    *,
    seed: int,
    block_length: int,
    samples: int,
) -> tuple[float, float]:
    count = len(values)
    if count == 0:
        return 0.0, 0.0
    rng = random.Random(seed)
    restart_probability = 1.0 / block_length
    means: list[float] = []
    for _ in range(samples):
        index = rng.randrange(count)
        total = 0.0
        for _ in range(count):
            total += values[index]
            if rng.random() < restart_probability:
                index = rng.randrange(count)
            else:
                index = (index + 1) % count
        means.append(total / count)
    means.sort()
    return _percentile(means, 0.025), _percentile(means, 0.975)


def _cost_adjusted_differences(
    observations: Sequence[_Observation],
    cost_bps: int,
) -> list[float]:
    cost_rate = cost_bps / 10_000.0
    return [
        (item.candidate_return - item.matched_baseline_return)
        - cost_rate
        * (item.candidate_turnover - item.matched_baseline_turnover)
        for item in observations
    ]


def _result_payload(
    request: OosWorkerRequestV1,
    *,
    verdict: OosVerdict,
    reason_codes: Sequence[str],
    aggregate_statistics: Mapping[str, float],
    common_sessions: int,
) -> dict[str, Any]:
    return {
        "schema_version": "oos_lockbox_result_v1",
        "challenger_id": request.challenger_id,
        "experiment_family": request.experiment_family,
        "submission_number": request.submission_number,
        "candidate_artifact_hash": request.candidate_artifact_hash,
        "evaluation_contract_hash": request.evaluation_contract_hash,
        "verdict": verdict,
        "reason_codes": list(reason_codes),
        "aggregate_statistics": dict(aggregate_statistics),
        "common_sessions": common_sessions,
        "budget_consumed": request.oos_budget_ordinal,
        "evaluated_at": request.evaluated_at,
    }


def _failure_result(
    request: OosWorkerRequestV1,
    reason_code: str,
) -> OosLockboxResultV1:
    payload = _result_payload(
        request,
        verdict=OosVerdict.FAIL,
        reason_codes=(reason_code,),
        aggregate_statistics={},
        common_sessions=0,
    )
    return OosLockboxResultV1.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )


def evaluate_private_request(
    request: OosWorkerRequestV1,
    *,
    private_root: Path,
) -> OosWorkerResponseV1:
    try:
        observations = _load_private_dataset(request, private_root)
    except PrivateDatasetError as exc:
        result = _failure_result(request, exc.reason_code)
    else:
        count = len(observations)
        differences = _cost_adjusted_differences(
            observations,
            request.base_cost_bps,
        )
        mean_difference = sum(differences) / count if count else 0.0
        interval = _stationary_bootstrap_interval(
            differences,
            seed=request.bootstrap_seed,
            block_length=request.bootstrap_block_length,
            samples=request.bootstrap_samples,
        )
        aggregate = {
            "mean_daily_difference": mean_difference,
            "annualized_difference": (
                mean_difference * request.annualization_sessions
            ),
            "newey_west_standard_error": _newey_west_standard_error(
                differences,
                request.newey_west_lag,
            ),
            "bootstrap_ci_lower": interval[0],
            "bootstrap_ci_upper": interval[1],
        }
        for cost_bps in request.cost_sensitivity_bps:
            sensitivity = _cost_adjusted_differences(observations, cost_bps)
            sensitivity_mean = (
                sum(sensitivity) / len(sensitivity) if sensitivity else 0.0
            )
            aggregate[
                f"cost_sensitivity_{cost_bps}_bps_annualized_difference"
            ] = sensitivity_mean * request.annualization_sessions
        reason_codes: list[str] = []
        if count < request.minimum_common_sessions:
            reason_codes.append("INSUFFICIENT_COMMON_SESSIONS")
        if mean_difference < request.minimum_mean_daily_difference:
            reason_codes.append("COST_ADJUSTED_EFFECT_NOT_MET")
        if reason_codes:
            verdict = OosVerdict.FAIL
        else:
            verdict = OosVerdict.PASS
            reason_codes.append("PREDECLARED_OOS_CRITERIA_PASSED")
        payload = _result_payload(
            request,
            verdict=verdict,
            reason_codes=reason_codes,
            aggregate_statistics=aggregate,
            common_sessions=count,
        )
        result = OosLockboxResultV1.model_validate(
            {**payload, "result_hash": canonical_hash(payload)}
        )
    response_payload = {
        "schema_version": "oos_worker_response_v1",
        "request_id": request.request_id,
        "request_hash": request.request_hash,
        "reservation_id": request.reservation_id,
        "reservation_hash": request.reservation_hash,
        "result": result,
    }
    return OosWorkerResponseV1.model_validate(
        {
            **response_payload,
            "response_hash": canonical_hash(response_payload),
        }
    )


def _abort() -> NoReturn:
    raise SystemExit(2)


def main() -> None:
    private_root_value = os.environ.get(PRIVATE_ROOT_ENV)
    if not private_root_value:
        _abort()
    try:
        raw_request = sys.stdin.buffer.read(1_048_577)
        if len(raw_request) > 1_048_576:
            _abort()
        decoded = json.loads(raw_request.decode("utf-8"))
        request = OosWorkerRequestV1.model_validate(decoded)
        response = evaluate_private_request(
            request,
            private_root=Path(private_root_value),
        )
        serialized = response.model_dump_json()
    except (
        json.JSONDecodeError,
        UnicodeError,
        ValidationError,
        ValueError,
        OSError,
    ):
        _abort()
    sys.stdout.write(serialized)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
