from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.experiments.ai_guard_factorial import FACTORIAL_ARM_IDS
from trading.experiments.ai_guard_factorial_runtime import (
    initialize_factorial_paper_arms,
)
from trading.persistence.factorial import FactorialPaperExperimentRepository
from trading.research.config import load_research_config
from trading.settings import Settings
from trading.ui.app import create_app


def test_operator_ui_selects_provider_and_processes_json(
    sqlite_database,
    repository_root,
    tmp_path,
) -> None:
    database_url, _, factory = sqlite_database
    settings = Settings(
        database_url=database_url,
        config_dir=repository_root / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
        commander_dir=tmp_path / "stock-commander",
    )
    with TestClient(create_app(settings=settings, session_factory=factory)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Quant Commander" in page.text
        assert "모의투자 현황" in page.text
        assert "현재 보유 종목" in page.text
        assert "제어 모델 선택" in page.text
        assert "Adaptive alpha research plane" in page.text
        assert "Champion 직접 수정 금지" in page.text
        assert "Recursive Improvement" in page.text
        assert "DISABLED · AUDIT ONLY" in page.text
        assert "EFFECTIVE EVENTS" in page.text
        assert "LEARNING ELIGIBLE" in page.text
        assert "Prospective Candidate Evidence" in page.text
        assert 'id="researchProspectiveEvaluation"' in page.text
        assert 'id="researchProspectiveReplay"' in page.text

        selected = client.post(
            "/api/control/provider",
            json={"provider": "CODEX_SOL_MAX", "expected_version": 0},
        )
        assert selected.status_code == 200
        assert selected.json()["selection"]["version"] == 1

        prepared = client.post(
            "/api/control/requests",
            json={"arm_scope": "B3-RISK", "context": {"news_analyses": []}},
        )
        assert prepared.status_code == 200
        request = prepared.json()["request"]
        assert (tmp_path / "stock-commander" / "inbox" / request["request_id"]).is_dir()

        output = {
            "schema_version": "adaptive_policy_decision_v1",
            "request_id": request["request_id"],
            "context_manifest_hash": request["context_manifest_hash"],
            "decision": "NO_CHANGE",
            "arm_scope": "B3-RISK",
            "base_policy_version": request["base_policy_version"],
            "effective_from": None,
            "expires_at": None,
            "operations": [],
            "evidence_news_event_ids": [],
            "raw_confidence": 0,
            "rollback_conditions": [],
            "rationale_summary": "No change.",
        }
        submitted = client.post(
            f"/api/control/requests/{request['request_id']}/decision",
            json={"provider": "CODEX_SOL_MAX", "output": output},
        )
        assert submitted.status_code == 200
        assert submitted.json()["receipt"]["status"] == "NO_CHANGE"

        research_status = client.get("/api/research/status")
        assert research_status.status_code == 200
        assert research_status.json()["real_order_routing"] is False
        assert research_status.json()["available_data_catalog"][
            "hardcoded_symbol_allowlist"
        ] is False
        assert research_status.json()["current_champion"] is None
        assert research_status.json()["operational_algorithm"][
            "mutation_policy"
        ] == "VERSIONED_CHALLENGER_ONLY"
        recursive = research_status.json()["recursive_improvement"]
        assert recursive["status"] == "DISABLED_RESEARCH_ONLY_PR4"
        assert recursive["enabled"] is False
        assert recursive["audit_only"] is True
        assert recursive["candidate_patch_policy"] == {
            "version": "candidate_patch_policy_v2",
            "contract_hash": (
                "73af5956c12a042eb99c0c15929b7f4db2b3b45110373204d39c8163fedc716c"
            ),
        }
        assert recursive["experiment_outcome_ledger"][
            "effective_unsuperseded_event_count"
        ] == 0
        assert recursive["experiment_outcome_ledger"][
            "effective_eligible_learning_forward_event_count"
        ] == 0
        assert recursive["automatic_outcome_maintenance_enabled"] is False
        assert recursive["portfolio_delta_sharpe"]["status"] == (
            "IMPLEMENTED_DISABLED"
        )
        assert recursive["portfolio_delta_sharpe"]["ledger"][
            "comparison_contract_count"
        ] == 0
        assert recursive["chronological_meta_oos"]["status"] == (
            "IMPLEMENTED_DISABLED"
        )
        assert recursive["chronological_meta_oos"]["ledger"][
            "plan_count"
        ] == 0
        assert recursive["automatic_promotion_enabled"] is False
        assert recursive["real_order_routing"] is False
        assert research_status.json()["promotion_gate"] == {
            "eligible_challenger_ids": [],
            "manually_approved_challenger_ids": [],
            "explicit_human_designation_available": False,
            "automatic_promotion_enabled": False,
            "champion_mutation_available": False,
            "real_order_routing": False,
        }
        factorial = research_status.json()["factorial_experiment"]
        assert factorial["status"] == "NOT_INITIALIZED"
        assert factorial["required_arms"] == list(FACTORIAL_ARM_IDS)
        assert factorial["real_order_routing"] is False
        prospective = research_status.json()["prospective_candidate"]
        assert prospective["status"] == "WAITING_FOR_PARENT_DECISION"
        assert prospective["strategy_id"] == "Q1-DET"
        assert prospective["strategy_version"] == "2.0.0"
        assert prospective["reference_universe"] == [
            "GLD",
            "QQQ",
            "SGOV",
            "SOXX",
            "TLT",
        ]
        assert prospective["request_count"] == 0
        assert prospective["shadow_started"] is False
        assert prospective["automatic_promotion_enabled"] is False
        assert prospective["real_order_routing"] is False
        outcomes = research_status.json()["prospective_outcomes"]
        assert outcomes["status"] == "WAITING_FOR_PROSPECTIVE_TARGET"
        assert outcomes["outcome_count"] == 0
        assert outcomes["terminal_failure_count"] == 0
        assert outcomes["falsification_input_ready"] is False
        assert outcomes["challenger_status_advanced"] is False
        assert outcomes["shadow_started"] is False
        assert outcomes["automatic_promotion_enabled"] is False
        assert outcomes["real_order_routing"] is False
        prospective_evaluation = research_status.json()[
            "prospective_evaluation"
        ]
        assert (
            prospective_evaluation["status"]
            == "WAITING_FOR_CHALLENGER"
        )
        assert prospective_evaluation["dataset"] is None
        assert prospective_evaluation["trace"] is None
        assert prospective_evaluation["falsification"] is None
        assert (
            prospective_evaluation["required_successful_sessions"]
            == 126
        )
        assert prospective_evaluation["falsification_started"] is False
        assert prospective_evaluation["oos_started"] is False
        assert prospective_evaluation["shadow_started"] is False
        assert (
            prospective_evaluation["automatic_promotion_enabled"]
            is False
        )
        assert prospective_evaluation["broker_access_permitted"] is False
        assert prospective_evaluation["real_order_routing"] is False

        research_selection = client.post(
            "/api/research/commander",
            json={"commander": "WEBGPT_SOL_PRO", "expected_version": 0},
        )
        assert research_selection.status_code == 200
        assert research_selection.json()["selection"]["selected_commander"] == (
            "WEBGPT_SOL_PRO"
        )
        stale = client.post(
            "/api/research/commander",
            json={"commander": "CODEX_SOL_MAX", "expected_version": 0},
        )
        assert stale.status_code == 409

        unknown_evaluation = client.post(
            "/api/research/promotion/evaluate",
            json={"challenger_id": "unknown-challenger"},
        )
        assert unknown_evaluation.status_code == 409
        assert unknown_evaluation.json()["detail"] == "unknown Challenger"
        unknown_approval = client.post(
            "/api/research/promotion/approve",
            json={
                "challenger_id": "unknown-challenger",
                "approved_by": "human-reviewer",
            },
        )
        assert unknown_approval.status_code == 409
        unknown_designation = client.post(
            "/api/research/champion/designate",
            json={
                "challenger_id": "unknown-challenger",
                "expected_current_version": "1.0.0",
                "designated_by": "human-reviewer",
                "idempotency_key": "ui-designation-unknown",
            },
        )
        assert unknown_designation.status_code == 409


def test_research_ui_reports_durable_factorial_paper_state(
    sqlite_database,
    repository_root,
    tmp_path,
) -> None:
    database_url, _, factory = sqlite_database
    settings = Settings(
        database_url=database_url,
        config_dir=repository_root / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
    )
    config = load_research_config(repository_root / "config")
    matched = config.factorial.matched_conditions
    effective_at = datetime(2026, 7, 24, 22, 0, tzinfo=UTC)
    arms = initialize_factorial_paper_arms(
        starting_capital_usd=Decimal(
            config.config.shadow.starting_capital_usd
        ),
        effective_at=effective_at,
        common_market_manifest_hash="a" * 64,
        forecast_hash="b" * 64,
        policy_version="factorial-policy-v1",
        decision_schedule_version=matched.decision_schedule_version,
        execution_scenario_version=matched.execution_scenario_version,
        cost_model_version=matched.cost_model_version,
        config_manifest_hash=config.manifest_hash,
    )
    FactorialPaperExperimentRepository(factory).initialize(
        run_id="factorial-ui-run",
        arms=arms,
        config_manifest_hash=config.manifest_hash,
        code_commit="public-test",
        effective_at=effective_at,
    )

    with TestClient(create_app(settings=settings, session_factory=factory)) as client:
        response = client.get(
            "/api/research/factorial/status",
            params={"run_id": "factorial-ui-run"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "SHADOW_RUNNING"
        assert payload["required_arms"] == list(FACTORIAL_ARM_IDS)
        assert set(payload["arms"]) == set(FACTORIAL_ARM_IDS)
        assert payload["schedule"] == {
            "timezone": "America/New_York",
            "daily_aggregation_time": "18:00",
            "decision_schedule_version": "research-daily-v1",
            "authority": "RESEARCH_PLANE_CONFIG",
        }
        assert payload["matched_conditions_ready"] is True
        assert payload["effects"]["ready"] is False
        assert payload["effects"]["common_sessions"] == 0
        assert payload["real_order_routing"] is False

        combined = client.get("/api/research/status")
        assert combined.status_code == 200
        assert combined.json()["factorial_experiment"]["run_id"] == (
            "factorial-ui-run"
        )


def test_q1_operator_ui_reports_versioned_paper_safety_status(
    sqlite_database,
    repository_root,
    tmp_path,
) -> None:
    database_url, _, factory = sqlite_database
    settings = Settings(
        database_url=database_url,
        config_dir=repository_root / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
        paper_algorithm_version=Q1_ALGORITHM_VERSION,
    )

    with TestClient(create_app(settings=settings, session_factory=factory)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "q1_math_core_v1" in page.text
        assert "ORDER-EVENT PENDING" in page.text

        response = client.get("/api/paper/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["state"] == "NOT_INITIALIZED"
        assert payload["algorithm_version"] == Q1_ALGORITHM_VERSION
        assert payload["real_order_routing"] is False
        assert payload["process"]["task_running"] is False
        assert payload["process"]["worker_state"] == (
            "STATUS_ONLY_PAPER_RUNTIME_DISABLED"
        )
