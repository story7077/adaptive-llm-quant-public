from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

import trading.cli as cli_module
from trading.cli import app
from trading.domain.hashing import canonical_hash
from trading.persistence.research import ResearchRepository
from trading.research.contracts import (
    AlgorithmProposalV1,
    AvailableDataCatalogV1,
    AvailableInstrumentV1,
    ChallengerManifestV1,
    ChallengerStatus,
    CommanderSelectionV1,
    ResearchDecisionV1,
)
from trading.research.evidence import (
    ResearchEvidenceBundleV1,
    research_source_content_hash,
)
from trading.research.file_runtime import (
    ResearchFileRuntimeError,
    atomic_write_json,
    load_json_object,
    resolve_local_output,
)
from trading.research.host import build_research_request
from trading.research.webgpt_scout import (
    AvailableDataCatalogEntry,
    WebGptScoutConfig,
    WebResearchQuestion,
    WebScoutRequestV1,
    available_data_catalog_hash,
)
from trading.runtime.prospective_evaluation import (
    ProspectiveEvaluationRunResult,
)


def _configure_cli(
    monkeypatch,
    *,
    database_url: str,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    monkeypatch.setenv("TRADING_CONFIG_DIR", str(repository_root / "config"))
    monkeypatch.setenv("TRADING_RAW_STORE", str(tmp_path / "raw"))
    monkeypatch.setenv(
        "TRADING_PAPER_ACCOUNT_FILE",
        str(repository_root / "config" / "paper-account.example.yaml"),
    )


def _catalog(as_of: datetime) -> AvailableDataCatalogV1:
    payload = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-cli-v1",
        "as_of": as_of,
        "data_available_cutoff": as_of,
        "instruments": [
            AvailableInstrumentV1(
                symbol="SPY",
                asset_class="US_ETF",
                first_available_at=as_of - timedelta(days=1000),
                point_in_time_membership_available=True,
                daily_history_sessions=800,
                intraday_history_sessions=200,
                execution_supported=True,
            )
        ],
        "dataset_versions": {"daily": "pit-daily-v1"},
    }
    return AvailableDataCatalogV1(
        **payload,
        catalog_hash=canonical_hash(payload),
    )


def _evidence(
    *,
    request_id: str,
    cycle_id: str,
    as_of: datetime,
    context_hash: str = "9" * 64,
    catalog_hash: str = "8" * 64,
) -> ResearchEvidenceBundleV1:
    published_at = as_of - timedelta(hours=2)
    first_available_at = as_of - timedelta(hours=1, minutes=59)
    excerpt = "The issuer reported a bounded point-in-time operating result."
    url = "https://example.test/official-source"
    title = "Official source"
    source_hash = research_source_content_hash(
        url=url,
        title=title,
        publisher="Official Publisher",
        published_at=published_at,
        first_available_at=first_available_at,
        excerpt=excerpt,
    )
    return ResearchEvidenceBundleV1.model_validate(
        {
            "schema_version": "research_evidence_bundle_v1",
            "request_id": request_id,
            "research_cycle_id": cycle_id,
            "role": "WEB_SCOUT",
            "context_manifest_hash": context_hash,
            "available_data_catalog_hash": catalog_hash,
            "model_family": "GPT-5.6 Sol Pro",
            "reasoning_profile": "xhigh",
            "browser_session_id": "browser-cli",
            "conversation_id": "conversation-cli",
            "agbrowse_request_id": "agbrowse-cli",
            "as_of": as_of,
            "data_available_cutoff": as_of,
            "captured_at": as_of,
            "queries": [
                {
                    "query_id": "query-cli",
                    "purpose": "DISCOVER_ALPHA",
                    "query": "Find primary evidence for a durable cross-asset mechanism.",
                    "started_at": as_of - timedelta(minutes=30),
                    "completed_at": as_of - timedelta(minutes=10),
                    "status": "COMPLETED",
                    "source_ids": ["source-official"],
                    "instrument_scope": ["SPY"],
                    "factor_scope": ["quality"],
                }
            ],
            "sources": [
                {
                    "source_id": "source-official",
                    "url": url,
                    "title": title,
                    "publisher": "Official Publisher",
                    "published_at": published_at,
                    "first_available_at": first_available_at,
                    "captured_at": as_of,
                    "source_tier": "TIER_1_OFFICIAL",
                    "content_hash": source_hash,
                    "excerpt": excerpt,
                    "license_note": "Short factual excerpt retained for provenance.",
                    "instrument_tags": ["SPY"],
                    "factor_tags": ["quality"],
                    "corroborated": True,
                    "contradiction": False,
                }
            ],
            "claims": [
                {
                    "claim_id": "claim-cli",
                    "claim_kind": "ECONOMIC_MECHANISM",
                    "statement": "The primary source supports a falsifiable mechanism.",
                    "verification_status": "CORROBORATED",
                    "source_ids": ["source-official"],
                    "instrument_tags": ["SPY"],
                    "factor_tags": ["quality"],
                    "falsification_test": "Neutralize quality exposure.",
                }
            ],
            "unresolved_questions": [],
        }
    )


