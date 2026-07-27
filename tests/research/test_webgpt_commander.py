from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.contracts import (
    AvailableDataCatalogV1,
    AvailableInstrumentV1,
    CommanderSelectionV1,
    ResearchCommanderKind,
    ResearchDecisionV1,
)
from trading.research.host import build_research_request
from trading.research.webgpt_commander import (
    COMMANDER_CREATED_AT_SENTINEL,
    COMMANDER_HASH_SENTINEL,
    WebGptActiveResearchCommander,
    WebGptCommanderError,
    parse_webgpt_commander_result,
    render_webgpt_commander_prompt,
)
from trading.research.webgpt_scout import (
    ProcessResult,
    WebGptScoutConfig,
    WebGptScoutError,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


class FakeRunner:
    def __init__(self, responses: list[ProcessResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[str], str | None, int]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_seconds: int,
    ) -> ProcessResult:
        self.calls.append((list(args), input_text, timeout_seconds))
        return self.responses.pop(0)


def _process_json(payload: dict[str, object]) -> ProcessResult:
    return ProcessResult(returncode=0, stdout=json.dumps(payload), stderr="")


def _selection(
    *,
    selection_id: str = "selection-webgpt-1",
    version: int = 1,
) -> CommanderSelectionV1:
    return CommanderSelectionV1(
        selection_id=selection_id,
        version=version,
        selected_commander=ResearchCommanderKind.WEBGPT_SOL_PRO,
        effective_at=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=2),
        config_hash="b" * 64,
    )


def _request(selection: CommanderSelectionV1 | None = None):
    selected = selection or _selection()
    catalog_payload = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-webgpt-v1",
        "as_of": NOW + timedelta(minutes=1),
        "data_available_cutoff": NOW,
        "instruments": [
            AvailableInstrumentV1(
                symbol="SPY",
                asset_class="US_ETF",
                first_available_at=NOW - timedelta(days=1000),
                point_in_time_membership_available=True,
                daily_history_sessions=800,
                intraday_history_sessions=200,
                execution_supported=True,
            )
        ],
        "dataset_versions": {"daily": "pit-daily-v1"},
    }
    catalog = AvailableDataCatalogV1(
        **catalog_payload,
        catalog_hash=canonical_hash(catalog_payload),
    )
    return build_research_request(
        request_id="request-webgpt-1",
        research_cycle_id="cycle-webgpt-1",
        commander_selection=selected,
        created_at=NOW,
        as_of=NOW + timedelta(minutes=1),
        data_available_cutoff=NOW,
        expires_at=NOW + timedelta(hours=2),
        source_snapshot_commit="a" * 40,
        champion_version="1.0.0",
        experiment_family="adaptive-alpha-v1",
        champion_manifest={"strategy_id": "T1"},
        active_challenger_manifests=[],
        strategy_performance_summary={"common_sessions": 252},
        failure_case_clusters=[],
        regime_summary={"regime": "mixed"},
        execution_cost_summary={"cost_bps": 8.0},
        capacity_summary={"capacity_usd": 1000000},
        recent_market_evidence=[],
        recent_web_research=[{"evidence_bundle_hash": "c" * 64}],
        available_data_catalog=catalog,
        allowed_change_scope=["src/trading/strategies/challengers/"],
        forbidden_change_scope=["src/trading/risk/"],
        experiment_budget={"submissions_remaining": 3},
    )


