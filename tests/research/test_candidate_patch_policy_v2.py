from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.sandbox_contract import (
    CANDIDATE_PATCH_POLICY_V2,
    CANDIDATE_PATCH_POLICY_V2_CONTRACT,
    CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
    CandidatePatchRejected,
    candidate_patch_policy_contract_hash,
    inspect_candidate_patch,
    inspect_recursive_candidate_patch,
)

EXPECTED_V2_POLICY_HASH = (
    "73af5956c12a042eb99c0c15929b7f4db2b3b45110373204d39c8163fedc716c"
)


def _patch(*paths: str) -> bytes:
    return "".join(
        f"diff --git a/{path} b/{path}\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+safe = True\n"
        for path in paths
    ).encode()


def _modified_patch(path: str) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1 +1 @@\n"
        "-safe = False\n"
        "+safe = True\n"
    ).encode()


def _deleted_patch(path: str) -> bytes:
    return (
        f"diff --git a/{path} b/{path}\n"
        "deleted file mode 100644\n"
        f"--- a/{path}\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-safe = True\n"
    ).encode()


def test_public_v2_policy_contract_hash_is_sealed() -> None:
    assert CANDIDATE_PATCH_POLICY_V2_CONTRACT["new_files_only"] is True
    assert CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH == EXPECTED_V2_POLICY_HASH
    assert (
        candidate_patch_policy_contract_hash(CANDIDATE_PATCH_POLICY_V2)
        == EXPECTED_V2_POLICY_HASH
    )


def test_public_v2_policy_fixture_matches_the_runtime_contract(
    repository_root: Path,
) -> None:
    fixture = json.loads(
        (
            repository_root
            / "contracts"
            / "candidate-patch-policy-v2.json"
        ).read_text(encoding="utf-8")
    )
    assert fixture == json.loads(
        json.dumps(CANDIDATE_PATCH_POLICY_V2_CONTRACT)
    )
    assert canonical_hash(fixture) == EXPECTED_V2_POLICY_HASH


def test_v2_accepts_only_challenger_implementation_and_candidate_test() -> None:
    paths = (
        "src/trading/strategies/challengers/t2_v1/model.py",
        "tests/candidates/test_t2_v1.py",
    )
    inspection = inspect_recursive_candidate_patch(
        changed_paths=paths,
        patch_bytes=_patch(*paths),
    )
    assert inspection.changed_paths == paths


@pytest.mark.parametrize(
    "path",
    (
        "src/trading/research/meta_controller.py",
        "src/trading/research/portfolio_delta_sharpe.py",
        "src/trading/research/oos_worker.py",
        "config/research/research-plane.yaml",
        "src/trading/persistence/models.py",
        "migrations/versions/0015_unsafe.py",
        "tests/research/test_trusted_judge.py",
        "src/trading/execution/order_state.py",
        "src/trading/risk/state_machine.py",
        "src/trading/ledger/postings.py",
        ".github/workflows/relax-thresholds.yml",
    ),
)
def test_v2_rejects_trusted_judge_and_control_plane_changes(path: str) -> None:
    with pytest.raises(CandidatePatchRejected):
        inspect_recursive_candidate_patch(
            changed_paths=(path,),
            patch_bytes=_patch(path),
        )


@pytest.mark.parametrize(
    "path",
    (
        "src/trading/strategies/challengers/../../research/meta_controller.py",
        "./src/trading/strategies/challengers/candidate.py",
        "src//trading/strategies/challengers/candidate.py",
        "volume:/src/trading/strategies/challengers/candidate.py",
    ),
)
def test_v2_rejects_relative_path_bypass(path: str) -> None:
    with pytest.raises(CandidatePatchRejected, match="unsafe patch path"):
        inspect_recursive_candidate_patch(
            changed_paths=(path,),
            patch_bytes=_patch(path),
        )


def test_v2_rejects_symbolic_link_patch() -> None:
    path = "src/trading/strategies/challengers/link.py"
    patch = (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        "+../../research/meta_controller.py\n"
    ).encode()
    with pytest.raises(CandidatePatchRejected, match="symbolic-link"):
        inspect_recursive_candidate_patch(
            changed_paths=(path,),
            patch_bytes=patch,
        )


@pytest.mark.parametrize("patch_factory", (_modified_patch, _deleted_patch))
def test_v2_rejects_changes_to_existing_files(
    patch_factory: Callable[[str], bytes],
) -> None:
    implementation = "src/trading/strategies/challengers/t2_v1/model.py"
    test = "tests/candidates/test_t2_v1.py"
    patch = patch_factory(implementation) + _patch(test)
    with pytest.raises(CandidatePatchRejected, match=r"new files only|new-file"):
        inspect_recursive_candidate_patch(
            changed_paths=(implementation, test),
            patch_bytes=patch,
        )


def test_v2_rejects_rename_style_diff_sections() -> None:
    old = "src/trading/strategies/challengers/t2_v1/old.py"
    new = "src/trading/strategies/challengers/t2_v2/new.py"
    test = "tests/candidates/test_t2_v2.py"
    patch = (
        f"diff --git a/{old} b/{new}\n"
        "similarity index 100%\n"
        f"rename from {old}\n"
        f"rename to {new}\n"
    ).encode() + _patch(test)
    with pytest.raises(CandidatePatchRejected, match="new files only"):
        inspect_recursive_candidate_patch(
            changed_paths=(old, new, test),
            patch_bytes=patch,
        )


def test_v1_policy_remains_available_for_historical_artifacts() -> None:
    paths = (
        "src/trading/strategies/challengers/legacy.py",
        "tests/research/test_legacy.py",
    )
    inspection = inspect_candidate_patch(
        changed_paths=paths,
        patch_bytes=_patch(*paths),
    )
    assert inspection.changed_paths == paths


def test_v1_replay_still_accepts_existing_file_modification() -> None:
    implementation = "src/trading/strategies/challengers/legacy.py"
    test = "tests/research/test_legacy.py"
    patch = _modified_patch(implementation) + _modified_patch(test)
    inspection = inspect_candidate_patch(
        changed_paths=(implementation, test),
        patch_bytes=patch,
    )
    assert inspection.changed_paths == (implementation, test)


def test_v1_replay_keeps_its_historical_symlink_patch_semantics() -> None:
    implementation = "src/trading/strategies/challengers/legacy-link.py"
    test = "tests/research/test_legacy_link.py"
    patch = (
        f"diff --git a/{implementation} b/{implementation}\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        f"+++ b/{implementation}\n"
        "@@ -0,0 +1 @@\n"
        "+legacy-target.py\n"
    ).encode() + _patch(test)
    inspection = inspect_candidate_patch(
        changed_paths=(implementation, test),
        patch_bytes=patch,
    )
    assert inspection.changed_paths == (implementation, test)
