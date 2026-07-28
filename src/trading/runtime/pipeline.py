from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.synthetic import SyntheticScenario, build_demo_scenario
from trading.domain.contracts import FeatureSnapshot, model_payload
from trading.domain.events import make_domain_event
from trading.domain.hashing import canonical_hash, canonical_json, stable_id
from trading.experiments.arms import ArmState
from trading.persistence.inbox import ProcessedEventInbox
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FeatureSnapshotRow,
    FillRow,
    LedgerPostingRow,
    LedgerTransactionRow,
    NavSnapshotRow,
    NewsEventRow,
    OrderIntentRow,
    PolicyPatchRow,
    PolicyVersionRow,
    PortfolioDecisionRow,
    RiskDecisionRow,
    RunRow,
    ShadowArmRow,
    StrategyForecastRow,
)
from trading.persistence.outbox import Outbox
from trading.persistence.repositories import DomainEventRepository, SourceRecordRepository
from trading.runtime.provenance import workspace_code_version
from trading.runtime.simulation import SimulationArtifacts, simulate_scenario
from trading.settings import ConfigBundle, Settings


class SeedError(RuntimeError):
    pass


class IdempotentFeatureConsumer:
    name = "phase0_feature_fixture_consumer_v1"

    def consume(
        self,
        session: Session,
        *,
        event_id: str,
        features: list[FeatureSnapshot],
        processed_at: datetime,
    ) -> bool:
        inbox = ProcessedEventInbox(session, self.name)
        if inbox.seen(event_id):
            return False
        result_hash = canonical_hash(features)
        if not inbox.record(event_id, processed_at, result_hash):
            return False
        for feature in features:
            session.add(
                FeatureSnapshotRow(
                    feature_snapshot_id=feature.feature_snapshot_id,
                    symbol=feature.symbol,
                    decision_time=feature.decision_time,
                    data_available_cutoff=feature.data_available_cutoff,
                    input_manifest_hash=feature.input_manifest_hash,
                    payload_json=model_payload(feature),
                )
            )
        return True


def seed_demo(
    *,
    settings: Settings,
    config: ConfigBundle,
    session_factory: sessionmaker[Session],
) -> tuple[dict[str, Any], str, bool]:
    scenario = build_demo_scenario()
    with session_factory() as session:
        existing = session.get(RunRow, scenario.run_id)
        if existing is not None:
            if (
                existing.status == "COMPLETED"
                and existing.result_manifest is not None
                and existing.result_hash is not None
            ):
                return existing.result_manifest, existing.result_hash, False
            raise SeedError(
                f"Run {scenario.run_id} exists with incomplete status {existing.status}; "
                "use an isolated fresh database"
            )

    artifacts = simulate_scenario(
        scenario,
        config_manifest_hash=config.manifest_hash,
        code_version=workspace_code_version(settings.config_dir.parent),
    )
    _write_raw_scenario(settings.raw_store, scenario)
    source_payload = scenario.model_dump(mode="json")
    event = make_domain_event(
        aggregate_type="SourceRecord",
        aggregate_id=artifacts.source_record.source_id,
        event_type="SourceRecordIngested",
        occurred_at=scenario.started_at,
        available_at=scenario.started_at,
        payload={
            "source_id": artifacts.source_record.source_id,
            "run_id": scenario.run_id,
            "scenario_hash": canonical_hash(scenario),
        },
        correlation_id=scenario.run_id,
    )

    with session_factory.begin() as session:
        session.add(
            RunRow(
                run_id=scenario.run_id,
                mode="PAPER",
                experiment_version="phase0_demo_v1",
                config_manifest_hash=config.manifest_hash,
                code_commit=str(artifacts.manifest["code_version"]),
                started_at=scenario.started_at,
                ended_at=None,
                status="STARTED",
                result_manifest=None,
                result_hash=None,
            )
        )
        SourceRecordRepository(session).add(
            artifacts.source_record,
            payload=source_payload,
        )
        DomainEventRepository(session).add_with_outbox(event, "source-records")

    consumer = IdempotentFeatureConsumer()
    with session_factory.begin() as session:
        pending = Outbox(session).pending(as_of=scenario.decision_time)
        if len(pending) != 1:
            raise SeedError(f"Expected one pending outbox event after restart, got {len(pending)}")
        applied = consumer.consume(
            session,
            event_id=pending[0].event_id,
            features=artifacts.feature_snapshots,
            processed_at=scenario.decision_time,
        )
        if not applied:
            raise SeedError("First event delivery was unexpectedly ignored")
        Outbox(session).mark_published(pending[0], scenario.decision_time)

    with session_factory.begin() as session:
        duplicate_applied = consumer.consume(
            session,
            event_id=event.event_id,
            features=artifacts.feature_snapshots,
            processed_at=scenario.decision_time,
        )
        if duplicate_applied:
            raise SeedError("Duplicate event changed economic state")

    with session_factory.begin() as session:
        _persist_artifacts(session, artifacts)
        run = session.get(RunRow, scenario.run_id)
        if run is None:
            raise SeedError("Run disappeared before completion")
        run.status = "COMPLETED"
        run.ended_at = max(
            arm.nav_snapshot.as_of for arm in artifacts.arms.values()
        )
        run.result_manifest = artifacts.manifest
        run.result_hash = artifacts.result_hash

    return artifacts.manifest, artifacts.result_hash, True


