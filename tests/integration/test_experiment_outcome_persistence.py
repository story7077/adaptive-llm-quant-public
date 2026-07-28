from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash, stable_id
from trading.persistence.experiment_outcomes import (
    ExperimentOutcomePersistenceError,
    ExperimentOutcomeRepository,
)
from trading.persistence.models import ResearchExperimentOutcomeEventRow
from trading.research.experiment_outcomes import (
    ExperimentInformationRole,
    ExperimentMaturityStatus,
    ExperimentOutcomeEventKind,
    ExperimentOutcomeEventV1,
    ExperimentOutcomeMaturationInputV1,
    ExperimentStage,
    ResearchActionKind,
    ResearchExperimentActionV1,
)

NOW = datetime(2026, 1, 2, 15, 0, tzinfo=UTC)
DUE = NOW + timedelta(days=2)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _action(
    experiment_id: str,
    role: ExperimentInformationRole,
) -> ResearchExperimentActionV1:
    payload = {
        "schema_version": "research_experiment_action_v1",
        "action_id": stable_id("research-experiment-action", experiment_id),
        "experiment_id": experiment_id,
        "research_cycle_id": "cycle-ledger",
        "proposal_id": f"proposal-{experiment_id}",
        "challenger_id": f"challenger-{experiment_id}",
        "parent_strategy_id": "parent",
        "parent_strategy_version": "1.0.0",
        "candidate_strategy_version": "1.1.0",
        "primary_action_kind": ResearchActionKind.ADD_FEATURE,
        "secondary_action_kinds": (),
        "mechanism_tags": ("diversification",),
        "information_role": role,
        "decision_at": NOW,
        "maturity_due_at": DUE,
        "predicted_delta_sharpe_lower": -0.1,
        "predicted_delta_sharpe_median": 0.1,
        "predicted_delta_sharpe_upper": 0.3,
        "predicted_failure_codes": ("NO_EDGE",),
        "complexity_delta": 1.0,
        "candidate_artifact_hash": HASH_A,
        "evaluation_contract_hash": HASH_B,
        "source_artifact_hashes": (HASH_A,),
        "source_data_available_at": (NOW,),
        "legacy_proposal": False,
        "meta_training_permitted": (
            role is ExperimentInformationRole.LEARNING_FORWARD
        ),
        "idempotency_key": f"action-{experiment_id}",
        "created_at": NOW,
    }
    return ResearchExperimentActionV1.model_validate(
        {**payload, "action_hash": canonical_hash(payload)}
    )


def _maturation(
    experiment_id: str,
    *,
    idempotency_key: str,
    available_at: datetime = DUE,
    maturity_status: ExperimentMaturityStatus = (
        ExperimentMaturityStatus.MATURED
    ),
    point: float | None = 0.2,
    supersedes_event_id: str | None = None,
) -> ExperimentOutcomeMaturationInputV1:
    economic = maturity_status is ExperimentMaturityStatus.MATURED and point is not None
    return ExperimentOutcomeMaturationInputV1(
        experiment_id=experiment_id,
        event_kind=(
            ExperimentOutcomeEventKind.OUTCOME_CORRECTED
            if supersedes_event_id is not None
            else (
                ExperimentOutcomeEventKind.ECONOMIC_OUTCOME_MATURED
                if economic
                else ExperimentOutcomeEventKind.EXPERIMENT_REGISTERED
            )
        ),
        experiment_stage=ExperimentStage.FORWARD,
        evaluation_window_start=NOW if economic else None,
        evaluation_window_end=available_at if economic else None,
        available_at=available_at,
        maturity_status=maturity_status,
        technical_success=True if economic else None,
        technical_failure_codes=(),
        portfolio_delta_sharpe_point=point if economic else None,
        portfolio_delta_sharpe_lcb=0.05 if economic else None,
        portfolio_delta_sharpe_ucb=0.35 if economic else None,
        worst_cost_delta_sharpe_lcb=0.01 if economic else None,
        drawdown_delta=-0.01 if economic else None,
        tail_loss_delta=-0.005 if economic else None,
        turnover_delta=0.1 if economic else None,
        cost_delta_bps=2.0 if economic else None,
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(HASH_A,) if economic else (),
        source_data_available_at=(available_at,) if economic else (),
        supersedes_event_id=supersedes_event_id,
        idempotency_key=idempotency_key,
        created_at=available_at,
    )


