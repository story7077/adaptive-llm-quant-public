from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from trading.domain.hashing import canonical_hash
from trading.research.contracts import (
    AlgorithmProposalV1,
    AvailableDataCatalogV1,
    AvailableInstrumentV1,
    CommanderSelectionV1,
    ResearchCommanderKind,
    ResearchDecisionKind,
    ResearchDecisionV1,
)
from trading.research.host import ResearchHostError, build_research_request
from trading.research.proposal import (
    ProposalValidationError,
    validate_proposal_against_catalog,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def _catalog(*symbols: str) -> AvailableDataCatalogV1:
    payload = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-1",
        "as_of": NOW,
        "data_available_cutoff": NOW,
        "instruments": [
            AvailableInstrumentV1(
                symbol=symbol,
                asset_class="US_ETF" if symbol in {"SPY", "QQQ"} else "US_EQUITY",
                first_available_at=NOW - timedelta(days=1000),
                point_in_time_membership_available=True,
                daily_history_sessions=800,
                intraday_history_sessions=200,
                execution_supported=True,
            )
            for symbol in symbols
        ],
        "dataset_versions": {"daily": "daily-pit-v1"},
    }
    return AvailableDataCatalogV1(
        **payload,
        catalog_hash=canonical_hash(payload),
    )


def _request():
    catalog = _catalog("AAPL", "SPY")
    selection = CommanderSelectionV1(
        selection_id="selection-1",
        version=1,
        selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
        effective_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=1),
        config_hash="b" * 64,
    )
    request = build_research_request(
        request_id="request-1",
        research_cycle_id="cycle-1",
        commander_selection=selection,
        created_at=NOW,
        as_of=NOW + timedelta(minutes=1),
        data_available_cutoff=NOW,
        expires_at=NOW + timedelta(hours=2),
        source_snapshot_commit="a" * 40,
        champion_version="1.0.0",
        experiment_family="cross-sectional-alpha",
        champion_manifest={"strategy_id": "T1"},
        active_challenger_manifests=[],
        strategy_performance_summary={"sessions": 252},
        failure_case_clusters=[],
        regime_summary={"regime": "mixed"},
        execution_cost_summary={"cost_bps": 8.0},
        capacity_summary={"capacity_usd": 1000000},
        recent_market_evidence=[],
        recent_web_research=[],
        available_data_catalog=catalog,
        allowed_change_scope=["src/trading/strategies/"],
        forbidden_change_scope=["src/trading/risk/"],
        experiment_budget={"submissions_remaining": 3},
    )
    return request, catalog


def _proposal(*, universe: list[str]) -> AlgorithmProposalV1:
    payload = {
        "schema_version": "algorithm_proposal_v1",
        "proposal_id": "proposal-1",
        "hypothesis_id": "hypothesis-1",
        "hypothesis": "A cross-sectional quality trend signal persists after costs.",
        "economic_mechanism": "Slow-moving institutional demand and quality repricing.",
        "why_current_model_failed": "The current model has no cross-sectional quality input.",
        "parent_strategy_id": "T1",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "T1",
        "proposed_strategy_version": "2.0.0",
        "target_horizon": "H20D",
        "target_universe": universe,
        "required_data": ["PIT fundamentals", "adjusted daily bars"],
        "feature_changes": ["add PIT quality composite"],
        "signal_formula_changes": ["rank quality and medium-term trend"],
        "entry_rule_changes": [],
        "exit_rule_changes": ["monthly rebalance"],
        "position_sizing_changes": ["inverse volatility"],
        "regime_activation_changes": [],
        "calibration_changes": ["walk-forward calibration"],
        "expected_edge_source": "institutional repricing",
        "expected_failure_modes": ["quality crowding"],
        "invalidation_conditions": ["net matched return is non-positive"],
        "placebo_tests": ["symbol shuffle"],
        "stress_tests": ["3x cost"],
        "minimum_economic_effect": {"annualized_excess_return": 0.02},
        "estimated_capacity": {"usd": 1000000},
        "estimated_turnover": {"annual": 2.0},
        "estimated_cost_sensitivity": {"bps": 10},
        "files_allowed_to_change": ["src/trading/strategies/challengers/"],
        "tests_required": ["future_data_leakage"],
        "evidence_source_ids": ["source-1"],
        "raw_confidence": 0.4,
    }
    return AlgorithmProposalV1(
        **payload,
        proposal_hash=canonical_hash(payload),
    )


