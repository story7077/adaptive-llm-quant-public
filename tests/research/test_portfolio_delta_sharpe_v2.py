from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.domain.hashing import canonical_hash
from trading.research.contracts import OosVerdict, PromotionVerdict
from trading.research.oos_v2 import (
    OosLockboxResultV2,
    OosWorkerRequestV2,
    evaluate_private_request_v2,
)
from trading.research.portfolio_delta_sharpe import (
    PortfolioComparisonContractV1,
    PortfolioDeltaSharpeError,
    PortfolioIntegrationMode,
    PortfolioReturnObservationV1,
    RiskFreeSeriesMode,
    StationaryBootstrapContractV1,
    build_portfolio_comparison_contract,
    evaluate_portfolio_delta_sharpe,
)
from trading.research.promotion_v2 import (
    PromotionEvaluationContractV2,
    build_promotion_evidence_v2,
    build_trusted_shadow_performance_summary_v2,
    evaluate_trusted_promotion_evidence_v2,
)

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
HASH_D = "d" * 64
HASH_E = "e" * 64
HASH_F = "f" * 64
CREATED = datetime(2025, 1, 1, tzinfo=UTC)


def _contract(
    *,
    created_at: datetime = CREATED,
    candidate_artifact_hash: str = HASH_C,
) -> PortfolioComparisonContractV1:
    return build_portfolio_comparison_contract(
        champion_portfolio_manifest_hash=HASH_A,
        candidate_portfolio_manifest_hash=HASH_B,
        candidate_artifact_hash=candidate_artifact_hash,
        allocation_policy_version="allocation-v1",
        allocation_policy_hash=HASH_D,
        integration_mode=PortfolioIntegrationMode.ADD_SLEEVE,
        sleeve_replaced_or_added="research-sleeve",
        candidate_risk_budget=0.15,
        weight_selection_data_cutoff=CREATED - timedelta(days=2),
        allocation_policy_created_at=CREATED - timedelta(days=1),
        starting_nav=100_000,
        market_data_manifest_hash=HASH_E,
        execution_contract_hash=HASH_F,
        cost_model_hash="1" * 64,
        risk_free_series_manifest_hash="2" * 64,
        risk_free_series_mode=RiskFreeSeriesMode.EXPLICIT_ZERO,
        common_session_policy="INTERSECTION_NO_INTERPOLATION",
        annualization_sessions=252,
        bootstrap_contract=StationaryBootstrapContractV1(
            configured_seed=7077,
            samples=300,
            expected_block_sessions=10,
            lower_quantile=0.025,
            variance_epsilon=1e-12,
        ),
        cost_stress_multipliers=(1.0, 2.0, 3.0),
        maximum_absolute_daily_return=1.0,
        created_at=created_at,
    )


def _rows(
    *,
    count: int = 180,
    uplift: float = 0.0005,
    candidate_cost: float = 0.00005,
    champion_cost: float = 0.00005,
    identical: bool = False,
    volatile_candidate: bool = False,
) -> tuple[PortfolioReturnObservationV1, ...]:
    rows: list[PortfolioReturnObservationV1] = []
    for index in range(count):
        champion = 0.0003 + 0.004 * math.sin(index * 0.31)
        if identical:
            candidate = champion
        elif volatile_candidate:
            candidate = 0.001 + (0.04 if index % 2 == 0 else -0.04)
        else:
            candidate = champion + uplift
        rows.append(
            PortfolioReturnObservationV1(
                session_index=index,
                session_key=f"session-{index:04d}",
                available_at=CREATED + timedelta(days=index + 1),
                candidate_portfolio_return_before_cost=candidate,
                champion_portfolio_return_before_cost=champion,
                candidate_base_cost_return=candidate_cost,
                champion_base_cost_return=champion_cost,
                risk_free_daily_return=0.0,
            )
        )
    return tuple(rows)


def test_identical_portfolios_have_zero_delta_and_no_positive_lcb() -> None:
    result = evaluate_portfolio_delta_sharpe(
        observations=_rows(identical=True),
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )

    assert abs(result.delta_sharpe_point) <= 1e-12
    assert abs(result.delta_sharpe_lcb) <= 1e-12
    assert not result.delta_sharpe_lcb > 0.0


def test_higher_mean_with_large_volatility_can_fail_delta_sharpe() -> None:
    result = evaluate_portfolio_delta_sharpe(
        observations=_rows(volatile_candidate=True),
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )

    assert sum(
        row.candidate_portfolio_return_before_cost for row in _rows(
            volatile_candidate=True
        )
    ) > sum(
        row.champion_portfolio_return_before_cost for row in _rows(
            volatile_candidate=True
        )
    )
    assert result.delta_sharpe_lcb < 0