def _proposal_payload() -> dict[str, object]:
    return {
        "schema_version": "algorithm_proposal_v1",
        "proposal_id": "proposal-webgpt-1",
        "hypothesis_id": "hypothesis-webgpt-1",
        "hypothesis": "A bounded trend revision may improve matched net returns.",
        "economic_mechanism": "Slow institutional repricing persists after costs.",
        "why_current_model_failed": "The Champion omits the bounded feature.",
        "parent_strategy_id": "T1",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "T1",
        "proposed_strategy_version": "1.1.0",
        "target_horizon": "H20D",
        "target_universe": ["SPY"],
        "required_data": ["adjusted PIT daily bars"],
        "feature_changes": ["add bounded trend stability"],
        "signal_formula_changes": ["combine trend with stability"],
        "entry_rule_changes": [],
        "exit_rule_changes": ["monthly rebalance"],
        "position_sizing_changes": ["inverse volatility"],
        "regime_activation_changes": [],
        "calibration_changes": ["walk-forward only"],
        "expected_edge_source": "slow repricing",
        "expected_failure_modes": ["trend reversal"],
        "invalidation_conditions": ["matched net return is non-positive"],
        "placebo_tests": ["date shift"],
        "stress_tests": ["3x modeled costs"],
        "minimum_economic_effect": {"annualized_difference": 0.02},
        "estimated_capacity": {"usd": 1000000},
        "estimated_turnover": {"annual": 2.0},
        "estimated_cost_sensitivity": {"bps": 10},
        "files_allowed_to_change": ["src/trading/strategies/challengers/"],
        "tests_required": ["future_data_leakage"],
        "evidence_source_ids": ["source-1"],
        "raw_confidence": 0.4,
        "proposal_hash": COMMANDER_HASH_SENTINEL,
    }


def _decision_payload(
    request,
    *,
    with_proposal: bool = False,
) -> dict[str, object]:
    return {
        "schema_version": "research_decision_v1",
        "request_id": request.request_id,
        "research_cycle_id": request.research_cycle_id,
        "selected_commander": request.selected_commander.value,
        "commander_selection_id": request.commander_selection_id,
        "commander_selection_version": request.commander_selection_version,
        "source_snapshot_commit": request.source_snapshot_commit,
        "champion_version": request.champion_version,
        "experiment_family": request.experiment_family,
        "context_manifest_hash": request.context_manifest_hash,
        "request_schema_version": request.schema_version,
        "request_expires_at": request.expires_at.isoformat(),
        "decision": (
            "PROPOSE_STRATEGY_REVISION" if with_proposal else "NO_RESEARCH_CHANGE"
        ),
        "rationale": "The bounded evidence supports this conservative decision.",
        "proposal": _proposal_payload() if with_proposal else None,
        "requested_evidence": [],
        "created_at": COMMANDER_CREATED_AT_SENTINEL,
        "output_hash": COMMANDER_HASH_SENTINEL,
    }


def _config(tmp_path: Path) -> WebGptScoutConfig:
    agbrowse_root = tmp_path / "external-agbrowse"
    agbrowse_root.mkdir()
    agbrowse_entry = agbrowse_root / "agbrowse.mjs"
    bridge = agbrowse_root / "research-bridge.mjs"
    agbrowse_entry.write_text("// test\n", encoding="utf-8")
    bridge.write_text("// test\n", encoding="utf-8")
    return WebGptScoutConfig(
        node_executable="node",
        agbrowse_entry=agbrowse_entry,
        agbrowse_root=agbrowse_root,
        bridge_script=bridge,
        cdp_endpoint="http://127.0.0.1:9222",
        artifact_root=tmp_path / "artifacts",
        raw_object_root=tmp_path / "raw",
        poll_timeout_seconds=30,
        command_timeout_seconds=5,
        rebind_timeout_seconds=3,
    )


