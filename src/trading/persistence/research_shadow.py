from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import Fill, LedgerEntry, OrderIntent, model_payload
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.q1 import OrderEventType
from trading.domain.time import require_aware_utc
from trading.ledger.journal import portfolio_opening_entry
from trading.ledger.nav import calculate_nav
from trading.persistence.models import (
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
    ResearchShadowArmRegistrationRow,
    RunRow,
    ShadowArmRow,
)
from trading.research.shadow import ShadowExecutionContract
from trading.research.shadow_runtime import (
    SHADOW_RUNTIME_VERSION,
    MatchedQuoteBundleV1,
    MatchedShadowCycleResultV1,
    MatchedShadowPerformanceSummaryV1,
    ShadowArmCycleResultV1,
    ShadowArmRole,
    ShadowArmStateV1,
    ShadowPairRuntimeSpecV1,
    ShadowPaperParametersV1,
    ShadowStrategyBindingV1,
    ShadowTargetDecisionV1,
    build_initial_shadow_state,
    build_shadow_pair_runtime_spec,
    execute_matched_shadow_cycle,
    summarize_matched_shadow_results,
)


class ResearchShadowPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ResearchShadowInitialization:
    spec: ShadowPairRuntimeSpecV1
    created: bool


class ResearchShadowRuntimeRepository:
    """Durable append-only adapter over the existing generic paper tables."""

    real_order_routing = False

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def initialize_from_lifecycle(
        self,
        *,
        challenger_id: str,
        champion_artifact_hash: str,
        paper_parameters: ShadowPaperParametersV1,
        code_version: str,
        created_at: datetime,
    ) -> ResearchShadowInitialization:
        timestamp = require_aware_utc(created_at)
        with self._session_factory.begin() as session:
            manifest = session.get(ChallengerManifestRow, challenger_id)
            if manifest is None:
                raise ResearchShadowPersistenceError("unknown Research Challenger")
            registrations = list(
                session.scalars(
                    select(ResearchShadowArmRegistrationRow)
                    .where(
                        ResearchShadowArmRegistrationRow.challenger_id
                        == challenger_id
                    )
                    .order_by(ResearchShadowArmRegistrationRow.arm_role)
                )
            )
            if len(registrations) != 2:
                raise ResearchShadowPersistenceError(
                    "Research Lifecycle must register exactly two shadow arms"
                )
            by_role = {row.arm_role: row for row in registrations}
            if set(by_role) != {"CHAMPION", "CHALLENGER"}:
                raise ResearchShadowPersistenceError(
                    "Research shadow pair requires Champion and Challenger roles"
                )
            if any(row.real_order_routing for row in registrations):
                raise ResearchShadowPersistenceError(
                    "real broker routing is unavailable"
                )
            pair_ids = {row.shadow_pair_id for row in registrations}
            contract_hashes = {
                row.execution_contract_hash for row in registrations
            }
            if len(pair_ids) != 1 or len(contract_hashes) != 1:
                raise ResearchShadowPersistenceError(
                    "Research shadow pair is not matched"
                )
            self._require_lifecycle_shadow_start(
                session,
                challenger_id=challenger_id,
            )
            champion_row = by_role["CHAMPION"]
            challenger_row = by_role["CHALLENGER"]
            if len(champion_row.arm_id) > 30 or len(challenger_row.arm_id) > 30:
                raise ResearchShadowPersistenceError(
                    "generic paper table arm_id limit is 30 characters"
                )
            execution_contract = _execution_contract_from_registration(
                champion_row.payload_json
            )
            challenger_contract = _execution_contract_from_registration(
                challenger_row.payload_json
            )
            if execution_contract != challenger_contract:
                raise ResearchShadowPersistenceError(
                    "registered shadow execution contracts differ"
                )
            if canonical_hash(_execution_contract_payload(execution_contract)) not in (
                contract_hashes
            ):
                raise ResearchShadowPersistenceError(
                    "registered execution contract hash mismatch"
                )
            oos = session.get(OosLockboxResultRow, challenger_row.oos_result_id)
            if oos is None or oos.challenger_id != challenger_id:
                raise ResearchShadowPersistenceError(
                    "Challenger shadow registration lacks its OOS artifact"
                )
            champion = ShadowStrategyBindingV1(
                role=ShadowArmRole.CHAMPION,
                arm_id=champion_row.arm_id,
                strategy_id=champion_row.strategy_id,
                strategy_version=champion_row.strategy_version,
                artifact_hash=champion_artifact_hash,
            )
            challenger = ShadowStrategyBindingV1(
                role=ShadowArmRole.CHALLENGER,
                arm_id=challenger_row.arm_id,
                strategy_id=challenger_row.strategy_id,
                strategy_version=challenger_row.strategy_version,
                artifact_hash=oos.candidate_artifact_hash,
            )
            spec = build_shadow_pair_runtime_spec(
                shadow_pair_id=next(iter(pair_ids)),
                challenger_id=challenger_id,
                champion=champion,
                challenger=challenger,
                execution_contract=execution_contract,
                paper_parameters=paper_parameters,
                code_version=code_version,
                created_at=timestamp,
            )
            existing_run = session.get(RunRow, spec.run_id)
            if existing_run is not None:
                existing_spec = _spec_from_run(existing_run)
                if not _same_runtime_binding(existing_spec, spec):
                    raise ResearchShadowPersistenceError(
                        "shadow initialization idempotency conflict"
                    )
                return ResearchShadowInitialization(
                    spec=existing_spec,
                    created=False,
                )
            session.add(
                RunRow(
                    run_id=spec.run_id,
                    mode="PAPER",
                    experiment_version=SHADOW_RUNTIME_VERSION,
                    config_manifest_hash=spec.runtime_contract_hash,
                    code_commit=code_version,
                    started_at=timestamp,
                    ended_at=None,
                    status="RUNNING",
                    result_manifest=cast(
                        dict[str, Any],
                        canonical_data(spec.model_dump(mode="python")),
                    ),
                    result_hash=spec.spec_hash,
                )
            )
            session.flush()
            for role in (ShadowArmRole.CHAMPION, ShadowArmRole.CHALLENGER):
                self._persist_initial_arm(
                    session,
                    spec=spec,
                    role=role,
                )
            return ResearchShadowInitialization(spec=spec, created=True)

    def append_matched_cycle(
        self,
        *,
        run_id: str,
        champion_target: ShadowTargetDecisionV1,
        challenger_target: ShadowTargetDecisionV1,
        quote_bundle: MatchedQuoteBundleV1,
    ) -> MatchedShadowCycleResultV1:
        cycle_event_id = stable_id(
            "research-shadow-cycle",
            run_id,
            champion_target.decision_time,
        )
        with self._session_factory.begin() as session:
            run = self._run_for_update(session, run_id)
            spec = _spec_from_run(run)
            if run.experiment_version != SHADOW_RUNTIME_VERSION:
                raise ResearchShadowPersistenceError(
                    "run is not a Research shadow runtime"
                )
            existing = session.get(DomainEventRow, cycle_event_id)
            if existing is not None:
                result = MatchedShadowCycleResultV1.model_validate(
                    existing.payload_json
                )
                if (
                    result.champion.target.target_hash
                    != champion_target.target_hash
                    or result.challenger.target.target_hash
                    != challenger_target.target_hash
                    or result.quote_bundle.bundle_hash
                    != quote_bundle.bundle_hash
                ):
                    raise ResearchShadowPersistenceError(
                        "shadow cycle idempotency binding mismatch"
                    )
                return result
            self._require_lifecycle_shadow_start(
                session,
                challenger_id=spec.challenger_id,
            )
            self._lock_pair_arms(session, spec)
            champion_state = self._latest_state(
                session,
                run_id=run_id,
                arm_id=spec.champion.arm_id,
            )
            challenger_state = self._latest_state(
                session,
                run_id=run_id,
                arm_id=spec.challenger.arm_id,
            )
            result = execute_matched_shadow_cycle(
                spec=spec,
                champion_state=champion_state,
                challenger_state=challenger_state,
                champion_target=champion_target,
                challenger_target=challenger_target,
                quote_bundle=quote_bundle,
            )
            for arm_result in (result.champion, result.challenger):
                self._persist_arm_cycle(
                    session,
                    spec=spec,
                    arm_result=arm_result,
                    quote_bundle=quote_bundle,
                )
            session.add(
                DomainEventRow(
                    event_id=cycle_event_id,
                    aggregate_type="RESEARCH_MATCHED_SHADOW_CYCLE",
                    aggregate_id=spec.run_id,
                    event_type="MATCHED_PAPER_CYCLE_COMMITTED",
                    event_version="v1",
                    occurred_at=quote_bundle.as_of,
                    available_at=quote_bundle.as_of,
                    payload_json=cast(
                        dict[str, Any],
                        canonical_data(result.model_dump(mode="python")),
                    ),
                    payload_hash=result.result_hash,
                    causation_id=None,
                    correlation_id=spec.shadow_pair_id,
                    idempotency_key=stable_id(
                        "research-shadow-cycle-idempotency",
                        run_id,
                        champion_target.decision_time,
                    ),
                    created_at=quote_bundle.as_of,
                )
            )
            return result

    def deterministic_replay_hash(self, run_id: str) -> str:
        spec = self.load_spec(run_id)
        stored = self.cycle_results(run_id)
        champion_state = build_initial_shadow_state(
            spec=spec,
            role=ShadowArmRole.CHAMPION,
        )
        challenger_state = build_initial_shadow_state(
            spec=spec,
            role=ShadowArmRole.CHALLENGER,
        )
        replayed_hashes: list[str] = []
        for expected in stored:
            actual = execute_matched_shadow_cycle(
                spec=spec,
                champion_state=champion_state,
                challenger_state=challenger_state,
                champion_target=expected.champion.target,
                challenger_target=expected.challenger.target,
                quote_bundle=expected.quote_bundle,
            )
            if actual.result_hash != expected.result_hash:
                raise ResearchShadowPersistenceError(
                    "stored shadow cycle failed deterministic replay"
                )
            replayed_hashes.append(actual.result_hash)
            champion_state = actual.champion.next_state
            challenger_state = actual.challenger.next_state
        return canonical_hash(
            {
                "schema_version": "research_shadow_replay_v1",
                "run_id": run_id,
                "spec_hash": spec.spec_hash,
                "cycle_hashes": replayed_hashes,
            }
        )

    def performance_summary(
        self,
        run_id: str,
    ) -> MatchedShadowPerformanceSummaryV1:
        spec = self.load_spec(run_id)
        results = self.cycle_results(run_id)
        replay_hash = self.deterministic_replay_hash(run_id)
        return summarize_matched_shadow_results(
            spec=spec,
            results=results,
            replay_hash=replay_hash,
        )

    def load_spec(self, run_id: str) -> ShadowPairRuntimeSpecV1:
        with self._session_factory() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise ResearchShadowPersistenceError("unknown shadow run")
            return _spec_from_run(run)

    def cycle_results(
        self,
        run_id: str,
    ) -> tuple[MatchedShadowCycleResultV1, ...]:
        spec = self.load_spec(run_id)
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(DomainEventRow)
                    .where(
                        DomainEventRow.aggregate_type
                        == "RESEARCH_MATCHED_SHADOW_CYCLE",
                        DomainEventRow.aggregate_id == spec.run_id,
                    )
                    .order_by(
                        DomainEventRow.occurred_at,
                        DomainEventRow.event_id,
                    )
                )
            )
            results = tuple(
                MatchedShadowCycleResultV1.model_validate(row.payload_json)
                for row in rows
            )
            if any(result.run_id != run_id for result in results):
                raise ResearchShadowPersistenceError(
                    "shadow event belongs to another run"
                )
            return results

    def _persist_initial_arm(
        self,
        session: Session,
        *,
        spec: ShadowPairRuntimeSpecV1,
        role: ShadowArmRole,
    ) -> None:
        binding = spec.binding_for(role)
        state = build_initial_shadow_state(spec=spec, role=role)
        session.add(
            ShadowArmRow(
                arm_instance_id=stable_id(
                    "research-shadow-arm-instance",
                    spec.run_id,
                    binding.arm_id,
                ),
                run_id=spec.run_id,
                arm_id=binding.arm_id,
                created_at=spec.created_at,
            )
        )
        session.add(_state_row(state))
        opening = portfolio_opening_entry(
            arm_id=binding.arm_id,
            source_id=stable_id(
                "research-shadow-opening",
                spec.run_id,
                binding.arm_id,
            ),
            cash_usd=spec.starting_capital_usd,
            positions={},
            prices={},
            effective_at=spec.created_at,
        )
        _persist_ledger_entry(
            session,
            run_id=spec.run_id,
            entry=opening,
        )
        nav = calculate_nav(
            arm_id=binding.arm_id,
            as_of=spec.created_at,
            cash_usd=spec.starting_capital_usd,
            positions={},
            prices={},
        )
        session.add(
            NavSnapshotRow(
                nav_snapshot_id=nav.nav_snapshot_id,
                run_id=spec.run_id,
                arm_id=binding.arm_id,
                source_cycle_id=None,
                quote_manifest_hash=spec.market_input_manifest_hash,
                algorithm_version=SHADOW_RUNTIME_VERSION,
                config_manifest_hash=spec.runtime_contract_hash,
                code_version=spec.code_version,
                model_version=binding.strategy_version,
                source_manifest_hash=spec.market_input_manifest_hash,
                as_of=nav.as_of,
                nav_usd=nav.nav_usd,
                payload_json=model_payload(nav),
            )
        )

    def _persist_arm_cycle(
        self,
        session: Session,
        *,
        spec: ShadowPairRuntimeSpecV1,
        arm_result: ShadowArmCycleResultV1,
        quote_bundle: MatchedQuoteBundleV1,
    ) -> None:
        target = arm_result.target
        decision_id = stable_id(
            "research-shadow-decision",
            target.target_hash,
        )
        session.add(
            PortfolioDecisionRow(
                portfolio_decision_id=decision_id,
                run_id=spec.run_id,
                arm_id=target.arm_id,
                source_cycle_id=None,
                input_state_sequence=arm_result.next_state.sequence - 1,
                decision_time=target.decision_time,
                algorithm_version=SHADOW_RUNTIME_VERSION,
                scheduled_at=target.decision_time,
                signal_data_cutoff=target.signal_data_cutoff,
                portfolio_state_as_of=target.decision_time,
                quote_as_of=quote_bundle.as_of,
                decision_created_at=target.decision_time,
                valid_until=target.valid_until,
                calendar_session_id=None,
                config_manifest_hash=spec.runtime_contract_hash,
                code_version=spec.code_version,
                model_version=target.strategy_version,
                source_manifest_hash=target.market_input_manifest_hash,
                input_manifest_hash=quote_bundle.bundle_hash,
                payload_json=cast(
                    dict[str, Any],
                    canonical_data(target.model_dump(mode="python")),
                ),
                decision_hash=target.target_hash,
            )
        )
        fill_by_order = {fill.order_intent_id: fill for fill in arm_result.fills}
        cost_by_fill = {item.fill_id: item for item in arm_result.fill_costs}
        quotes = quote_bundle.quote_map()
        for order in arm_result.orders:
            quote = quotes[order.symbol]
            spread_bps = (
                (quote.ask_price - quote.bid_price)
                / quote.midpoint
                * spec.paper_parameters.basis_points_per_unit_return
            )
            session.add(
                OrderIntentRow(
                    order_intent_id=order.order_intent_id,
                    run_id=spec.run_id,
                    arm_id=target.arm_id,
                    source_cycle_id=None,
                    input_state_sequence=arm_result.next_state.sequence - 1,
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    created_at=order.created_at,
                    valid_until=target.valid_until,
                    decision_quote_id=quote.quote_id,
                    decision_reference_price=quote.midpoint,
                    algorithm_version=SHADOW_RUNTIME_VERSION,
                    config_manifest_hash=spec.runtime_contract_hash,
                    code_version=spec.code_version,
                    model_version=target.strategy_version,
                    source_manifest_hash=target.market_input_manifest_hash,
                    decision_spread_bps=spread_bps,
                    idempotency_key=order.idempotency_key,
                    payload_json=model_payload(order),
                    intent_hash=canonical_hash(order),
                )
            )
        session.flush()
        for fill in arm_result.fills:
            quote = quotes[fill.symbol]
            cost = cost_by_fill[fill.fill_id]
            session.add(
                FillRow(
                    fill_id=fill.fill_id,
                    order_intent_id=fill.order_intent_id,
                    run_id=spec.run_id,
                    arm_id=target.arm_id,
                    source_cycle_id=None,
                    quote_id=quote.quote_id,
                    quote_event_time=quote.event_time,
                    quote_available_at=quote.available_at,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    quantity=fill.quantity,
                    price=fill.price,
                    commission_usd=fill.commission_usd,
                    execution_scenario_id=fill.execution_scenario_id,
                    fill_hash=canonical_hash(fill),
                    algorithm_version=SHADOW_RUNTIME_VERSION,
                    config_manifest_hash=spec.runtime_contract_hash,
                    code_version=spec.code_version,
                    model_version=target.strategy_version,
                    source_manifest_hash=target.market_input_manifest_hash,
                    base_fill_cost_usd=cost.base_execution_cost_usd,
                    sensitivity_5bp_cost_usd=cost.sensitivity_5bp_cost_usd,
                    sensitivity_10bp_cost_usd=cost.sensitivity_10bp_cost_usd,
                    effective_at=fill.effective_at,
                    payload_json=model_payload(fill),
                )
            )
        session.flush()
        for order in arm_result.orders:
            self._persist_order_events(
                session,
                spec=spec,
                target=target,
                order=order,
                fill=fill_by_order.get(order.order_intent_id),
                quote_bundle=quote_bundle,
            )
        for entry in arm_result.ledger_entries:
            _persist_ledger_entry(session, run_id=spec.run_id, entry=entry)
        nav = arm_result.nav
        session.add(
            NavSnapshotRow(
                nav_snapshot_id=nav.nav_snapshot_id,
                run_id=spec.run_id,
                arm_id=target.arm_id,
                source_cycle_id=None,
                quote_manifest_hash=quote_bundle.quote_manifest_hash,
                algorithm_version=SHADOW_RUNTIME_VERSION,
                config_manifest_hash=spec.runtime_contract_hash,
                code_version=spec.code_version,
                model_version=target.strategy_version,
                source_manifest_hash=target.market_input_manifest_hash,
                as_of=nav.as_of,
                nav_usd=nav.nav_usd,
                payload_json=model_payload(nav),
            )
        )
        session.add(_state_row(arm_result.next_state))

    def _persist_order_events(
        self,
        session: Session,
        *,
        spec: ShadowPairRuntimeSpecV1,
        target: ShadowTargetDecisionV1,
        order: OrderIntent,
        fill: Fill | None,
        quote_bundle: MatchedQuoteBundleV1,
    ) -> None:
        remaining = order.quantity
        cumulative = Decimal("0")
        commission = Decimal("0")
        events: list[
            tuple[OrderEventType, Decimal, Decimal, str | None, str | None]
        ] = [
            (
                OrderEventType.CREATED,
                Decimal("0"),
                Decimal("0"),
                None,
                "HOST_VALIDATED_TARGET",
            )
        ]
        if fill is not None:
            fill_type = (
                OrderEventType.FILLED
                if fill.quantity == order.quantity
                else OrderEventType.PARTIALLY_FILLED
            )
            events.append(
                (
                    fill_type,
                    fill.quantity,
                    fill.commission_usd,
                    quote_bundle.quote_map()[order.symbol].quote_id,
                    "CONSERVATIVE_MATCHED_PAPER_FILL",
                )
            )
            if fill.quantity < order.quantity:
                events.append(
                    (
                        OrderEventType.EXPIRED,
                        Decimal("0"),
                        Decimal("0"),
                        None,
                        "UNFILLED_RESIDUAL_EXPIRED",
                    )
                )
        else:
            events.append(
                (
                    OrderEventType.EXPIRED,
                    Decimal("0"),
                    Decimal("0"),
                    None,
                    "LIQUIDITY_OR_CASH_CAP_PREVENTED_FILL",
                )
            )
        for sequence, (
            event_type,
            quantity_delta,
            commission_delta,
            quote_id,
            reason,
        ) in enumerate(events, start=1):
            remaining -= quantity_delta
            cumulative += quantity_delta
            commission += commission_delta
            payload = {
                "schema_version": "research_shadow_order_event_v1",
                "order_intent_id": order.order_intent_id,
                "event_sequence": sequence,
                "event_type": event_type.value,
                "quantity_delta": quantity_delta,
                "commission_delta_usd": commission_delta,
                "remaining_quantity": remaining,
                "cumulative_filled_quantity": cumulative,
                "cumulative_commission_usd": commission,
                "quote_id": quote_id,
                "occurred_at": quote_bundle.as_of,
                "reason": reason,
                "target_hash": target.target_hash,
                "runtime_contract_hash": spec.runtime_contract_hash,
            }
            event_id = stable_id(
                "research-shadow-order-event",
                order.order_intent_id,
                sequence,
            )
            session.add(
                OrderEventRow(
                    order_event_id=event_id,
                    order_intent_id=order.order_intent_id,
                    run_id=spec.run_id,
                    arm_id=target.arm_id,
                    source_cycle_id=None,
                    algorithm_version=SHADOW_RUNTIME_VERSION,
                    event_sequence=sequence,
                    event_type=event_type.value,
                    quantity_delta=quantity_delta,
                    commission_delta_usd=commission_delta,
                    remaining_quantity=remaining,
                    cumulative_filled_quantity=cumulative,
                    cumulative_commission_usd=commission,
                    quote_id=quote_id,
                    occurred_at=quote_bundle.as_of,
                    available_at=quote_bundle.as_of,
                    reason=reason,
                    source_id=target.target_id,
                    worker_fence_token=stable_id(
                        "research-shadow-host-fence",
                        spec.run_id,
                    ),
                    cycle_attempt_count=1,
                    idempotency_key=stable_id(
                        "research-shadow-order-event-idempotency",
                        event_id,
                    ),
                    config_manifest_hash=spec.runtime_contract_hash,
                    code_version=spec.code_version,
                    model_version=target.strategy_version,
                    source_manifest_hash=target.market_input_manifest_hash,
                    event_hash=canonical_hash(payload),
                    payload_json=cast(
                        dict[str, Any],
                        canonical_data(payload),
                    ),
                    created_at=quote_bundle.as_of,
                )
            )

    @staticmethod
    def _require_lifecycle_shadow_start(
        session: Session,
        *,
        challenger_id: str,
    ) -> None:
        start_event = session.scalar(
            select(ChallengerEventRow)
            .where(
                ChallengerEventRow.challenger_id == challenger_id,
                ChallengerEventRow.to_status == "SHADOW_RUNNING",
                ChallengerEventRow.reason_code == "EXPLICIT_SHADOW_START",
            )
            .order_by(desc(ChallengerEventRow.sequence))
            .limit(1)
        )
        if start_event is None:
            raise ResearchShadowPersistenceError(
                "ResearchLifecycle.start_shadow must run before paper runtime"
            )
        latest = session.scalar(
            select(ChallengerEventRow)
            .where(ChallengerEventRow.challenger_id == challenger_id)
            .order_by(desc(ChallengerEventRow.sequence))
            .limit(1)
        )
        if latest is None or latest.to_status not in {
            "SHADOW_RUNNING",
            "PROMOTION_ELIGIBLE",
        }:
            raise ResearchShadowPersistenceError(
                "Research Lifecycle no longer permits shadow execution"
            )

    @staticmethod
    def _run_for_update(session: Session, run_id: str) -> RunRow:
        statement = select(RunRow).where(RunRow.run_id == run_id)
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        run = session.scalar(statement)
        if run is None:
            raise ResearchShadowPersistenceError("unknown shadow run")
        return run

    @staticmethod
    def _lock_pair_arms(
        session: Session,
        spec: ShadowPairRuntimeSpecV1,
    ) -> None:
        statement = (
            select(ShadowArmRow)
            .where(
                ShadowArmRow.run_id == spec.run_id,
                ShadowArmRow.arm_id.in_(
                    (spec.champion.arm_id, spec.challenger.arm_id)
                ),
            )
            .order_by(ShadowArmRow.arm_id)
        )
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            statement = statement.with_for_update()
        rows = list(session.scalars(statement))
        if len(rows) != 2:
            raise ResearchShadowPersistenceError(
                "matched shadow run lost an independent arm"
            )

    @staticmethod
    def _latest_state(
        session: Session,
        *,
        run_id: str,
        arm_id: str,
    ) -> ShadowArmStateV1:
        row = session.scalar(
            select(ArmStateSnapshotRow)
            .where(
                ArmStateSnapshotRow.run_id == run_id,
                ArmStateSnapshotRow.arm_id == arm_id,
            )
            .order_by(
                desc(ArmStateSnapshotRow.sequence),
                desc(ArmStateSnapshotRow.created_at),
            )
            .limit(1)
        )
        if row is None:
            raise ResearchShadowPersistenceError("shadow arm state is missing")
        state = ShadowArmStateV1.model_validate(row.payload_json)
        if state.state_hash != row.state_hash:
            raise ResearchShadowPersistenceError("shadow state row hash mismatch")
        return state


