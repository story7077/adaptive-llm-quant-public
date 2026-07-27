from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.algorithm import LEGACY_FORWARD_ALGORITHM_VERSION
from trading.domain.contracts import (
    Fill,
    NavSnapshot,
    OrderIntent,
    PortfolioDecision,
    model_payload,
)
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.paper import PaperAccountSpec, PaperBootstrapCompletion
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.experiments.arms import ARM_IDS, ArmState
from trading.ledger.journal import portfolio_opening_entry
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    LedgerPostingRow,
    LedgerTransactionRow,
    NavSnapshotRow,
    OrderIntentRow,
    PaperBootstrapCompletionRow,
    PaperExecutionAttemptRow,
    PaperPositionRow,
    PortfolioDecisionRow,
    RunRow,
    ShadowArmRow,
)
from trading.persistence.paper import PaperBootstrapService, ProvisionedPaperAccount
from trading.runtime.provenance import workspace_code_version
from trading.settings import ConfigBundle

PAPER_ARM_IDS = (*ARM_IDS, "HOLD")
FORWARD_ORDER_ENABLED_ARMS = ("B0-CASH", "B0-QQQ", "B0-VOL", "B3-RISK")
NAV_QUANTUM = Decimal("0.0000000001")
STRATEGY_READINESS = {
    "T1": {
        "state": "BLOCKED",
        "reason_code": "PIT_SOXX_MEMBERSHIP_AND_OOS_CALIBRATION_REQUIRED",
    },
    "R1": {
        "state": "WARMING",
        "reason_code": (
            "60_COMPLETE_INTRADAY_SESSIONS_20_SAME_CLOCK_SESSIONS_"
            "AND_OOS_CALIBRATION_REQUIRED"
        ),
    },
    "X1": {
        "state": "BLOCKED",
        "reason_code": "VERSIONED_OOS_CALIBRATION_REQUIRED",
    },
}
BASELINE_READINESS = {
    "B0-CASH": {
        "state": "ORDER_READY_AFTER_T0",
        "reason_code": "VERSIONED_USD_CASH_CONTROL",
    },
    "B0-QQQ": {
        "state": "ORDER_READY_AFTER_T0",
        "reason_code": "VERSIONED_QQQ_BUY_AND_HOLD_CONTROL",
    },
    "B0-VOL": {
        "state": "ORDER_READY_AFTER_T0",
        "reason_code": "VERSIONED_20D_VOL_TARGET_CORE",
    },
    "B3-RISK": {
        "state": "ORDER_READY_AFTER_T0",
        "reason_code": "B0_VOL_CORE_WITH_LLM_RISK_REDUCTION_ONLY",
    },
}


class PaperRuntimeError(RuntimeError):
    pass