def _happy_responses(request, answer: dict[str, object]) -> list[ProcessResult]:
    model_binding = {
        "ok": True,
        "model_family": "GPT-5.6 Sol Pro",
        "model_base": "GPT-5.6 Sol",
        "access_tier": "Pro",
        "reasoning_profile": "xhigh",
        "browser_session_id": "browser-commander-1",
        "request_id": request.request_id,
        "role": "RESEARCH_COMMANDER",
        "headed": True,
        "cdp_connected": True,
        "ui_tuple_verified": True,
        "fallback_used": False,
    }
    return [
        _process_json(
            {
                "ok": True,
                "status": "ready",
                "capabilities": [
                    {
                        "capabilityId": "chatgpt-active-tab-verification",
                        "state": "ok",
                    },
                    {"capabilityId": "chatgpt-composer-visible", "state": "ok"},
                ],
            }
        ),
        _process_json(
            {
                **model_binding,
                "conversation_id": "conversation-scout",
                "conversation_ids": ["conversation-scout"],
                "conversation_hrefs": ["https://chatgpt.com/c/conversation-scout"],
            }
        ),
        _process_json(
            {
                **model_binding,
                "status": "armed",
                "target_id": "target-commander-1",
                "conversation_id": "new-chat:target-commander-1",
                "active_browse_mode": "WEB_SEARCH",
                "active_browse_armed": True,
            }
        ),
        _process_json({"ok": True, "targetId": "target-commander-1"}),
        _process_json(
            {
                "ok": True,
                "status": "sent",
                "sessionId": "agbrowse-commander-1",
                "url": "https://chatgpt.com/",
            }
        ),
        _process_json(
            {
                "ok": True,
                "session": {
                    "targetId": "target-commander-1",
                    "sessionId": "agbrowse-commander-1",
                },
            }
        ),
        _process_json(
            {
                "ok": True,
                "rebound": True,
                "browser_session_id": "browser-commander-1",
                "role": "RESEARCH_COMMANDER",
                "target_id": "target-commander-1",
                "conversation_id": "conversation-commander-new",
                "conversation_url": (
                    "https://chatgpt.com/c/conversation-commander-new"
                ),
            }
        ),
        _process_json(
            {
                **model_binding,
                "conversation_id": "conversation-commander-new",
            }
        ),
        _process_json(
            {
                "ok": True,
                "status": "assistant-detected",
                "browser_session_id": "browser-commander-1",
                "request_id": request.request_id,
                "role": "RESEARCH_COMMANDER",
                "target_id": "target-commander-1",
                "conversation_id": "conversation-commander-new",
                "assistant_count": 1,
            }
        ),
        _process_json(
            {
                "ok": True,
                "status": "complete",
                "sessionId": "agbrowse-commander-1",
                "url": "https://chatgpt.com/c/conversation-commander-new",
                "answerText": json.dumps(answer),
            }
        ),
        _process_json(
            {
                **model_binding,
                "conversation_id": "conversation-commander-new",
                "response_complete": True,
                "thinking_stopped": False,
                "interrupted": False,
                "active_browse_verified": True,
                "active_browse_evidence_count": 1,
                "observed_at": (NOW + timedelta(minutes=4)).isoformat(),
            }
        ),
    ]


def test_prompt_is_hash_bound_and_has_no_api_or_order_fallback() -> None:
    request = _request()
    prompt = render_webgpt_commander_prompt(request)

    assert request.context_manifest_hash in prompt
    assert '"selected_commander":"WEBGPT_SOL_PRO"' in prompt
    assert "There is no API fallback" in prompt
    assert "must not edit a Champion" in prompt
    assert "must not" in prompt and "create an order" in prompt
    assert "Do not default to semiconductors, SOXL, or SOXS" in prompt
    assert COMMANDER_CREATED_AT_SENTINEL in prompt
    assert COMMANDER_HASH_SENTINEL in prompt


def test_happy_path_uses_shared_verified_transport_and_host_hashes(
    tmp_path: Path,
) -> None:
    selection = _selection()
    request = _request(selection)
    runner = FakeRunner(_happy_responses(request, _decision_payload(request)))
    commander = WebGptActiveResearchCommander(
        config=_config(tmp_path),
        selection_provider=lambda: selection,
        runner=runner,
        clock=lambda: NOW + timedelta(minutes=2),
    )

    decision = commander.command(
        request,
        prior_conversation_ids=["conversation-prior-role"],
    )

    assert isinstance(decision, ResearchDecisionV1)
    assert decision.created_at == NOW + timedelta(minutes=4)
    assert decision.output_hash == canonical_hash(
        decision.model_dump(mode="python", exclude={"output_hash"})
    )
    assert len(runner.calls) == 11
    assert all("api" not in " ".join(call[0]).lower() for call in runner.calls)
    assert any("RESEARCH_COMMANDER" in call[0] for call in runner.calls)
    result_path = (
        tmp_path
        / "artifacts"
        / request.research_cycle_id
        / request.request_id
        / "RESEARCH_COMMANDER"
        / "result.json"
    )
    assert result_path.is_file()
    assert json.loads(result_path.read_text(encoding="utf-8"))["output_hash"] == (
        decision.output_hash
    )