def test_stable_uplift_produces_positive_delta_sharpe_lcb() -> None:
    result = evaluate_portfolio_delta_sharpe(
        observations=_rows(),
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )

    assert result.delta_sharpe_point > 0
    assert result.delta_sharpe_lcb > 0


def test_three_x_cost_can_reject_a_base_cost_winner() -> None:
    result = evaluate_portfolio_delta_sharpe(
        observations=_rows(
            uplift=0.0005,
            candidate_cost=0.00025,
            champion_cost=0.00005,
        ),
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )

    assert result.cost_stress_results[0].delta_sharpe_lcb > 0
    assert result.cost_stress_results[2].delta_sharpe_lcb < 0
    assert result.worst_cost_delta_sharpe_lcb < 0


def test_swapping_portfolios_reverses_point_delta_sign() -> None:
    rows = _rows()
    first = evaluate_portfolio_delta_sharpe(
        observations=rows,
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )
    swapped = tuple(
        row.model_copy(
            update={
                "candidate_portfolio_return_before_cost": (
                    row.champion_portfolio_return_before_cost
                ),
                "champion_portfolio_return_before_cost": (
                    row.candidate_portfolio_return_before_cost
                ),
                "candidate_base_cost_return": row.champion_base_cost_return,
                "champion_base_cost_return": row.candidate_base_cost_return,
            }
        )
        for row in rows
    )
    second = evaluate_portfolio_delta_sharpe(
        observations=swapped,
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )

    assert math.isclose(
        second.delta_sharpe_point,
        -first.delta_sharpe_point,
        rel_tol=1e-12,
        abs_tol=1e-12,
    )


def test_same_seed_artifact_and_contract_replay_identically() -> None:
    first = evaluate_portfolio_delta_sharpe(
        observations=_rows(),
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )
    second = evaluate_portfolio_delta_sharpe(
        observations=_rows(),
        comparison_contract=_contract(),
        evaluation_contract_hash="3" * 64,
    )

    assert first == second
    assert first.result_hash == second.result_hash


def test_contract_hash_and_degenerate_variance_fail_closed() -> None:
    payload = _contract().model_dump(mode="python")
    payload["candidate_risk_budget"] = 0.2
    with pytest.raises(ValidationError, match="contract hash mismatch"):
        PortfolioComparisonContractV1.model_validate(payload)

    constant_rows = tuple(
        row.model_copy(
            update={
                "candidate_portfolio_return_before_cost": 0.001,
                "champion_portfolio_return_before_cost": 0.001,
            }
        )
        for row in _rows()
    )
    with pytest.raises(
        PortfolioDeltaSharpeError,
        match="DEGENERATE_VARIANCE",
    ):
        evaluate_portfolio_delta_sharpe(
            observations=constant_rows,
            comparison_contract=_contract(),
            evaluation_contract_hash="3" * 64,
        )


def _write_private_dataset(
    root: Path,
    *,
    contract: PortfolioComparisonContractV1,
    rows: tuple[PortfolioReturnObservationV1, ...],
    candidate_manifest_hash: str | None = None,
) -> tuple[str, str]:
    dataset_id = "portfolio-oos-v2"
    payload = {
        "schema_version": "oos_private_dataset_v2",
        "dataset_id": dataset_id,
        "candidate_artifact_hash": contract.candidate_artifact_hash,
        "evaluation_contract_hash": "3" * 64,
        "portfolio_comparison_contract_hash": contract.contract_hash,
        "champion_portfolio_manifest_hash": (
            contract.champion_portfolio_manifest_hash
        ),
        "candidate_portfolio_manifest_hash": (
            candidate_manifest_hash
            if candidate_manifest_hash is not None
            else contract.candidate_portfolio_manifest_hash
        ),
        "allocation_policy_hash": contract.allocation_policy_hash,
        "market_data_manifest_hash": contract.market_data_manifest_hash,
        "execution_contract_hash": contract.execution_contract_hash,
        "cost_model_hash": contract.cost_model_hash,
        "risk_free_series_manifest_hash": (
            contract.risk_free_series_manifest_hash
        ),
        "source_data_manifest_hash": "4" * 64,
        "candidate_replay_hash": "5" * 64,
        "trusted_producer_version": "trusted_candidate_evaluation_v2",
        "independent_trade_count": 60,
        "observations": [
            item.model_dump(mode="json") for item in rows
        ],
    }
    document = {**payload, "dataset_hash": canonical_hash(payload)}
    (root / f"{dataset_id}.json").write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )
    return dataset_id, str(document["dataset_hash"])