class PaperBootstrapNotReady(PaperRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PaperRunInitialization:
    run_id: str
    account_spec_id: str
    created: bool
    state: str


@dataclass(frozen=True, slots=True)
class CommonQuoteMarks:
    prices: dict[str, Decimal]
    quote_ids: dict[str, str]
    common_mark_at: datetime


class PaperRuntimeService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: ConfigBundle,
        workspace_root: Path,
        clock: Clock | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._workspace_root = workspace_root
        self._clock = clock or SystemClock()
        self._market = MarketDataRepository(session_factory)
        self._bootstrap = PaperBootstrapService(session_factory)

    def initialize(
        self,
        *,
        run_id: str,
        account_file: Path,
        now: datetime | None = None,
    ) -> PaperRunInitialization:
        instant = self._now(now)
        provisioned = self._bootstrap.provision_from_file(account_file, now=instant)
        created = False
        with self._session_factory.begin() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                run = RunRow(
                    run_id=run_id,
                    mode="PAPER",
                    experiment_version="paper_forward_v2",
                    config_manifest_hash=self._config.manifest_hash,
                    code_commit=workspace_code_version(self._workspace_root),
                    started_at=instant,
                    ended_at=None,
                    status="PENDING_BOOTSTRAP",
                    result_manifest={
                        "schema_version": "paper-run.v2",
                        "account_spec_id": provisioned.account_spec_id,
                        "performance_basis": "T0_COMMON_IEX_MARK",
                        "trading_mode": "FORWARD_BASELINE_WITH_AI_RISK",
                        "order_enabled_arms": list(FORWARD_ORDER_ENABLED_ARMS),
                        "legacy_pnl_included": False,
                        "real_order_routing": False,
                    },
                    result_hash=None,
                )
                session.add(run)
                created = True
            else:
                self._validate_existing_run(run, provisioned)
            for arm_id in PAPER_ARM_IDS:
                arm_instance_id = stable_id("arm", run_id, arm_id)
                if session.get(ShadowArmRow, arm_instance_id) is None:
                    session.add(
                        ShadowArmRow(
                            arm_instance_id=arm_instance_id,
                            run_id=run_id,
                            arm_id=arm_id,
                            created_at=instant,
                        )
                    )
        state = self.status(run_id)["state"]
        return PaperRunInitialization(
            run_id=run_id,
            account_spec_id=provisioned.account_spec_id,
            created=created,
            state=str(state),
        )

    def bootstrap_from_fresh_quotes(
        self,
        *,
        run_id: str,
        session_open_at: datetime,
        account_file: Path,
        max_quote_age_seconds: int,
        max_quote_skew_seconds: int = 20,
        now: datetime | None = None,
    ) -> PaperBootstrapCompletion:
        instant = self._now(now)
        open_at = require_aware_utc(session_open_at, "session_open_at")
        provisioned = self._bootstrap.provision_from_file(account_file, now=instant)
        existing = self._completion(run_id)
        if existing is not None:
            self._ensure_opening_state(
                run_id=run_id,
                provisioned=provisioned,
                completion=existing,
            )
            return existing
        marks = self._common_quote_marks(
            spec=provisioned.spec,
            open_at=open_at,
            as_of=instant,
            max_quote_age_seconds=max_quote_age_seconds,
            max_quote_skew_seconds=max_quote_skew_seconds,
        )
        result = self._bootstrap.complete(
            run_id=run_id,
            account_spec_id=provisioned.account_spec_id,
            prices=marks.prices,
            common_mark_at=marks.common_mark_at,
            source_kind="ALPACA_IEX_MIDPOINT",
            source_record_ids=marks.quote_ids,
            now=instant,
        )
        self._ensure_opening_state(
            run_id=run_id,
            provisioned=provisioned,
            completion=result.completion,
        )
        return result.completion

    def record_nav(
        self,
        *,
        run_id: str,
        as_of: datetime | None = None,
        snapshot_scope: str | None = None,
        max_quote_age_seconds: int = 15,
        max_quote_skew_seconds: int = 20,
    ) -> list[NavSnapshot]:
        instant = self._now(as_of)
        if max_quote_age_seconds <= 0 or max_quote_skew_seconds <= 0:
            raise ValueError("NAV quote age and skew limits must be positive")
        completion = self._completion(run_id)
        if completion is None:
            raise PaperBootstrapNotReady("Paper T0 is not established")
        states = self._latest_states(run_id, as_of=instant)
        if set(states) != set(PAPER_ARM_IDS):
            raise PaperRuntimeError("Paper arm state is incomplete")
        if snapshot_scope is not None:
            existing = self._nav_snapshots_for_scope(
                run_id=run_id,
                snapshot_scope=snapshot_scope,
            )
            if existing:
                if set(existing) != set(PAPER_ARM_IDS):
                    raise PaperRuntimeError("Scoped NAV snapshot set is incomplete")
                return [existing[arm_id] for arm_id in sorted(existing)]
        symbols = sorted(
            {
                symbol
                for state in states.values()
                for symbol, quantity in state.positions.items()
                if quantity != 0
            }
        )
        prices: dict[str, Decimal] = {}
        quote_ids: dict[str, str] = {}
        quote_times: list[datetime] = []
        for symbol in symbols:
            quote = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                as_of=instant,
            )
            if quote is None:
                raise PaperRuntimeError(f"Missing NAV quote for {symbol}")
            event_time = _aware(quote.event_time)
            age_seconds = (instant - event_time).total_seconds()
            if age_seconds < 0 or age_seconds > max_quote_age_seconds:
                raise PaperRuntimeError(f"Stale NAV quote for {symbol}")
            if (
                quote.bid_price <= 0
                or quote.ask_price <= 0
                or quote.ask_price < quote.bid_price
                or quote.bid_size_round_lots <= 0
                or quote.ask_size_round_lots <= 0
            ):
                raise PaperRuntimeError(f"Non-executable NAV quote for {symbol}")
            prices[symbol] = (quote.bid_price + quote.ask_price) / Decimal("2")
            quote_ids[symbol] = quote.quote_id
            quote_times.append(event_time)
        if (
            quote_times
            and (max(quote_times) - min(quote_times)).total_seconds()
            > max_quote_skew_seconds
        ):
            raise PaperRuntimeError("NAV quote bundle exceeds the allowed time skew")

        snapshots = [
            build_precise_nav(
                run_id=run_id,
                arm_id=arm_id,
                as_of=instant,
                cash_usd=state.cash_usd,
                positions=state.positions,
                prices=prices,
                quote_ids=quote_ids,
                snapshot_scope=snapshot_scope,
            )
            for arm_id, state in sorted(states.items())
        ]
        with self._session_factory.begin() as session:
            for snapshot in snapshots:
                if session.get(NavSnapshotRow, snapshot.nav_snapshot_id) is not None:
                    continue
                session.add(
                    NavSnapshotRow(
                        nav_snapshot_id=snapshot.nav_snapshot_id,
                        run_id=run_id,
                        arm_id=snapshot.arm_id,
                        as_of=snapshot.as_of,
                        nav_usd=snapshot.nav_usd,
                        payload_json=model_payload(snapshot),
                    )
                )
        return snapshots

    def status(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                return {
                    "run_id": run_id,
                    "state": "NOT_INITIALIZED",
                    "algorithm_version": LEGACY_FORWARD_ALGORITHM_VERSION,
                    "real_order_routing": False,
                }
            if run.experiment_version != LEGACY_FORWARD_ALGORITHM_VERSION:
                raise PaperRuntimeError(
                    f"Run {run_id!r} belongs to {run.experiment_version}"
                )
            completion_row = session.scalar(
                select(PaperBootstrapCompletionRow).where(
                    PaperBootstrapCompletionRow.run_id == run_id
                )
            )
            latest_navs = self._latest_nav_rows(session, run_id)
            hold_nav = latest_navs.get("HOLD")
            initial_nav = (
                None if completion_row is None else completion_row.initial_nav_usd
            )
            account_spec_id = (
                completion_row.account_spec_id
                if completion_row is not None
                else str((run.result_manifest or {}).get("account_spec_id", ""))
            )
            states = self._latest_states(run_id)
            activity = {
                arm_id: self._arm_activity_payload(
                    session,
                    run_id=run_id,
                    arm_id=arm_id,
                    state=states.get(arm_id),
                )
                for arm_id in PAPER_ARM_IDS
            }
            transition_complete = {
                arm_id: _forward_transition_complete(
                    arm_id,
                    states.get(arm_id),
                    activity[arm_id]["orders"],
                    activity[arm_id]["positions"],
                    activity[arm_id]["latest_target_weights"],
                )
                for arm_id in FORWARD_ORDER_ENABLED_ARMS
            }
            configured_positions = self._position_payload(
                session,
                run_id=run_id,
                account_spec_id=account_spec_id,
            )
            for arm_id in PAPER_ARM_IDS:
                if states.get(arm_id) is None:
                    activity[arm_id]["positions"] = configured_positions
            return {
                "run_id": run_id,
                "state": run.status,
                "algorithm_version": LEGACY_FORWARD_ALGORITHM_VERSION,
                "trading_mode": "FORWARD_BASELINE_WITH_AI_RISK",
                "order_generation_armed": True,
                "order_generation_enabled": run.status == "RUNNING",
                "order_enabled_arms": list(FORWARD_ORDER_ENABLED_ARMS),
                "forward_evaluation": self._config.get("forward-paper.yaml")[
                    "evaluation"
                ],
                "forward_transition": {
                    "complete_by_arm": transition_complete,
                    "matched_comparison_ready": (
                        transition_complete["B0-VOL"]
                        and transition_complete["B3-RISK"]
                    ),
                    "current_return_window": (
                        "MATCHED_COMPARISON_READY_FROM_T0_WITH_TRANSITION_HISTORY"
                        if (
                            transition_complete["B0-VOL"]
                            and transition_complete["B3-RISK"]
                        )
                        else "INHERITED_ACCOUNT_TRANSITION_FROM_T0"
                    ),
                    "ai_only_attribution_available": False,
                },
                "baseline_readiness": BASELINE_READINESS,
                "strategy_readiness": STRATEGY_READINESS,
                "performance_start_at": (
                    None
                    if completion_row is None
                    else _iso(completion_row.common_mark_at)
                ),
                "initial_nav_usd": _decimal_or_none(initial_nav),
                "hold_nav_usd": _decimal_or_none(
                    None if hold_nav is None else hold_nav.nav_usd
                ),
                "arms": {
                    arm_id: {
                        "nav_usd": _decimal_or_none(
                            None
                            if latest_navs.get(arm_id) is None
                            else latest_navs[arm_id].nav_usd
                        ),
                        "cash_usd": (
                            None
                            if latest_navs.get(arm_id) is None
                            else str(
                                latest_navs[arm_id].payload_json.get("cash_usd")
                            )
                        ),
                        "positions_market_value_usd": (
                            None
                            if latest_navs.get(arm_id) is None
                            else str(
                                latest_navs[arm_id].payload_json.get(
                                    "positions_market_value_usd"
                                )
                            )
                        ),
                        "return_since_t0": _return_string(
                            None
                            if latest_navs.get(arm_id) is None
                            else latest_navs[arm_id].nav_usd,
                            initial_nav,
                        ),
                        "active_return_vs_hold": _active_return_string(
                            None
                            if latest_navs.get(arm_id) is None
                            else latest_navs[arm_id].nav_usd,
                            None if hold_nav is None else hold_nav.nav_usd,
                            initial_nav,
                        ),
                        "order_enabled": arm_id in FORWARD_ORDER_ENABLED_ARMS,
                        "sequence": (
                            None
                            if states.get(arm_id) is None
                            else states[arm_id].sequence
                        ),
                        "positions": activity[arm_id]["positions"],
                        "orders": activity[arm_id]["orders"],
                        "fills": activity[arm_id]["fills"],
                    }
                    for arm_id in PAPER_ARM_IDS
                },
                "frozen_cash": self._frozen_cash(session, account_spec_id),
                "positions": (
                    activity["B3-RISK"]["positions"]
                    if "B3-RISK" in activity
                    else configured_positions
                ),
                "orders": activity.get("B3-RISK", {}).get("orders", []),
                "fills": activity.get("B3-RISK", {}).get("fills", []),
                "legacy_pnl_included": False,
                "real_order_routing": False,
            }

    def bounded_decision_context(
        self,
        run_id: str,
        *,
        as_of: datetime,
        arm_id: str = "B3-RISK",
    ) -> dict[str, Any]:
        instant = require_aware_utc(as_of, "as_of")
        with self._session_factory() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise PaperRuntimeError(f"Unknown paper run: {run_id}")
            state_row = session.scalar(
                select(ArmStateSnapshotRow)
                .where(
                    ArmStateSnapshotRow.run_id == run_id,
                    ArmStateSnapshotRow.arm_id == arm_id,
                    ArmStateSnapshotRow.created_at <= instant,
                )
                .order_by(desc(ArmStateSnapshotRow.sequence))
                .limit(1)
            )
            nav_row = session.scalar(
                select(NavSnapshotRow)
                .where(
                    NavSnapshotRow.run_id == run_id,
                    NavSnapshotRow.arm_id == arm_id,
                    NavSnapshotRow.as_of <= instant,
                )
                .order_by(desc(NavSnapshotRow.as_of))
                .limit(1)
            )
            order_rows = list(
                session.scalars(
                    select(OrderIntentRow).where(
                        OrderIntentRow.run_id == run_id,
                        OrderIntentRow.arm_id == arm_id,
                        OrderIntentRow.created_at <= instant,
                        OrderIntentRow.valid_until > instant,
                    )
                )
            )
            order_ids = [row.order_intent_id for row in order_rows]
            decision_rows = list(
                session.scalars(
                    select(PortfolioDecisionRow)
                    .where(
                        PortfolioDecisionRow.run_id == run_id,
                        PortfolioDecisionRow.arm_id == arm_id,
                        PortfolioDecisionRow.source_cycle_id.is_not(None),
                        PortfolioDecisionRow.decision_time <= instant,
                    )
                )
            )
            eligible_decisions = [
                row
                for row in decision_rows
                if _aware(
                    PortfolioDecision.model_validate(
                        row.payload_json
                    ).created_at
                )
                <= instant
            ]
            latest_decision_id = (
                None
                if not eligible_decisions
                else max(
                    eligible_decisions,
                    key=_portfolio_decision_sort_key,
                ).portfolio_decision_id
            )
            canceled_order_ids: set[str] = (
                {
                    str(order_intent_id)
                    for order_intent_id in session.scalars(
                        select(PaperExecutionAttemptRow.order_intent_id).where(
                            PaperExecutionAttemptRow.order_intent_id.in_(order_ids),
                            PaperExecutionAttemptRow.created_at <= instant,
                            PaperExecutionAttemptRow.status
                            == "LOSS_GUARD_BLOCKED_PENDING_BUY",
                        )
                    )
                }
                if order_ids
                else set()
            )
            fill_rows = (
                list(
                    session.scalars(
                        select(FillRow).where(
                            FillRow.order_intent_id.in_(order_ids),
                            FillRow.effective_at <= instant,
                        )
                    )
                )
                if order_ids
                else []
            )
        filled_by_order: dict[str, Decimal] = {}
        for row in fill_rows:
            fill = Fill.model_validate(row.payload_json)
            filled_by_order[row.order_intent_id] = (
                filled_by_order.get(row.order_intent_id, Decimal("0"))
                + fill.quantity
            )
        pending_orders: list[dict[str, str]] = []
        for row in order_rows:
            intent = OrderIntent.model_validate(row.payload_json)
            if (
                intent.portfolio_decision_id != latest_decision_id
                or intent.order_intent_id in canceled_order_ids
            ):
                continue
            remaining = intent.quantity - filled_by_order.get(
                intent.order_intent_id,
                Decimal("0"),
            )
            if remaining > 0:
                pending_orders.append(
                    {
                        "order_intent_id": intent.order_intent_id,
                        "symbol": intent.symbol,
                        "side": intent.side.value,
                        "remaining_quantity": format(remaining, "f"),
                    }
                )
        state = (
            None
            if state_row is None
            else ArmState.from_payload(state_row.payload_json)
        )
        return {
            "run_id": run_id,
            "arm_id": arm_id,
            "as_of": _iso(instant),
            "run_state": run.status,
            "state_sequence": None if state is None else state.sequence,
            "cash_usd": None if state is None else format(state.cash_usd, "f"),
            "positions": (
                {}
                if state is None
                else {
                    symbol: format(quantity, "f")
                    for symbol, quantity in sorted(state.positions.items())
                    if quantity != 0
                }
            ),
            "nav_usd": None if nav_row is None else format(nav_row.nav_usd, "f"),
            "nav_as_of": None if nav_row is None else _iso(nav_row.as_of),
            "pending_orders": pending_orders,
        }

    def _common_quote_marks(
        self,
        *,
        spec: PaperAccountSpec,
        open_at: datetime,
        as_of: datetime,
        max_quote_age_seconds: int,
        max_quote_skew_seconds: int,
    ) -> CommonQuoteMarks:
        prices: dict[str, Decimal] = {}
        quote_ids: dict[str, str] = {}
        event_times: list[datetime] = []
        missing: list[str] = []
        for position in spec.positions:
            quote = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=position.symbol,
                as_of=as_of,
            )
            if quote is None:
                missing.append(f"{position.symbol}:NO_QUOTE")
                continue
            event_time = _aware(quote.event_time)
            age = (as_of - event_time).total_seconds()
            if event_time < open_at:
                missing.append(f"{position.symbol}:PRE_OPEN")
                continue
            if age < 0 or age > max_quote_age_seconds:
                missing.append(f"{position.symbol}:STALE")
                continue
            if (
                quote.bid_price <= 0
                or quote.ask_price <= 0
                or quote.ask_price < quote.bid_price
                or quote.bid_size_round_lots <= 0
                or quote.ask_size_round_lots <= 0
            ):
                missing.append(f"{position.symbol}:NON_EXECUTABLE")
                continue
            prices[position.symbol] = (
                quote.bid_price + quote.ask_price
            ) / Decimal("2")
            quote_ids[position.symbol] = quote.quote_id
            event_times.append(event_time)
        if missing:
            raise PaperBootstrapNotReady(
                "Fresh common T0 marks are not ready: " + ", ".join(missing)
            )
        if not event_times:
            raise PaperBootstrapNotReady("No paper positions have quote marks")
        if (max(event_times) - min(event_times)).total_seconds() > max_quote_skew_seconds:
            raise PaperBootstrapNotReady("T0 quote bundle exceeds the allowed time skew")
        return CommonQuoteMarks(
            prices=prices,
            quote_ids=quote_ids,
            common_mark_at=max(event_times),
        )

    def _ensure_opening_state(
        self,
        *,
        run_id: str,
        provisioned: ProvisionedPaperAccount,
        completion: PaperBootstrapCompletion,
    ) -> None:
        prices, quote_ids = self._bootstrap_marks(run_id)
        cash_usd = next(
            item.amount
            for item in provisioned.spec.cash
            if item.currency == provisioned.spec.base_currency and item.tradable
        )
        positions = {
            item.symbol: item.quantity for item in provisioned.spec.positions
        }
        with self._session_factory.begin() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise PaperRuntimeError(f"Unknown paper run: {run_id}")
            for arm_id in PAPER_ARM_IDS:
                existing = session.scalar(
                    select(ArmStateSnapshotRow).where(
                        ArmStateSnapshotRow.run_id == run_id,
                        ArmStateSnapshotRow.arm_id == arm_id,
                        ArmStateSnapshotRow.sequence == 0,
                    )
                )
                if existing is not None:
                    continue
                state = ArmState(
                    arm_id=arm_id,
                    initial_cash_usd=completion.initial_nav_usd,
                    cash_usd=cash_usd,
                    positions=dict(positions),
                    sequence=0,
                )
                entry = portfolio_opening_entry(
                    arm_id=arm_id,
                    source_id=completion.bootstrap_completion_id,
                    cash_usd=cash_usd,
                    positions=positions,
                    prices=prices,
                    effective_at=completion.common_mark_at,
                )
                session.add(
                    LedgerTransactionRow(
                        ledger_transaction_id=entry.transaction.ledger_transaction_id,
                        run_id=run_id,
                        arm_id=arm_id,
                        source_id=entry.transaction.source_id,
                        effective_at=entry.transaction.effective_at,
                        payload_json=model_payload(entry.transaction),
                    )
                )
                session.flush()
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
                    ArmStateSnapshotRow(
                        arm_state_snapshot_id=stable_id(
                            "armstate",
                            run_id,
                            arm_id,
                            0,
                        ),
                        run_id=run_id,
                        arm_id=arm_id,
                        sequence=0,
                        payload_json=state.as_payload(),
                        created_at=completion.completed_at,
                    )
                )
                snapshot = build_precise_nav(
                    run_id=run_id,
                    arm_id=arm_id,
                    as_of=completion.common_mark_at,
                    cash_usd=cash_usd,
                    positions=positions,
                    prices=prices,
                    quote_ids=quote_ids,
                    snapshot_scope=completion.bootstrap_completion_id,
                )
                session.add(
                    NavSnapshotRow(
                        nav_snapshot_id=snapshot.nav_snapshot_id,
                        run_id=run_id,
                        arm_id=arm_id,
                        as_of=snapshot.as_of,
                        nav_usd=snapshot.nav_usd,
                        payload_json=model_payload(snapshot),
                    )
                )
            run.status = "RUNNING"

    def _latest_states(
        self,
        run_id: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, ArmState]:
        with self._session_factory() as session:
            statement = select(ArmStateSnapshotRow).where(
                ArmStateSnapshotRow.run_id == run_id
            )
            if as_of is not None:
                statement = statement.where(
                    ArmStateSnapshotRow.created_at <= as_of
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        ArmStateSnapshotRow.arm_id,
                        ArmStateSnapshotRow.sequence.desc(),
                    )
                )
            )
        states: dict[str, ArmState] = {}
        for row in rows:
            if row.arm_id not in states:
                states[row.arm_id] = ArmState.from_payload(row.payload_json)
        return states

    def _completion(self, run_id: str) -> PaperBootstrapCompletion | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(PaperBootstrapCompletionRow).where(
                    PaperBootstrapCompletionRow.run_id == run_id
                )
            )
            return (
                None
                if row is None
                else PaperBootstrapCompletion.model_validate(row.payload_json)
            )

    def _bootstrap_marks(
        self,
        run_id: str,
    ) -> tuple[dict[str, Decimal], dict[str, str]]:
        from trading.persistence.models import PaperBootstrapMarkRow

        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(PaperBootstrapMarkRow).where(
                        PaperBootstrapMarkRow.run_id == run_id
                    )
                )
            )
        return (
            {row.symbol: row.price for row in rows},
            {
                row.symbol: row.source_record_id or row.bootstrap_mark_id
                for row in rows
            },
        )

    def _nav_snapshots_for_scope(
        self,
        *,
        run_id: str,
        snapshot_scope: str,
    ) -> dict[str, NavSnapshot]:
        snapshot_ids = {
            arm_id: stable_id("nav", run_id, arm_id, snapshot_scope)
            for arm_id in PAPER_ARM_IDS
        }
        with self._session_factory() as session:
            rows = {
                arm_id: session.get(NavSnapshotRow, snapshot_id)
                for arm_id, snapshot_id in snapshot_ids.items()
            }
        return {
            arm_id: NavSnapshot.model_validate(row.payload_json)
            for arm_id, row in rows.items()
            if row is not None
        }

    @staticmethod
    def _latest_nav_rows(
        session: Session,
        run_id: str,
    ) -> dict[str, NavSnapshotRow]:
        rows = list(
            session.scalars(
                select(NavSnapshotRow)
                .where(NavSnapshotRow.run_id == run_id)
                .order_by(NavSnapshotRow.arm_id, desc(NavSnapshotRow.as_of))
            )
        )
        latest: dict[str, NavSnapshotRow] = {}
        for row in rows:
            latest.setdefault(row.arm_id, row)
        return latest

    @staticmethod
    def _frozen_cash(
        session: Session,
        account_spec_id: str,
    ) -> list[dict[str, str]]:
        if not account_spec_id:
            return []
        from trading.persistence.models import PaperCashBalanceRow

        rows = list(
            session.scalars(
                select(PaperCashBalanceRow).where(
                    PaperCashBalanceRow.account_spec_id
                    == account_spec_id,
                    PaperCashBalanceRow.tradable.is_(False),
                )
            )
        )
        return [
            {
                "currency": row.currency,
                "amount": format(row.amount, "f"),
                "reason": row.exclusion_reason or "EXCLUDED",
            }
            for row in rows
        ]

    def _position_payload(
        self,
        session: Session,
        *,
        run_id: str,
        account_spec_id: str,
    ) -> list[dict[str, str | None]]:
        if not account_spec_id:
            return []
        from trading.persistence.models import PaperBootstrapMarkRow

        rows = list(
            session.scalars(
                select(PaperPositionRow)
                .where(
                    PaperPositionRow.account_spec_id == account_spec_id
                )
                .order_by(PaperPositionRow.symbol)
            )
        )
        opening_marks = {
            row.symbol: row.price
            for row in session.scalars(
                select(PaperBootstrapMarkRow).where(
                    PaperBootstrapMarkRow.run_id == run_id
                )
            )
        }
        as_of = self._clock.now()
        payload: list[dict[str, str | None]] = []
        for row in rows:
            quote = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=row.symbol,
                as_of=as_of,
            )
            price = (
                None
                if quote is None or quote.bid_price <= 0 or quote.ask_price <= 0
                else (quote.bid_price + quote.ask_price) / Decimal("2")
            )
            opening = opening_marks.get(row.symbol)
            payload.append(
                {
                "symbol": row.symbol,
                "quantity": format(row.quantity, "f"),
                "execution_mode": (
                    "SELL_ONLY"
                    if row.symbol
                    in set(self._config_symbols("sell_only_symbols"))
                    else "ENABLED"
                ),
                    "opening_price": _decimal_or_none(opening),
                    "current_price": _decimal_or_none(price),
                    "market_value_usd": _decimal_or_none(
                        None if price is None else row.quantity * price
                    ),
                    "return_since_t0": _return_string(price, opening),
                }
            )
        return payload

    def _arm_activity_payload(
        self,
        session: Session,
        *,
        run_id: str,
        arm_id: str,
        state: ArmState | None,
    ) -> dict[str, Any]:
        if state is None:
            return {
                "positions": [],
                "orders": [],
                "fills": [],
                "latest_target_weights": {},
            }
        from trading.persistence.models import PaperBootstrapMarkRow

        opening_marks = {
            row.symbol: row.price
            for row in session.scalars(
                select(PaperBootstrapMarkRow).where(
                    PaperBootstrapMarkRow.run_id == run_id
                )
            )
        }
        intent_rows = list(
            session.scalars(
                select(OrderIntentRow)
                .where(
                    OrderIntentRow.run_id == run_id,
                    OrderIntentRow.arm_id == arm_id,
                )
                .order_by(OrderIntentRow.created_at, OrderIntentRow.order_intent_id)
            )
        )
        forward_decision_rows = list(
            session.scalars(
                select(PortfolioDecisionRow).where(
                    PortfolioDecisionRow.run_id == run_id,
                    PortfolioDecisionRow.arm_id == arm_id,
                    PortfolioDecisionRow.source_cycle_id.is_not(None),
                )
            )
        )
        latest_forward_decision_id = (
            None
            if not forward_decision_rows
            else max(
                forward_decision_rows,
                key=_portfolio_decision_sort_key,
            ).portfolio_decision_id
        )
        terminal_attempts = {
            row.order_intent_id: row.status
            for row in session.scalars(
                select(PaperExecutionAttemptRow)
                .where(
                    PaperExecutionAttemptRow.order_intent_id.in_(
                        [item.order_intent_id for item in intent_rows]
                    ),
                    PaperExecutionAttemptRow.status.in_(
                        (
                            "LOSS_GUARD_BLOCKED_PENDING_BUY",
                            "SUPERSEDED_BY_NEWER_PORTFOLIO_DECISION",
                        )
                    ),
                )
                .order_by(PaperExecutionAttemptRow.created_at)
            )
        }
        fill_rows = list(
            session.scalars(
                select(FillRow)
                .where(
                    FillRow.run_id == run_id,
                    FillRow.arm_id == arm_id,
                )
                .order_by(FillRow.effective_at, FillRow.fill_id)
            )
        )
        fills = [Fill.model_validate(row.payload_json) for row in fill_rows]
        fills_by_order: dict[str, list[Fill]] = {}
        buys_by_symbol: dict[str, list[Fill]] = {}
        for fill in fills:
            fills_by_order.setdefault(fill.order_intent_id, []).append(fill)
            if fill.side.value == "BUY":
                buys_by_symbol.setdefault(fill.symbol, []).append(fill)

        as_of = self._clock.now()
        positions: list[dict[str, str | None]] = []
        sell_only = set(self._config_symbols("sell_only_symbols"))
        for symbol, quantity in sorted(state.positions.items()):
            if quantity == 0:
                continue
            quote = self._market.latest_quote(
                provider=PROVIDER,
                feed=FEED,
                symbol=symbol,
                as_of=as_of,
            )
            price = (
                None
                if quote is None or quote.bid_price <= 0 or quote.ask_price <= 0
                else (quote.bid_price + quote.ask_price) / Decimal("2")
            )
            opening = opening_marks.get(symbol)
            if opening is None and buys_by_symbol.get(symbol):
                buy_fills = buys_by_symbol[symbol]
                bought = sum((fill.quantity for fill in buy_fills), Decimal("0"))
                if bought > 0:
                    opening = sum(
                        (
                            fill.quantity * fill.price + fill.commission_usd
                            for fill in buy_fills
                        ),
                        Decimal("0"),
                    ) / bought
            positions.append(
                {
                    "symbol": symbol,
                    "quantity": format(quantity, "f"),
                    "execution_mode": (
                        "SELL_ONLY" if symbol in sell_only else "ENABLED"
                    ),
                    "opening_price": _decimal_or_none(opening),
                    "current_price": _decimal_or_none(price),
                    "market_value_usd": _decimal_or_none(
                        None if price is None else quantity * price
                    ),
                    "return_since_t0": _return_string(price, opening),
                }
            )

        orders: list[dict[str, Any]] = []
        for row in intent_rows[-20:]:
            intent = OrderIntent.model_validate(row.payload_json)
            order_fills = fills_by_order.get(intent.order_intent_id, [])
            filled_quantity = sum(
                (fill.quantity for fill in order_fills),
                Decimal("0"),
            )
            fill_notional = sum(
                (fill.quantity * fill.price for fill in order_fills),
                Decimal("0"),
            )
            if filled_quantity >= intent.quantity:
                status = "FILLED"
            elif row.source_cycle_id is not None and (
                intent.portfolio_decision_id
                != latest_forward_decision_id
            ):
                status = (
                    "PARTIAL_SUPERSEDED"
                    if filled_quantity > 0
                    else "SUPERSEDED"
                )
            elif (
                terminal_attempts.get(intent.order_intent_id)
                == "LOSS_GUARD_BLOCKED_PENDING_BUY"
            ):
                status = (
                    "PARTIAL_CANCELED_BY_LOSS_GUARD"
                    if filled_quantity > 0
                    else "CANCELED_BY_LOSS_GUARD"
                )
            elif filled_quantity > 0:
                status = (
                    "PARTIAL_EXPIRED"
                    if row.valid_until is not None
                    and _aware(row.valid_until) <= as_of
                    else "PARTIAL"
                )
            elif row.valid_until is not None and _aware(row.valid_until) <= as_of:
                status = "EXPIRED"
            else:
                status = "PENDING"
            orders.append(
                {
                    "order_intent_id": intent.order_intent_id,
                    "status": status,
                    "symbol": intent.symbol,
                    "side": intent.side.value,
                    "requested_quantity": format(intent.quantity, "f"),
                    "filled_quantity": format(filled_quantity, "f"),
                    "average_fill_price": _decimal_or_none(
                        None
                        if filled_quantity == 0
                        else fill_notional / filled_quantity
                    ),
                    "created_at": _iso(intent.created_at),
                    "valid_until": (
                        None
                        if row.valid_until is None
                        else _iso(row.valid_until)
                    ),
                }
            )
        fill_payloads = [
            {
                "fill_id": fill.fill_id,
                "order_intent_id": fill.order_intent_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": format(fill.quantity, "f"),
                "price": format(fill.price, "f"),
                "commission_usd": format(fill.commission_usd, "f"),
                "effective_at": _iso(fill.effective_at),
                "quote_id": row.quote_id,
            }
            for fill, row in zip(fills[-20:], fill_rows[-20:], strict=True)
        ]
        latest_target_weights: dict[str, float] = {}
        if latest_forward_decision_id is not None:
            latest_decision_row = session.get(
                PortfolioDecisionRow,
                latest_forward_decision_id,
            )
            if latest_decision_row is not None:
                latest_target_weights = dict(
                    PortfolioDecision.model_validate(
                        latest_decision_row.payload_json
                    ).target_weights_pre_risk
                )
        return {
            "positions": positions,
            "orders": list(reversed(orders)),
            "fills": list(reversed(fill_payloads)),
            "latest_target_weights": latest_target_weights,
        }

    def _config_symbols(self, key: str) -> tuple[str, ...]:
        universe = self._config.get("universe.yaml")
        return tuple(str(item).upper() for item in universe.get(key, ()))

    def _validate_existing_run(
        self,
        run: RunRow,
        provisioned: ProvisionedPaperAccount,
    ) -> None:
        if (
            run.mode != "PAPER"
            or run.experiment_version != LEGACY_FORWARD_ALGORITHM_VERSION
        ):
            raise PaperRuntimeError(
                f"Run {run.run_id!r} is not a legacy paper run"
            )
        manifest = run.result_manifest or {}
        if manifest.get("account_spec_id") != provisioned.account_spec_id:
            raise PaperRuntimeError(
                f"Run {run.run_id!r} is bound to a different account snapshot"
            )
        if run.config_manifest_hash != self._config.manifest_hash:
            raise PaperRuntimeError(
                f"Run {run.run_id!r} was created with a different config manifest"
            )
        current_code_version = workspace_code_version(self._workspace_root)
        if run.code_commit != current_code_version:
            raise PaperRuntimeError(
                f"Run {run.run_id!r} was created with a different code version"
            )

    def _now(self, value: datetime | None) -> datetime:
        return self._clock.now() if value is None else require_aware_utc(value)


