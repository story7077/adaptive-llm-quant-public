from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.contracts import OosVerdict, OosWorkerRequestV1
from trading.research.oos_lockbox import OosLockboxError, OosProcessClient
from trading.research.oos_worker import evaluate_private_request

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=1)


def _observations(count: int = 126) -> list[dict[str, object]]:
    return [
        {
            "session_key": f"locked-{index:03d}",
            "available_at": (CUTOFF - timedelta(days=count - index)).isoformat(),
            "candidate_return": 0.0015 + (index % 7) * 0.00001,
            "matched_baseline_return": 0.0005,
            "candidate_turnover": 0.10,
            "matched_baseline_turnover": 0.05,
        }
        for index in range(count)
    ]


def _write_dataset(
    private_root: Path,
    *,
    dataset_id: str = "synthetic-v1",
    observations: list[dict[str, object]] | None = None,
    candidate_artifact_hash: str = "a" * 64,
    evaluation_contract_hash: str = "b" * 64,
) -> str:
    payload = {
        "schema_version": "oos_private_dataset_v1",
        "dataset_id": dataset_id,
        "candidate_artifact_hash": candidate_artifact_hash,
        "evaluation_contract_hash": evaluation_contract_hash,
        "source_data_manifest_hash": "d" * 64,
        "candidate_replay_hash": "e" * 64,
        "trusted_producer_version": "trusted_candidate_evaluation_v1",
        "observations": _observations() if observations is None else observations,
    }
    dataset_hash = canonical_hash(payload)
    private_root.mkdir(parents=True, exist_ok=True)
    (private_root / f"{dataset_id}.json").write_text(
        json.dumps({**payload, "dataset_hash": dataset_hash}),
        encoding="utf-8",
    )
    return dataset_hash


def _request(
    dataset_hash: str,
    *,
    dataset_id: str = "synthetic-v1",
    cutoff: datetime = CUTOFF,
    expected_source_data_manifest_hash: str = "d" * 64,
    expected_candidate_replay_hash: str = "e" * 64,
    expected_trusted_producer_version: str = (
        "trusted_candidate_evaluation_v1"
    ),
) -> OosWorkerRequestV1:
    payload = {
        "schema_version": "oos_worker_request_v1",
        "request_id": "oos-request-1",
        "challenger_id": "challenger-1",
        "experiment_family": "family-1",
        "submission_number": 1,
        "candidate_artifact_hash": "a" * 64,
        "evaluation_contract_hash": "b" * 64,
        "reservation_id": "reservation-1",
        "reservation_hash": "c" * 64,
        "oos_budget_ordinal": 1,
        "dataset_id": dataset_id,
        "dataset_manifest_hash": dataset_hash,
        "expected_source_data_manifest_hash": (
            expected_source_data_manifest_hash
        ),
        "expected_candidate_replay_hash": expected_candidate_replay_hash,
        "expected_trusted_producer_version": expected_trusted_producer_version,
        "data_available_cutoff": cutoff,
        "minimum_common_sessions": 126,
        "minimum_mean_daily_difference": 0.0005,
        "annualization_sessions": 252,
        "newey_west_lag": 5,
        "bootstrap_seed": 7077,
        "bootstrap_block_length": 10,
        "bootstrap_samples": 100,
        "base_cost_bps": 10,
        "cost_sensitivity_bps": (0, 5, 10),
        "evaluated_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    return OosWorkerRequestV1.model_validate(
        {**payload, "request_hash": canonical_hash(payload)}
    )


