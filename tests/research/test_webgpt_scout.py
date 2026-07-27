from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.webgpt_scout import (
    RUNTIME_BINDING_SENTINEL,
    SOURCE_HASH_SENTINEL,
    AvailableDataCatalogEntry,
    ProcessResult,
    WebGptActiveResearchScout,
    WebGptScoutConfig,
    WebGptScoutError,
    WebScoutRequestV1,
    available_data_catalog_hash,
    render_web_scout_prompt,
)

AS_OF = datetime(2026, 7, 27, 21, 0, tzinfo=UTC)


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


def process_json(payload: dict[str, object]) -> ProcessResult:
    return ProcessResult(returncode=0, stdout=json.dumps(payload), stderr="")


def catalog_entries() -> list[AvailableDataCatalogEntry]:
    return [
        AvailableDataCatalogEntry(
            symbol="AAPL",
            asset_kind="US_EQUITY",
            primary_venue="NASDAQ",
            dataset_ids=["daily-bars", "sec-filings"],
            point_in_time_fields=["available_at"],
        ),
        AvailableDataCatalogEntry(
            symbol="SPY",
            asset_kind="US_ETF",
            primary_venue="NYSE ARCA",
            dataset_ids=["daily-bars", "constituents-pit"],
            point_in_time_fields=["available_at", "membership_valid_from"],
        ),
    ]


def make_request(*, prior_conversations: list[str] | None = None) -> WebScoutRequestV1:
    catalog = catalog_entries()
    return WebScoutRequestV1(
        request_id="request-001",
        research_cycle_id="cycle-001",
        created_at=AS_OF - timedelta(minutes=1),
        as_of=AS_OF,
        data_available_cutoff=AS_OF,
        expires_at=AS_OF + timedelta(hours=2),
        context_manifest_hash=canonical_hash({"cycle": 1}),
        catalog_version="us-listed-v1",
        available_data_catalog_hash=available_data_catalog_hash("us-listed-v1", catalog),
        available_data_catalog=catalog,
        research_questions=[
            {
                "question_id": "question-alpha-1",
                "purpose": "DISCOVER_ALPHA",
                "question": "Which quality effects survive known factor neutralization?",
                "instrument_scope": ["AAPL", "SPY"],
                "factor_scope": ["quality", "market beta"],
            },
            {
                "question_id": "question-failure-1",
                "purpose": "EXPLAIN_STRATEGY_FAILURE",
                "question": "Which regimes falsify the current strategy mechanism?",
                "instrument_scope": ["SPY"],
                "factor_scope": ["regime"],
            },
        ],
        query_budget=8,
        prior_conversation_ids=prior_conversations or [],
    )


def evidence_result(request: WebScoutRequestV1) -> dict[str, object]:
    published_at = AS_OF - timedelta(hours=2)
    first_available_at = AS_OF - timedelta(hours=1, minutes=59)
    captured_at = AS_OF + timedelta(minutes=4)
    excerpt = "The filing reports a bounded operating metric used only as research evidence."
    url = "https://www.sec.gov/example/aapl"
    title = "Issuer filing"
    publisher = "SEC"
    return {
        "schema_version": "research_evidence_bundle_v1",
        "request_id": request.request_id,
        "research_cycle_id": request.research_cycle_id,
        "role": "WEB_SCOUT",
        "context_manifest_hash": request.context_manifest_hash,
        "available_data_catalog_hash": request.available_data_catalog_hash,
        "model_family": "GPT-5.6 Sol Pro",
        "reasoning_profile": "xhigh",
        "browser_session_id": RUNTIME_BINDING_SENTINEL,
        "conversation_id": RUNTIME_BINDING_SENTINEL,
        "agbrowse_request_id": RUNTIME_BINDING_SENTINEL,
        "as_of": request.as_of.isoformat(),
        "data_available_cutoff": request.data_available_cutoff.isoformat(),
        "captured_at": captured_at.isoformat(),
        "queries": [
            {
                "query_id": "query-001",
                "purpose": "DISCOVER_ALPHA",
                "query": "site:sec.gov AAPL quality operating evidence",
                "started_at": AS_OF.isoformat(),
                "completed_at": (AS_OF + timedelta(minutes=2)).isoformat(),
                "status": "COMPLETED",
                "source_ids": ["source-sec-001"],
                "instrument_scope": ["AAPL", "SPY"],
                "factor_scope": ["quality"],
            }
        ],
        "sources": [
            {
                "source_id": "source-sec-001",
                "url": url,
                "title": title,
                "publisher": publisher,
                "published_at": published_at.isoformat(),
                "first_available_at": first_available_at.isoformat(),
                "captured_at": captured_at.isoformat(),
                "source_tier": "TIER_1_OFFICIAL",
                "content_hash": SOURCE_HASH_SENTINEL,
                "excerpt": excerpt,
                "license_note": "Short factual excerpt; raw page is not persisted here.",
                "instrument_tags": ["AAPL"],
                "factor_tags": ["quality"],
                "corroborated": True,
                "contradiction": False,
            }
        ],
        "claims": [
            {
                "claim_id": "claim-quality-001",
                "claim_kind": "FALSIFICATION_LEAD",
                "statement": "The operating metric supports a falsifiable quality hypothesis.",
                "verification_status": "CORROBORATED",
                "source_ids": ["source-sec-001"],
                "instrument_tags": ["AAPL", "SPY"],
                "factor_tags": ["quality"],
                "falsification_test": "Remove market and quality factor exposure before OOS.",
            }
        ],
        "unresolved_questions": [],
    }