def test_outcome_idempotency_conflict_and_superseding_correction(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action("experiment-correction", ExperimentInformationRole.LEARNING_FORWARD)
    assert repository.register_action(action) is True
    assert repository.register_action(action) is False

    first_input = _maturation(
        action.experiment_id,
        idempotency_key="mature-first",
    )
    first, created = repository.append_outcome(first_input)
    assert created is True
    repeated, repeated_created = repository.append_outcome(first_input)
    assert repeated_created is False
    assert repeated.event_hash == first.event_hash

    conflicting_payload = first_input.model_dump(mode="python")
    conflicting_payload["portfolio_delta_sharpe_point"] = 0.25
    conflicting_payload["portfolio_delta_sharpe_ucb"] = 0.4
    conflicting = ExperimentOutcomeMaturationInputV1.model_validate(
        conflicting_payload
    )
    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="idempotency conflict",
    ):
        repository.append_outcome(conflicting)

    correction, correction_created = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="mature-correction",
            available_at=DUE + timedelta(days=1),
            point=0.1,
            supersedes_event_id=first.event_id,
        )
    )
    assert correction_created is True
    status = repository.status()
    assert status["event_count"] == 2
    assert status["effective_unsuperseded_event_count"] == 1
    assert status["superseded_event_count"] == 1
    assert status["eligible_learning_forward_event_count"] == 1
    assert status["effective_eligible_learning_forward_event_count"] == 1
    snapshot, persisted = repository.materialize_memory(
        as_of=DUE + timedelta(days=1),
        data_available_cutoff=DUE + timedelta(days=1),
        created_at=DUE + timedelta(days=1),
        persist=True,
    )
    assert persisted is True
    assert snapshot.included_event_hashes == (correction.event_hash,)
    assert snapshot.excluded_invalid_event_count == 1
    assert snapshot.action_statistics[0].matured_economic_outcome_count == 1


def test_second_economic_outcome_requires_explicit_correction(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-duplicate-economic",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    first, _ = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="economic-first",
        )
    )

    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="explicit correction",
    ):
        repository.append_outcome(
            _maturation(
                action.experiment_id,
                idempotency_key="economic-duplicate",
                available_at=DUE + timedelta(days=1),
                point=0.1,
            )
        )

    correction, created = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="economic-explicit-correction",
            available_at=DUE + timedelta(days=1),
            point=0.1,
            supersedes_event_id=first.event_id,
        )
    )
    assert created is True
    assert correction.supersedes_event_id == first.event_id


def test_terminal_outcome_cannot_be_reopened_without_explicit_correction(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-terminal-transition",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    first, _ = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="terminal-first",
        )
    )

    pending = _maturation(
        action.experiment_id,
        idempotency_key="terminal-reopened",
        available_at=DUE + timedelta(days=1),
        maturity_status=ExperimentMaturityStatus.PENDING,
        point=None,
    )
    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="explicit correction",
    ):
        repository.append_outcome(pending)

    technical = ExperimentOutcomeMaturationInputV1(
        experiment_id=action.experiment_id,
        event_kind=ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED,
        experiment_stage=ExperimentStage.TEST,
        available_at=DUE + timedelta(days=1),
        maturity_status=ExperimentMaturityStatus.MATURED,
        technical_success=True,
        technical_failure_codes=(),
        evaluation_contract_hash=HASH_B,
        source_artifact_hashes=(),
        source_data_available_at=(),
        idempotency_key="terminal-technical-reopen",
        created_at=DUE + timedelta(days=1),
    )
    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="explicit correction",
    ):
        repository.append_outcome(technical)

    assert repository.due_experiments(as_of=DUE + timedelta(days=1)) == ()

    correction, created = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="terminal-correction",
            available_at=DUE + timedelta(days=1),
            point=0.1,
            supersedes_event_id=first.event_id,
        )
    )
    assert created is True
    assert correction.supersedes_event_id == first.event_id


def test_successful_technical_event_remains_due_for_economic_maturation(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-technical-then-economic",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    repository.append_outcome(
        ExperimentOutcomeMaturationInputV1(
            experiment_id=action.experiment_id,
            event_kind=(
                ExperimentOutcomeEventKind.TECHNICAL_OUTCOME_RECORDED
            ),
            experiment_stage=ExperimentStage.TEST,
            available_at=NOW,
            maturity_status=ExperimentMaturityStatus.MATURED,
            technical_success=True,
            evaluation_contract_hash=HASH_B,
            idempotency_key="technical-success-before-maturity",
            created_at=NOW,
        )
    )

    assert repository.due_experiments(as_of=NOW) == ()
    assert tuple(
        item.experiment_id
        for item in repository.due_experiments(as_of=DUE)
    ) == (action.experiment_id,)

    repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="economic-after-technical-success",
        )
    )
    assert repository.due_experiments(as_of=DUE) == ()


def test_registration_event_is_allowed_only_at_chain_start(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-single-registration",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="registration-first",
            available_at=NOW,
            maturity_status=ExperimentMaturityStatus.PENDING,
            point=None,
        )
    )

    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="only as the first event",
    ):
        repository.append_outcome(
            _maturation(
                action.experiment_id,
                idempotency_key="registration-second",
                available_at=NOW + timedelta(seconds=1),
                maturity_status=ExperimentMaturityStatus.PENDING,
                point=None,
            )
        )