def _proposal() -> AlgorithmProposalV1:
    payload = {
        "schema_version": "algorithm_proposal_v1",
        "proposal_id": "proposal-cli-v1",
        "hypothesis_id": "hypothesis-cli-v1",
        "hypothesis": "A versioned quality feature may improve cost-adjusted alpha.",
        "economic_mechanism": "Persistent profitability may be underreacted to.",
        "why_current_model_failed": "The Champion omits this bounded feature.",
        "parent_strategy_id": "T1",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "T1",
        "proposed_strategy_version": "1.1.0",
        "target_horizon": "1d",
        "target_universe": ["SPY"],
        "required_data": ["pit_daily_bars"],
        "feature_changes": ["Add a point-in-time quality feature."],
        "signal_formula_changes": ["Blend quality with the existing score."],
        "entry_rule_changes": [],
        "exit_rule_changes": [],
        "position_sizing_changes": [],
        "regime_activation_changes": [],
        "calibration_changes": [],
        "expected_edge_source": "Slow information diffusion.",
        "expected_failure_modes": ["Quality becomes crowded."],
        "invalidation_conditions": ["No effect after factor neutralization."],
        "placebo_tests": ["Shift the feature date."],
        "stress_tests": ["Triple modeled costs."],
        "minimum_economic_effect": {"annualized_difference": 0.01},
        "estimated_capacity": {"usd": 1000000},
        "estimated_turnover": {"annualized": 1.2},
        "estimated_cost_sensitivity": {"triple_cost": True},
        "files_allowed_to_change": [
            "src/trading/strategies/challengers/t1_v1_1_0.py"
        ],
        "tests_required": ["tests/research/test_t1_v1_1_0.py"],
        "evidence_source_ids": ["source-official"],
        "raw_confidence": 0.4,
    }
    return AlgorithmProposalV1(
        **payload,
        proposal_hash=canonical_hash(payload),
    )


def _decision(
    request,
    proposal: AlgorithmProposalV1,
    created_at: datetime,
) -> ResearchDecisionV1:
    payload = {
        "schema_version": "research_decision_v1",
        "request_id": request.request_id,
        "research_cycle_id": request.research_cycle_id,
        "selected_commander": request.selected_commander,
        "commander_selection_id": request.commander_selection_id,
        "commander_selection_version": request.commander_selection_version,
        "source_snapshot_commit": request.source_snapshot_commit,
        "champion_version": request.champion_version,
        "experiment_family": request.experiment_family,
        "context_manifest_hash": request.context_manifest_hash,
        "request_schema_version": request.schema_version,
        "request_expires_at": request.expires_at,
        "decision": "PROPOSE_STRATEGY_REVISION",
        "rationale": "The bounded hypothesis is ready for isolated falsification.",
        "proposal": proposal,
        "requested_evidence": [],
        "created_at": created_at,
    }
    return ResearchDecisionV1(
        **payload,
        output_hash=canonical_hash(payload),
    )


