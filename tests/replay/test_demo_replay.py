from __future__ import annotations

import json
from pathlib import Path

from trading.data.synthetic import build_demo_scenario
from trading.replay.engine import replay_full
from trading.replay.verifier import verify_run
from trading.runtime.pipeline import load_arm_states, seed_demo
from trading.runtime.simulation import simulate_scenario

GOLDEN_CODE_VERSION = "phase0_golden_fixture_v1"


def test_demo_seed_is_idempotent(seeded_demo, config_bundle) -> None:
    settings, _, factory, manifest, result_hash = seeded_demo
    second_manifest, second_hash, created = seed_demo(
        settings=settings,
        config=config_bundle,
        session_factory=factory,
    )
    assert created is False
    assert second_hash == result_hash
    assert second_manifest == manifest


def test_full_replay_is_hash_equivalent(seeded_demo) -> None:
    _, _, factory, manifest, result_hash = seeded_demo
    replay = replay_full(factory, "demo_run")
    assert replay.result_hash == result_hash
    assert replay.manifest == manifest

    report = verify_run(factory, "demo_run")
    assert report.passed
    assert all(report.checks.values())

    with factory() as session:
        recovered = load_arm_states(session, "demo_run")
    replay_states = {
        arm_id: artifacts.state for arm_id, artifacts in replay.artifacts.arms.items()
    }
    assert recovered == replay_states


def test_golden_fixture_is_unchanged(
    config_bundle,
    repository_root: Path,
) -> None:
    artifacts = simulate_scenario(
        build_demo_scenario(),
        config_manifest_hash=config_bundle.manifest_hash,
        code_version=GOLDEN_CODE_VERSION,
    )
    manifest = artifacts.manifest
    result_hash = artifacts.result_hash
    golden = json.loads(
        (repository_root / "tests" / "fixtures" / "demo_golden.json").read_text(
            encoding="utf-8"
        )
    )
    projection = {
        "result_hash": result_hash,
        "counts": manifest["counts"],
        "arms": {
            arm_id: {
                key: arm[key]
                for key in (
                    "target_hash",
                    "order_count",
                    "fill_count",
                    "ledger_hash",
                    "nav_hash",
                )
            }
            for arm_id, arm in manifest["arms"].items()
        },
    }
    assert projection == golden