def _decision(request, proposal: AlgorithmProposalV1) -> ResearchDecisionV1:
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
        "decision": ResearchDecisionKind.PROPOSE_STRATEGY_REVISION,
        "rationale": "The hypothesis is economically explicit and falsifiable.",
        "proposal": proposal,
        "requested_evidence": [],
        "created_at": NOW + timedelta(minutes=3),
    }
    return ResearchDecisionV1(
        **payload,
        output_hash=canonical_hash(payload),
    )


def test_request_hash_changes_when_context_changes() -> None:
    request, _ = _request()
    payload = request.model_dump(mode="json")
    payload["strategy_performance_summary"]["sessions"] = 253
    with pytest.raises(ValueError, match="context_manifest_hash mismatch"):
        type(request).model_validate(payload)


@pytest.mark.parametrize(
    "field",
    ["commander_selection_id", "commander_selection_version"],
)
def test_request_requires_exact_commander_selection_binding(field: str) -> None:
    request, _ = _request()
    payload = request.model_dump(mode="json")
    del payload[field]
    with pytest.raises(ValueError, match=field):
        type(request).model_validate(payload)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("commander_selection_id", "selection-other"),
        ("commander_selection_version", 2),
    ],
)
def test_commander_selection_binding_is_part_of_request_hash(
    field: str,
    replacement: object,
) -> None:
    request, _ = _request()
    payload = request.model_dump(mode="json")
    payload[field] = replacement
    with pytest.raises(ValueError, match="context_manifest_hash mismatch"):
        type(request).model_validate(payload)


def test_sensitive_request_field_is_rejected() -> None:
    catalog = _catalog("SPY")
    with pytest.raises(ResearchHostError, match="sensitive field"):
        build_research_request(
            request_id="request-sensitive",
            research_cycle_id="cycle-sensitive",
            commander_selection=CommanderSelectionV1(
                selection_id="selection-sensitive",
                version=1,
                selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
                effective_at=NOW,
                created_at=NOW,
                config_hash="b" * 64,
            ),
            created_at=NOW,
            as_of=NOW,
            data_available_cutoff=NOW,
            expires_at=NOW + timedelta(hours=1),
            source_snapshot_commit="a" * 40,
            champion_version="1.0.0",
            experiment_family="family",
            champion_manifest={"account_id": "forbidden"},
            active_challenger_manifests=[],
            strategy_performance_summary={},
            failure_case_clusters=[],
            regime_summary={},
            execution_cost_summary={},
            capacity_summary={},
            recent_market_evidence=[],
            recent_web_research=[],
            available_data_catalog=catalog,
            allowed_change_scope=["src/trading/strategies/"],
            forbidden_change_scope=["src/trading/risk/"],
            experiment_budget={},
        )


def test_request_rejects_selection_not_yet_effective() -> None:
    catalog = _catalog("SPY")
    future_selection = CommanderSelectionV1(
        selection_id="selection-future",
        version=2,
        selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
        effective_at=NOW + timedelta(seconds=1),
        created_at=NOW,
        config_hash="b" * 64,
    )
    with pytest.raises(ResearchHostError, match="not available"):
        build_research_request(
            request_id="request-future",
            research_cycle_id="cycle-future",
            commander_selection=future_selection,
            created_at=NOW,
            as_of=NOW,
            data_available_cutoff=NOW,
            expires_at=NOW + timedelta(hours=1),
            source_snapshot_commit="a" * 40,
            champion_version="1.0.0",
            experiment_family="family",
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
            allowed_change_scope=["src/trading/strategies/"],
            forbidden_change_scope=["src/trading/risk/"],
            experiment_budget={},
        )


def test_decision_binding_rejects_stale_selection_and_expiry() -> None:
    request, _ = _request()
    decision = _decision(request, _proposal(universe=["AAPL", "SPY"]))
    selection = CommanderSelectionV1(
        selection_id="selection-1",
        version=1,
        selected_commander=ResearchCommanderKind.CODEX_SOL_MAX,
        effective_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=1),
        config_hash="b" * 64,
    )
    decision.assert_bound_to(
        request,
        received_at=NOW + timedelta(minutes=4),
        current_selection=selection,
    )
    stale = selection.model_copy(
        update={
            "selection_id": "selection-2",
            "version": 2,
            "selected_commander": ResearchCommanderKind.WEBGPT_SOL_PRO,
            "effective_at": NOW + timedelta(minutes=1),
        }
    )
    with pytest.raises(ValueError, match="STALE_SELECTION"):
        decision.assert_bound_to(
            request,
            received_at=NOW + timedelta(minutes=4),
            current_selection=stale,
        )
    switched_back = selection.model_copy(
        update={
            "selection_id": "selection-3",
            "version": 3,
            "effective_at": NOW + timedelta(minutes=2),
        }
    )
    with pytest.raises(ValueError, match="STALE_SELECTION"):
        decision.assert_bound_to(
            request,
            received_at=NOW + timedelta(minutes=4),
            current_selection=switched_back,
        )
    with pytest.raises(ValueError, match="expired"):
        decision.assert_bound_to(
            request,
            received_at=request.expires_at,
            current_selection=selection,
        )