def test_proposal_and_decision_hashes_are_computed_only_by_host() -> None:
    selection = _selection()
    request = _request(selection)

    decision = parse_webgpt_commander_result(
        json.dumps(_decision_payload(request, with_proposal=True)),
        request=request,
        received_at=NOW + timedelta(minutes=4),
        current_selection=selection,
    )

    assert decision.proposal is not None
    assert decision.proposal.proposal_hash == canonical_hash(
        decision.proposal.model_dump(mode="python", exclude={"proposal_hash"})
    )
    assert decision.output_hash == canonical_hash(
        decision.model_dump(mode="python", exclude={"output_hash"})
    )


def test_model_mismatch_fails_before_send_and_writes_no_result(tmp_path: Path) -> None:
    selection = _selection()
    request = _request(selection)
    responses = _happy_responses(request, _decision_payload(request))
    preflight = json.loads(responses[1].stdout)
    preflight["model_family"] = "GPT-5.6 Sol"
    responses[1] = _process_json(preflight)
    runner = FakeRunner(responses)

    with pytest.raises(WebGptScoutError, match="model_mismatch"):
        WebGptActiveResearchCommander(
            config=_config(tmp_path),
            selection_provider=lambda: selection,
            runner=runner,
            clock=lambda: NOW + timedelta(minutes=2),
        ).command(request)

    assert len(runner.calls) == 2
    result_path = (
        tmp_path
        / "artifacts"
        / request.research_cycle_id
        / request.request_id
        / "RESEARCH_COMMANDER"
        / "result.json"
    )
    assert not result_path.exists()


def test_prior_role_conversation_cannot_be_reused(tmp_path: Path) -> None:
    selection = _selection()
    request = _request(selection)
    responses = _happy_responses(request, _decision_payload(request))
    rebound = json.loads(responses[6].stdout)
    rebound["conversation_id"] = "conversation-prior-role"
    rebound["conversation_url"] = "https://chatgpt.com/c/conversation-prior-role"
    responses[6] = _process_json(rebound)

    with pytest.raises(WebGptScoutError, match="conversation_reused"):
        WebGptActiveResearchCommander(
            config=_config(tmp_path),
            selection_provider=lambda: selection,
            runner=FakeRunner(responses),
            clock=lambda: NOW + timedelta(minutes=2),
        ).command(
            request,
            prior_conversation_ids=["conversation-prior-role"],
        )


def test_role_binding_change_is_rejected(tmp_path: Path) -> None:
    selection = _selection()
    request = _request(selection)
    responses = _happy_responses(request, _decision_payload(request))
    rebound = json.loads(responses[6].stdout)
    rebound["role"] = "WEB_SCOUT"
    responses[6] = _process_json(rebound)

    with pytest.raises(WebGptScoutError, match="role_binding_mismatch"):
        WebGptActiveResearchCommander(
            config=_config(tmp_path),
            selection_provider=lambda: selection,
            runner=FakeRunner(responses),
            clock=lambda: NOW + timedelta(minutes=2),
        ).command(request)


def test_selection_change_during_run_discards_decision(tmp_path: Path) -> None:
    selection = _selection()
    stale = _selection(selection_id="selection-webgpt-2", version=2)
    request = _request(selection)
    selections = iter([selection, stale])
    commander = WebGptActiveResearchCommander(
        config=_config(tmp_path),
        selection_provider=lambda: next(selections),
        runner=FakeRunner(_happy_responses(request, _decision_payload(request))),
        clock=lambda: NOW + timedelta(minutes=2),
    )

    with pytest.raises(WebGptCommanderError, match="decision_binding_invalid"):
        commander.command(request)

    result_path = (
        tmp_path
        / "artifacts"
        / request.research_cycle_id
        / request.request_id
        / "RESEARCH_COMMANDER"
        / "result.json"
    )
    assert not result_path.exists()


def test_response_binding_mismatch_fails_closed(tmp_path: Path) -> None:
    selection = _selection()
    request = _request(selection)
    answer = _decision_payload(request)
    answer["context_manifest_hash"] = "d" * 64

    with pytest.raises(WebGptCommanderError, match="decision_binding_invalid"):
        WebGptActiveResearchCommander(
            config=_config(tmp_path),
            selection_provider=lambda: selection,
            runner=FakeRunner(_happy_responses(request, answer)),
            clock=lambda: NOW + timedelta(minutes=2),
        ).command(request)
