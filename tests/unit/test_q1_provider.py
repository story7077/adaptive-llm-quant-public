from __future__ import annotations

import json
import subprocess
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from trading.control.providers import CommanderProvider
from trading.control.service import ControlPlaneService
from trading.llm.q1_overlay import (
    Q1LlmOverlayDecision,
    validate_q1_overlay_config,
)
from trading.runtime.q1_config import (
    Q1LlmTransportConfig,
    llm_transport_config,
)
from trading.runtime.q1_provider import (
    Q1CommanderBundle,
    Q1ProviderAuditStatus,
    Q1SelectedCommanderProvider,
    export_q1_commander_bundle,
    run_q1_codex_bundle,
)
from trading.settings import Settings, load_q1_config_bundle

NOW = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)


def _transport_config(
    *,
    provider_timeout: str = "0.75",
    transport_timeout: str = "0.50",
    poll_interval: str = "0.01",
) -> Q1LlmTransportConfig:
    return Q1LlmTransportConfig(
        provider_timeout_seconds=Decimal(provider_timeout),
        transport_timeout_seconds=Decimal(transport_timeout),
        transport_poll_interval_seconds=Decimal(poll_interval),
    )


def _settings(
    database_url: str,
    tmp_path: Path,
    *,
    real_llm_enabled: bool,
) -> Settings:
    return Settings(
        database_url=database_url,
        config_dir=tmp_path / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=real_llm_enabled,
        production_unlock=False,
        commander_dir=tmp_path / "commander",
        market_data_enabled=False,
    )


def _request() -> dict[str, Any]:
    return {
        "schema_version": "q1_llm_review_request_v1",
        "request_id": "q1-review-1",
        "context_manifest_hash": "a" * 64,
        "calendar_session_id": "XNYS-2026-07-27",
        "scheduled_at": NOW,
        "portfolio_state_as_of": NOW - timedelta(seconds=1),
        "quote_as_of": NOW - timedelta(milliseconds=100),
        "q1_det": {
            "portfolio_decision_id": "q1-det-decision",
            "decision_hash": "b" * 64,
            "target_weights": {
                "QQQ": "0.40",
                "SOXX": "0.20",
                "USD_CASH": "0.40",
            },
            "input_manifest_hash": "c" * 64,
        },
        "q1_llm": {
            "state_sequence": 3,
            "positions": {"QQQ": "1"},
            "settled_cash_usd": "500",
            "unsettled_receivables_usd": "0",
            "current_nav_usd": "1000",
            "current_weights": {
                "QQQ": "0.50",
                "SOXX": "0",
                "USD_CASH": "0.50",
            },
        },
        "quotes": {
            "QQQ": {
                "quote_id": "quote-qqq",
                "bid": "500.00",
                "ask": "500.02",
                "available_at": NOW - timedelta(milliseconds=100),
            },
            "SOXX": {
                "quote_id": "quote-soxx",
                "bid": "250.00",
                "ask": "250.02",
                "available_at": NOW - timedelta(milliseconds=200),
            },
        },
        "news_events": [{"news_event_id": "news-한국어", "event_type": "MACRO"}],
        "allowed_evidence_event_ids": ["news-한국어"],
        "allowed_outputs": {
            "risk_multiplier": [1.0, 0.75, 0.5],
            "block_new_entries": "boolean",
            "new_symbols": [],
            "order_quantities": "forbidden",
            "broker_actions": "forbidden",
        },
        "real_order_routing": False,
    }


def _output(
    request: dict[str, Any],
    *,
    multiplier: float = 0.75,
) -> dict[str, Any]:
    return {
        "schema_version": "q1_llm_overlay_v1",
        "request_id": request["request_id"],
        "context_manifest_hash": request["context_manifest_hash"],
        "risk_multiplier": multiplier,
        "block_new_entries": True,
        "evidence_event_ids": ["news-한국어"],
        "rationale": "확인된 거시 이벤트 동안 위험을 줄입니다.",
        "effective_time": NOW.isoformat(),
        "expiry_time": (NOW + timedelta(hours=2)).isoformat(),
        "created_at": (NOW - timedelta(seconds=1)).isoformat(),
    }


