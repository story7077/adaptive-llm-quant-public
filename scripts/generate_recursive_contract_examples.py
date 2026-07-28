from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from trading.domain.hashing import canonical_hash
from trading.research.contracts import (
    AvailableDataCatalogV1,
    AvailableInstrumentV1,
    ResearchCommanderKind,
    ResearchDecisionKind,
)
from trading.research.experiment_outcomes import (
    AlgorithmProposalV2,
    ResearchActionKind,
    build_research_memory_snapshot_from_verified_events,
)
from trading.research.meta_controller import (
    MetaControllerParametersV1,
    build_meta_controller_training_view,
    build_research_action_plan,
    build_research_context,
)
from trading.research.v2_contracts import (
    ResearchDecisionV2,
    ResearchRequestV2,
)

NOW = datetime(2026, 7, 28, 15, 0, tzinfo=UTC)


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def generate(output_dir: Path) -> None:
    destination = output_dir.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = build_research_memory_snapshot_from_verified_events(
        events=(),
        as_of=NOW,
        data_available_cutoff=NOW,
        created_at=NOW,
    )
    training_view = build_meta_controller_training_view(
        snapshot=snapshot,
        verified_events=(),
        contexts_by_research_cycle={},
    )
    context = build_research_context(
        regime_cluster_id="regime-neutral",
        failure_cluster_id="failure-none",
        portfolio_exposure_cluster_id="exposure-balanced",
    )
    plan = build_research_action_plan(
        research_cycle_id="cycle-v2-synthetic-001",
        snapshot=snapshot,
        training_view=training_view,
        context=context,
        parameters=MetaControllerParametersV1(
            policy_version="hierarchical-contextual-ucb-v1",
            maximum_actions_per_cycle=2,
            prior_strength=4.0,
            exploration_coefficient=0.25,
            exploration_floor=0.01,
            technical_failure_weight=0.25,
            reward_clip=1.0,
            turnover_penalty_weight=0.05,
            turnover_scale=1.0,
            drawdown_penalty_weight=0.10,
            drawdown_scale=0.05,
            cost_penalty_weight=0.05,
            cost_scale_bps=10.0,
            complexity_penalty_weight=0.05,
            complexity_scale=1.0,
        ),
        config_hash="a" * 64,
        available_action_kinds=(
            ResearchActionKind.ADD_FEATURE,
            ResearchActionKind.CHANGE_EXIT_RULE,
        ),
        maximum_total_submissions=2,
        idempotency_key="meta-plan-v2-synthetic-001",
        generated_at=NOW,
    )
    instrument = AvailableInstrumentV1(
        symbol="QQQ",
        asset_class="US_ETF",
        first_available_at=NOW - timedelta(days=1000),
        point_in_time_membership_available=True,
        daily_history_sessions=800,
        intraday_history_sessions=200,
        execution_supported=True,
    )
    catalog_payload: dict[str, object] = {
        "schema_version": "available_data_catalog_v1",
        "catalog_id": "catalog-v2-synthetic",
        "as_of": NOW,
        "data_available_cutoff": NOW,
        "instruments": [instrument],
        "dataset_versions": {"daily": "synthetic-pit-v1"},
    }
    catalog = AvailableDataCatalogV1(
        schema_version="available_data_catalog_v1",
        catalog_id="catalog-v2-synthetic",
        as_of=NOW,
        data_available_cutoff=NOW,
        instruments=[instrument],
        dataset_versions={"daily": "synthetic-pit-v1"},
        catalog_hash=canonical_hash(catalog_payload),
    )
    request_payload = {
        "schema_version": "research_request_v2",
        "request_id": "request-v2-synthetic-001",
        "research_cycle_id": plan.research_cycle_id,
        "selected_commander": ResearchCommanderKind.CODEX_SOL_MAX,
        "commander_selection_id": "selection-v2-synthetic-001",
        "commander_selection_version": 1,
        "created_at": NOW,
        "as_of": NOW,
        "data_available_cutoff": NOW,
        "expires_at": NOW + timedelta(hours=2),
        "source_snapshot_commit": "b" * 40,
        "champion_version": "1.0.0",
        "experiment_family": "synthetic-recursive-family",
        "champion_manifest": {"strategy_id": "CHAMPION"},
        "active_challenger_manifests": [],
        "strategy_performance_summary": {
            "source": "research_memory_snapshot_v1",
            "snapshot_hash": snapshot.snapshot_hash,
        },
        "failure_case_clusters": [],
        "regime_summary": {
            "source": "research_action_plan_v1",
            "context": context.model_dump(mode="json"),
        },
        "execution_cost_summary": {"model": "synthetic-cost-v1"},
        "capacity_summary": {"status": "SYNTHETIC_ONLY"},
        "recent_market_evidence": [
            {"source_id": "source-synthetic-1"}
        ],
        "recent_web_research": [],
        "available_data_catalog": catalog.model_dump(mode="json"),
        "allowed_change_scope": [
            "src/trading/strategies/challengers/**",
            "tests/candidates/**",
        ],
        "forbidden_change_scope": [
            "src/trading/risk/**",
            "src/trading/execution/**",
        ],
        "experiment_budget": {
            "family_submission_limit": 5,
            "family_submissions_used": 0,
            "oos_budget_limit": 2,
            "oos_budget_used": 0,
        },
        "research_memory_snapshot": snapshot,
        "research_action_plan": plan,
    }
    request = ResearchRequestV2.model_validate(
        {
            **request_payload,
            "context_manifest_hash": canonical_hash(request_payload),
        }
    )
    proposal_payload = {
        "schema_version": "algorithm_proposal_v2",
        "proposal_id": "proposal-v2-synthetic-001",
        "hypothesis_id": "hypothesis-v2-synthetic-001",
        "hypothesis": "A bounded feature may diversify portfolio errors.",
        "economic_mechanism": "Independent information may reduce correlated errors.",
        "why_current_model_failed": "The parent omitted the declared feature.",
        "parent_strategy_id": "CHAMPION",
        "parent_strategy_version": "1.0.0",
        "proposed_strategy_id": "CHALLENGER_SYNTHETIC",
        "proposed_strategy_version": "1.1.0",
        "target_horizon": "DAILY",
        "target_universe": ["QQQ"],
        "required_data": ["adjusted_daily_bars"],
        "feature_changes": ["add one bounded PIT feature"],
        "signal_formula_changes": [],
        "entry_rule_changes": [],
        "exit_rule_changes": [],
        "position_sizing_changes": [],
        "regime_activation_changes": [],
        "calibration_changes": [],
        "expected_edge_source": "Diversifying point-in-time information.",
        "expected_failure_modes": ["NO_EDGE"],
        "invalidation_conditions": ["Forward lower bound is non-positive."],
        "placebo_tests": ["date_shift"],
        "stress_tests": ["cost_3x"],
        "minimum_economic_effect": {
            "metric": "portfolio_delta_sharpe_lcb",
            "threshold": 0.0,
            "comparison": "strictly_greater",
        },
        "estimated_capacity": {"usd": 100000},
        "estimated_turnover": {"one_way_daily": 0.05},
        "estimated_cost_sensitivity": {
            "cost_1x": 0.10,
            "cost_2x": 0.05,
            "cost_3x": 0.00,
        },
        "files_allowed_to_change": [
            "src/trading/strategies/challengers/synthetic/**",
            "tests/candidates/test_synthetic.py",
        ],
        "tests_required": ["tests/candidates/test_synthetic.py"],
        "evidence_source_ids": ["source-synthetic-1"],
        "raw_confidence": 0.5,
        "patch_policy_version": "candidate_patch_policy_v2",
        "primary_action_kind": ResearchActionKind.ADD_FEATURE,
        "secondary_action_kinds": (),
        "mechanism_tags": ("diversification",),
        "predicted_portfolio_delta_sharpe": {
            "lower": -0.1,
            "median": 0.1,
            "upper": 0.3,
        },
        "predicted_failure_codes": ("NO_EDGE",),
        "complexity_delta": 1.0,
    }
    proposal = AlgorithmProposalV2.model_validate(
        {
            **proposal_payload,
            "proposal_hash": canonical_hash(proposal_payload),
        }
    )
    decision_payload: dict[str, object] = {
        "schema_version": "research_decision_v2",
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
        "decision": ResearchDecisionKind.PROPOSE_FEATURE_REVISION,
        "rationale": "The bounded synthetic evidence supports falsification.",
        "proposal": proposal,
        "requested_evidence": [],
        "research_memory_snapshot_hash": snapshot.snapshot_hash,
        "research_action_plan_hash": plan.plan_hash,
        "created_at": NOW + timedelta(minutes=1),
    }
    decision = ResearchDecisionV2.model_validate(
        {
            **decision_payload,
            "output_hash": canonical_hash(decision_payload),
        }
    )
    _write(
        destination / "research-request-v2.example.json",
        request.model_dump(mode="json"),
    )
    _write(
        destination / "algorithm-proposal-v2.example.json",
        proposal.model_dump(mode="json"),
    )
    _write(
        destination / "research-decision-v2.example.json",
        decision.model_dump(mode="json"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    generate(arguments.output_dir)


if __name__ == "__main__":
    main()