@pytest.mark.parametrize(
    "field",
    ["commander_selection_id", "commander_selection_version"],
)
def test_decision_requires_exact_commander_selection_binding(field: str) -> None:
    request, _ = _request()
    decision = _decision(request, _proposal(universe=["AAPL", "SPY"]))
    payload = decision.model_dump(mode="json")
    del payload[field]
    with pytest.raises(ValueError, match=field):
        ResearchDecisionV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("commander_selection_id", "selection-other"),
        ("commander_selection_version", 2),
    ],
)
def test_decision_rejects_mismatched_selection_record(
    field: str,
    replacement: object,
) -> None:
    request, _ = _request()
    decision = _decision(request, _proposal(universe=["AAPL", "SPY"]))
    payload = decision.model_dump(mode="python", exclude={"output_hash"})
    payload[field] = replacement
    mismatched = ResearchDecisionV1(
        **payload,
        output_hash=canonical_hash(payload),
    )
    selection = CommanderSelectionV1(
        selection_id=request.commander_selection_id,
        version=request.commander_selection_version,
        selected_commander=request.selected_commander,
        effective_at=NOW - timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=1),
        config_hash="b" * 64,
    )
    with pytest.raises(ValueError, match="binding mismatch"):
        mismatched.assert_bound_to(
            request,
            received_at=NOW + timedelta(minutes=4),
            current_selection=selection,
        )


def test_proposal_uses_catalog_not_hardcoded_semiconductor_universe() -> None:
    request, catalog = _request()
    proposal = _proposal(universe=["AAPL", "SPY"])
    validate_proposal_against_catalog(
        proposal,
        catalog=catalog,
        evidence_source_ids={"source-1"},
        request=request,
    )
    outside = _proposal(universe=["MSFT"])
    with pytest.raises(ProposalValidationError, match="outside"):
        validate_proposal_against_catalog(
            outside,
            catalog=catalog,
            evidence_source_ids={"source-1"},
            request=request,
        )


def test_new_strategy_id_still_requires_a_new_version() -> None:
    payload = _proposal(universe=["AAPL", "SPY"]).model_dump(
        mode="python",
        exclude={"proposal_hash"},
    )
    payload["proposed_strategy_id"] = "NEW-T1"
    payload["proposed_strategy_version"] = payload["parent_strategy_version"]

    with pytest.raises(
        ValueError,
        match="proposed strategy version must differ from parent strategy version",
    ):
        AlgorithmProposalV1.model_validate(
            {**payload, "proposal_hash": canonical_hash(payload)}
        )


def test_proposal_cannot_escape_request_file_scope() -> None:
    request, catalog = _request()
    payload = _proposal(universe=["AAPL", "SPY"]).model_dump(mode="python")
    payload["files_allowed_to_change"] = ["src/trading/risk/"]
    payload_without_hash = {
        key: value for key, value in payload.items() if key != "proposal_hash"
    }
    payload["proposal_hash"] = canonical_hash(payload_without_hash)
    proposal = AlgorithmProposalV1.model_validate(payload)

    with pytest.raises(ProposalValidationError, match="file scope"):
        validate_proposal_against_catalog(
            proposal,
            catalog=catalog,
            evidence_source_ids={"source-1"},
            request=request,
        )


def test_proposal_accepts_path_under_recursive_request_scope() -> None:
    request, catalog = _request()
    request = request.model_copy(
        update={
            "allowed_change_scope": [
                "src/trading/strategies/challengers/**",
                "config/strategies/challengers/**",
            ]
        }
    )
    payload = _proposal(universe=["AAPL", "SPY"]).model_dump(mode="python")
    payload["files_allowed_to_change"] = [
        "src/trading/strategies/challengers/alpha_v2/**",
        "config/strategies/challengers/alpha-v2.yaml",
    ]
    payload_without_hash = {
        key: value for key, value in payload.items() if key != "proposal_hash"
    }
    payload["proposal_hash"] = canonical_hash(payload_without_hash)
    proposal = AlgorithmProposalV1.model_validate(payload)

    validate_proposal_against_catalog(
        proposal,
        catalog=catalog,
        evidence_source_ids={"source-1"},
        request=request,
    )