def make_config(tmp_path: Path) -> WebGptScoutConfig:
    agbrowse_root = tmp_path / "external-agbrowse"
    agbrowse_root.mkdir()
    agbrowse_entry = agbrowse_root / "agbrowse.mjs"
    bridge_script = agbrowse_root / "research-bridge.mjs"
    agbrowse_entry.write_text("// test only\n", encoding="utf-8")
    bridge_script.write_text("// test only\n", encoding="utf-8")
    return WebGptScoutConfig(
        node_executable="node",
        agbrowse_entry=agbrowse_entry,
        agbrowse_root=agbrowse_root,
        bridge_script=bridge_script,
        cdp_endpoint="http://127.0.0.1:9222",
        artifact_root=tmp_path / "artifacts",
        raw_object_root=tmp_path / "raw",
        poll_timeout_seconds=30,
        command_timeout_seconds=5,
        rebind_timeout_seconds=3,
    )


def happy_responses(request: WebScoutRequestV1) -> list[ProcessResult]:
    model_binding = {
        "ok": True,
        "model_family": "GPT-5.6 Sol Pro",
        "model_base": "GPT-5.6 Sol",
        "access_tier": "Pro",
        "reasoning_profile": "xhigh",
        "browser_session_id": "browser-001",
        "request_id": request.request_id,
        "role": "WEB_SCOUT",
        "headed": True,
        "cdp_connected": True,
        "ui_tuple_verified": True,
        "fallback_used": False,
    }
    return [
        process_json(
            {
                "ok": True,
                "status": "ready",
                "capabilities": [
                    {
                        "capabilityId": "chatgpt-active-tab-verification",
                        "state": "ok",
                    },
                    {
                        "capabilityId": "chatgpt-composer-visible",
                        "state": "ok",
                    },
                ],
            }
        ),
        process_json(
            {
                **model_binding,
                "conversation_id": "conversation-home",
                "conversation_ids": ["conversation-old"],
                "conversation_hrefs": ["https://chatgpt.com/c/conversation-old"],
            }
        ),
        process_json(
            {
                **model_binding,
                "status": "armed",
                "target_id": "target-001",
                "conversation_id": "new-chat:target-001",
                "active_browse_mode": "WEB_SEARCH",
                "active_browse_armed": True,
            }
        ),
        process_json(
            {
                "ok": True,
                "targetId": "target-001",
            }
        ),
        process_json(
            {
                "ok": True,
                "status": "sent",
                "sessionId": "agbrowse-session-001",
                "url": "https://chatgpt.com/",
            }
        ),
        process_json(
            {
                "ok": True,
                "session": {
                    "targetId": "target-001",
                    "sessionId": "agbrowse-session-001",
                },
            }
        ),
        process_json(
            {
                "ok": True,
                "rebound": True,
                "browser_session_id": "browser-001",
                "role": "WEB_SCOUT",
                "target_id": "target-001",
                "conversation_id": "conversation-new",
                "conversation_url": "https://chatgpt.com/c/conversation-new",
            }
        ),
        process_json({**model_binding, "conversation_id": "conversation-new"}),
        process_json(
            {
                "ok": True,
                "status": "assistant-detected",
                "browser_session_id": "browser-001",
                "request_id": request.request_id,
                "role": "WEB_SCOUT",
                "target_id": "target-001",
                "conversation_id": "conversation-new",
                "assistant_count": 1,
            }
        ),
        process_json(
            {
                "ok": True,
                "status": "complete",
                "sessionId": "agbrowse-session-001",
                "url": "https://chatgpt.com/c/conversation-new",
                "answerText": json.dumps(evidence_result(request)),
            }
        ),
        process_json(
            {
                **model_binding,
                "conversation_id": "conversation-new",
                "response_complete": True,
                "thinking_stopped": False,
                "interrupted": False,
                "active_browse_verified": True,
                "active_browse_evidence_count": 1,
                "observed_at": (AS_OF + timedelta(minutes=4)).isoformat(),
            }
        ),
    ]


