from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading.domain.hashing import canonical_hash
from trading.persistence.research import (
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.research.contracts import (
    AvailableDataCatalogV1,
    AvailableInstrumentV1,
    CommanderSelectionV1,
    ResearchCommanderKind,
    ResearchDecisionKind,
    ResearchDecisionV1,
    ResearchRequestV1,
)
from trading.research.host import build_research_request

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


def _catalog() -> AvailableDataCatalogV1:
    payload = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-integration",
        "as_of": NOW,
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
        "dataset_versions": {"daily": "pit-v1"},
    }
    return AvailableDataCatalogV1(
        **payload,
        catalog_hash=canonical_hash(payload),
    )


def _request(
    selection: CommanderSelectionV1,
    *,
    identity: str | None = None,
):
    suffix = identity or selection.selected_commander.value.lower()
    created_at = selection.effective_at
    return build_research_request(
        request_id=f"request-{suffix}",
        research_cycle_id=f"cycle-{suffix}",
        commander_selection=selection,
        created_at=created_at,
        as_of=created_at,
        data_available_cutoff=created_at,
        expires_at=created_at + timedelta(hours=2),
        source_snapshot_commit="a" * 40,
        champion_version="1.0.0",
        experiment_family="integration-family",
        champion_manifest={"strategy_id": "CHAMPION"},
        active_challenger_manifests=[],
        strategy_performance_summary={},
        failure_case_clusters=[],
        regime_summary={},
        execution_cost_summary={},
        capacity_summary={},
        recent_market_evidence=[],
        recent_web_research=[],
        available_data_catalog=_catalog(),
        allowed_change_scope=["src/trading/strategies/"],
        forbidden_change_scope=["src/trading/risk/"],
        experiment_budget={"submissions_remaining": 1},
    )


def _decision(request: ResearchRequestV1) -> ResearchDecisionV1:
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
        "decision": ResearchDecisionKind.NO_RESEARCH_CHANGE,
        "rationale": "No bounded revision is justified.",
        "proposal": None,
        "requested_evidence": [],
        "created_at": request.created_at + timedelta(minutes=1),
    }
    return ResearchDecisionV1(
        **payload,
        output_hash=canonical_hash(payload),
    )


def test_selection_and_cycle_are_append_only_and_stale_selection_fails(
    sqlite_database,
) -> None:
    _, engine, factory = sqlite_database
    repository = ResearchRepository(factory)
    first = repository.select_commander(
        ResearchCommanderKind.CODEX_SOL_MAX,
        config_hash="b" * 64,
        effective_at=NOW,
        created_at=NOW,
        expected_version=0,
    )
    assert first.version == 1
    first_request = _request(first)
    assert repository.create_cycle(first_request)
    assert not repository.create_cycle(first_request)

    second = repository.select_commander(
        ResearchCommanderKind.WEBGPT_SOL_PRO,
        config_hash="b" * 64,
        effective_at=NOW + timedelta(minutes=1),
        created_at=NOW + timedelta(minutes=1),
        expected_version=1,
    )
    assert second.version == 2
    with pytest.raises(ResearchPersistenceError, match="STALE_SELECTION"):
        repository.create_cycle(_request(first, identity="stale-after-switch"))

    third = repository.select_commander(
        ResearchCommanderKind.CODEX_SOL_MAX,
        config_hash="b" * 64,
        effective_at=NOW + timedelta(minutes=2),
        created_at=NOW + timedelta(minutes=2),
        expected_version=2,
    )
    assert third.version == 3
    with pytest.raises(ResearchPersistenceError, match="STALE_SELECTION"):
        repository.create_cycle(_request(first, identity="stale-after-switch-back"))
    with pytest.raises(ResearchPersistenceError, match="STALE_SELECTION"):
        repository.accept_decision(
            _decision(first_request),
            received_at=NOW + timedelta(minutes=3),
        )
    assert repository.create_cycle(_request(third, identity="fresh-after-switch-back"))

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE research_commander_selections "
                "SET selected_commander='CODEX_SOL_MAX' WHERE version=2"
            )
        )


def test_experiment_budget_is_idempotent(sqlite_database) -> None:
    _, _, factory = sqlite_database
    repository = ResearchRepository(factory)
    arguments = {
        "experiment_family": "family",
        "event_type": "CANDIDATE_SUBMITTED",
        "submission_delta": 1,
        "oos_budget_delta": 0,
        "hypothesis_delta": 0,
        "failure_delta": 0,
        "idempotency_key": "candidate-1",
        "created_at": NOW,
    }
    assert repository.append_budget_event(**arguments)
    assert not repository.append_budget_event(**arguments)
    assert repository.budget_totals("family") == {
        "submissions": 1,
        "oos_budget_used": 0,
        "hypotheses": 0,
        "failures": 0,
    }
