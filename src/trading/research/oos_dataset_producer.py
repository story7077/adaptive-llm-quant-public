from __future__ import annotations

import hashlib
import os
import re
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from pydantic import Field, model_validator

from trading.domain.contracts import DomainModel
from trading.domain.hashing import canonical_hash, canonical_json, stable_id
from trading.research.candidate_abi import CandidateExecutor
from trading.research.candidate_evaluation import (
    CandidateEvaluationDatasetV1,
    CandidateEvaluationError,
    CandidateEvaluationScenarioV1,
    execute_candidate_dataset_twice,
)
from trading.research.contracts import HASH_PATTERN, IDENTIFIER_PATTERN
from trading.research.evaluation_contracts import BASE_VARIANT_ID
from trading.research.oos_worker import (
    PRIVATE_DATASET_SCHEMA_VERSION,
    TRUSTED_DATASET_PRODUCER_VERSION,
)


class PrivateOosDatasetManifestV1(DomainModel):
    """Non-row metadata safe to return across the OOS lockbox boundary."""

    schema_version: str = Field(default="private_oos_dataset_manifest_v1")
    dataset_id: str = Field(pattern=IDENTIFIER_PATTERN)
    candidate_artifact_hash: str = Field(pattern=HASH_PATTERN)
    evaluation_contract_hash: str = Field(pattern=HASH_PATTERN)
    source_data_manifest_hash: str = Field(pattern=HASH_PATTERN)
    candidate_replay_hash: str = Field(pattern=HASH_PATTERN)
    trusted_producer_version: str = Field(pattern=IDENTIFIER_PATTERN)
    common_session_count: int = Field(gt=0)
    private_file_hash: str = Field(pattern=HASH_PATTERN)
    dataset_hash: str = Field(pattern=HASH_PATTERN)
    manifest_hash: str = Field(pattern=HASH_PATTERN)

    @model_validator(mode="after")
    def validate_hash(self) -> PrivateOosDatasetManifestV1:
        payload = self.model_dump(mode="python", exclude={"manifest_hash"})
        if canonical_hash(payload) != self.manifest_hash:
            raise ValueError("private OOS manifest hash mismatch")
        return self


class PrivateOosDatasetProducerError(RuntimeError):
    pass


def produce_private_oos_dataset(
    *,
    dataset: CandidateEvaluationDatasetV1,
    executor: CandidateExecutor,
    evaluation_contract_hash: str,
    config_hash: str,
    code_hash: str,
    created_at: datetime,
    private_root: Path,
    private_dataset_id: str,
) -> PrivateOosDatasetManifestV1:
    """Run the Candidate on hidden features and atomically create one dataset."""

    if re.fullmatch(IDENTIFIER_PATTERN, private_dataset_id) is None:
        raise PrivateOosDatasetProducerError("private OOS dataset ID is invalid")
    for label, value in (
        ("evaluation contract", evaluation_contract_hash),
        ("config", config_hash),
        ("code", code_hash),
    ):
        if re.fullmatch(HASH_PATTERN, value) is None:
            raise PrivateOosDatasetProducerError(f"{label} hash is invalid")
    root = _validated_private_root(private_root)
    output_path = (root / f"{private_dataset_id}.json").resolve(strict=False)
    if output_path.parent != root:
        raise PrivateOosDatasetProducerError("private OOS output escaped its root")
    if output_path.exists() or output_path.is_symlink():
        raise PrivateOosDatasetProducerError("private OOS dataset is append-only")
    base_scenarios = _base_scenarios(dataset)
    base_dataset = _base_dataset(dataset, base_scenarios)
    try:
        execution = execute_candidate_dataset_twice(
            dataset=base_dataset,
            executor=executor,
            config_hash=config_hash,
            code_hash=code_hash,
            created_at=created_at,
        )
    except CandidateEvaluationError as exc:
        raise PrivateOosDatasetProducerError(str(exc)) from exc
    rows = tuple(
        _private_row(scenario, response)
        for scenario, response in zip(
            base_dataset.scenarios,
            execution.responses,
            strict=True,
        )
    )
    payload = {
        "schema_version": PRIVATE_DATASET_SCHEMA_VERSION,
        "dataset_id": private_dataset_id,
        "candidate_artifact_hash": dataset.candidate_artifact_hash,
        "evaluation_contract_hash": evaluation_contract_hash,
        "source_data_manifest_hash": dataset.source_data_manifest_hash,
        "candidate_replay_hash": execution.replay.artifact_hash,
        "trusted_producer_version": TRUSTED_DATASET_PRODUCER_VERSION,
        "observations": list(rows),
    }
    document = {**payload, "dataset_hash": canonical_hash(payload)}
    serialized = canonical_json(document)
    file_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    manifest_payload = {
        "schema_version": "private_oos_dataset_manifest_v1",
        "dataset_id": private_dataset_id,
        "candidate_artifact_hash": dataset.candidate_artifact_hash,
        "evaluation_contract_hash": evaluation_contract_hash,
        "source_data_manifest_hash": dataset.source_data_manifest_hash,
        "candidate_replay_hash": execution.replay.artifact_hash,
        "trusted_producer_version": TRUSTED_DATASET_PRODUCER_VERSION,
        "common_session_count": len(rows),
        "private_file_hash": file_hash,
        "dataset_hash": document["dataset_hash"],
    }
    manifest = PrivateOosDatasetManifestV1.model_validate(
        {**manifest_payload, "manifest_hash": canonical_hash(manifest_payload)}
    )
    _write_exclusive_atomic(output_path, serialized)
    return manifest