def _manifest(
    decision: ResearchDecisionV1,
    proposal: AlgorithmProposalV1,
    created_at: datetime,
    *,
    challenger_id: str = "challenger-cli-v1",
    estimated_turnover: dict[str, object] | None = None,
) -> ChallengerManifestV1:
    payload = {
        "schema_version": "challenger_manifest_v1",
        "challenger_id": challenger_id,
        "strategy_id": proposal.proposed_strategy_id,
        "strategy_version": proposal.proposed_strategy_version,
        "parent_version": proposal.parent_strategy_version,
        "hypothesis_id": proposal.hypothesis_id,
        "experiment_family": decision.experiment_family,
        "source_commit": "1" * 40,
        "patch_hash": "2" * 64,
        "proposal_hash": proposal.proposal_hash,
        "code_hash": "3" * 64,
        "config_hash": "4" * 64,
        "test_manifest_hash": "5" * 64,
        "created_by_commander": decision.selected_commander,
        "implemented_by_builder": "CODEX_CANDIDATE_BUILDER",
        "evidence_source_ids": proposal.evidence_source_ids,
        "required_data": proposal.required_data,
        "decision_horizon": proposal.target_horizon,
        "execution_universe": proposal.target_universe,
        "estimated_turnover": (
            proposal.estimated_turnover
            if estimated_turnover is None
            else estimated_turnover
        ),
        "estimated_capacity": proposal.estimated_capacity,
        "status": "PROPOSED",
        "created_at": created_at,
    }
    return ChallengerManifestV1(
        **payload,
        manifest_hash=canonical_hash(payload),
    )


