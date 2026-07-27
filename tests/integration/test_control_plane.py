from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.control.bundles import export_request_bundle
from trading.control.providers import CommanderProvider
from trading.control.service import ControlPlaneService, DecisionConflict

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def no_change_output(request) -> dict[str, object]:
    return {
        "schema_version": "adaptive_policy_decision_v1",
        "request_id": request.request_id,
        "context_manifest_hash": request.context_manifest_hash,
        "decision": "NO_CHANGE",
        "arm_scope": request.arm_scope,
        "base_policy_version": request.base_policy_version,
        "effective_from": None,
        "expires_at": None,
        "operations": [],
        "evidence_news_event_ids": [],
        "raw_confidence": 0.1,
        "rollback_conditions": [],
        "rationale_summary": "No bounded change is justified.",
    }


def reduction_output(
    request,
    *,
    risk_multiplier: float = 0.75,
    effective_from: datetime = NOW,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    expiry = expires_at or effective_from + timedelta(hours=2)
    return {
        "schema_version": "adaptive_policy_decision_v1",
        "request_id": request.request_id,
        "context_manifest_hash": request.context_manifest_hash,
        "decision": "APPLY_PATCH",
        "arm_scope": request.arm_scope,
        "base_policy_version": request.base_policy_version,
        "effective_from": effective_from.isoformat(),
        "expires_at": expiry.isoformat(),
        "operations": [
            {
                "action": "REDUCE_RISK_BUDGET",
                "target_kind": "PORTFOLIO",
                "target_id": "TOTAL",
                "risk_budget_delta": None,
                "risk_multiplier": risk_multiplier,
                "blocked": None,
            }
        ],
        "evidence_news_event_ids": ["news-1"],
        "raw_confidence": 0.8,
        "rollback_conditions": [
            {
                "condition_id": "rollback-time",
                "condition_type": "TIME_REACHED",
                "field": "current_time",
                "operator": "GTE",
                "value": expiry.isoformat(),
                "evaluation_window": None,
                "source_ids": [],
            }
        ],
        "rationale_summary": "Reduce portfolio risk while the event remains unresolved.",
    }


def test_selected_provider_can_submit_no_change_idempotently(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    selection, changed = service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    assert changed
    assert selection.version == 1

    request = service.create_request(
        arm_scope="B3-RISK",
        context={"뉴스": "근거 부족"},
        now=NOW,
    )
    output = no_change_output(request)
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=output,
        now=NOW,
    )
    assert receipt.status == "NO_CHANGE"

    replay = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=output,
        now=NOW,
    )
    assert replay.decision_id == receipt.decision_id
    assert replay.idempotent_replay


def test_valid_reduction_compiles_new_policy_version(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.WEBGPT_SOL_PRO,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-1"}]},
        now=NOW,
    )
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.WEBGPT_SOL_PRO,
        output=reduction_output(request),
        now=NOW,
    )
    assert receipt.status == "ACCEPTED"
    assert receipt.applied_policy_version == 1
    assert receipt.compiled_policy_hash is not None
    assert (
        service.status(active_at=NOW)["policies"]["B3-RISK"][
            "portfolio_risk_multiplier"
        ]
        == 0.75
    )


def test_future_created_patch_does_not_contaminate_historical_policy(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    accepted_at = NOW + timedelta(minutes=4)
    request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-1"}]},
        as_of=NOW,
        data_available_cutoff=NOW,
        now=accepted_at,
    )
    assert request.created_at == accepted_at
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=reduction_output(request, effective_from=NOW),
        now=accepted_at,
    )
    assert receipt.status == "ACCEPTED"
    assert receipt.applied_policy_version == 1

    historical = service.active_policy_state(
        arm_scope="B3-RISK",
        scope_id="legacy_global",
        active_at=NOW,
    )
    assert historical.version == 0
    assert historical.portfolio_risk_multiplier == 1.0
    assert historical.source_patch_id is None

    current = service.active_policy_state(
        arm_scope="B3-RISK",
        scope_id="legacy_global",
        active_at=accepted_at,
    )
    assert current.version == 1
    assert current.portfolio_risk_multiplier == 0.75
    assert current.source_patch_id is not None


def test_expired_patch_appends_a_restored_policy_version(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-1"}]},
        now=NOW,
    )
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=reduction_output(request),
        now=NOW,
    )
    assert receipt.applied_policy_version == 1

    restored = service.active_policy_state(
        arm_scope="B3-RISK",
        scope_id="legacy_global",
        active_at=NOW + timedelta(hours=2),
    )
    assert restored.version == 2
    assert restored.portfolio_risk_multiplier == 1.0
    assert restored.source_patch_id is None

    next_request = service.create_request(
        arm_scope="B3-RISK",
        context={},
        now=NOW + timedelta(hours=2),
    )
    assert next_request.base_policy_version == 2