def test_prompt_requires_active_browse_and_catalog_wide_alpha_research() -> None:
    prompt = render_web_scout_prompt(make_request())

    assert "Actively browse the live web" in prompt
    assert "durable, falsifiable alpha hypotheses" in prompt
    assert "complete versioned available_data_catalog" in prompt
    assert "Do not default to semiconductors, SOXL, or SOXS" in prompt
    assert "social-only claims must be UNVERIFIED" in prompt
    assert "does not create orders" in prompt


def test_happy_path_binds_fresh_browser_conversation_and_request(tmp_path: Path) -> None:
    request = make_request(prior_conversations=["conversation-prior-role"])
    runner = FakeRunner(happy_responses(request))
    scout = WebGptActiveResearchScout(make_config(tmp_path), runner=runner)

    bundle = scout.scout(request)

    assert bundle.browser_session_id == "browser-001"
    assert bundle.conversation_id == "conversation-new"
    assert bundle.agbrowse_request_id == "agbrowse-session-001"
    assert bundle.queries[0].source_ids == ["source-sec-001"]
    assert bundle.claims[0].falsification_test is not None
    assert len(runner.calls) == 11
    prepare_call = runner.calls[2][0]
    assert "prepare-active-browse" in prepare_call
    tab_switch_call = runner.calls[3][0]
    assert tab_switch_call[-3:] == ["tab-switch", "target-001", "--json"]
    send_call = runner.calls[4][0]
    assert "--reuse-tab" in send_call
    assert "--new-tab" not in send_call
    assert send_call[send_call.index("--vendor") :][:2] == ["--vendor", "chatgpt"]
    rebind_call = runner.calls[6][0]
    assert rebind_call[rebind_call.index("--role") :][:2] == ["--role", "WEB_SCOUT"]
    assistant_wait_call = runner.calls[8][0]
    assert "await-assistant" in assistant_wait_call
    assert all("api" not in " ".join(call[0]).lower() for call in runner.calls)

    result_path = (
        tmp_path
        / "artifacts"
        / request.research_cycle_id
        / request.request_id
        / "WEB_SCOUT"
        / "result.json"
    )
    assert result_path.is_file()
    assert "The filing reports" in result_path.read_text(encoding="utf-8")


