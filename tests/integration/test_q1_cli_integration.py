from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from typer.testing import CliRunner

from trading.cli import app
from trading.domain.algorithm import (
    LEGACY_FORWARD_ALGORITHM_VERSION,
    Q1_ALGORITHM_VERSION,
)
from trading.persistence.models import (
    LedgerPostingRow,
    LedgerTransactionRow,
    RunRow,
)


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
    assert (
        "research/candidate-prospective-evaluation.yaml"
        in payload["files"]
    )
    assert Q1_ALGORITHM_VERSION in payload["configs"]
    assert "CANDIDATE_PROSPECTIVE_EVALUATION_V2" in payload["configs"]


def test_operator_ui_serve_commands_reject_non_loopback_hosts() -> None:
    runner = CliRunner()

    for command in (
        ["ui", "serve", "--host", "0.0.0.0"],
        ["paper", "serve", "--host", "192.0.2.10"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code != 0
        assert "loopback" in result.output


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


def test_ledger_verify_accepts_q1_arm_and_scopes_to_run(
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
    for run_id in ("ledger-q1-a", "ledger-q1-b"):
        initialized = runner.invoke(
            app,
            [
                "paper",
                "init",
                "--run-id",
                run_id,
                "--algorithm-version",
                Q1_ALGORITHM_VERSION,
            ],
        )
        assert initialized.exit_code == 0, initialized.output

    with factory() as session:
        session.add_all(
            [
                LedgerTransactionRow(
                    ledger_transaction_id="ledger-q1-a-capital",
                    run_id="ledger-q1-a",
                    arm_id="Q1-DET",
                    source_id="test-capital-a",
                    effective_at=datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
                    payload_json={},
                ),
                LedgerTransactionRow(
                    ledger_transaction_id="ledger-q1-b-unbalanced",
                    run_id="ledger-q1-b",
                    arm_id="Q1-DET",
                    source_id="test-unbalanced-b",
                    effective_at=datetime(2026, 7, 30, 13, 30, tzinfo=UTC),
                    payload_json={},
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                LedgerPostingRow(
                    posting_id="ledger-q1-a-debit",
                    ledger_transaction_id="ledger-q1-a-capital",
                    account_code="CASH",
                    asset_code="USD",
                    quantity_delta=Decimal("100"),
                    usd_value_delta=Decimal("100"),
                    payload_json={},
                ),
                LedgerPostingRow(
                    posting_id="ledger-q1-a-credit",
                    ledger_transaction_id="ledger-q1-a-capital",
                    account_code="CAPITAL",
                    asset_code="USD",
                    quantity_delta=Decimal("-100"),
                    usd_value_delta=Decimal("-100"),
                    payload_json={},
                ),
                LedgerPostingRow(
                    posting_id="ledger-q1-b-only-posting",
                    ledger_transaction_id="ledger-q1-b-unbalanced",
                    account_code="CASH",
                    asset_code="USD",
                    quantity_delta=Decimal("1"),
                    usd_value_delta=Decimal("1"),
                    payload_json={},
                ),
            ]
        )
        session.commit()

    scoped = runner.invoke(
        app,
        [
            "ledger",
            "verify",
            "--arm",
            "Q1-DET",
            "--run-id",
            "ledger-q1-a",
        ],
    )
    assert scoped.exit_code == 0, scoped.output
    payload = json.loads(scoped.output)
    assert payload == {
        "arm_id": "Q1-DET",
        "balanced": True,
        "run_id": "ledger-q1-a",
        "transaction_count": 1,
        "unbalanced_transaction_ids": [],
    }

    unscoped = runner.invoke(
        app,
        ["ledger", "verify", "--arm", "Q1-DET"],
    )
    assert unscoped.exit_code == 1
    assert json.loads(unscoped.output)["unbalanced_transaction_ids"] == [
        "ledger-q1-b-unbalanced"
    ]

    missing = runner.invoke(
        app,
        [
            "ledger",
            "verify",
            "--arm",
            "Q1-DET",
            "--run-id",
            "missing-run",
        ],
    )
    assert missing.exit_code != 0
    assert "Unknown run: missing-run" in missing.output