def test_private_worker_returns_only_hash_bound_aggregates(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(private_root)
    request = _request(dataset_hash)

    first = evaluate_private_request(request, private_root=private_root)
    second = evaluate_private_request(request, private_root=private_root)

    assert first == second
    assert first.result.verdict is OosVerdict.PASS
    assert first.result.common_sessions == 126
    assert set(first.result.aggregate_statistics) == {
        "mean_daily_difference",
        "annualized_difference",
        "newey_west_standard_error",
        "bootstrap_ci_lower",
        "bootstrap_ci_upper",
        "cost_sensitivity_0_bps_annualized_difference",
        "cost_sensitivity_5_bps_annualized_difference",
        "cost_sensitivity_10_bps_annualized_difference",
    }
    serialized = first.model_dump_json()
    assert "locked-000" not in serialized
    assert str(private_root) not in serialized
    assert "candidate_return" not in serialized


def test_process_client_executes_worker_out_of_process(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(private_root)
    request = _request(dataset_hash)
    client = OosProcessClient(
        private_root=private_root,
        clock=lambda: NOW,
    )

    response = client.evaluate(request)

    assert response.result.verdict is OosVerdict.PASS
    assert response.request_hash == request.request_hash
    assert str(private_root) not in response.model_dump_json()


@pytest.mark.parametrize(
    ("observations", "cutoff", "expected_reason"),
    [
        (
            [
                *_observations(125),
                {
                    **_observations(1)[0],
                    "session_key": "locked-000",
                },
            ],
            CUTOFF,
            "LOCKBOX_DATA_DUPLICATE",
        ),
        (
            [
                {
                    **item,
                    "available_at": (NOW + timedelta(minutes=1)).isoformat(),
                }
                for item in _observations()
            ],
            CUTOFF,
            "LOCKBOX_DATA_PIT_INVALID",
        ),
        (
            [
                {
                    key: value
                    for key, value in item.items()
                    if key != "candidate_turnover"
                }
                for item in _observations()
            ],
            CUTOFF,
            "LOCKBOX_DATA_INCOMPLETE",
        ),
    ],
)
def test_worker_fails_closed_for_private_dataset_contract_violations(
    tmp_path: Path,
    observations: list[dict[str, object]],
    cutoff: datetime,
    expected_reason: str,
) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(private_root, observations=observations)

    response = evaluate_private_request(
        _request(dataset_hash, cutoff=cutoff),
        private_root=private_root,
    )

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == [expected_reason]
    assert response.result.common_sessions == 0
    assert response.result.aggregate_statistics == {}


def test_worker_rejects_dataset_hash_mismatch_without_details(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    _write_dataset(private_root)

    response = evaluate_private_request(
        _request("f" * 64),
        private_root=private_root,
    )

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == ["LOCKBOX_DATA_HASH_MISMATCH"]
    assert response.result.aggregate_statistics == {}


@pytest.mark.parametrize(
    ("artifact_hash", "contract_hash"),
    [
        ("f" * 64, "b" * 64),
        ("a" * 64, "f" * 64),
    ],
)
def test_worker_rejects_dataset_bound_to_another_candidate_or_contract(
    tmp_path: Path,
    artifact_hash: str,
    contract_hash: str,
) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(
        private_root,
        candidate_artifact_hash=artifact_hash,
        evaluation_contract_hash=contract_hash,
    )

    response = evaluate_private_request(
        _request(dataset_hash),
        private_root=private_root,
    )

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == ["LOCKBOX_DATA_HASH_MISMATCH"]
    assert response.result.aggregate_statistics == {}


@pytest.mark.parametrize(
    ("request_overrides", "expected_reason"),
    [
        (
            {"expected_source_data_manifest_hash": "f" * 64},
            "LOCKBOX_DATA_HASH_MISMATCH",
        ),
        (
            {"expected_candidate_replay_hash": "f" * 64},
            "LOCKBOX_DATA_HASH_MISMATCH",
        ),
    ],
)
def test_worker_rejects_dataset_with_unexpected_source_or_replay_binding(
    tmp_path: Path,
    request_overrides: dict[str, str],
    expected_reason: str,
) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(private_root)

    response = evaluate_private_request(
        _request(dataset_hash, **request_overrides),
        private_root=private_root,
    )

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == [expected_reason]
    assert response.result.aggregate_statistics == {}


def test_cost_adjusted_effect_and_minimum_session_gate_fail_closed(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(
        private_root,
        observations=_observations(125),
    )

    response = evaluate_private_request(
        _request(dataset_hash),
        private_root=private_root,
    )

    assert response.result.verdict is OosVerdict.FAIL
    assert "INSUFFICIENT_COMMON_SESSIONS" in response.result.reason_codes
    assert response.result.common_sessions == 125


def test_base_cost_is_applied_before_economic_effect_gate(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    observations = [
        {
            **item,
            "candidate_return": 0.0007,
            "matched_baseline_return": 0.0005,
            "candidate_turnover": 1.0,
            "matched_baseline_turnover": 0.0,
        }
        for item in _observations()
    ]
    dataset_hash = _write_dataset(private_root, observations=observations)

    response = evaluate_private_request(
        _request(dataset_hash),
        private_root=private_root,
    )

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == ["COST_ADJUSTED_EFFECT_NOT_MET"]
    assert response.result.aggregate_statistics["mean_daily_difference"] < 0


def test_expired_process_request_is_rejected_before_worker_spawn(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    dataset_hash = _write_dataset(private_root)
    request = _request(dataset_hash)
    client = OosProcessClient(
        private_root=private_root,
        clock=lambda: request.expires_at,
    )

    with pytest.raises(OosLockboxError, match="OOS_WORKER_REQUEST_EXPIRED"):
        client.evaluate(request)