def _validated_private_root(private_root: Path) -> Path:
    try:
        root = private_root.resolve(strict=True)
    except OSError as exc:
        raise PrivateOosDatasetProducerError("private OOS root is unavailable") from exc
    if root.is_symlink() or not root.is_dir():
        raise PrivateOosDatasetProducerError("private OOS root is unsafe")
    is_junction = getattr(root, "is_junction", None)
    if callable(is_junction) and is_junction():
        raise PrivateOosDatasetProducerError("private OOS root is unsafe")
    return root


def _write_exclusive_atomic(output_path: Path, serialized: str) -> None:
    descriptor = -1
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".oos-dataset-",
            suffix=".tmp",
            dir=output_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, output_path)
    except FileExistsError:
        raise PrivateOosDatasetProducerError(
            "private OOS dataset is append-only"
        ) from None
    except OSError as exc:
        raise PrivateOosDatasetProducerError(
            f"private OOS dataset write failed: {type(exc).__name__}"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _base_scenarios(
    dataset: CandidateEvaluationDatasetV1,
) -> tuple[CandidateEvaluationScenarioV1, ...]:
    scenarios = tuple(
        item
        for item in dataset.scenarios
        if item.request.variant.key
        == (
            BASE_VARIANT_ID,
            BASE_VARIANT_ID,
            BASE_VARIANT_ID,
            BASE_VARIANT_ID,
            BASE_VARIANT_ID,
        )
    )
    decision_times = tuple(item.request.decision_time for item in scenarios)
    if not scenarios or len(decision_times) != len(set(decision_times)):
        raise PrivateOosDatasetProducerError(
            "private OOS requires exactly one base scenario per session"
        )
    return scenarios


def _base_dataset(
    dataset: CandidateEvaluationDatasetV1,
    scenarios: tuple[CandidateEvaluationScenarioV1, ...],
) -> CandidateEvaluationDatasetV1:
    from trading.research.candidate_evaluation import (
        build_candidate_evaluation_dataset,
    )

    return build_candidate_evaluation_dataset(
        dataset_id=dataset.dataset_id + "-base-oos",
        challenger_id=dataset.challenger_id,
        candidate_artifact_hash=dataset.candidate_artifact_hash,
        source_data_manifest_hash=dataset.source_data_manifest_hash,
        eligible_instrument_count=dataset.eligible_instrument_count,
        eligible_non_survivor_count=dataset.eligible_non_survivor_count,
        scenarios=scenarios,
    )


def _private_row(
    scenario: CandidateEvaluationScenarioV1,
    response: object,
) -> dict[str, object]:
    from trading.research.candidate_abi import CandidateDecisionResponseV1

    validated = CandidateDecisionResponseV1.model_validate(response)
    targets = {item.symbol: item.target_weight for item in validated.targets}
    current = {
        item.symbol: item.current_weight for item in scenario.request.instruments
    }
    outcomes = {item.symbol: item for item in scenario.outcomes}
    candidate_return = sum(
        targets[symbol] * outcomes[symbol].forward_return for symbol in targets
    )
    baseline_return = sum(
        item.baseline_target_weight * item.forward_return
        for item in scenario.outcomes
    )
    candidate_turnover = _one_way_turnover(current, targets)
    baseline_current = {
        item.symbol: item.baseline_current_weight for item in scenario.outcomes
    }
    baseline_target = {
        item.symbol: item.baseline_target_weight for item in scenario.outcomes
    }
    baseline_turnover = _one_way_turnover(baseline_current, baseline_target)
    return {
        "session_key": stable_id(
            "private-oos-session",
            scenario.request.decision_time,
            scenario.scenario_hash,
        ),
        "available_at": max(
            item.outcome_available_at for item in scenario.outcomes
        ),
        "candidate_return": candidate_return,
        "matched_baseline_return": baseline_return,
        "candidate_turnover": candidate_turnover,
        "matched_baseline_turnover": baseline_turnover,
    }


def _one_way_turnover(
    current: dict[str, float],
    target: dict[str, float],
) -> float:
    if set(current) != set(target):
        raise PrivateOosDatasetProducerError("turnover universes differ")
    risky = sum(abs(target[symbol] - current[symbol]) for symbol in current)
    current_cash = 1 - sum(current.values())
    target_cash = 1 - sum(target.values())
    return 0.5 * (risky + abs(target_cash - current_cash))
