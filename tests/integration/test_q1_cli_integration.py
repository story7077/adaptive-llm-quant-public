from __future__ import annotations

import json

from typer.testing import CliRunner

from trading.cli import app
from trading.domain.algorithm import (
    LEGACY_FORWARD_ALGORITHM_VERSION,
    Q1_ALGORITHM_VERSION,
)
from trading.persistence.models import RunRow


def _configure_cli_environment(
    monkeypatch,
    *,
    database_url: str,
    repository_root,
    tmp_path,
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    monkeypatch.setenv(
        "TRADING_CONFIG_DIR",
        str(repository_root / "config"),
    )
    monkeypatch.setenv("TRADING_RAW_STORE", str(tmp_path / "raw"))
    monkeypatch.setenv(
        "TRADING_PAPER_ACCOUNT_FILE",
        str(repository_root / "config" / "paper-account.example.yaml"),
    )
    monkeypatch.delenv("TRADING_PAPER_ALGORITHM_VERSION", raising=False)


def test_config_validate_all_includes_legacy_and_q1(
    repository_root,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "TRADING_CONFIG_DIR",
        str(repository_root / "config"),
    )

    result = CliRunner().invoke(app, ["config", "validate", "--all"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["valid"] is True
    assert payload["algorithm_versions"] == [
        LEGACY_FORWARD_ALGORITHM_VERSION,
        Q1_ALGORITHM_VERSION,
    ]
    assert "q1-math-core.yaml" in payload["files"]
    assert Q1_ALGORITHM_VERSION in payload["configs"]


def test_paper_cli_requires_explicit_q1_and_preserves_run_identity(
    sqlite_database,
    repository_root,
    tmp_path,
    monkeypatch,
) -> None:
    database_url, _, factory = sqlite_database
    _configure_cli_environment(
        monkeypatch,
        database_url=database_url,
        repository_root=repository_root,
        tmp_path=tmp_path,
    )
    runner = CliRunner()

    legacy = runner.invoke(
        app,
        ["paper", "init", "--run-id", "cli-legacy"],
    )
    assert legacy.exit_code == 0, legacy.output
    assert json.loads(legacy.output)["algorithm_version"] == (
        LEGACY_FORWARD_ALGORITHM_VERSION
    )

    q1 = runner.invoke(
        app,
        [
            "paper",
            "init",
            "--run-id",
            "cli-q1",
            "--algorithm-version",
            Q1_ALGORITHM_VERSION,
        ],
    )
    assert q1.exit_code == 0, q1.output
    assert json.loads(q1.output)["algorithm_version"] == Q1_ALGORITHM_VERSION

    status = runner.invoke(
        app,
        [
            "paper",
            "status",
            "--run-id",
            "cli-q1",
            "--algorithm-version",
            Q1_ALGORITHM_VERSION,
        ],
    )
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["algorithm_version"] == (
        Q1_ALGORITHM_VERSION
    )

    mismatched_status = runner.invoke(
        app,
        ["paper", "status", "--run-id", "cli-q1"],
    )
    assert mismatched_status.exit_code != 0

    conflicting_switch = runner.invoke(
        app,
        [
            "paper",
            "init",
            "--run-id",
            "cli-legacy",
            "--algorithm-version",
            Q1_ALGORITHM_VERSION,
        ],
    )
    assert conflicting_switch.exit_code != 0

    with factory() as session:
        legacy_run = session.get(RunRow, "cli-legacy")
        q1_run = session.get(RunRow, "cli-q1")
        assert legacy_run is not None
        assert q1_run is not None
        assert legacy_run.experiment_version == (
            LEGACY_FORWARD_ALGORITHM_VERSION
        )
        assert q1_run.experiment_version == Q1_ALGORITHM_VERSION