def test_memory_snapshot_excludes_future_unmatured_and_protected_roles(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    roles = {
        "learning": ExperimentInformationRole.LEARNING_FORWARD,
        "discovery": ExperimentInformationRole.DISCOVERY,
        "oos": ExperimentInformationRole.PROMOTION_OOS,
        "audit": ExperimentInformationRole.META_AUDIT,
        "pending": ExperimentInformationRole.LEARNING_FORWARD,
        "future": ExperimentInformationRole.LEARNING_FORWARD,
    }
    for name, role in roles.items():
        repository.register_action(_action(f"experiment-{name}", role))
    learning, _ = repository.append_outcome(
        _maturation("experiment-learning", idempotency_key="mature-learning")
    )
    discovery, _ = repository.append_outcome(
        _maturation("experiment-discovery", idempotency_key="mature-discovery")
    )
    repository.append_outcome(
        _maturation("experiment-oos", idempotency_key="mature-oos")
    )
    repository.append_outcome(
        _maturation("experiment-audit", idempotency_key="mature-audit")
    )
    repository.append_outcome(
        _maturation(
            "experiment-pending",
            idempotency_key="pending",
            available_at=NOW,
            maturity_status=ExperimentMaturityStatus.PENDING,
            point=None,
        )
    )
    future_time = DUE + timedelta(days=3)
    repository.append_outcome(
        _maturation(
            "experiment-future",
            idempotency_key="mature-future",
            available_at=future_time,
        )
    )

    snapshot, _ = repository.materialize_memory(
        as_of=future_time,
        data_available_cutoff=DUE,
        created_at=future_time,
        persist=False,
    )
    assert snapshot.included_event_hashes == (
        discovery.event_hash,
        learning.event_hash,
    )
    assert snapshot.excluded_future_event_count == 1
    assert snapshot.excluded_unmatured_event_count == 1
    assert snapshot.excluded_oos_event_count == 1
    assert snapshot.excluded_meta_audit_event_count == 1
    by_action = {
        item.primary_action_kind: item for item in snapshot.action_statistics
    }
    statistic = by_action[ResearchActionKind.ADD_FEATURE]
    assert statistic.included_event_count == 2
    assert statistic.matured_economic_outcome_count == 1
    assert len(snapshot.nearest_historical_analogs) == 1
    assert (
        snapshot.nearest_historical_analogs[0].experiment_id
        == "experiment-learning"
    )


def test_later_appends_do_not_change_a_past_memory_snapshot(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    repository.register_action(
        _action("experiment-pit", ExperimentInformationRole.LEARNING_FORWARD)
    )
    first, _ = repository.append_outcome(
        _maturation("experiment-pit", idempotency_key="mature-pit")
    )
    past_as_of = DUE
    before, _ = repository.materialize_memory(
        as_of=past_as_of,
        data_available_cutoff=past_as_of,
        created_at=past_as_of,
        persist=True,
    )
    repository.register_action(
        _action("experiment-later", ExperimentInformationRole.LEARNING_FORWARD)
    )
    repository.append_outcome(
        _maturation(
            "experiment-later",
            idempotency_key="mature-later",
            available_at=DUE + timedelta(days=1),
        )
    )
    after, persisted = repository.materialize_memory(
        as_of=past_as_of,
        data_available_cutoff=past_as_of,
        created_at=past_as_of,
        persist=True,
    )
    assert persisted is False
    assert before.snapshot_hash == after.snapshot_hash
    assert before.included_event_hashes == (first.event_hash,)


def test_future_correction_does_not_supersede_at_an_earlier_cutoff(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-future-correction",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    first, _ = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="mature-original",
        )
    )
    correction_time = DUE + timedelta(days=1)
    correction, _ = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="mature-future-correction",
            available_at=correction_time,
            point=0.1,
            supersedes_event_id=first.event_id,
        )
    )

    past, _ = repository.materialize_memory(
        as_of=correction_time,
        data_available_cutoff=DUE,
        created_at=correction_time,
        persist=False,
    )
    current, _ = repository.materialize_memory(
        as_of=correction_time,
        data_available_cutoff=correction_time,
        created_at=correction_time,
        persist=False,
    )

    assert past.included_event_hashes == (first.event_hash,)
    assert past.excluded_future_event_count == 1
    assert current.included_event_hashes == (correction.event_hash,)
    assert current.excluded_invalid_event_count == 1


def test_correction_availability_cannot_predate_superseded_outcome(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-correction-availability",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    first, _ = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="availability-original",
            available_at=DUE + timedelta(days=2),
        )
    )
    correction = _maturation(
        action.experiment_id,
        idempotency_key="availability-backdated-correction",
        available_at=DUE + timedelta(days=1),
        point=0.1,
        supersedes_event_id=first.event_id,
    ).model_copy(update={"created_at": DUE + timedelta(days=3)})

    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="cannot predate",
    ):
        repository.append_outcome(correction)


