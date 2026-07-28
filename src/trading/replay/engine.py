from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trading.persistence.models import RunRow
from trading.runtime.pipeline import load_scenario_for_run
from trading.runtime.simulation import SimulationArtifacts, simulate_scenario


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    mode: str
    manifest: dict[str, Any]
    result_hash: str
    artifacts: SimulationArtifacts


def replay_full(
    session_factory: sessionmaker[Session],
    run_id: str,
) -> ReplayResult:
    with session_factory() as session:
        run = session.get(RunRow, run_id)
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        scenario = load_scenario_for_run(session, run_id)
        config_manifest_hash = run.config_manifest_hash
        code_version = run.code_commit
    artifacts = simulate_scenario(
        scenario,
        config_manifest_hash=config_manifest_hash,
        code_version=code_version,
    )
    return ReplayResult(
        run_id=run_id,
        mode="FULL",
        manifest=artifacts.manifest,
        result_hash=artifacts.result_hash,
        artifacts=artifacts,
    )
