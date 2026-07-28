from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.candidate_artifact import (
    CandidateArtifactBundleV1,
    CandidateRequestBindingV1,
    CandidateRuntimeV1,
    build_candidate_artifact_bundle,
)
from trading.research.candidate_experiment import (
    CandidateExperimentRegistrationError,
    _canonical_provenance,
    select_forward_maturity_sessions,
    verify_candidate_test_manifest,
)
from trading.research.contracts import ResearchCommanderKind
from trading.research.scheduler import VersionedResearchMarketSession

NOW = datetime(2026, 7, 28, 21, 0, tzinfo=UTC)


def _attestation(
    **overrides: object,
) -> tuple[dict[str, object], CandidateArtifactBundleV1]:
    runtime = CandidateRuntimeV1(
        implementation="CPython",
        version="3.13.12",
        abi_tag="cpython-313",
        executable_sha256="a" * 64,
    )
    manifest: dict[str, object] = {
        "schema_version": "candidate_test_manifest_v1",
        "status": "PASSED",
        "exit_code": 0,
        "source_snapshot_hash": "b" * 64,
        "candidate_tree_hash_before": "c" * 64,
        "candidate_tree_hash_after": "c" * 64,
        "patch_hash": "d" * 64,
        "proposal_hash": "e" * 64,
        "builder_result_hash": "f" * 64,
        "declared_entrypoint": (
            "trading.strategies.challengers.example:decide"
        ),
        "output_limit_exceeded": False,
        "candidate_tree_unchanged": True,
        "candidate_source_projection_unchanged": True,
        "candidate_test_projection_unchanged": True,
        "host_abi_test_unchanged": True,
        "host_principal_persisted": False,
        "raw_output_persisted": False,
        "broker_access_permitted": False,
        "credential_access_permitted": False,
        "network_access_permitted": False,
        "real_order_routing": False,
        "execution_contract_version": (
            "candidate-test-unelevated-workspace-v4"
        ),
        "runtime": runtime.model_dump(mode="python"),
        "test_count": {
            "collected": 3,
            "passed": 3,
            "failed": 0,
            "errors": 0,
        },
    }
    manifest.update(overrides)
    artifact = build_candidate_artifact_bundle(
        bundle_id="candidate-bundle-example",
        challenger_id="challenger-example",
        request_binding=CandidateRequestBindingV1(
            request_id="request-example",
            research_cycle_id="cycle-example",
            context_manifest_hash="1" * 64,
            source_snapshot_commit="2" * 40,
            champion_version="1.0.0",
            experiment_family="family-example",
            selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
            commander_selection_id="selection-example",
            commander_selection_version=1,
        ),
        source_snapshot_hash="b" * 64,
        candidate_tree_hash="c" * 64,
        code_hash="3" * 64,
        config_hash="4" * 64,
        patch_hash="d" * 64,
        proposal_hash="e" * 64,
        builder_result_hash="f" * 64,
        test_manifest_hash=canonical_hash(manifest),
        challenger_manifest_hash="5" * 64,
        validation_request_hash="6" * 64,
        runtime=runtime,
        declared_entrypoint=(
            "trading.strategies.challengers.example:decide"
        ),
    )
    return manifest, artifact


def _calendar_session(
    offset: int,
    *,
    available_at: datetime = NOW - timedelta(days=1),
    suffix: str = "",
) -> VersionedResearchMarketSession:
    session_date = date(2026, 7, 29) + timedelta(days=offset)
    open_at = datetime.combine(
        session_date,
        datetime.min.time(),
        tzinfo=UTC,
    ) + timedelta(hours=13, minutes=30)
    return VersionedResearchMarketSession(
        calendar_session_id=f"calendar-{offset}{suffix}",
        calendar_version="calendar-v1",
        session_date=session_date,
        open_at=open_at,
        close_at=open_at + timedelta(hours=6, minutes=30),
        available_at=available_at,
        session_hash=canonical_hash(
            {
                "offset": offset,
                "suffix": suffix,
                "available_at": available_at,
            }
        ),
    )


def test_candidate_test_attestation_is_bound_and_reduce_only() -> None:
    manifest, artifact = _attestation()

    verified = verify_candidate_test_manifest(
        manifest,
        artifact=artifact,
    )

    assert verified.manifest_hash == artifact.test_manifest_hash
    assert verified.passed_test_count == 3
    assert verified.execution_contract_version.endswith("-v4")
    assert artifact.real_order_routing is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "FAILED", "attestation mismatch"),
        ("network_access_permitted", True, "attestation mismatch"),
        ("host_principal_persisted", True, "attestation mismatch"),
        (
            "test_count",
            {"collected": 3, "passed": 2, "failed": 1, "errors": 0},
            "not a clean passing",
        ),
    ],
)
def test_candidate_test_attestation_fails_closed(
    field: str,
    value: object,
    message: str,
) -> None:
    manifest, artifact = _attestation(**{field: value})

    with pytest.raises(
        CandidateExperimentRegistrationError,
        match=message,
    ):
        verify_candidate_test_manifest(
            manifest,
            artifact=artifact,
        )


def test_candidate_test_attestation_rejects_unsealed_payload() -> None:
    manifest, artifact = _attestation()
    manifest["status"] = "FAILED"

    with pytest.raises(
        CandidateExperimentRegistrationError,
        match="hash does not match",
    ):
        verify_candidate_test_manifest(
            manifest,
            artifact=artifact,
        )


def test_maturity_uses_full_future_pit_sessions_and_latest_revision() -> None:
    sessions = tuple(_calendar_session(index) for index in range(64))
    revised = _calendar_session(
        10,
        available_at=NOW - timedelta(hours=1),
        suffix="-revision",
    )
    unavailable_revision = _calendar_session(
        11,
        available_at=NOW + timedelta(seconds=1),
        suffix="-future",
    )

    selected = select_forward_maturity_sessions(
        (*sessions, revised, unavailable_revision),
        decision_at=NOW,
        horizon_sessions=63,
    )

    assert len(selected) == 63
    assert selected[0].session_date == date(2026, 7, 29)
    assert selected[10].calendar_session_id.endswith("-revision")
    assert selected[11].calendar_session_id == "calendar-11"
    assert selected[-1].close_at > selected[0].close_at


def test_maturity_rejects_partial_and_insufficient_calendar() -> None:
    partial = _calendar_session(0).model_copy(
        update={"open_at": NOW - timedelta(hours=1)}
    )

    with pytest.raises(
        CandidateExperimentRegistrationError,
        match="required=2, available=1",
    ):
        select_forward_maturity_sessions(
            (partial, _calendar_session(1)),
            decision_at=NOW,
            horizon_sessions=2,
        )


def test_source_provenance_is_sorted_by_time_before_hash() -> None:
    earlier = NOW - timedelta(hours=2)
    later = NOW - timedelta(hours=1)

    hashes, available_at = _canonical_provenance(
        (
            ("0" * 64, later),
            ("f" * 64, earlier),
        )
    )

    assert hashes == ("f" * 64, "0" * 64)
    assert available_at == (earlier, later)
