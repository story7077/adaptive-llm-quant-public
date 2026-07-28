from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from trading.domain.hashing import canonical_hash
from trading.persistence.models import (
    AlgorithmProposalRow,
    ArmStateSnapshotRow,
    ChallengerEventRow,
    ChallengerManifestRow,
    DomainEventRow,
    FillRow,
    LedgerPostingRow,
    LedgerTransactionRow,
    NavSnapshotRow,
    OosLockboxResultRow,
    OrderEventRow,
    OrderIntentRow,
    PortfolioDecisionRow,
    ResearchCommanderSelectionRow,
    ResearchCycleRow,
    ResearchShadowArmRegistrationRow,
    ShadowArmRow,
)
from trading.persistence.research import ResearchRepository
from trading.persistence.research_shadow import ResearchShadowRuntimeRepository
from trading.research.shadow import ShadowExecutionContract
from trading.research.shadow_runtime import (
    ShadowArmRole,
    ShadowPaperParametersV1,
    ShadowQuoteV1,
    build_matched_quote_bundle,
    build_shadow_target_decision,
)

NOW = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
MARKET_HASH = "a" * 64
CHALLENGER_ARTIFACT_HASH = "c" * 64


def _parameters() -> ShadowPaperParametersV1:
    return ShadowPaperParametersV1(
        contract_version="shadow-paper-v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("0"),
        delay_penalty_bps=Decimal("1"),
        displayed_participation_rate=Decimal("0.10"),
        adv_participation_rate=Decimal("0.025"),
        minimum_order_notional_usd=Decimal("25"),
        quantity_quantum=Decimal("0.000001"),
        price_quantum=Decimal("0.0001"),
        sensitivity_5_bps=Decimal("5"),
        sensitivity_10_bps=Decimal("10"),
        basis_points_per_unit_return=Decimal("10000"),
        maximum_quote_age_seconds=15,
        weight_tolerance=Decimal("0.000001"),
        real_order_routing=False,
    )


def _execution_contract() -> ShadowExecutionContract:
    return ShadowExecutionContract(
        market_input_manifest_hash=MARKET_HASH,
        decision_schedule_version="schedule-v1",
        execution_scenario_version="execution-v1",
        cost_model_version="cost-v1",
        starting_capital_usd="100000.00",
        liquidity_policy_version="liquidity-v1",
    )


