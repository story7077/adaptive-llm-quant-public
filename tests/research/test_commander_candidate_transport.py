from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.candidate_abi import (
    CandidateDecisionConstraintsV1,
    CandidateDecisionRequestV1,
    CandidateEvaluationVariantV1,
    CandidateFeatureValueV1,
    CandidateInstrumentInputV1,
    CandidateTargetV1,
    build_candidate_decision_request,
    build_candidate_decision_response,
)
from trading.research.candidate_artifact import (
    CandidateRequestBindingV1,
    CandidateRuntimeV1,
    build_candidate_artifact_bundle,
)
from trading.research.candidate_process import build_candidate_process_result
from trading.research.commander_candidate import (
    CommanderCandidateError,
    HostProcessResult,
    connect_candidate_runtime,
)
from trading.research.config import load_research_config
from trading.research.contracts import ResearchCommanderKind

NOW = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64


def _bundle():
    return build_candidate_artifact_bundle(
        bundle_id="bundle-candidate-transport",
        challenger_id="challenger-candidate-transport",
        request_binding=CandidateRequestBindingV1(
            request_id="research-request-transport",
            research_cycle_id="research-cycle-transport",
            context_manifest_hash=HASH_A,
            source_snapshot_commit="1" * 40,
            champion_version="1.0.0",
            experiment_family="candidate-transport",
            selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
            commander_selection_id="selection-transport",
            commander_selection_version=1,
        ),
        source_snapshot_hash=HASH_B,
        candidate_tree_hash=HASH_C,
        code_hash=HASH_D,
        config_hash="e" * 64,
        patch_hash="f" * 64,
        proposal_hash="1" * 64,
        builder_result_hash="2" * 64,
        test_manifest_hash="3" * 64,
        challenger_manifest_hash="4" * 64,
        validation_request_hash="5" * 64,
        runtime=CandidateRuntimeV1(
            implementation="CPython",
            version="3.13.12",
            abi_tag="cpython-313",
            executable_sha256="6" * 64,
        ),
        declared_entrypoint="candidate.strategy:decide",
    )


def _request(bundle):
    feature_time = NOW - timedelta(minutes=1)
    instruments = (
        CandidateInstrumentInputV1(
            symbol="QQQ",
            current_weight=0,
            membership_available_at=NOW - timedelta(days=365),
            membership_valid_from=NOW - timedelta(days=365),
            instrument_is_non_survivor=False,
            features=(
                CandidateFeatureValueV1(
                    name="signal",
                    value=1,
                    source_event_time=feature_time,
                    available_at=feature_time,
                    source_revision=0,
                    revision_available_at=feature_time,
                    revision_was_known_at_cutoff=True,
                    source_hash=canonical_hash({"signal": 1}),
                ),
            ),
        ),
    )
    return build_candidate_decision_request(
        request_id="candidate-request-transport",
        challenger_id=bundle.challenger_id,
        candidate_artifact_hash=bundle.bundle_hash,
        strategy_id="Q1-DET",
        strategy_version="2.0.0",
        decision_time=NOW,
        signal_data_cutoff=NOW,
        variant=CandidateEvaluationVariantV1(),
        instruments=instruments,
        constraints=CandidateDecisionConstraintsV1(
            maximum_gross_weight=1,
            minimum_cash_weight=0,
            maximum_weight_by_symbol={"QQQ": 0.8},
            numeric_tolerance=1e-12,
        ),
        strategy_parameters={"signal_scale": 1.0},
        source_data_manifest_hash="7" * 64,
    )