def test_research_cli_executes_versioned_file_pipeline(
    sqlite_database,
    repository_root,
    tmp_path,
    monkeypatch,
) -> None:
    database_url, _, factory = sqlite_database
    _configure_cli(
        monkeypatch,
        database_url=database_url,
        repository_root=repository_root,
        tmp_path=tmp_path,
    )
    runner = CliRunner()
    selected = runner.invoke(
        app,
        [
            "research",
            "select",
            "--commander",
            "CODEX_SOL_MAX",
            "--expected-version",
            "0",
        ],
    )
    assert selected.exit_code == 0, selected.output
    selection = CommanderSelectionV1.model_validate(
        json.loads(selected.output)["selection"]
    )
    as_of = selection.created_at + timedelta(minutes=1)
    catalog = _catalog(as_of)
    evidence = _evidence(
        request_id="scout-cli-v1",
        cycle_id="cycle-cli-v1",
        as_of=as_of,
    )
    request = build_research_request(
        request_id="request-cli-v1",
        research_cycle_id="cycle-cli-v1",
        commander_selection=selection,
        created_at=as_of,
        as_of=as_of,
        data_available_cutoff=as_of,
        expires_at=as_of + timedelta(hours=2),
        source_snapshot_commit="a" * 40,
        champion_version="1.0.0",
        experiment_family="family-cli-v1",
        champion_manifest={"strategy_id": "T1"},
        active_challenger_manifests=[],
        strategy_performance_summary={},
        failure_case_clusters=[],
        regime_summary={},
        execution_cost_summary={},
        capacity_summary={},
        recent_market_evidence=[],
        recent_web_research=[
            {"evidence_bundle_hash": canonical_hash(evidence)}
        ],
        available_data_catalog=catalog,
        allowed_change_scope=["src/trading/strategies/"],
        forbidden_change_scope=["src/trading/risk/"],
        experiment_budget={"submissions_remaining": 2},
    )
    proposal = _proposal()
    decision = _decision(request, proposal, as_of + timedelta(minutes=1))
    manifest = _manifest(
        decision,
        proposal,
        as_of + timedelta(minutes=2),
    )
    inputs = tmp_path / "inputs"
    request_file = inputs / "request.json"
    evidence_file = inputs / "evidence.json"
    catalog_file = inputs / "catalog.json"
    decision_file = inputs / "decision.json"
    manifest_file = inputs / "manifest.json"
    for path, value in (
        (request_file, request),
        (evidence_file, evidence),
        (catalog_file, catalog),
        (decision_file, decision),
        (manifest_file, manifest),
    ):
        assert atomic_write_json(path, value)

    prepared = runner.invoke(
        app,
        [
            "research",
            "cycle-prepare",
            "--request",
            str(request_file),
            "--bundle-root",
            str(tmp_path / "runs"),
        ],
    )
    assert prepared.exit_code == 0, prepared.output
    assert json.loads(prepared.output)["real_order_routing"] is False
    assert (
        tmp_path
        / "runs"
        / request.research_cycle_id
        / "request"
        / "research_request.json"
    ).is_file()

    evidence_import = runner.invoke(
        app,
        [
            "research",
            "evidence-import",
            "--request",
            str(request_file),
            "--evidence",
            str(evidence_file),
            "--imported-at",
            (as_of + timedelta(minutes=1)).isoformat(),
        ],
    )
    assert evidence_import.exit_code == 0, evidence_import.output
    assert json.loads(evidence_import.output)["created"] is True

    decision_import = runner.invoke(
        app,
        [
            "research",
            "decision-import",
            "--request",
            str(request_file),
            "--decision",
            str(decision_file),
            "--catalog",
            str(catalog_file),
            "--evidence",
            str(evidence_file),
            "--received-at",
            (as_of + timedelta(minutes=2)).isoformat(),
        ],
    )
    assert decision_import.exit_code == 0, decision_import.output
    assert json.loads(decision_import.output)["proposal_id"] == proposal.proposal_id

    def reject_status_scan(*_: object, **__: object) -> None:
        raise AssertionError("Challenger registration must use exact proposal lookup")

    with monkeypatch.context() as registration_patch:
        registration_patch.setattr(
            ResearchRepository,
            "status",
            reject_status_scan,
        )
        registered = runner.invoke(
            app,
            [
                "research",
                "challenger-register",
                "--decision",
                str(decision_file),
                "--manifest",
                str(manifest_file),
            ],
        )
    assert registered.exit_code == 0, registered.output
    registered_payload = json.loads(registered.output)
    assert registered_payload["created"] is True
    assert registered_payload["real_order_routing"] is False

    status = ResearchRepository(factory).status()
    assert status["challengers"][0]["challenger_id"] == manifest.challenger_id
    assert status["challengers"][0]["current_status"] == "PROPOSED"

    mismatched = _manifest(
        decision,
        proposal,
        as_of + timedelta(minutes=3),
        challenger_id="challenger-cli-mismatch",
        estimated_turnover={"annualized": 99},
    )
    mismatch_file = inputs / "manifest-mismatch.json"
    assert atomic_write_json(mismatch_file, mismatched)
    rejected = runner.invoke(
        app,
        [
            "research",
            "challenger-register",
            "--decision",
            str(decision_file),
            "--manifest",
            str(mismatch_file),
        ],
    )
    assert rejected.exit_code != 0
    assert "estimated_turnover" in rejected.output