def test_selected_codex_dispatches_strict_hash_addressed_bundle(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    settings = _settings(
        database_url,
        tmp_path,
        real_llm_enabled=True,
    )
    captured: list[Q1CommanderBundle] = []

    def runner(bundle: Q1CommanderBundle) -> dict[str, Any]:
        captured.append(bundle)
        return _output(_request())

    provider = Q1SelectedCommanderProvider(
        factory,
        settings=settings,
        transport_config=_transport_config(),
        repo_root=tmp_path,
        codex_runner=runner,
    )
    result = provider(_request())

    assert isinstance(result, Q1LlmOverlayDecision)
    assert result.risk_multiplier == 0.75
    assert len(captured) == 1
    bundle = captured[0]
    assert bundle.provider is CommanderProvider.CODEX_SOL_MAX
    assert bundle.codex_command is not None
    assert bundle.directory.name == bundle.bundle_hash
    assert len(bundle.bundle_hash) == 64
    request_text = bundle.request_file.read_text(encoding="utf-8")
    assert "news-한국어" in request_text
    assert bundle.schema_file.is_file()
    output_schema = json.loads(bundle.schema_file.read_text(encoding="utf-8"))
    assert output_schema["additionalProperties"] is False
    assert set(output_schema["required"]) == set(output_schema["properties"])
    assert len(output_schema["required"]) == len(output_schema["properties"])
    assert "schema_version" in output_schema["required"]
    assert '"default"' not in json.dumps(output_schema, sort_keys=True)
    assert bundle.prompt_file.is_file()
    audit = provider.audit_for_request("q1-review-1")
    assert len(audit) == 1
    record = audit[0]
    assert record.status is Q1ProviderAuditStatus.VALIDATED
    assert record.selection_id is not None
    assert record.selection_version == 1
    assert record.provider == "CODEX_SOL_MAX"
    assert record.model == "gpt-5.6-sol"
    assert record.reasoning_profile == "max"
    assert record.config_hash is not None
    assert record.bundle_hash == bundle.bundle_hash
    assert len(record.transport_config_hash) == 64
    assert record.validated_output_hash is not None
    serialized_audit = json.dumps(record.as_payload(), sort_keys=True)
    assert "request.json" not in serialized_audit
    assert "stdout" not in serialized_audit
    assert "must-not-be-exported" not in serialized_audit


def test_webgpt_uses_the_same_output_json_ingress(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    selection, _ = service.select_provider(
        CommanderProvider.WEBGPT_SOL_PRO,
        expected_version=0,
        now=NOW,
    )
    settings = _settings(
        database_url,
        tmp_path,
        real_llm_enabled=True,
    )
    request = _request()
    bundle = export_q1_commander_bundle(
        request,
        selection=selection,
        commander_dir=settings.commander_dir or tmp_path,
        transport_config=_transport_config(),
    )
    assert bundle.codex_command is None

    def delayed_output() -> None:
        time.sleep(0.03)
        bundle.output_file.write_text(
            json.dumps(_output(request), ensure_ascii=False),
            encoding="utf-8",
        )

    writer = Thread(target=delayed_output)
    writer.start()

    result = Q1SelectedCommanderProvider(
        factory,
        settings=settings,
        transport_config=_transport_config(),
        repo_root=tmp_path,
    )(request)
    writer.join()

    assert isinstance(result, Q1LlmOverlayDecision)
    assert result.evidence_event_ids == ["news-한국어"]


def test_selection_change_during_codex_run_discards_output(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    settings = _settings(
        database_url,
        tmp_path,
        real_llm_enabled=True,
    )

    def runner(_: Q1CommanderBundle) -> dict[str, Any]:
        service.select_provider(
            CommanderProvider.WEBGPT_SOL_PRO,
            expected_version=1,
            now=NOW + timedelta(seconds=1),
        )
        return _output(_request())

    provider = Q1SelectedCommanderProvider(
        factory,
        settings=settings,
        transport_config=_transport_config(),
        repo_root=tmp_path,
        codex_runner=runner,
    )
    result = provider(_request())

    assert result is None
    stale_record = provider.audit_for_request("q1-review-1")[-1]
    assert stale_record.status is (
        Q1ProviderAuditStatus.STALE_SELECTION
    )
    assert stale_record.validated_output_hash is not None


def test_gate_and_absent_selection_fail_closed_without_export(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    disabled = _settings(
        database_url,
        tmp_path,
        real_llm_enabled=False,
    )
    invoked = False

    def runner(_: Q1CommanderBundle) -> dict[str, Any]:
        nonlocal invoked
        invoked = True
        return _output(_request())

    provider = Q1SelectedCommanderProvider(
        factory,
        settings=disabled,
        transport_config=_transport_config(),
        repo_root=tmp_path,
        codex_runner=runner,
    )
    assert provider(_request()) is None
    assert not invoked
    assert not (tmp_path / "commander").exists()
    assert provider.audit_for_request("q1-review-1")[-1].status is (
        Q1ProviderAuditStatus.GATE_DISABLED
    )

    enabled = _settings(
        database_url,
        tmp_path,
        real_llm_enabled=True,
    )
    assert Q1SelectedCommanderProvider(
        factory,
        settings=enabled,
        transport_config=_transport_config(),
        repo_root=tmp_path,
        codex_runner=runner,
    )(_request()) is None
    assert not invoked


def test_invalid_output_or_sensitive_request_fails_closed(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    ControlPlaneService(factory).select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    settings = _settings(
        database_url,
        tmp_path,
        real_llm_enabled=True,
    )

    def invalid_runner(_: Q1CommanderBundle) -> dict[str, Any]:
        return {
            **_output(_request()),
            "broker_action": "BUY",
        }

    provider = Q1SelectedCommanderProvider(
        factory,
        settings=settings,
        transport_config=_transport_config(),
        repo_root=tmp_path,
        codex_runner=invalid_runner,
    )
    assert provider(_request()) is None
    assert provider.audit_for_request("q1-review-1")[-1].status is (
        Q1ProviderAuditStatus.OUTPUT_INVALID
    )

    sensitive = _request()
    sensitive["news_events"] = [
        {
            "news_event_id": "news-한국어",
            "api_key": "must-not-be-exported",
        }
    ]
    assert provider(sensitive) is None
    request_files = list((tmp_path / "commander").rglob("request.json"))
    assert all(
        "must-not-be-exported" not in path.read_text(encoding="utf-8")
        for path in request_files
    )


def test_versioned_transport_config_validates_outer_timeout_order(
    repository_root: Path,
) -> None:
    loaded = load_q1_config_bundle(repository_root / "config")
    transport = llm_transport_config(loaded)

    assert transport.provider_timeout_seconds == Decimal("55")
    assert transport.transport_timeout_seconds == Decimal("50")
    assert transport.transport_poll_interval_seconds == Decimal("1")

    invalid = deepcopy(loaded.document)
    invalid["llm"]["transport_timeout_seconds"] = 55
    with pytest.raises(
        ValueError,
        match="transport_timeout_seconds must be shorter",
    ):
        validate_q1_overlay_config(invalid)

    with pytest.raises(
        ValueError,
        match="transport timeout must be shorter",
    ):
        Q1LlmTransportConfig(
            provider_timeout_seconds=Decimal("1"),
            transport_timeout_seconds=Decimal("1"),
            transport_poll_interval_seconds=Decimal("0.1"),
        )


def test_webgpt_missing_output_stops_at_transport_timeout(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    ControlPlaneService(factory).select_provider(
        CommanderProvider.WEBGPT_SOL_PRO,
        expected_version=0,
        now=NOW,
    )
    provider = Q1SelectedCommanderProvider(
        factory,
        settings=_settings(
            database_url,
            tmp_path,
            real_llm_enabled=True,
        ),
        transport_config=_transport_config(
            provider_timeout="0.05",
            transport_timeout="0.02",
            poll_interval="0.005",
        ),
        repo_root=tmp_path,
    )

    started = time.monotonic()
    assert provider(_request()) is None
    elapsed = time.monotonic() - started

    assert elapsed >= 0.01
    assert elapsed < 1
    timeout_record = provider.audit_for_request("q1-review-1")[-1]
    assert timeout_record.status is Q1ProviderAuditStatus.TRANSPORT_TIMEOUT
    assert timeout_record.validated_output_hash is None


def test_codex_subprocess_uses_versioned_transport_timeout(
    sqlite_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, factory = sqlite_database
    selection, _ = ControlPlaneService(factory).select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = _request()
    transport = _transport_config(
        provider_timeout="1.0",
        transport_timeout="0.8",
        poll_interval="0.1",
    )
    bundle = export_q1_commander_bundle(
        request,
        selection=selection,
        commander_dir=tmp_path / "commander",
        transport_config=transport,
    )
    captured_timeout: list[float] = []

    def fake_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del command
        captured_timeout.append(float(kwargs["timeout"]))
        bundle.output_file.write_text(
            json.dumps(_output(request), ensure_ascii=False),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(
        "trading.runtime.q1_provider.subprocess.run",
        fake_run,
    )

    result = run_q1_codex_bundle(bundle)

    assert result is not None
    assert captured_timeout == [0.8]


def test_codex_nonzero_exit_is_a_transport_failure(
    sqlite_database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, _, factory = sqlite_database
    ControlPlaneService(factory).select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )

    def failed_run(
        command: tuple[str, ...],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del command, kwargs
        return subprocess.CompletedProcess([], 2, "", "sensitive stderr")

    monkeypatch.setattr(
        "trading.runtime.q1_provider.subprocess.run",
        failed_run,
    )
    provider = Q1SelectedCommanderProvider(
        factory,
        settings=_settings(
            database_url,
            tmp_path,
            real_llm_enabled=True,
        ),
        transport_config=_transport_config(),
        repo_root=tmp_path,
    )

    assert provider(_request()) is None
    audit = provider.audit_for_request("q1-review-1")[-1]
    assert audit.status is Q1ProviderAuditStatus.TRANSPORT_FAILED
    assert "sensitive stderr" not in json.dumps(audit.as_payload())


def test_provider_audit_mapping_and_attempt_history_are_bounded(
    sqlite_database,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    provider = Q1SelectedCommanderProvider(
        factory,
        settings=_settings(
            database_url,
            tmp_path,
            real_llm_enabled=True,
        ),
        transport_config=_transport_config(),
        repo_root=tmp_path,
        audit_request_capacity=1,
        audit_attempts_per_request=2,
    )
    first = _request()
    first["request_id"] = "request-first"
    second = _request()
    second["request_id"] = "request-second"

    assert provider(first) is None
    assert provider.audit_for_request("request-first")[-1].status is (
        Q1ProviderAuditStatus.NO_SELECTION
    )
    assert provider(second) is None
    assert provider(second) is None
    assert provider(second) is None

    assert provider.audit_for_request("request-first") == ()
    retained = provider.audit_for_request("request-second")
    assert [record.attempt_index for record in retained] == [2, 3]
    assert all(
        record.status is Q1ProviderAuditStatus.NO_SELECTION
        for record in retained
    )