def test_compiler_rejects_out_of_bounds_patch(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-1"}]},
        now=NOW,
    )
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=reduction_output(request, risk_multiplier=1.2),
        now=NOW,
    )
    assert receipt.status == "REJECTED"
    assert receipt.reason_code == "POLICY_COMPILE_REJECTED"
    assert service.status(active_at=NOW)["policies"]["B3-RISK"]["version"] == 0


def test_overlapping_policy_ttls_expire_only_their_own_effects(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    first_request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-1"}]},
        now=NOW,
    )
    first = reduction_output(
        first_request,
        risk_multiplier=0.75,
        expires_at=NOW + timedelta(hours=5),
    )
    assert service.submit_decision(
        request_id=first_request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=first,
        now=NOW,
    ).status == "ACCEPTED"
    first_patch_id = service.active_policy_state(
        arm_scope="B3-RISK",
        scope_id="legacy_global",
        active_at=NOW,
    ).source_patch_id
    assert first_patch_id is not None

    second_at = NOW + timedelta(hours=2)
    second_request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-1"}]},
        now=second_at,
    )
    second_expiry = NOW + timedelta(hours=3)
    second = {
        **reduction_output(
            second_request,
            effective_from=second_at,
            expires_at=second_expiry,
        ),
        "operations": [
            {
                "action": "BLOCK_NEW_ENTRIES",
                "target_kind": "SYMBOL",
                "target_id": "QQQ",
                "risk_budget_delta": None,
                "risk_multiplier": None,
                "blocked": True,
            }
        ],
    }
    assert service.submit_decision(
        request_id=second_request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=second,
        now=second_at,
    ).status == "ACCEPTED"

    after_second_expiry = service.active_policy_state(
        arm_scope="B3-RISK",
        scope_id="legacy_global",
        active_at=second_expiry,
    )
    assert after_second_expiry.version == 3
    assert after_second_expiry.portfolio_risk_multiplier == 0.75
    assert after_second_expiry.blocked_targets == frozenset()
    assert after_second_expiry.source_patch_id == first_patch_id

    after_all_expiry = service.active_policy_state(
        arm_scope="B3-RISK",
        scope_id="legacy_global",
        active_at=NOW + timedelta(hours=5),
    )
    assert after_all_expiry.version == 4
    assert after_all_expiry.portfolio_risk_multiplier == 1.0
    assert after_all_expiry.source_patch_id is None


def test_patch_cannot_cite_news_outside_prepared_context(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(
        arm_scope="B3-RISK",
        context={"news_analyses": [{"news_event_id": "news-allowed"}]},
        now=NOW,
    )
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=reduction_output(request),
        now=NOW,
    )

    assert receipt.status == "REJECTED"
    assert receipt.reason_code == "EVIDENCE_NOT_IN_CONTEXT"


def test_selection_change_invalidates_prepared_request(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(arm_scope="B3-RISK", context={}, now=NOW)
    service.select_provider(
        CommanderProvider.WEBGPT_SOL_PRO,
        expected_version=1,
        now=NOW + timedelta(seconds=1),
    )
    receipt = service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=no_change_output(request),
        now=NOW + timedelta(seconds=1),
    )
    assert receipt.status == "REJECTED"
    assert receipt.reason_code == "STALE_SELECTION"


def test_one_request_cannot_accept_two_different_outputs(sqlite_database) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(arm_scope="B3-RISK", context={}, now=NOW)
    first = no_change_output(request)
    service.submit_decision(
        request_id=request.request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=first,
        now=NOW,
    )
    second = dict(first)
    second["rationale_summary"] = "A different answer."
    with pytest.raises(DecisionConflict, match="different decision"):
        service.submit_decision(
            request_id=request.request_id,
            provider=CommanderProvider.CODEX_SOL_MAX,
            output=second,
            now=NOW,
        )


def test_codex_bundle_is_isolated_and_utf8(sqlite_database, tmp_path) -> None:
    _, _, factory = sqlite_database
    service = ControlPlaneService(factory)
    service.select_provider(
        CommanderProvider.CODEX_SOL_MAX,
        expected_version=0,
        now=NOW,
    )
    request = service.create_request(
        arm_scope="B3-RISK",
        context={"요약": "정책 변경 근거"},
        now=NOW,
    )
    bundle = export_request_bundle(request, commander_dir=tmp_path / "commander")
    assert "정책 변경 근거" in bundle.request_file.read_text(encoding="utf-8")
    assert bundle.codex_command is not None
    assert "--ephemeral" in bundle.codex_command
    assert "--ignore-user-config" in bundle.codex_command
    assert "read-only" in bundle.codex_command
    assert "gpt-5.6-sol" in bundle.codex_command
    assert 'model_reasoning_effort="max"' in bundle.codex_command