class _CommanderRunner:
    def __init__(
        self,
        *,
        bundle,
        fail: bool = False,
        artifact_hash: str | None = None,
    ) -> None:
        self.bundle = bundle
        self.fail = fail
        self.artifact_hash = artifact_hash or bundle.bundle_hash
        self.calls: list[tuple[str, ...]] = []
        self.transient_paths: list[Path] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> HostProcessResult:
        del cwd, timeout_seconds
        call = tuple(args)
        self.calls.append(call)
        if self.fail:
            return HostProcessResult(
                returncode=2,
                stdout=b"",
                stderr=b"APCA_API_SECRET_KEY=must-never-leak",
            )
        if "candidate-runtime-info" in call:
            payload = {
                "schema_version": "candidate_runtime_attestation_v1",
                "isolation_kind": "native_windows_codex_sandbox",
                "isolation_version": "candidate_runtime_v1",
                "candidate_artifact_hash": self.artifact_hash,
                "candidate_tree_hash": self.bundle.candidate_tree_hash,
                "runtime": self.bundle.runtime.model_dump(mode="json"),
                "worker_code_hash": "8" * 64,
                "declared_entrypoint": self.bundle.declared_entrypoint,
                "network_access_permitted": False,
                "credential_access_permitted": False,
                "broker_access_permitted": False,
                "filesystem_write_permitted": False,
                "real_order_routing": False,
            }
            return _json_result(payload)
        request_path = Path(call[call.index("--request") + 1])
        security_path = Path(call[call.index("--security") + 1])
        self.transient_paths.extend((request_path, security_path))
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        security_payload = json.loads(security_path.read_text(encoding="utf-8"))
        request = CandidateDecisionRequestV1.model_validate(request_payload)
        response = build_candidate_decision_response(
            request=request,
            targets=(
                CandidateTargetV1(
                    symbol="QQQ",
                    score=1,
                    target_weight=0.7,
                ),
            ),
        )
        lane = call[call.index("--execution-lane") + 1]
        result = build_candidate_process_result(
            invocation_id=f"candidate-invocation-{lane.lower()}",
            request_hash=request.request_hash,
            candidate_artifact_hash=self.bundle.bundle_hash,
            security_contract_hash=security_payload["security_contract_hash"],
            exit_code=0,
            timed_out=False,
            resource_limit_exceeded=False,
            stdout_utf8=response.model_dump_json(),
            stderr_sha256=hashlib.sha256(b"").hexdigest(),
            stderr_bytes=0,
        )
        return _json_result(result.model_dump(mode="json"))


def _json_result(payload: dict[str, object]) -> HostProcessResult:
    return HostProcessResult(
        returncode=0,
        stdout=json.dumps(payload).encode("utf-8"),
        stderr=b"",
    )


def _runtime_paths(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "commander"
    run = root / ".local" / "runs" / "cycle"
    python = root / ".venv" / "Scripts" / "python.exe"
    run.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    python.touch()
    return root, run


def test_connected_runtime_attests_and_uses_independent_replay_lane(
    tmp_path: Path,
    repository_root: Path,
) -> None:
    bundle = _bundle()
    request = _request(bundle)
    root, run = _runtime_paths(tmp_path)
    runner = _CommanderRunner(bundle=bundle)
    connection = connect_candidate_runtime(
        bundle=bundle,
        commander_root=root,
        run_root=run,
        research_config=load_research_config(repository_root / "config"),
        runner=runner,
    )

    primary = connection.primary_executor.execute(request)
    replay = connection.replay_executor.execute(request)

    assert primary.output_hash == replay.output_hash
    assert connection.security.real_order_routing is False
    assert connection.attestation.credential_access_permitted is False
    assert any("PRIMARY" in call for call in runner.calls)
    assert any("REPLAY" in call for call in runner.calls)
    assert all("-I" in call and "research_commander.cli" in call for call in runner.calls)
    assert all(not path.exists() for path in runner.transient_paths)


def test_runtime_attestation_mismatch_fails_closed(
    tmp_path: Path,
    repository_root: Path,
) -> None:
    bundle = _bundle()
    root, run = _runtime_paths(tmp_path)

    with pytest.raises(
        CommanderCandidateError,
        match="COMMANDER_CANDIDATE_ATTESTATION_MISMATCH",
    ):
        connect_candidate_runtime(
            bundle=bundle,
            commander_root=root,
            run_root=run,
            research_config=load_research_config(repository_root / "config"),
            runner=_CommanderRunner(bundle=bundle, artifact_hash="9" * 64),
        )


def test_host_failure_never_relays_stderr_or_credentials(
    tmp_path: Path,
    repository_root: Path,
) -> None:
    bundle = _bundle()
    root, run = _runtime_paths(tmp_path)

    with pytest.raises(CommanderCandidateError) as captured:
        connect_candidate_runtime(
            bundle=bundle,
            commander_root=root,
            run_root=run,
            research_config=load_research_config(repository_root / "config"),
            runner=_CommanderRunner(bundle=bundle, fail=True),
        )

    assert str(captured.value) == "COMMANDER_CANDIDATE_HOST_COMMAND_FAILED"
    assert "SECRET" not in str(captured.value)