def test_scout_cli_writes_only_to_local_ignored_output(
    tmp_path,
    monkeypatch,
) -> None:
    local_repository = tmp_path / "public"
    local_repository.mkdir()
    local_root = local_repository / ".local" / "research"
    artifact_root = local_root / "artifacts"
    raw_root = local_root / "raw"
    as_of = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
    entry = AvailableDataCatalogEntry(
        symbol="SPY",
        asset_kind="US_ETF",
        primary_venue="ARCX",
        dataset_ids=["pit-daily-v1"],
        point_in_time_fields=["available_at"],
        available_from=as_of - timedelta(days=1000),
    )
    catalog_hash = available_data_catalog_hash("catalog-scout-v1", [entry])
    request = WebScoutRequestV1(
        request_id="scout-request-cli",
        research_cycle_id="scout-cycle-cli",
        created_at=as_of,
        as_of=as_of,
        data_available_cutoff=as_of,
        expires_at=as_of + timedelta(hours=2),
        context_manifest_hash="7" * 64,
        catalog_version="catalog-scout-v1",
        available_data_catalog_hash=catalog_hash,
        available_data_catalog=[entry],
        research_questions=[
            WebResearchQuestion(
                question_id="question-cli",
                purpose="DISCOVER_ALPHA",
                question="Find a falsifiable primary-source mechanism.",
                instrument_scope=["SPY"],
                factor_scope=["quality"],
            )
        ],
        query_budget=4,
    )
    bundle = _evidence(
        request_id=request.request_id,
        cycle_id=request.research_cycle_id,
        as_of=as_of,
        context_hash=request.context_manifest_hash,
        catalog_hash=request.available_data_catalog_hash,
    )
    request_file = tmp_path / "scout-request.json"
    assert atomic_write_json(request_file, request)
    config = WebGptScoutConfig(
        node_executable="node",
        agbrowse_entry=tmp_path / "agbrowse.mjs",
        agbrowse_root=tmp_path,
        bridge_script=tmp_path / "bridge.mjs",
        cdp_endpoint="http://127.0.0.1:9222",
        artifact_root=artifact_root,
        raw_object_root=raw_root,
    )

    class _FakeScout:
        def __init__(self, _: WebGptScoutConfig) -> None:
            pass

        def scout(self, supplied: WebScoutRequestV1) -> ResearchEvidenceBundleV1:
            assert supplied == request
            return bundle

    monkeypatch.setenv("TRADING_REAL_LLM_ENABLED", "true")
    monkeypatch.setattr(cli_module, "repo_root", lambda: local_repository)
    monkeypatch.setattr(
        cli_module.WebGptScoutConfig,
        "from_env",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(cli_module, "WebGptActiveResearchScout", _FakeScout)
    output = local_root / "evidence.json"
    result = CliRunner().invoke(
        app,
        [
            "research",
            "scout",
            "--request",
            str(request_file),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence_bundle_hash"] == canonical_hash(bundle)
    assert payload["real_order_routing"] is False
    assert load_json_object(output)["request_id"] == request.request_id

    monkeypatch.setenv("TRADING_REAL_BROKER_ENABLED", "true")
    blocked = CliRunner().invoke(
        app,
        [
            "research",
            "scout",
            "--request",
            str(request_file),
            "--output",
            str(local_root / "must-not-exist.json"),
        ],
    )
    assert blocked.exit_code != 0
    assert blocked.exception is not None
    assert "Real broker routing is not implemented" in str(blocked.exception)
    assert not (local_root / "must-not-exist.json").exists()


def test_commander_cli_writes_validated_decision_to_prepared_cycle(
    sqlite_database,
    repository_root,
    tmp_path,
    monkeypatch,
) -> None:
    database_url, _, _ = sqlite_database
    _configure_cli(
        monkeypatch,
        database_url=database_url,
        repository_root=repository_root,
        tmp_path=tmp_path,
    )
    monkeypatch.setenv("TRADING_REAL_LLM_ENABLED", "true")
    monkeypatch.setenv("TRADING_REAL_BROKER_ENABLED", "false")
    runner = CliRunner()
    selected = runner.invoke(
        app,
        [
            "research",
            "select",
            "--commander",
            "WEBGPT_SOL_PRO",
            "--expected-version",
            "0",
        ],
    )
    assert selected.exit_code == 0, selected.output
    selection = CommanderSelectionV1.model_validate(
        json.loads(selected.output)["selection"]
    )
    as_of = selection.created_at + timedelta(minutes=1)
    catalog = _catalog(as_of)
    request = build_research_request(
        request_id="request-commander-cli",
        research_cycle_id="cycle-commander-cli",
        commander_selection=selection,
        created_at=as_of,
        as_of=as_of,
        data_available_cutoff=as_of,
        expires_at=as_of + timedelta(hours=2),
        source_snapshot_commit="d" * 40,
        champion_version="1.0.0",
        experiment_family="adaptive-alpha-cli",
        champion_manifest={"strategy_id": "T1"},
        active_challenger_manifests=[],
        strategy_performance_summary={},
        failure_case_clusters=[],
        regime_summary={},
        execution_cost_summary={},
        capacity_summary={},
        recent_market_evidence=[],
        recent_web_research=[],
        available_data_catalog=catalog,
        allowed_change_scope=["src/trading/strategies/challengers/"],
        forbidden_change_scope=["src/trading/risk/"],
        experiment_budget={"submissions_remaining": 2},
    )
    decision = _decision(
        request,
        _proposal(),
        as_of + timedelta(minutes=1),
    )
    request_file = tmp_path / "commander-request.json"
    assert atomic_write_json(request_file, request)
    runs = tmp_path / "commander-runs"
    prepared = runner.invoke(
        app,
        [
            "research",
            "cycle-prepare",
            "--request",
            str(request_file),
            "--bundle-root",
            str(runs),
        ],
    )
    assert prepared.exit_code == 0, prepared.output

    config = WebGptScoutConfig(
        node_executable="node",
        agbrowse_entry=tmp_path / "agbrowse.mjs",
        agbrowse_root=tmp_path,
        bridge_script=tmp_path / "bridge.mjs",
        cdp_endpoint="http://127.0.0.1:9222",
        artifact_root=tmp_path / "commander-artifacts",
        raw_object_root=tmp_path / "commander-raw",
    )

    class _FakeCommander:
        def __init__(self, *, config, selection_provider) -> None:
            assert config.artifact_root == tmp_path / "commander-artifacts"
            assert selection_provider() == selection

        def command(
            self,
            supplied,
            *,
            prior_conversation_ids=(),
        ) -> ResearchDecisionV1:
            assert supplied == request
            assert tuple(prior_conversation_ids) == ("conversation-scout-cli",)
            return decision

    monkeypatch.setattr(
        cli_module.WebGptScoutConfig,
        "from_env",
        classmethod(lambda cls: config),
    )
    monkeypatch.setattr(
        cli_module,
        "WebGptActiveResearchCommander",
        _FakeCommander,
    )
    completed = runner.invoke(
        app,
        [
            "research",
            "commander-run",
            "--request",
            str(request_file),
            "--bundle-root",
            str(runs),
            "--prior-conversation-id",
            "conversation-scout-cli",
        ],
    )

    assert completed.exit_code == 0, completed.output
    response = json.loads(completed.output)
    assert response["decision_hash"] == decision.output_hash
    assert response["api_fallback_used"] is False
    assert response["real_order_routing"] is False
    output = (
        runs
        / request.research_cycle_id
        / "output"
        / "research_decision.json"
    )
    stored = ResearchDecisionV1.model_validate(load_json_object(output))
    assert stored == decision


def test_research_cli_exposes_only_trusted_promotion_commands(
    sqlite_database,
    repository_root,
    tmp_path,
    monkeypatch,
) -> None:
    database_url, _, _ = sqlite_database
    _configure_cli(
        monkeypatch,
        database_url=database_url,
        repository_root=repository_root,
        tmp_path=tmp_path,
    )
    runner = CliRunner()

    schema_result = runner.invoke(app, ["research", "schema"])
    assert schema_result.exit_code == 0, schema_result.output
    schema = json.loads(schema_result.output)
    assert "trusted_shadow_summary" in schema
    assert "promotion_evidence" in schema
    assert "trusted_promotion_evaluation" in schema
    assert "champion_designation" in schema
    assert "algorithm_proposal_v2" in schema
    assert "research_experiment_action_v1" in schema
    assert "experiment_outcome_maturation_input_v1" in schema
    assert "experiment_outcome_event_v1" in schema
    assert "research_memory_snapshot_v1" in schema
    assert "research_request_v2" in schema
    assert "research_decision_v2" in schema
    assert "research_action_plan_v1" in schema
    assert "research_work_execution_request_v1" in schema
    assert "research_work_execution_result_v1" in schema
    assert "candidate_evaluation_dataset_v2" in schema
    assert "candidate_evaluation_source_manifest_v2" in schema
    assert "prospective_evaluation_config_v1" in schema
    assert "oos_v2_shadow_plan_v1" in schema
    assert "shadow_activation_plan_v1" in schema
    assert "matched_shadow_cycle_commit_v1" in schema
    assert "prospective_shadow_cycle_source_v1" in schema
    assert schema["automatic_promotion_enabled"] is False
    assert schema["real_order_routing"] is False

    help_result = runner.invoke(app, ["research", "--help"])
    assert help_result.exit_code == 0, help_result.output
    for command in (
        "shadow-summary-record",
        "promotion-evaluate",
        "promotion-approve",
        "champion-designate",
        "oos-v2",
        "shadow-runtime",
    ):
        assert command in help_result.output
    shadow_help = runner.invoke(
        app,
        ["research", "shadow-runtime", "--help"],
    )
    assert shadow_help.exit_code == 0, shadow_help.output
    assert "prospective-cycle" in shadow_help.output

    rejected = runner.invoke(
        app,
        [
            "research",
            "promotion-evaluate",
            "--challenger-id",
            "unknown-challenger",
            "--evaluated-at",
            "2026-07-27T20:00:00Z",
        ],
    )
    assert rejected.exit_code != 0
    assert "unknown Challenger" in rejected.output


def test_prospective_evaluation_cli_waits_fail_closed(
    sqlite_database,
    repository_root,
    tmp_path,
    monkeypatch,
) -> None:
    database_url, _, _ = sqlite_database
    _configure_cli(
        monkeypatch,
        database_url=database_url,
        repository_root=repository_root,
        tmp_path=tmp_path,
    )
    commander_root = tmp_path / "commander"
    commander_run = tmp_path / "commander-run"
    commander_root.mkdir()
    commander_run.mkdir()
    calls: list[dict[str, object]] = []

    def waiting_result(
        **kwargs: object,
    ) -> ProspectiveEvaluationRunResult:
        calls.append(kwargs)
        return ProspectiveEvaluationRunResult(
            status="WAITING_FOR_FORWARD_OUTCOMES",
            challenger_status=ChallengerStatus.PROPOSED,
            successful_forward_sessions=12,
            required_forward_sessions=126,
            terminal_failure_count=1,
        )

    monkeypatch.setattr(
        cli_module,
        "_run_prospective_evaluation",
        waiting_result,
    )
    result = CliRunner().invoke(
        app,
        [
            "research",
            "prospective-evaluation-run",
            "--challenger-id",
            "challenger-cli-waiting",
            "--commander-root",
            str(commander_root),
            "--commander-run",
            str(commander_run),
        ],
    )

    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "WAITING_FOR_FORWARD_OUTCOMES"
    assert payload["successful_forward_sessions"] == 12
    assert payload["required_forward_sessions"] == 126
    assert payload["terminal_failure_count"] == 1
    assert payload["dataset"] is None
    assert payload["trace"] is None
    assert payload["replay"] is None
    assert payload["falsification"] is None
    assert payload["oos_started"] is False
    assert payload["shadow_started"] is False
    assert payload["automatic_promotion_enabled"] is False
    assert payload["broker_access_permitted"] is False
    assert payload["real_order_routing"] is False
    assert len(calls) == 1


def test_atomic_utf8_io_and_repository_local_output_guard(tmp_path) -> None:
    output = tmp_path / "결과" / "evidence.json"
    assert atomic_write_json(output, {"설명": "알파 연구"})
    assert not atomic_write_json(output, {"설명": "알파 연구"})
    assert load_json_object(output) == {"설명": "알파 연구"}

    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    assert resolve_local_output(
        Path(".local/research"),
        repository_root=repository_root,
    ) == repository_root / ".local" / "research"
    assert resolve_local_output(
        Path("data/raw/research"),
        repository_root=repository_root,
    ) == repository_root / "data" / "raw" / "research"
    try:
        resolve_local_output(
            Path("docs/generated"),
            repository_root=repository_root,
        )
    except ResearchFileRuntimeError as exc:
        assert "must be under" in str(exc)
    else:
        raise AssertionError("public-tree output was not rejected")