def test_due_lookup_ignores_events_created_after_the_requested_as_of(
    sqlite_database,
) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-due-pit",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="mature-after-cutoff",
            available_at=DUE + timedelta(days=1),
        )
    )

    due_at_maturity = repository.due_experiments(as_of=DUE)
    due_after_outcome = repository.due_experiments(
        as_of=DUE + timedelta(days=1)
    )

    assert tuple(item.experiment_id for item in due_at_maturity) == (
        action.experiment_id,
    )
    assert due_after_outcome == ()


def test_event_chain_rejects_backdated_append(sqlite_database) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action(
        "experiment-created-at-order",
        ExperimentInformationRole.LEARNING_FORWARD,
    )
    repository.register_action(action)
    first, _ = repository.append_outcome(
        _maturation(
            action.experiment_id,
            idempotency_key="mature-created-first",
            available_at=DUE,
        ).model_copy(update={"created_at": DUE + timedelta(days=1)})
    )
    payload = _maturation(
        action.experiment_id,
        idempotency_key="mature-created-regression",
        available_at=DUE,
        point=0.1,
        supersedes_event_id=first.event_id,
    )

    with pytest.raises(ValueError, match="creation time cannot regress"):
        repository.append_outcome(payload)


def test_hash_chain_tamper_is_detected(sqlite_database) -> None:
    _, _, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    action = _action("experiment-tamper", ExperimentInformationRole.LEARNING_FORWARD)
    repository.register_action(action)
    first, _ = repository.append_outcome(
        _maturation(action.experiment_id, idempotency_key="mature-one")
    )
    second_input = _maturation(
        action.experiment_id,
        idempotency_key="mature-two",
        available_at=DUE + timedelta(days=1),
        supersedes_event_id=first.event_id,
    )
    valid_second, _ = repository.prepare_outcome(second_input)
    tampered_payload = valid_second.model_dump(
        mode="python",
        exclude={"event_hash"},
    )
    tampered_payload["previous_event_hash"] = "f" * 64
    tampered = ExperimentOutcomeEventV1.model_validate(
        {
            **tampered_payload,
            "event_hash": canonical_hash(tampered_payload),
        }
    )
    with factory.begin() as session:
        session.add(
            ResearchExperimentOutcomeEventRow(
                event_id=tampered.event_id,
                action_id=action.action_id,
                experiment_id=tampered.experiment_id,
                research_cycle_id=tampered.research_cycle_id,
                proposal_id=tampered.proposal_id,
                challenger_id=tampered.challenger_id,
                information_role=tampered.information_role.value,
                primary_action_kind=tampered.primary_action_kind.value,
                event_kind=tampered.event_kind.value,
                experiment_stage=tampered.experiment_stage.value,
                event_sequence=tampered.event_sequence,
                available_at=tampered.available_at,
                maturity_due_at=tampered.maturity_due_at,
                maturity_status=tampered.maturity_status.value,
                eligible_for_meta_training=(
                    tampered.eligible_for_meta_training
                ),
                previous_event_hash=tampered.previous_event_hash,
                supersedes_event_id=tampered.supersedes_event_id,
                idempotency_key=tampered.idempotency_key,
                maturation_input_hash=tampered.maturation_input_hash,
                event_hash=tampered.event_hash,
                payload_json=model_payload(tampered),
                created_at=tampered.created_at,
            )
        )
    with pytest.raises(
        ExperimentOutcomePersistenceError,
        match="previous hash mismatch",
    ):
        repository.event_chain(action.experiment_id)
    assert first.event_hash != tampered.previous_event_hash


def test_ledger_tables_reject_update_and_delete(sqlite_database) -> None:
    _, engine, factory = sqlite_database
    repository = ExperimentOutcomeRepository(factory)
    repository.register_action(
        _action("experiment-guard", ExperimentInformationRole.LEARNING_FORWARD)
    )
    repository.append_outcome(
        _maturation("experiment-guard", idempotency_key="mature-guard")
    )
    snapshot, _ = repository.materialize_memory(
        as_of=DUE,
        data_available_cutoff=DUE,
        created_at=DUE,
        persist=True,
    )
    attempts = (
        (
            "UPDATE research_experiment_actions "
            "SET primary_action_kind='REMOVE_FEATURE'"
        ),
        "DELETE FROM research_experiment_outcome_events",
        (
            "UPDATE research_memory_snapshots "
            f"SET snapshot_hash='{'f' * 64}' "
            f"WHERE snapshot_id='{snapshot.snapshot_id}'"
        ),
    )
    for statement in attempts:
        with (
            engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError, match="append-only"),
        ):
            connection.execute(text(statement))