def load_scenario_for_run(session: Session, run_id: str) -> SyntheticScenario:
    source = SourceRecordRepository(session).get_for_run(run_id)
    if source is None:
        raise SeedError(f"No source scenario found for run {run_id}")
    content = source.payload_json.get("content")
    if not isinstance(content, dict):
        raise SeedError("Stored source scenario payload is invalid")
    return SyntheticScenario.model_validate(content)


def load_arm_states(session: Session, run_id: str) -> dict[str, ArmState]:
    rows = list(
        session.scalars(
            select(ArmStateSnapshotRow)
            .where(ArmStateSnapshotRow.run_id == run_id)
            .order_by(ArmStateSnapshotRow.arm_id, ArmStateSnapshotRow.sequence.desc())
        )
    )
    states: dict[str, ArmState] = {}
    for row in rows:
        if row.arm_id not in states:
            states[row.arm_id] = ArmState.from_payload(row.payload_json)
    return states


def _write_raw_scenario(raw_store: Path, scenario: SyntheticScenario) -> None:
    raw_store.mkdir(parents=True, exist_ok=True)
    path = raw_store / f"{scenario.run_id}.json"
    content = canonical_json(scenario) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != content:
            raise SeedError(f"Immutable raw scenario differs from existing file: {path}")
        return
    path.write_text(content, encoding="utf-8", newline="\n")


