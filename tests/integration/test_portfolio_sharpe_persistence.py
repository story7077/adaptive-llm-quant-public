from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from trading.persistence.models import (
    AlgorithmProposalRow,
    ChallengerManifestRow,
    OosBudgetReservationRow,
    ResearchCandidateArtifactRow,
    ResearchCommanderSelectionRow,
    ResearchCycleRow,
)
from trading.persistence.portfolio_sharpe import (
    PortfolioSharpePersistenceError,
)
from trading.persistence.research import ResearchRepository
from trading.research.portfolio_delta_sharpe import (
    PortfolioIntegrationMode,
    RiskFreeSeriesMode,
    StationaryBootstrapContractV1,
    build_portfolio_comparison_contract,
)

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)
CANDIDATE_ARTIFACT_HASH = "a" * 64


def _seed_registered_artifact(
    factory: sessionmaker[Session],
) -> ResearchRepository:
    with factory.begin() as session:
        session.add(
            ResearchCommanderSelectionRow(
                selection_id="selection-portfolio-v2",
                version=1,
                selected_commander="CODEX_SOL_MAX",
                effective_at=NOW,
                config_hash="b" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ResearchCycleRow(
                research_cycle_id="cycle-portfolio-v2",
                request_id="request-portfolio-v2",
                selection_id="selection-portfolio-v2",
                selection_version=1,
                selected_commander="CODEX_SOL_MAX",
                source_snapshot_commit="c" * 40,
                champion_version="1.0.0",
                experiment_family="portfolio-family",
                as_of=NOW,
                data_available_cutoff=NOW,
                expires_at=NOW + timedelta(hours=2),
                context_manifest_hash="d" * 64,
                request_hash="e" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            AlgorithmProposalRow(
                proposal_id="proposal-portfolio-v2",
                research_cycle_id="cycle-portfolio-v2",
                hypothesis_id="hypothesis-portfolio-v2",
                parent_strategy_id="T1",
                parent_strategy_version="1.0.0",
                proposed_strategy_id="T1",
                proposed_strategy_version="1.1.0",
                proposal_hash="f" * 64,
                evidence_manifest_hash="0" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ChallengerManifestRow(
                challenger_id="challenger-1",
                proposal_id="proposal-portfolio-v2",
                strategy_id="T1",
                strategy_version="1.1.0",
                parent_version="1.0.0",
                experiment_family="portfolio-family",
                source_commit="c" * 40,
                patch_hash="1" * 64,
                code_hash="2" * 64,
                config_hash="3" * 64,
                test_manifest_hash="4" * 64,
                initial_status="PROPOSED",
                manifest_hash="5" * 64,
                payload_json={},
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            ResearchCandidateArtifactRow(
                bundle_id="bundle-portfolio-v2",
                challenger_id="challenger-1",
                proposal_id="proposal-portfolio-v2",
                research_cycle_id="cycle-portfolio-v2",
                candidate_tree_hash="6" * 64,
                code_hash="2" * 64,
                config_hash="3" * 64,
                test_manifest_hash="4" * 64,
                declared_entrypoint="trading.strategies.challengers.t1:decide",
                bundle_hash=CANDIDATE_ARTIFACT_HASH,
                real_order_routing=False,
                payload_json={},
                created_at=NOW,
            )
        )
    return ResearchRepository(factory)


def _contract(*, created_at: datetime = NOW):
    return build_portfolio_comparison_contract(
        champion_portfolio_manifest_hash="1" * 64,
        candidate_portfolio_manifest_hash="2" * 64,
        candidate_artifact_hash=CANDIDATE_ARTIFACT_HASH,
        allocation_policy_version="fixed-sleeve-v1",
        allocation_policy_hash="3" * 64,
        integration_mode=PortfolioIntegrationMode.REPLACE_SLEEVE,
        sleeve_replaced_or_added="research_sleeve",
        candidate_risk_budget=0.10,
        weight_selection_data_cutoff=NOW - timedelta(days=1),
        allocation_policy_created_at=NOW - timedelta(hours=1),
        starting_nav=100_000.0,
        market_data_manifest_hash="4" * 64,
        execution_contract_hash="5" * 64,
        cost_model_hash="6" * 64,
        risk_free_series_manifest_hash="7" * 64,
        risk_free_series_mode=RiskFreeSeriesMode.EXPLICIT_ZERO,
        common_session_policy="INTERSECTION_NO_INTERPOLATION",
        annualization_sessions=252,
        bootstrap_contract=StationaryBootstrapContractV1(
            configured_seed=7077,
            samples=500,
            expected_block_sessions=10,
            lower_quantile=0.025,
            variance_epsilon=1e-12,
        ),
        cost_stress_multipliers=(1.0, 2.0, 3.0),
        maximum_absolute_daily_return=1.0,
        created_at=created_at,
    )


def test_portfolio_contract_is_idempotent_and_append_only(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, engine, factory = sqlite_database
    research = _seed_registered_artifact(factory)
    repository = research.portfolio_sharpe()
    contract = _contract()

    assert repository.store_comparison_contract(
        challenger_id="challenger-1",
        contract=contract,
    )
    assert not repository.store_comparison_contract(
        challenger_id="challenger-1",
        contract=contract,
    )
    assert repository.comparison_contract(
        challenger_id="challenger-1"
    ) == contract

    with pytest.raises(
        PortfolioSharpePersistenceError,
        match="already has a portfolio contract",
    ):
        repository.store_comparison_contract(
            challenger_id="challenger-1",
            contract=_contract(created_at=NOW + timedelta(minutes=1)),
        )

    for statement in (
        "UPDATE portfolio_comparison_contracts "
        "SET allocation_policy_hash='mutated'",
        "DELETE FROM portfolio_comparison_contracts",
    ):
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(text(statement))


def test_portfolio_contract_cannot_be_created_after_oos_reservation(
    sqlite_database: tuple[str, Engine, sessionmaker[Session]],
) -> None:
    _, _, factory = sqlite_database
    research = _seed_registered_artifact(factory)
    contract = _contract()
    with factory.begin() as session:
        session.add(
            OosBudgetReservationRow(
                reservation_id="reservation-before-contract",
                challenger_id="challenger-1",
                experiment_family="family-1",
                submission_number=1,
                submission_ordinal=1,
                oos_budget_ordinal=1,
                candidate_artifact_hash=contract.candidate_artifact_hash,
                evaluation_contract_hash="8" * 64,
                idempotency_key="reservation-before-contract",
                reservation_hash="9" * 64,
                payload_json={},
                created_at=NOW,
                expires_at=NOW + timedelta(hours=1),
            )
        )

    with pytest.raises(
        PortfolioSharpePersistenceError,
        match="cannot change after OOS begins",
    ):
        research.portfolio_sharpe().store_comparison_contract(
            challenger_id="challenger-1",
            contract=contract,
        )