def _seed_lifecycle_pair(factory, *, include_start_event: bool = True) -> None:
    execution = _execution_contract()
    execution_payload = {
        "market_input_manifest_hash": execution.market_input_manifest_hash,
        "decision_schedule_version": execution.decision_schedule_version,
        "execution_scenario_version": execution.execution_scenario_version,
        "cost_model_version": execution.cost_model_version,
        "starting_capital_usd": execution.starting_capital_usd,
        "liquidity_policy_version": execution.liquidity_policy_version,
    }
    contract_hash = canonical_hash(execution_payload)
    with factory.begin() as session:
        session.add(
            ResearchCommanderSelectionRow(
                selection_id="selection-1",
                version=1,
                selected_commander="CODEX_SOL_MAX",
                effective_at=NOW,
                config_hash="1" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ResearchCycleRow(
                research_cycle_id="research-cycle-1",
                request_id="request-1",
                selection_id="selection-1",
                selection_version=1,
                selected_commander="CODEX_SOL_MAX",
                source_snapshot_commit="2" * 64,
                champion_version="1.0.0",
                experiment_family="family-1",
                as_of=NOW,
                data_available_cutoff=NOW,
                expires_at=NOW + timedelta(days=1),
                context_manifest_hash="3" * 64,
                request_hash="4" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            AlgorithmProposalRow(
                proposal_id="proposal-1",
                research_cycle_id="research-cycle-1",
                hypothesis_id="hypothesis-1",
                parent_strategy_id="T1",
                parent_strategy_version="1.0.0",
                proposed_strategy_id="T1",
                proposed_strategy_version="1.1.0",
                proposal_hash="5" * 64,
                evidence_manifest_hash="6" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ChallengerManifestRow(
                challenger_id="challenger-1",
                proposal_id="proposal-1",
                strategy_id="T1",
                strategy_version="1.1.0",
                parent_version="1.0.0",
                experiment_family="family-1",
                source_commit="7" * 64,
                patch_hash="8" * 64,
                code_hash="9" * 64,
                config_hash="a" * 64,
                test_manifest_hash="b" * 64,
                initial_status="PROPOSED",
                manifest_hash="d" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            OosLockboxResultRow(
                oos_result_id="oos-result-1",
                challenger_id="challenger-1",
                experiment_family="family-1",
                submission_number=1,
                candidate_artifact_hash=CHALLENGER_ARTIFACT_HASH,
                evaluation_contract_hash="e" * 64,
                verdict="PASS",
                common_sessions=126,
                result_hash="f" * 64,
                payload_json={},
                evaluated_at=NOW,
                created_at=NOW,
            )
        )
        session.flush()
        for role, arm_id, version in (
            ("CHAMPION", "champion-arm", "1.0.0"),
            ("CHALLENGER", "challenger-arm", "1.1.0"),
        ):
            registration_payload = {
                "execution_contract": execution_payload,
            }
            session.add(
                ResearchShadowArmRegistrationRow(
                    shadow_registration_id=f"registration-{role.lower()}",
                    shadow_pair_id="pair-1",
                    challenger_id="challenger-1",
                    oos_result_id="oos-result-1",
                    arm_role=role,
                    arm_id=arm_id,
                    strategy_id="T1",
                    strategy_version=version,
                    execution_contract_hash=contract_hash,
                    real_order_routing=False,
                    payload_json=registration_payload,
                    created_at=NOW,
                )
            )
        session.flush()
        session.add(
            ChallengerEventRow(
                challenger_event_id="challenger-event-shadow-pending",
                challenger_id="challenger-1",
                sequence=1,
                from_status="PROPOSED",
                to_status="SHADOW_PENDING",
                reason_code="OOS_PASS_SHADOW_REGISTERED",
                artifact_hash="f" * 64,
                idempotency_key="shadow-pending-1",
                event_hash="0" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
    if include_start_event:
        ResearchRepository(factory).start_shadow_evaluation(
            challenger_id="challenger-1",
            idempotency_key="shadow-start-1",
            created_at=NOW,
        )


def _cycle_inputs(spec):
    event_time = NOW + timedelta(minutes=1)
    bundle = build_matched_quote_bundle(
        market_input_manifest_hash=MARKET_HASH,
        as_of=event_time + timedelta(seconds=1),
        quotes=(
            ShadowQuoteV1(
                quote_id="quote-qqq",
                instrument_id="QQQ",
                event_time=event_time,
                available_at=event_time,
                bid_price=Decimal("199"),
                ask_price=Decimal("201"),
                bid_size_shares=Decimal("10000"),
                ask_size_shares=Decimal("10000"),
                adv_shares=Decimal("1000000"),
                source_hash="1" * 64,
            ),
            ShadowQuoteV1(
                quote_id="quote-spy",
                instrument_id="SPY",
                event_time=event_time,
                available_at=event_time,
                bid_price=Decimal("99"),
                ask_price=Decimal("101"),
                bid_size_shares=Decimal("10000"),
                ask_size_shares=Decimal("10000"),
                adv_shares=Decimal("1000000"),
                source_hash="2" * 64,
            ),
        ),
    )
    common = {
        "spec": spec,
        "decision_time": NOW,
        "signal_data_cutoff": NOW - timedelta(minutes=1),
        "valid_until": NOW + timedelta(minutes=20),
        "quote_manifest_hash": bundle.quote_manifest_hash,
    }
    champion = build_shadow_target_decision(
        target_id="target-champion",
        role=ShadowArmRole.CHAMPION,
        target_weights={
            "SPY": Decimal("0.5"),
            "USD_CASH": Decimal("0.5"),
        },
        **common,
    )
    challenger = build_shadow_target_decision(
        target_id="target-challenger",
        role=ShadowArmRole.CHALLENGER,
        target_weights={
            "QQQ": Decimal("0.5"),
            "USD_CASH": Decimal("0.5"),
        },
        **common,
    )
    return champion, challenger, bundle


def test_lifecycle_registered_pair_persists_independent_matched_paper_books(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    _seed_lifecycle_pair(factory)
    repository = ResearchShadowRuntimeRepository(factory)
    initialized = repository.initialize_from_lifecycle(
        challenger_id="challenger-1",
        champion_artifact_hash="b" * 64,
        paper_parameters=_parameters(),
        code_version="code-v1",
        created_at=NOW,
    )
    repeated_initialization = repository.initialize_from_lifecycle(
        challenger_id="challenger-1",
        champion_artifact_hash="b" * 64,
        paper_parameters=_parameters(),
        code_version="code-v1",
        created_at=NOW + timedelta(seconds=5),
    )
    assert initialized.created is True
    assert repeated_initialization.created is False
    champion, challenger, bundle = _cycle_inputs(initialized.spec)
    first = repository.append_matched_cycle(
        run_id=initialized.spec.run_id,
        champion_target=champion,
        challenger_target=challenger,
        quote_bundle=bundle,
    )
    replayed_request = repository.append_matched_cycle(
        run_id=initialized.spec.run_id,
        champion_target=champion,
        challenger_target=challenger,
        quote_bundle=bundle,
    )
    assert first.result_hash == replayed_request.result_hash
    assert first.champion.next_state.position_map() == {
        "SPY": first.champion.fills[0].quantity
    }
    assert first.challenger.next_state.position_map() == {
        "QQQ": first.challenger.fills[0].quantity
    }
    assert first.champion.next_state.cash_usd >= 0
    assert first.challenger.next_state.cash_usd >= 0

    with factory() as session:
        expected_counts = {
            ShadowArmRow: 2,
            PortfolioDecisionRow: 2,
            OrderIntentRow: 2,
            FillRow: 2,
            OrderEventRow: 4,
            LedgerTransactionRow: 4,
            LedgerPostingRow: 10,
            NavSnapshotRow: 4,
            ArmStateSnapshotRow: 4,
            DomainEventRow: 1,
        }
        for model, expected in expected_counts.items():
            assert session.scalar(select(func.count()).select_from(model)) == expected

    replay_hash = repository.deterministic_replay_hash(initialized.spec.run_id)
    assert replay_hash == repository.deterministic_replay_hash(
        initialized.spec.run_id
    )
    summary = repository.performance_summary(initialized.spec.run_id)
    assert summary.replay_hash == replay_hash
    assert summary.common_sessions == 1
    assert summary.profitability_claimed is False


def test_runtime_refuses_pair_without_explicit_lifecycle_shadow_start(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    _seed_lifecycle_pair(factory, include_start_event=False)
    repository = ResearchShadowRuntimeRepository(factory)
    with pytest.raises(
        RuntimeError,
        match=r"ResearchLifecycle\.start_shadow",
    ):
        repository.initialize_from_lifecycle(
            challenger_id="challenger-1",
            champion_artifact_hash="b" * 64,
            paper_parameters=_parameters(),
            code_version="code-v1",
            created_at=NOW,
        )