def build_precise_nav(
    *,
    run_id: str,
    arm_id: str,
    as_of: datetime,
    cash_usd: Decimal,
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
    quote_ids: dict[str, str],
    snapshot_scope: str | None = None,
) -> NavSnapshot:
    missing = sorted(set(positions) - set(prices))
    if missing:
        raise ValueError(f"Missing NAV prices for: {missing}")
    market_value = sum(
        (quantity * prices[symbol] for symbol, quantity in positions.items()),
        Decimal("0"),
    ).quantize(NAV_QUANTUM, rounding=ROUND_HALF_EVEN)
    cash = cash_usd.quantize(NAV_QUANTUM, rounding=ROUND_HALF_EVEN)
    nav = cash + market_value
    price_manifest_hash = canonical_hash(
        {
            "prices": {
                symbol: format(price, "f")
                for symbol, price in sorted(prices.items())
            },
            "quote_ids": dict(sorted(quote_ids.items())),
        }
    )
    return NavSnapshot(
        nav_snapshot_id=(
            stable_id("nav", run_id, arm_id, snapshot_scope)
            if snapshot_scope is not None
            else stable_id("nav", run_id, arm_id, as_of, price_manifest_hash)
        ),
        arm_id=arm_id,
        as_of=as_of,
        cash_usd=cash,
        positions_market_value_usd=market_value,
        nav_usd=nav,
        price_manifest_hash=price_manifest_hash,
        created_at=as_of,
    )