def _worker_request(
    *,
    contract: PortfolioComparisonContractV1,
    dataset_id: str,
    dataset_hash: str,
    cutoff: datetime,
) -> OosWorkerRequestV2:
    payload = {
        "schema_version": "oos_worker_request_v2",
        "request_id": "oos-v2-request",
        "challenger_id": "challenger-v2",
        "experiment_family": "family-v2",
        "submission_number": 1,
        "candidate_artifact_hash": contract.candidate_artifact_hash,
        "evaluation_contract_hash": "3" * 64,
        "reservation_id": "reservation-v2",
        "reservation_hash": "6" * 64,
        "oos_budget_ordinal": 1,
        "dataset_id": dataset_id,
        "dataset_manifest_hash": dataset_hash,
        "expected_source_data_manifest_hash": "4" * 64,
        "expected_candidate_replay_hash": "5" * 64,
        "expected_trusted_producer_version": (
            "trusted_candidate_evaluation_v2"
        ),
        "portfolio_comparison_contract": contract,
        "data_available_cutoff": cutoff,
        "minimum_common_sessions": 126,
        "minimum_independent_trades": 30,
        "minimum_delta_sharpe_lcb": 0.0,
        "minimum_worst_cost_delta_sharpe_lcb": 0.0,
        "evaluated_at": cutoff,
        "expires_at": cutoff + timedelta(hours=1),
    }
    return OosWorkerRequestV2.model_validate(
        {**payload, "request_hash": canonical_hash(payload)}
    )


def test_oos_v2_is_aggregate_only_and_binding_failures_are_closed(
    tmp_path: Path,
) -> None:
    contract = _contract()
    rows = _rows()
    dataset_id, dataset_hash = _write_private_dataset(
        tmp_path,
        contract=contract,
        rows=rows,
    )
    request = _worker_request(
        contract=contract,
        dataset_id=dataset_id,
        dataset_hash=dataset_hash,
        cutoff=max(row.available_at for row in rows),
    )
    response = evaluate_private_request_v2(request, private_root=tmp_path)

    assert response.result.verdict is OosVerdict.PASS
    serialized = response.model_dump_json()
    assert '"observations"' not in serialized
    assert "bootstrap_samples" not in serialized
    assert "session_key" not in serialized

    other_root = tmp_path / "mismatch"
    other_root.mkdir()
    bad_dataset_id, bad_hash = _write_private_dataset(
        other_root,
        contract=contract,
        rows=rows,
        candidate_manifest_hash="9" * 64,
    )
    bad_request = _worker_request(
        contract=contract,
        dataset_id=bad_dataset_id,
        dataset_hash=bad_hash,
        cutoff=max(row.available_at for row in rows),
    )
    bad = evaluate_private_request_v2(
        bad_request,
        private_root=other_root,
    )
    assert bad.result.verdict is OosVerdict.FAIL
    assert bad.result.reason_codes == (
        "PORTFOLIO_CONTRACT_BINDING_INVALID",
    )


def test_allocation_policy_created_after_oos_is_a_pit_failure(
    tmp_path: Path,
) -> None:
    rows = _rows()
    late_contract = _contract(
        created_at=rows[10].available_at,
    )
    dataset_id, dataset_hash = _write_private_dataset(
        tmp_path,
        contract=late_contract,
        rows=rows,
    )
    request = _worker_request(
        contract=late_contract,
        dataset_id=dataset_id,
        dataset_hash=dataset_hash,
        cutoff=max(row.available_at for row in rows),
    )
    response = evaluate_private_request_v2(request, private_root=tmp_path)

    assert response.result.verdict is OosVerdict.FAIL
    assert response.result.reason_codes == (
        "ALLOCATION_POLICY_NOT_FIXED_BEFORE_OOS",
    )


def _oos_result(
    *,
    contract: PortfolioComparisonContractV1,
    rows: tuple[PortfolioReturnObservationV1, ...],
    verdict: OosVerdict,
) -> OosLockboxResultV2:
    metric = evaluate_portfolio_delta_sharpe(
        observations=rows,
        comparison_contract=contract,
        evaluation_contract_hash="3" * 64,
    )
    reasons = (
        ("PREDECLARED_PORTFOLIO_OOS_CRITERIA_PASSED",)
        if verdict is OosVerdict.PASS
        else ("DELTA_SHARPE_LCB_NOT_MET",)
    )
    payload = {
        "schema_version": "oos_lockbox_result_v2",
        "challenger_id": "challenger-v2",
        "experiment_family": "family-v2",
        "submission_number": 1,
        "candidate_artifact_hash": contract.candidate_artifact_hash,
        "evaluation_contract_hash": "3" * 64,
        "portfolio_comparison_contract_hash": contract.contract_hash,
        "verdict": verdict,
        "reason_codes": reasons,
        "common_sessions": metric.common_sessions,
        "independent_trades": 60,
        "candidate_portfolio_sharpe": metric.candidate_portfolio_sharpe,
        "champion_portfolio_sharpe": metric.champion_portfolio_sharpe,
        "delta_sharpe_point": metric.delta_sharpe_point,
        "delta_sharpe_lcb": metric.delta_sharpe_lcb,
        "delta_sharpe_ucb": metric.delta_sharpe_ucb,
        "worst_cost_delta_sharpe_lcb": (
            metric.worst_cost_delta_sharpe_lcb
        ),
        "cost_stress_results": metric.cost_stress_results,
        "no_degenerate_variance": True,
        "portfolio_contract_binding_valid": True,
        "allocation_policy_fixed_before_oos": True,
        "all_metrics_finite": True,
        "budget_consumed": 1,
        "evaluated_at": CREATED + timedelta(days=200),
    }
    return OosLockboxResultV2.model_validate(
        {**payload, "result_hash": canonical_hash(payload)}
    )