def _execution_contract_from_registration(
    payload: dict[str, Any],
) -> ShadowExecutionContract:
    raw = payload.get("execution_contract")
    if not isinstance(raw, dict):
        raise ResearchShadowPersistenceError(
            "shadow registration lacks execution contract"
        )
    untyped = cast(dict[object, object], raw)
    required: set[str] = {
        "market_input_manifest_hash",
        "decision_schedule_version",
        "execution_scenario_version",
        "cost_model_version",
        "starting_capital_usd",
        "liquidity_policy_version",
    }
    if set(untyped) != required or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in untyped.items()
    ):
        raise ResearchShadowPersistenceError(
            "shadow registration execution contract is invalid"
        )
    typed = cast(dict[str, str], untyped)
    return ShadowExecutionContract(**typed)


def _execution_contract_payload(
    contract: ShadowExecutionContract,
) -> dict[str, str]:
    return {
        "market_input_manifest_hash": contract.market_input_manifest_hash,
        "decision_schedule_version": contract.decision_schedule_version,
        "execution_scenario_version": contract.execution_scenario_version,
        "cost_model_version": contract.cost_model_version,
        "starting_capital_usd": contract.starting_capital_usd,
        "liquidity_policy_version": contract.liquidity_policy_version,
    }


def _spec_from_run(run: RunRow) -> ShadowPairRuntimeSpecV1:
    if run.result_manifest is None:
        raise ResearchShadowPersistenceError("shadow run lacks immutable spec")
    spec = ShadowPairRuntimeSpecV1.model_validate(run.result_manifest)
    if (
        run.result_hash != spec.spec_hash
        or run.config_manifest_hash != spec.runtime_contract_hash
        or run.code_commit != spec.code_version
    ):
        raise ResearchShadowPersistenceError("shadow run spec binding mismatch")
    return spec