def _return_string(
    current: Decimal | None,
    initial: Decimal | None,
) -> str | None:
    if current is None or initial is None or initial == 0:
        return None
    return format(current / initial - Decimal("1"), ".10f")


def _active_return_string(
    current: Decimal | None,
    hold: Decimal | None,
    initial: Decimal | None,
) -> str | None:
    if current is None or hold is None or initial is None or initial == 0:
        return None
    return format((current - hold) / initial, ".10f")


def _portfolio_decision_sort_key(
    row: PortfolioDecisionRow,
) -> tuple[datetime, datetime, str]:
    decision = PortfolioDecision.model_validate(row.payload_json)
    return (
        _aware(row.decision_time),
        _aware(decision.created_at),
        row.portfolio_decision_id,
    )


def _forward_transition_complete(
    arm_id: str,
    state: ArmState | None,
    orders: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    latest_target_weights: dict[str, float],
) -> bool:
    if (
        state is None
        or state.sequence <= 0
        or not latest_target_weights
    ):
        return False
    held = {
        symbol
        for symbol, quantity in state.positions.items()
        if quantity != 0
    }
    allowed: set[str] = set() if arm_id == "B0-CASH" else {"QQQ"}
    def pending(order: dict[str, Any]) -> bool:
        status = str(order.get("status", ""))
        return status == "PENDING" or (
            status.startswith("PARTIAL")
            and "EXPIRED" not in status
            and "SUPERSEDED" not in status
            and "CANCELED" not in status
        )

    if held - allowed or any(pending(order) for order in orders):
        return False
    position_values: dict[str, Decimal] = {}
    for position in positions:
        market_value = position.get("market_value_usd")
        if market_value is None:
            return False
        position_values[str(position["symbol"])] = Decimal(str(market_value))
    nav = state.cash_usd + sum(position_values.values(), Decimal("0"))
    if nav <= 0:
        return False
    current_weights = {
        symbol: value / nav for symbol, value in position_values.items()
    }
    current_weights["USD_CASH"] = state.cash_usd / nav
    tolerance = Decimal("0.05")
    return all(
        abs(
            current_weights.get(symbol, Decimal("0"))
            - Decimal(str(latest_target_weights.get(symbol, 0.0)))
        )
        <= tolerance
        for symbol in set(current_weights) | set(latest_target_weights)
    )


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")