def _persist_artifacts(session: Session, artifacts: SimulationArtifacts) -> None:
    run_id = artifacts.scenario.run_id
    for forecast in artifacts.forecasts:
        feature_manifest_hash = canonical_hash(forecast.feature_snapshot_ids)
        session.add(
            StrategyForecastRow(
                forecast_id=forecast.forecast_id,
                strategy_id=forecast.strategy_id,
                strategy_version=forecast.strategy_version,
                experiment_version=forecast.experiment_version,
                decision_time=forecast.decision_time,
                horizon=forecast.horizon.value,
                input_manifest_hash=feature_manifest_hash,
                payload_json=model_payload(forecast),
            )
        )
    session.add(
        NewsEventRow(
            news_event_id=artifacts.news_event.news_event_id,
            as_of=artifacts.news_event.as_of,
            payload_json=model_payload(artifacts.news_event),
            output_hash=artifacts.news_event.output_hash,
        )
    )
    session.add(
        PolicyPatchRow(
            patch_id=artifacts.policy_patch.patch_id,
            scope_id="legacy_global",
            arm_scope=artifacts.policy_patch.arm_scope,
            base_policy_version=artifacts.policy_patch.base_policy_version,
            effective_from=artifacts.policy_patch.effective_from,
            expires_at=artifacts.policy_patch.expires_at,
            payload_json=model_payload(artifacts.policy_patch),
        )
    )

    for arm_id, arm in artifacts.arms.items():
        session.add(
            ShadowArmRow(
                arm_instance_id=stable_id("arm", run_id, arm_id),
                run_id=run_id,
                arm_id=arm_id,
                created_at=artifacts.scenario.started_at,
            )
        )
        session.add(
            PolicyVersionRow(
                policy_version_id=stable_id(
                    "policy", run_id, arm_id, arm.policy_state.version
                ),
                scope_id="legacy_global",
                arm_id=arm_id,
                version=arm.policy_state.version,
                source_patch_id=arm.policy_state.source_patch_id,
                payload_json=arm.policy_state.as_payload(),
                created_at=artifacts.scenario.decision_time,
            )
        )
        session.add(
            PortfolioDecisionRow(
                portfolio_decision_id=arm.portfolio_decision.portfolio_decision_id,
                run_id=run_id,
                arm_id=arm_id,
                decision_time=arm.portfolio_decision.decision_time,
                payload_json=model_payload(arm.portfolio_decision),
                decision_hash=canonical_hash(arm.portfolio_decision),
            )
        )
        session.flush()
        session.add(
            RiskDecisionRow(
                risk_decision_id=arm.risk_decision.risk_decision_id,
                portfolio_decision_id=arm.risk_decision.portfolio_decision_id,
                approved=arm.risk_decision.approved,
                payload_json=model_payload(arm.risk_decision),
            )
        )
        for intent in arm.order_intents:
            session.add(
                OrderIntentRow(
                    order_intent_id=intent.order_intent_id,
                    run_id=run_id,
                    arm_id=arm_id,
                    idempotency_key=intent.idempotency_key,
                    payload_json=model_payload(intent),
                    intent_hash=canonical_hash(intent),
                )
            )
        session.flush()
        for fill in arm.fills:
            session.add(
                FillRow(
                    fill_id=fill.fill_id,
                    order_intent_id=fill.order_intent_id,
                    run_id=run_id,
                    arm_id=arm_id,
                    effective_at=fill.effective_at,
                    payload_json=model_payload(fill),
                )
            )
        for entry in arm.ledger_entries:
            transaction = entry.transaction
            session.add(
                LedgerTransactionRow(
                    ledger_transaction_id=transaction.ledger_transaction_id,
                    run_id=run_id,
                    arm_id=arm_id,
                    source_id=transaction.source_id,
                    effective_at=transaction.effective_at,
                    payload_json=model_payload(transaction),
                )
            )
        session.flush()
        for entry in arm.ledger_entries:
            for posting in entry.postings:
                session.add(
                    LedgerPostingRow(
                        posting_id=posting.posting_id,
                        ledger_transaction_id=posting.ledger_transaction_id,
                        account_code=posting.account_code,
                        asset_code=posting.asset_code,
                        quantity_delta=posting.quantity_delta,
                        usd_value_delta=posting.usd_value_delta,
                        payload_json=model_payload(posting),
                    )
                )
        session.add(
            NavSnapshotRow(
                nav_snapshot_id=arm.nav_snapshot.nav_snapshot_id,
                run_id=run_id,
                arm_id=arm_id,
                as_of=arm.nav_snapshot.as_of,
                nav_usd=arm.nav_snapshot.nav_usd,
                payload_json=model_payload(arm.nav_snapshot),
            )
        )
        session.add(
            ArmStateSnapshotRow(
                arm_state_snapshot_id=stable_id(
                    "armstate", run_id, arm_id, arm.state.sequence
                ),
                run_id=run_id,
                arm_id=arm_id,
                sequence=arm.state.sequence,
                payload_json=arm.state.as_payload(),
                created_at=arm.nav_snapshot.as_of,
            )
        )


def completed_run(session: Session, run_id: str) -> RunRow | None:
    return session.scalar(
        select(RunRow).where(
            RunRow.run_id == run_id,
            RunRow.status == "COMPLETED",
        )
    )