def _same_runtime_binding(
    existing: ShadowPairRuntimeSpecV1,
    requested: ShadowPairRuntimeSpecV1,
) -> bool:
    return (
        existing.run_id == requested.run_id
        and existing.shadow_pair_id == requested.shadow_pair_id
        and existing.challenger_id == requested.challenger_id
        and existing.champion == requested.champion
        and existing.challenger == requested.challenger
        and existing.execution_contract == requested.execution_contract
        and existing.execution_contract_hash
        == requested.execution_contract_hash
        and existing.paper_parameters == requested.paper_parameters
        and existing.runtime_contract_hash == requested.runtime_contract_hash
        and existing.code_version == requested.code_version
        and not existing.real_order_routing
        and not requested.real_order_routing
    )


def _state_row(state: ShadowArmStateV1) -> ArmStateSnapshotRow:
    return ArmStateSnapshotRow(
        arm_state_snapshot_id=stable_id(
            "research-shadow-state",
            state.run_id,
            state.arm_id,
            state.sequence,
            state.state_hash,
        ),
        run_id=state.run_id,
        arm_id=state.arm_id,
        sequence=state.sequence,
        source_cycle_id=None,
        state_hash=state.state_hash,
        payload_json=cast(
            dict[str, Any],
            canonical_data(state.model_dump(mode="python")),
        ),
        created_at=state.as_of,
    )


def _persist_ledger_entry(
    session: Session,
    *,
    run_id: str,
    entry: LedgerEntry,
) -> None:
    transaction = entry.transaction
    session.add(
        LedgerTransactionRow(
            ledger_transaction_id=transaction.ledger_transaction_id,
            run_id=run_id,
            arm_id=transaction.arm_id,
            source_id=transaction.source_id,
            effective_at=transaction.effective_at,
            payload_json=model_payload(transaction),
        )
    )
    session.flush()
    for posting in entry.postings:
        session.add(
            LedgerPostingRow(
                posting_id=posting.posting_id,
                ledger_transaction_id=transaction.ledger_transaction_id,
                account_code=posting.account_code,
                asset_code=posting.asset_code,
                quantity_delta=posting.quantity_delta,
                usd_value_delta=posting.usd_value_delta,
                payload_json=model_payload(posting),
            )
        )