def test_model_mismatch_fails_before_send(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    preflight = json.loads(responses[1].stdout)
    preflight["model_family"] = "GPT-5.6 Sol"
    responses[1] = process_json(preflight)
    runner = FakeRunner(responses)

    with pytest.raises(WebGptScoutError, match="model_mismatch"):
        WebGptActiveResearchScout(make_config(tmp_path), runner=runner).scout(request)

    assert len(runner.calls) == 2


def test_access_tier_mismatch_fails_before_send(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    preflight = json.loads(responses[1].stdout)
    preflight["access_tier"] = "Free"
    responses[1] = process_json(preflight)
    runner = FakeRunner(responses)

    with pytest.raises(WebGptScoutError, match="model_mismatch"):
        WebGptActiveResearchScout(make_config(tmp_path), runner=runner).scout(request)

    assert len(runner.calls) == 2


def test_web_search_mode_must_be_armed_before_send(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    prepared = json.loads(responses[2].stdout)
    prepared["active_browse_armed"] = False
    responses[2] = process_json(prepared)
    runner = FakeRunner(responses)

    with pytest.raises(WebGptScoutError, match="active_browse_not_armed"):
        WebGptActiveResearchScout(make_config(tmp_path), runner=runner).scout(request)

    assert len(runner.calls) == 3


def test_reused_conversation_is_rejected(tmp_path: Path) -> None:
    request = make_request(prior_conversations=["conversation-reused"])
    responses = happy_responses(request)
    rebound = json.loads(responses[6].stdout)
    rebound["conversation_id"] = "conversation-reused"
    rebound["conversation_url"] = "https://chatgpt.com/c/conversation-reused"
    responses[6] = process_json(rebound)

    with pytest.raises(WebGptScoutError, match="conversation_reused"):
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)


def test_postflight_must_prove_active_browse(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    postflight = json.loads(responses[10].stdout)
    postflight["active_browse_verified"] = False
    postflight["active_browse_evidence_count"] = 0
    responses[10] = process_json(postflight)

    with pytest.raises(WebGptScoutError, match="active_browse_not_verified"):
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)


def test_interrupted_or_thinking_stopped_response_is_rejected(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    poll["thinkingStopped"] = True
    poll["interrupted"] = True
    responses[9] = process_json(poll)

    with pytest.raises(WebGptScoutError, match="response_not_complete"):
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)


def test_result_cannot_tag_instrument_outside_versioned_catalog(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["sources"][0]["instrument_tags"] = ["MSFT"]
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    with pytest.raises(WebGptScoutError, match="catalog_scope_violation"):
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)


def test_model_cannot_choose_runtime_binding_ids(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["browser_session_id"] = "model-invented-browser"
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    with pytest.raises(WebGptScoutError, match="result_runtime_placeholder_invalid"):
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)


def test_model_cannot_choose_source_content_hash(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["sources"][0]["content_hash"] = "0" * 64
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    with pytest.raises(
        WebGptScoutError,
        match="result_source_hash_placeholder_invalid",
    ):
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)


def test_non_symbol_instrument_tags_move_to_factor_tags_deterministically(
    tmp_path: Path,
) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["sources"][0]["instrument_tags"] = ["AAPL", "Fixed Income", "AAPL"]
    result["sources"][0]["factor_tags"] = ["quality"]
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    bundle = WebGptActiveResearchScout(
        make_config(tmp_path),
        runner=FakeRunner(responses),
    ).scout(request)

    assert bundle.sources[0].instrument_tags == ["AAPL"]
    assert bundle.sources[0].factor_tags == ["quality", "fixed income"]


def test_unsupported_corroboration_is_downgraded_conservatively(
    tmp_path: Path,
) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["sources"][0]["source_tier"] = "TIER_4_INDUSTRY_ANALYSIS"
    result["sources"][0]["publisher"] = "Single Publisher"
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    bundle = WebGptActiveResearchScout(
        make_config(tmp_path),
        runner=FakeRunner(responses),
    ).scout(request)

    assert bundle.claims[0].verification_status.value == "UNVERIFIED"


def test_social_source_cannot_remain_corroborated_after_claim_downgrade(
    tmp_path: Path,
) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["sources"][0]["source_tier"] = "TIER_5_SOCIAL"
    result["sources"][0]["publisher"] = "Social Publisher"
    result["sources"][0]["corroborated"] = True
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    bundle = WebGptActiveResearchScout(
        make_config(tmp_path),
        runner=FakeRunner(responses),
    ).scout(request)

    assert bundle.claims[0].verification_status.value == "UNVERIFIED"
    assert bundle.sources[0].corroborated is False


def test_model_capture_times_are_replaced_by_postflight_time(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["captured_at"] = (AS_OF - timedelta(days=1)).isoformat()
    result["sources"][0]["captured_at"] = (AS_OF - timedelta(days=1)).isoformat()
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    bundle = WebGptActiveResearchScout(
        make_config(tmp_path),
        runner=FakeRunner(responses),
    ).scout(request)

    assert bundle.captured_at == AS_OF + timedelta(minutes=4)
    assert bundle.sources[0].captured_at == bundle.captured_at


def test_schema_errors_fail_without_echoing_model_payload(tmp_path: Path) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    result = json.loads(poll["answerText"])
    result["sources"][0]["source_tier"] = "SECRET_MARKER_INVALID_TIER"
    poll["answerText"] = json.dumps(result)
    responses[9] = process_json(poll)

    with pytest.raises(WebGptScoutError) as captured:
        WebGptActiveResearchScout(
            make_config(tmp_path),
            runner=FakeRunner(responses),
        ).scout(request)

    assert captured.value.code == "result_schema_invalid"
    assert "SECRET_MARKER_INVALID_TIER" not in captured.value.detail


def test_raw_newlines_inside_json_strings_are_escaped_deterministically(
    tmp_path: Path,
) -> None:
    request = make_request()
    responses = happy_responses(request)
    poll = json.loads(responses[9].stdout)
    poll["answerText"] = poll["answerText"].replace(
        "bounded operating metric",
        "bounded\noperating metric",
    )
    responses[9] = process_json(poll)

    bundle = WebGptActiveResearchScout(
        make_config(tmp_path),
        runner=FakeRunner(responses),
    ).scout(request)

    assert "bounded\noperating metric" in bundle.sources[0].excerpt


def test_env_config_requires_explicit_external_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [
        "TRADING_RESEARCH_NODE_EXECUTABLE",
        "TRADING_RESEARCH_AGBROWSE_ENTRY",
        "TRADING_RESEARCH_AGBROWSE_ROOT",
        "TRADING_RESEARCH_WEBGPT_BRIDGE",
        "TRADING_RESEARCH_CDP_ENDPOINT",
        "TRADING_RESEARCH_ARTIFACT_ROOT",
        "TRADING_RESEARCH_RAW_OBJECT_ROOT",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(WebGptScoutError, match="config_missing"):
        WebGptScoutConfig.from_env()


def test_scout_request_supports_general_us_equity_and_etf_catalog() -> None:
    request = make_request()

    assert {entry.symbol for entry in request.available_data_catalog} == {"AAPL", "SPY"}
    assert {entry.asset_kind for entry in request.available_data_catalog} == {
        "US_EQUITY",
        "US_ETF",
    }
    assert "SOXL" not in {entry.symbol for entry in request.available_data_catalog}