def test_promotion_v2_rejects_negative_delta_lcb_despite_old_point_gates() -> None:
    contract = _contract()
    rows = _rows()
    evidence_hashes = tuple(
        canonical_hash({"session_index": item.session_index}) for item in rows
    )
    shadow = build_trusted_shadow_performance_summary_v2(
        summary_id="shadow-summary-v2",
        challenger_id="challenger-v2",
        shadow_pair_id="shadow-pair-v2",
        run_id="shadow-run-v2",
        current_champion_version="1.0.0",
        candidate_version="1.1.0",
        candidate_artifact_hash=contract.candidate_artifact_hash,
        comparison_contract=contract,
        execution_contract_hash=HASH_F,
        observations=rows,
        daily_evidence_hashes=evidence_hashes,
        independent_trades=60,
        annualized_net_excess_return_after_cost=0.10,
        matched_annualized_difference=0.05,
        economic_effect=0.03,
        maximum_drawdown=0.08,
        tail_loss=0.02,
        annualized_turnover=2.0,
        estimated_capacity_usd=1_000_000,
        regime_pass_fraction=0.8,
        runtime_error_rate=0.0,
        data_available_cutoff=max(row.available_at for row in rows),
        created_at=max(row.available_at for row in rows) + timedelta(hours=1),
    )
    oos = _oos_result(
        contract=contract,
        rows=_rows(volatile_candidate=True),
        verdict=OosVerdict.FAIL,
    )
    evidence = build_promotion_evidence_v2(
        evidence_id="promotion-evidence-v2",
        challenger_id="challenger-v2",
        current_champion_version="1.0.0",
        candidate_version="1.1.0",
        candidate_artifact_hash=contract.candidate_artifact_hash,
        comparison_contract=contract,
        falsification_report_hash="7" * 64,
        oos_result=oos,
        shadow_summary=shadow,
        replay_hash="8" * 64,
        annualized_net_excess_return_after_cost=0.10,
        matched_annualized_difference=0.05,
        economic_effect=0.03,
        maximum_drawdown=0.08,
        tail_loss=0.02,
        annualized_turnover=2.0,
        estimated_capacity_usd=1_000_000,
        regime_pass_fraction=0.8,
        runtime_error_rate=0.0,
        replay_reproducible=True,
        mandatory_tests_passed=True,
        data_available_cutoff=shadow.data_available_cutoff,
        created_at=shadow.created_at + timedelta(hours=1),
    )
    evaluation = evaluate_trusted_promotion_evidence_v2(
        evidence=evidence,
        contract=PromotionEvaluationContractV2(
            contract_version="promotion-v2",
            minimum_common_oos_sessions=126,
            minimum_forward_sessions=63,
            minimum_independent_trades=30,
            minimum_annualized_net_excess_return_after_cost=0.0,
            minimum_matched_annualized_difference=0.0,
            minimum_economic_effect=0.01,
            maximum_drawdown=0.20,
            maximum_tail_loss=0.05,
            maximum_annualized_turnover=12.0,
            minimum_capacity_usd=100_000,
            minimum_regime_pass_fraction=0.67,
            maximum_runtime_error_rate=0.01,
            minimum_oos_delta_sharpe_lcb=0.0,
            minimum_shadow_delta_sharpe_lcb=0.0,
            minimum_worst_cost_delta_sharpe_lcb=0.0,
        ),
        created_at=evidence.created_at + timedelta(hours=1),
    )

    assert evaluation.decision.verdict is PromotionVerdict.INELIGIBLE
    assert evaluation.decision.criteria[
        "net_excess_return_after_cost"
    ]
    assert not evaluation.decision.criteria[
        "oos_portfolio_delta_sharpe_lcb"
    ]
