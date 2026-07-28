from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Protocol, cast
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import FEED, PROVIDER
from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.q1 import Q1StrategyDecision
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.execution.alpaca_paper import (
    ALPACA_PAPER_OPEN_STATUSES,
    AlpacaPaperAccount,
    AlpacaPaperCancelResult,
    AlpacaPaperFillActivity,
    AlpacaPaperOrder,
    AlpacaPaperOrderRequest,
    AlpacaPaperPosition,
    AlpacaPaperTradingClient,
    AlpacaPaperTradingError,
    deterministic_client_order_id,
)
from trading.persistence.alpaca_paper import (
    AlpacaPaperPersistenceConflict,
    AlpacaPaperRepository,
)
from trading.persistence.models import (
    MarketCalendarSessionRow,
    MarketQuoteRow,
    MarketStreamStatusRow,
    NavSnapshotRow,
    PaperBrokerBindingRow,
    PaperBrokerCommandRow,
    PaperBrokerEventRow,
    PortfolioDecisionRow,
    RunRow,
)
from trading.runtime.provenance import workspace_code_version
from trading.settings import AlpacaPaperConfigBundle, Q1ConfigBundle

NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal("0")
ONE = Decimal("1")


class AlpacaPaperClient(Protocol):
    async def get_account(self) -> AlpacaPaperAccount: ...

    async def list_positions(self) -> tuple[AlpacaPaperPosition, ...]: ...

    async def list_orders(
        self,
        *,
        status: str = "all",
        limit: int = 500,
    ) -> tuple[AlpacaPaperOrder, ...]: ...

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> AlpacaPaperOrder | None: ...

    async def ensure_submitted(
        self,
        request: AlpacaPaperOrderRequest,
    ) -> AlpacaPaperOrder: ...

    async def cancel_order(
        self,
        broker_order_id: str,
    ) -> AlpacaPaperCancelResult: ...

    async def list_fill_activities(
        self,
        *,
        after: datetime | None = None,
        page_size: int = 100,
    ) -> tuple[AlpacaPaperFillActivity, ...]: ...

    async def aclose(self) -> None: ...


class Q1AlpacaPaperCanaryError(RuntimeError):
    pass


class Q1AlpacaPaperCanaryBlocked(Q1AlpacaPaperCanaryError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class _OrderPlan:
    source_decision_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    quote_id: str
    quote_as_of: datetime
    reason: str


class Q1AlpacaPaperCanaryService:
    """A separate Paper API canary that follows one Q1 arm.

    The service writes only ``paper_broker_*`` rows. It never writes Q1 fills,
    arm states, NAV rows, or matched-attribution results.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        q1_config: Q1ConfigBundle,
        paper_config: AlpacaPaperConfigBundle,
        client: AlpacaPaperClient | AlpacaPaperTradingClient,
        workspace_root: Path,
        clock: Clock | None = None,
    ) -> None:
        if q1_config.document.get("algorithm_version") != Q1_ALGORITHM_VERSION:
            raise Q1AlpacaPaperCanaryError(
                "Alpaca Paper canary requires q1_math_core_v1"
            )
        if q1_config.document.get("real_order_routing") is not False:
            raise Q1AlpacaPaperCanaryError(
                "Q1 real_order_routing must remain false"
            )
        if paper_config.document.get("real_order_routing") is not False:
            raise Q1AlpacaPaperCanaryError(
                "Alpaca Paper canary real_order_routing must remain false"
            )
        self._session_factory = session_factory
        self._q1_config = q1_config
        self._paper_bundle = paper_config
        self._config = paper_config.config
        self._client = client
        self._clock = clock or SystemClock()
        self._code_version = workspace_code_version(workspace_root)
        self._calendar_version = str(
            q1_config.document["market_calendar_version"]
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def sync(
        self,
        run_id: str,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        instant = (
            self._clock.now()
            if now is None
            else require_aware_utc(now)
        )
        try:
            return await self._sync_once(run_id=run_id, now=instant)
        except Q1AlpacaPaperCanaryBlocked as exc:
            self._record_blocked(
                run_id=run_id,
                now=instant,
                code=exc.code,
                detail=exc.detail,
            )
            return {
                **self.status(run_id),
                "reconciliation_status": exc.code,
                "last_error_code": exc.code,
                "last_error_detail": exc.detail,
            }
        except AlpacaPaperTradingError as exc:
            code = (
                self._config.unknown_outcome_state
                if exc.retryable
                else "BROKER_REQUEST_FAILED"
            )
            detail = (
                "Alpaca Paper request outcome requires reconciliation"
                if exc.retryable
                else "Alpaca Paper request was rejected"
            )
            self._record_blocked(
                run_id=run_id,
                now=instant,
                code=code,
                detail=detail,
            )
            return {
                **self.status(run_id),
                "reconciliation_status": code,
                "last_error_code": code,
                "last_error_detail": detail,
            }
        except AlpacaPaperPersistenceConflict:
            code = "PERSISTENCE_IDEMPOTENCY_CONFLICT"
            detail = "Immutable Alpaca Paper identity conflict"
            self._record_blocked(
                run_id=run_id,
                now=instant,
                code=code,
                detail=detail,
            )
            return {
                **self.status(run_id),
                "reconciliation_status": code,
                "last_error_code": code,
                "last_error_detail": detail,
            }

    def status(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            return alpaca_paper_canary_status(
                session,
                run_id=run_id,
                enabled=True,
                config=self._paper_bundle,
            )

    async def _sync_once(
        self,
        *,
        run_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        existing_binding, fills_after = self._binding_and_fill_cursor(
            run_id
        )
        account = await self._client.get_account()
        positions = await self._client.list_positions()
        orders = await self._client.list_orders(
            status="all",
            limit=self._config.order_history_limit,
        )
        open_orders = tuple(order for order in orders if order.is_open)

        if existing_binding is None:
            self._validate_new_binding(
                run_id=run_id,
                account=account,
                positions=positions,
                open_orders=open_orders,
            )
            binding = self._create_binding(
                run_id=run_id,
                account=account,
                now=now,
            )
            fills_after = now
        else:
            binding = existing_binding
            self._validate_bound_account(binding, account)

        fills = await self._client.list_fill_activities(
            after=fills_after,
            page_size=self._config.fill_activity_page_size,
        )
        command_by_client = self._persist_reconciliation_snapshot(
            binding=binding,
            account=account,
            positions=positions,
            orders=orders,
            fills=fills,
            now=now,
        )
        self._validate_dedicated_account(
            binding=binding,
            positions=positions,
            open_orders=open_orders,
            command_by_client=command_by_client,
        )

        decision_row, decision = self._latest_source_decision(run_id)
        risk_state = self._latest_source_risk_state(run_id)
        owned_open = tuple(
            order
            for order in open_orders
            if order.client_order_id in command_by_client
        )
        cancellation_required = tuple(
            order
            for order in owned_open
            if decision is None
            or command_by_client[order.client_order_id].source_decision_id
            != decision.portfolio_decision_id
            or now >= decision.valid_until
            or (
                risk_state
                in {"SOFT_STOP", "HARD_REDUCE", "CRITICAL_EXIT"}
                and order.side is OrderSide.BUY
            )
        )
        if cancellation_required:
            source_row = (
                decision_row
                if decision_row is not None
                else self._source_row_for_order(
                    command_by_client[cancellation_required[0].client_order_id]
                )
            )
            await self._cancel_orders(
                binding=binding,
                source_decision=source_row,
                orders=cancellation_required,
                now=now,
                reason=(
                    "DETERMINISTIC_RISK_BLOCKED_BUY"
                    if risk_state
                    in {"SOFT_STOP", "HARD_REDUCE", "CRITICAL_EXIT"}
                    else "SOURCE_TARGET_REPLACED_OR_EXPIRED"
                ),
            )
            self._record_ready(binding=binding, now=now)
            return self.status(run_id)

        if decision is None or decision_row is None:
            self._record_ready(binding=binding, now=now)
            return self.status(run_id)

        session_row = self._active_market_session(now)
        if session_row is None:
            if owned_open:
                await self._cancel_orders(
                    binding=binding,
                    source_decision=decision_row,
                    orders=owned_open,
                    now=now,
                    reason="OUTSIDE_ACTUAL_REGULAR_SESSION",
                )
            raise Q1AlpacaPaperCanaryBlocked(
                "OUTSIDE_ACTUAL_REGULAR_SESSION",
                "No Alpaca Paper order may be created outside the calendar session",
            )
        if now >= decision.valid_until:
            raise Q1AlpacaPaperCanaryBlocked(
                "SOURCE_DECISION_EXPIRED",
                "The source decision is no longer executable",
            )
        if owned_open:
            self._record_ready(binding=binding, now=now)
            return self.status(run_id)

        plans = self._plan_orders(
            account=account,
            positions=positions,
            decision=decision,
            decision_row=decision_row,
            risk_state=risk_state,
            now=now,
        )
        if plans:
            await self._submit_plans(
                binding=binding,
                source_decision=decision_row,
                plans=plans,
                now=now,
            )
        self._record_ready(binding=binding, now=now)
        return self.status(run_id)

    def _binding_and_fill_cursor(
        self,
        run_id: str,
    ) -> tuple[PaperBrokerBindingRow | None, datetime | None]:
        with self._session_factory() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                raise Q1AlpacaPaperCanaryBlocked(
                    "RUN_NOT_INITIALIZED",
                    "Initialize the q1 paper run before enabling the canary",
                )
            if run.experiment_version != Q1_ALGORITHM_VERSION:
                raise Q1AlpacaPaperCanaryBlocked(
                    "ALGORITHM_VERSION_MISMATCH",
                    "Alpaca Paper canary can follow only q1_math_core_v1",
                )
            if (
                run.config_manifest_hash != self._q1_config.manifest_hash
                or run.code_commit != self._code_version
            ):
                raise Q1AlpacaPaperCanaryBlocked(
                    "RUN_VERSION_MISMATCH",
                    "Initialize a new Q1 run for the current config and code",
                )
            binding = AlpacaPaperRepository(session).binding_for_run(run_id)
            if binding is None:
                return None, None
            last_fill = session.scalar(
                select(PaperBrokerEventRow)
                .where(
                    PaperBrokerEventRow.binding_id == binding.binding_id,
                    PaperBrokerEventRow.event_type == "FILL_ACTIVITY",
                )
                .order_by(
                    PaperBrokerEventRow.occurred_at.desc(),
                    PaperBrokerEventRow.event_id.desc(),
                )
                .limit(1)
            )
            return (
                binding,
                (
                    _aware(binding.created_at)
                    if last_fill is None
                    else _aware(last_fill.occurred_at)
                ),
            )

    def _validate_new_binding(
        self,
        *,
        run_id: str,
        account: AlpacaPaperAccount,
        positions: Sequence[AlpacaPaperPosition],
        open_orders: Sequence[AlpacaPaperOrder],
    ) -> None:
        del run_id
        if not account.is_trading_ready:
            raise Q1AlpacaPaperCanaryBlocked(
                "ACCOUNT_NOT_READY",
                "The Alpaca Paper account is not active and trade-ready",
            )
        if self._config.require_clean_account_on_first_bind and (
            any(position.quantity != ZERO for position in positions)
            or bool(open_orders)
        ):
            raise Q1AlpacaPaperCanaryBlocked(
                "ACCOUNT_NOT_CLEAN",
                "First binding requires no positions and no open orders",
            )

    def _create_binding(
        self,
        *,
        run_id: str,
        account: AlpacaPaperAccount,
        now: datetime,
    ) -> PaperBrokerBindingRow:
        source_arm = self._config.source_arm.value
        binding_id = stable_id(
            "alpaca-paper-binding",
            run_id,
            account.account_id_hash,
            self._paper_bundle.manifest_hash,
        )
        payload = _json_object(
            canonical_data(
                {
                    "schema_version": "alpaca-paper-binding.v1",
                    "execution_lane": self._config.execution_lane,
                    "provider": self._config.provider,
                    "source_arm": source_arm,
                    "rest_base_url": self._config.rest_base_url,
                    "dedicated_account_required": (
                        self._config.require_dedicated_account
                    ),
                    "real_order_routing": False,
                    "matched_attribution_included": False,
                }
            )
        )
        binding_hash = canonical_hash(
            {
                "binding_id": binding_id,
                "run_id": run_id,
                "source_arm": source_arm,
                "account_id_hash": account.account_id_hash,
                "initial_equity_usd": account.equity,
                "initial_cash_usd": account.cash,
                "config_manifest_hash": self._paper_bundle.manifest_hash,
                "code_version": self._code_version,
                "payload": payload,
            }
        )
        with self._session_factory.begin() as session:
            return AlpacaPaperRepository(session).create_binding(
                binding_id=binding_id,
                run_id=run_id,
                source_arm_id=source_arm,
                account_id_hash=account.account_id_hash,
                initial_equity_usd=account.equity,
                initial_cash_usd=account.cash,
                config_manifest_hash=self._paper_bundle.manifest_hash,
                code_version=self._code_version,
                binding_hash=binding_hash,
                payload_json=payload,
                created_at=now,
            )

    def _validate_bound_account(
        self,
        binding: PaperBrokerBindingRow,
        account: AlpacaPaperAccount,
    ) -> None:
        if binding.account_id_hash != account.account_id_hash:
            raise Q1AlpacaPaperCanaryBlocked(
                "ACCOUNT_IDENTITY_CHANGED",
                "The configured Alpaca Paper credentials identify another account",
            )
        if (
            binding.base_url != self._config.rest_base_url
            or binding.config_manifest_hash
            != self._paper_bundle.manifest_hash
            or binding.code_version != self._code_version
        ):
            raise Q1AlpacaPaperCanaryBlocked(
                "BINDING_VERSION_MISMATCH",
                "Start a new run after canary config or code changes",
            )
        if not account.is_trading_ready:
            raise Q1AlpacaPaperCanaryBlocked(
                "ACCOUNT_NOT_READY",
                "The bound Alpaca Paper account is not trade-ready",
            )

    def _persist_reconciliation_snapshot(
        self,
        *,
        binding: PaperBrokerBindingRow,
        account: AlpacaPaperAccount,
        positions: Sequence[AlpacaPaperPosition],
        orders: Sequence[AlpacaPaperOrder],
        fills: Sequence[AlpacaPaperFillActivity],
        now: datetime,
    ) -> dict[str, PaperBrokerCommandRow]:
        with self._session_factory.begin() as session:
            repository = AlpacaPaperRepository(session)
            commands = repository.commands(binding_id=binding.binding_id)
            command_by_client = {
                row.client_order_id: row
                for row in commands
                if row.client_order_id is not None
            }
            account_payload = _json_object(
                canonical_data(
                    {
                        "currency": account.currency,
                        "equity_usd": account.equity,
                        "cash_usd": account.cash,
                        "buying_power_usd": account.buying_power,
                        "buying_power_used_for_orders": False,
                        "status": account.status,
                        "account_blocked": account.account_blocked,
                        "trading_blocked": account.trading_blocked,
                        "trade_suspended_by_user": (
                            account.trade_suspended_by_user
                        ),
                        "account_ready": account.is_trading_ready,
                    }
                )
            )
            self._append_event(
                session=session,
                repository=repository,
                binding=binding,
                command=None,
                event_type="ACCOUNT_SNAPSHOT",
                status=(
                    "READY" if account.is_trading_ready else "BLOCKED"
                ),
                broker_order_id=None,
                client_order_id=None,
                provider_event_id=None,
                symbol=None,
                side=None,
                quantity=None,
                filled_quantity=None,
                fill_price=None,
                occurred_at=now,
                available_at=now,
                provider_request_id=account.provider_request_id,
                idempotency_key=stable_id(
                    "alpaca-account-snapshot",
                    binding.binding_id,
                    now,
                    canonical_hash(account_payload),
                ),
                payload=account_payload,
                created_at=now,
            )
            positions_payload = _json_object(
                canonical_data(
                    {
                        "positions": [
                            {
                                "symbol": item.symbol,
                                "quantity": item.quantity,
                                "market_value_usd": item.market_value,
                                "average_entry_price_usd": (
                                    item.average_entry_price
                                ),
                                "side": item.side,
                            }
                            for item in sorted(
                                positions,
                                key=lambda value: value.symbol,
                            )
                        ]
                    }
                )
            )
            self._append_event(
                session=session,
                repository=repository,
                binding=binding,
                command=None,
                event_type="POSITIONS_SNAPSHOT",
                status="RECONCILED",
                broker_order_id=None,
                client_order_id=None,
                provider_event_id=None,
                symbol=None,
                side=None,
                quantity=None,
                filled_quantity=None,
                fill_price=None,
                occurred_at=now,
                available_at=now,
                provider_request_id=None,
                idempotency_key=stable_id(
                    "alpaca-positions-snapshot",
                    binding.binding_id,
                    now,
                    canonical_hash(positions_payload),
                ),
                payload=positions_payload,
                created_at=now,
            )
            order_by_broker: dict[str, AlpacaPaperOrder] = {}
            for order in orders:
                command = command_by_client.get(order.client_order_id)
                if command is None:
                    continue
                order_by_broker[order.broker_order_id] = order
                idempotency_key = stable_id(
                    "alpaca-order-snapshot",
                    binding.binding_id,
                    order.broker_order_id,
                    order.status,
                    order.filled_quantity,
                    order.updated_at,
                )
                if self._event_exists(
                    session,
                    binding_id=binding.binding_id,
                    idempotency_key=idempotency_key,
                ):
                    continue
                self._append_event(
                    session=session,
                    repository=repository,
                    binding=binding,
                    command=command,
                    event_type="ORDER_SNAPSHOT",
                    status=order.status,
                    broker_order_id=order.broker_order_id,
                    client_order_id=order.client_order_id,
                    provider_event_id=None,
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    filled_quantity=order.filled_quantity,
                    fill_price=order.filled_average_price,
                    occurred_at=order.updated_at,
                    available_at=now,
                    provider_request_id=order.provider_request_id,
                    idempotency_key=idempotency_key,
                    payload=_order_payload(order),
                    created_at=now,
                )
            for fill in fills:
                order = order_by_broker.get(fill.broker_order_id)
                if order is None:
                    continue
                command = command_by_client[order.client_order_id]
                idempotency_key = stable_id(
                    "alpaca-fill-activity",
                    binding.binding_id,
                    fill.activity_id,
                )
                if self._event_exists(
                    session,
                    binding_id=binding.binding_id,
                    idempotency_key=idempotency_key,
                ):
                    continue
                self._append_event(
                    session=session,
                    repository=repository,
                    binding=binding,
                    command=command,
                    event_type="FILL_ACTIVITY",
                    status="FILLED_ACTIVITY",
                    broker_order_id=fill.broker_order_id,
                    client_order_id=order.client_order_id,
                    provider_event_id=fill.activity_id,
                    symbol=fill.symbol,
                    side=fill.side.value,
                    quantity=fill.quantity,
                    filled_quantity=max(
                        fill.quantity,
                        order.filled_quantity,
                    ),
                    fill_price=fill.price,
                    occurred_at=fill.executed_at,
                    available_at=now,
                    provider_request_id=fill.provider_request_id,
                    idempotency_key=idempotency_key,
                    payload=_json_object(
                        canonical_data(
                            {
                                "activity_id": fill.activity_id,
                                "broker_order_id": fill.broker_order_id,
                                "symbol": fill.symbol,
                                "side": fill.side,
                                "fill_quantity": fill.quantity,
                                "cumulative_filled_quantity": max(
                                    fill.quantity,
                                    order.filled_quantity,
                                ),
                                "fill_price": fill.price,
                                "executed_at": fill.executed_at,
                            }
                        )
                    ),
                    created_at=now,
                )
            return command_by_client

    def _validate_dedicated_account(
        self,
        *,
        binding: PaperBrokerBindingRow,
        positions: Sequence[AlpacaPaperPosition],
        open_orders: Sequence[AlpacaPaperOrder],
        command_by_client: dict[str, PaperBrokerCommandRow],
    ) -> None:
        invalid_positions = [
            item.symbol
            for item in positions
            if (
                item.symbol not in self._config.allowed_symbols
                or item.side != "long"
                or item.quantity < ZERO
                or item.quantity != item.quantity.to_integral_value()
            )
        ]
        if self._config.reject_foreign_positions and invalid_positions:
            raise Q1AlpacaPaperCanaryBlocked(
                "FOREIGN_OR_UNSAFE_POSITION",
                "Dedicated canary account contains a non-Q1 or non-long position",
            )
        actual_positions = {
            item.symbol: item.quantity
            for item in positions
            if item.quantity != ZERO
        }
        with self._session_factory() as session:
            fills = tuple(
                session.scalars(
                    select(PaperBrokerEventRow).where(
                        PaperBrokerEventRow.binding_id
                        == binding.binding_id,
                        PaperBrokerEventRow.event_type
                        == "FILL_ACTIVITY",
                    )
                )
            )
        expected_positions: dict[str, Decimal] = {}
        for fill in fills:
            if (
                fill.symbol is None
                or fill.side is None
                or fill.quantity is None
            ):
                raise Q1AlpacaPaperCanaryBlocked(
                    "FILL_RECONCILIATION_INCOMPLETE",
                    "An owned Paper fill is missing typed position fields",
                )
            direction = ONE if fill.side == "BUY" else -ONE
            expected_positions[fill.symbol] = (
                expected_positions.get(fill.symbol, ZERO)
                + direction * fill.quantity
            )
        symbols = set(actual_positions) | set(expected_positions)
        if self._config.reject_foreign_positions and any(
            actual_positions.get(symbol, ZERO)
            != expected_positions.get(symbol, ZERO)
            for symbol in symbols
        ):
            raise Q1AlpacaPaperCanaryBlocked(
                "POSITION_RECONCILIATION_MISMATCH",
                "Paper positions do not reconcile to owned fill activities",
            )
        foreign_orders = [
            item.client_order_id
            for item in open_orders
            if item.client_order_id not in command_by_client
        ]
        if self._config.reject_foreign_open_orders and foreign_orders:
            raise Q1AlpacaPaperCanaryBlocked(
                "FOREIGN_OPEN_ORDER",
                "Dedicated canary account contains an unknown open order",
            )
        open_counts: dict[str, int] = {}
        for order in open_orders:
            if order.client_order_id in command_by_client:
                open_counts[order.symbol] = (
                    open_counts.get(order.symbol, 0) + 1
                )
        if any(
            count > self._config.maximum_open_orders_per_symbol
            for count in open_counts.values()
        ):
            raise Q1AlpacaPaperCanaryBlocked(
                "TOO_MANY_OPEN_ORDERS",
                "Canary open-order count exceeds the configured symbol cap",
            )

    def _latest_source_decision(
        self,
        run_id: str,
    ) -> tuple[PortfolioDecisionRow | None, Q1StrategyDecision | None]:
        with self._session_factory() as session:
            row = session.scalar(
                select(PortfolioDecisionRow)
                .where(
                    PortfolioDecisionRow.run_id == run_id,
                    PortfolioDecisionRow.arm_id
                    == self._config.source_arm.value,
                    PortfolioDecisionRow.algorithm_version
                    == Q1_ALGORITHM_VERSION,
                )
                .order_by(
                    desc(PortfolioDecisionRow.decision_created_at),
                    desc(PortfolioDecisionRow.portfolio_decision_id),
                )
                .limit(1)
            )
            if row is None:
                return None, None
            if (
                row.config_manifest_hash != self._q1_config.manifest_hash
                or row.code_version != self._code_version
            ):
                raise Q1AlpacaPaperCanaryBlocked(
                    "SOURCE_DECISION_VERSION_MISMATCH",
                    "The latest source decision is outside the bound Q1 version",
                )
            return row, Q1StrategyDecision.model_validate(row.payload_json)

    def _source_row_for_order(
        self,
        command: PaperBrokerCommandRow,
    ) -> PortfolioDecisionRow:
        with self._session_factory() as session:
            row = session.get(
                PortfolioDecisionRow,
                command.source_decision_id,
            )
            if row is None:
                raise Q1AlpacaPaperCanaryBlocked(
                    "SOURCE_DECISION_MISSING",
                    "An owned broker order lost its immutable source decision",
                )
            session.expunge(row)
            return row

    def _latest_source_risk_state(self, run_id: str) -> str:
        with self._session_factory() as session:
            row = session.scalar(
                select(NavSnapshotRow)
                .where(
                    NavSnapshotRow.run_id == run_id,
                    NavSnapshotRow.arm_id
                    == self._config.source_arm.value,
                    NavSnapshotRow.algorithm_version
                    == Q1_ALGORITHM_VERSION,
                    NavSnapshotRow.config_manifest_hash
                    == self._q1_config.manifest_hash,
                    NavSnapshotRow.code_version == self._code_version,
                )
                .order_by(
                    NavSnapshotRow.as_of.desc(),
                    NavSnapshotRow.nav_snapshot_id.desc(),
                )
                .limit(1)
            )
            if row is None:
                return "NORMAL"
            value = row.payload_json.get("risk_state", "NORMAL")
            return (
                value
                if isinstance(value, str)
                and value
                in {
                    "NORMAL",
                    "SOFT_STOP",
                    "HARD_REDUCE",
                    "CRITICAL_EXIT",
                }
                else "NORMAL"
            )

    def _active_market_session(
        self,
        now: datetime,
    ) -> MarketCalendarSessionRow | None:
        local_date = now.astimezone(NEW_YORK).date()
        with self._session_factory() as session:
            return session.scalar(
                select(MarketCalendarSessionRow)
                .where(
                    MarketCalendarSessionRow.calendar_version
                    == self._calendar_version,
                    MarketCalendarSessionRow.algorithm_version
                    == Q1_ALGORITHM_VERSION,
                    MarketCalendarSessionRow.config_manifest_hash
                    == self._q1_config.manifest_hash,
                    MarketCalendarSessionRow.code_version
                    == self._code_version,
                    MarketCalendarSessionRow.session_date == local_date,
                    MarketCalendarSessionRow.available_at <= now,
                    MarketCalendarSessionRow.open_at <= now,
                    MarketCalendarSessionRow.close_at > now,
                )
                .order_by(
                    MarketCalendarSessionRow.available_at.desc(),
                    MarketCalendarSessionRow.calendar_session_id.desc(),
                )
                .limit(1)
            )

    def _plan_orders(
        self,
        *,
        account: AlpacaPaperAccount,
        positions: Sequence[AlpacaPaperPosition],
        decision: Q1StrategyDecision,
        decision_row: PortfolioDecisionRow,
        risk_state: str,
        now: datetime,
    ) -> tuple[_OrderPlan, ...]:
        del decision_row
        risky_weights = {
            symbol: decision.target_weights.get(symbol, ZERO)
            for symbol in self._config.allowed_symbols
        }
        if risk_state == "CRITICAL_EXIT":
            risky_weights = {
                symbol: ZERO for symbol in self._config.allowed_symbols
            }
        elif risk_state == "HARD_REDUCE":
            risky_weights["SOXX"] = min(
                risky_weights["SOXX"],
                Decimal("0.20"),
            )
            gross = sum(risky_weights.values(), ZERO)
            if gross > Decimal("0.50"):
                scale = Decimal("0.50") / gross
                risky_weights = {
                    symbol: weight * scale
                    for symbol, weight in risky_weights.items()
                }
        if (
            any(weight < ZERO for weight in risky_weights.values())
            or sum(risky_weights.values(), ZERO) > ONE
            or set(decision.target_weights)
            - {*self._config.allowed_symbols, "USD_CASH"}
        ):
            raise Q1AlpacaPaperCanaryBlocked(
                "INVALID_SOURCE_TARGET",
                "Source target violates the canary long-only universe",
            )
        buys_allowed = (
            risk_state == "NORMAL"
            and now.astimezone(NEW_YORK).time()
            < self._config.no_risk_increase_after_et
        )
        quotes = self._fresh_quotes(
            symbols=tuple(
                symbol
                for symbol in self._config.allowed_symbols
                if (
                    (
                        buys_allowed
                        and risky_weights[symbol] > ZERO
                    )
                    or any(
                        position.symbol == symbol
                        and position.quantity > ZERO
                        for position in positions
                    )
                )
            ),
            now=now,
        )
        if len(quotes) > 1:
            event_times = [quote.event_time for quote in quotes.values()]
            skew = max(event_times) - min(event_times)
            if skew > timedelta(
                seconds=(
                    self._config.maximum_multi_symbol_quote_skew_seconds
                )
            ):
                raise Q1AlpacaPaperCanaryBlocked(
                    "QUOTE_SKEW_EXCEEDED",
                    "Canary target quotes exceed the configured bundle skew",
                )

        current = {
            item.symbol: item.quantity
            for item in positions
            if item.quantity > ZERO
        }
        targets: dict[str, Decimal] = {}
        for symbol in self._config.allowed_symbols:
            quote = quotes.get(symbol)
            if quote is None:
                targets[symbol] = ZERO
                continue
            midpoint = (quote.bid_price + quote.ask_price) / Decimal("2")
            targets[symbol] = (
                account.equity * risky_weights[symbol] / midpoint
            ).to_integral_value(rounding=ROUND_FLOOR)

        plans: list[_OrderPlan] = []
        for symbol in sorted(self._config.allowed_symbols):
            delta = targets[symbol] - current.get(symbol, ZERO)
            if delta == ZERO:
                continue
            side = OrderSide.BUY if delta > ZERO else OrderSide.SELL
            if (
                side is OrderSide.BUY
                and not buys_allowed
            ):
                continue
            quote = quotes.get(symbol)
            if quote is None:
                raise Q1AlpacaPaperCanaryBlocked(
                    "QUOTE_REQUIRED",
                    f"A fresh {symbol} quote is required",
                )
            if (
                side is OrderSide.BUY
                and quote.ask_size_round_lots <= 0
            ) or (
                side is OrderSide.SELL
                and quote.bid_size_round_lots <= 0
            ):
                raise Q1AlpacaPaperCanaryBlocked(
                    "NO_DISPLAYED_EXECUTABLE_SIZE",
                    f"{symbol} has no displayed executable-side size",
                )
            quantity = abs(delta)
            reference = (
                quote.ask_price
                if side is OrderSide.BUY
                else quote.bid_price
            )
            notional = quantity * reference
            if notional < self._config.minimum_order_notional_usd:
                continue
            limit_price = self._limit_price(
                side=side,
                bid=quote.bid_price,
                ask=quote.ask_price,
            )
            plans.append(
                _OrderPlan(
                    source_decision_id=decision.portfolio_decision_id,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    limit_price=limit_price,
                    quote_id=quote.quote_id,
                    quote_as_of=_aware(quote.event_time),
                    reason=(
                        "FOLLOW_VERSIONED_SOURCE_TARGET"
                        if risk_state == "NORMAL"
                        else f"DETERMINISTIC_{risk_state}_TARGET"
                    ),
                )
            )
        sells = tuple(plan for plan in plans if plan.side is OrderSide.SELL)
        if sells:
            return sells
        buy_budget = max(ZERO, account.cash)
        feasible_buys: list[_OrderPlan] = []
        for plan in plans:
            required = plan.quantity * plan.limit_price
            if required > buy_budget:
                affordable = (
                    buy_budget / plan.limit_price
                ).to_integral_value(rounding=ROUND_FLOOR)
                if affordable <= ZERO:
                    continue
                plan = _OrderPlan(
                    source_decision_id=plan.source_decision_id,
                    symbol=plan.symbol,
                    side=plan.side,
                    quantity=affordable,
                    limit_price=plan.limit_price,
                    quote_id=plan.quote_id,
                    quote_as_of=plan.quote_as_of,
                    reason="FOLLOW_TARGET_SETTLED_CASH_CAPPED",
                )
                required = affordable * plan.limit_price
            if required < self._config.minimum_order_notional_usd:
                continue
            feasible_buys.append(plan)
            buy_budget -= required
        return tuple(feasible_buys)

    def _fresh_quotes(
        self,
        *,
        symbols: tuple[str, ...],
        now: datetime,
    ) -> dict[str, MarketQuoteRow]:
        if not symbols:
            return {}
        with self._session_factory() as session:
            stream = session.get(
                MarketStreamStatusRow,
                {"provider": PROVIDER, "feed": FEED},
            )
            if (
                stream is None
                or stream.state != self._config.required_stream_status
                or stream.last_quote_at is None
                or now - _aware(stream.last_quote_at)
                > timedelta(
                    seconds=self._config.maximum_quote_age_seconds
                )
            ):
                raise Q1AlpacaPaperCanaryBlocked(
                    "MARKET_STREAM_NOT_CONNECTED",
                    "A current CONNECTED Alpaca IEX stream is required",
                )
            rows: dict[str, MarketQuoteRow] = {}
            for symbol in symbols:
                quote = session.scalar(
                    select(MarketQuoteRow)
                    .where(
                        MarketQuoteRow.provider == PROVIDER,
                        MarketQuoteRow.feed == FEED,
                        MarketQuoteRow.symbol == symbol,
                        MarketQuoteRow.available_at <= now,
                        MarketQuoteRow.event_time <= now,
                        MarketQuoteRow.bid_price > ZERO,
                        MarketQuoteRow.ask_price > ZERO,
                        MarketQuoteRow.ask_price
                        >= MarketQuoteRow.bid_price,
                    )
                    .order_by(
                        MarketQuoteRow.event_time.desc(),
                        MarketQuoteRow.available_at.desc(),
                        MarketQuoteRow.quote_id.desc(),
                    )
                    .limit(1)
                )
                if quote is None or now - _aware(
                    quote.event_time
                ) > timedelta(
                    seconds=self._config.maximum_quote_age_seconds
                ):
                    raise Q1AlpacaPaperCanaryBlocked(
                        "QUOTE_STALE_OR_MISSING",
                        f"A fresh point-in-time {symbol} quote is required",
                    )
                rows[symbol] = quote
            return rows

    def _limit_price(
        self,
        *,
        side: OrderSide,
        bid: Decimal,
        ask: Decimal,
    ) -> Decimal:
        offset = self._config.limit_offset_bps / Decimal("10000")
        increment = self._config.price_increment_usd
        if side is OrderSide.BUY:
            raw = ask * (ONE + offset)
            units = (raw / increment).to_integral_value(
                rounding=ROUND_CEILING
            )
        else:
            raw = bid * (ONE - offset)
            units = (raw / increment).to_integral_value(
                rounding=ROUND_FLOOR
            )
        return units * increment

    async def _submit_plans(
        self,
        *,
        binding: PaperBrokerBindingRow,
        source_decision: PortfolioDecisionRow,
        plans: Sequence[_OrderPlan],
        now: datetime,
    ) -> None:
        for plan in plans:
            client_order_id = deterministic_client_order_id(
                run_id=binding.run_id,
                source_decision_id=source_decision.portfolio_decision_id,
                symbol=plan.symbol,
                side=plan.side,
            )
            command = self._ensure_submit_command(
                binding=binding,
                source_decision=source_decision,
                plan=plan,
                client_order_id=client_order_id,
                now=now,
            )
            request = AlpacaPaperOrderRequest(
                symbol=plan.symbol,
                side=plan.side,
                quantity=plan.quantity,
                limit_price=plan.limit_price,
                client_order_id=client_order_id,
                order_type=self._config.order_type,
                time_in_force=self._config.time_in_force,
                extended_hours=self._config.extended_hours,
            )
            order = await self._client.ensure_submitted(request)
            self._append_order_result(
                binding=binding,
                command=command,
                order=order,
                now=now,
            )

    def _ensure_submit_command(
        self,
        *,
        binding: PaperBrokerBindingRow,
        source_decision: PortfolioDecisionRow,
        plan: _OrderPlan,
        client_order_id: str,
        now: datetime,
    ) -> PaperBrokerCommandRow:
        idempotency_key = (
            f"SUBMIT:{binding.binding_id}:{client_order_id}"
        )
        with self._session_factory.begin() as session:
            repository = AlpacaPaperRepository(session)
            existing = next(
                (
                    row
                    for row in repository.commands(
                        binding_id=binding.binding_id,
                        command_type="SUBMIT",
                    )
                    if row.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing is not None:
                return existing
            payload = _json_object(
                canonical_data(
                    {
                        "schema_version": "alpaca-paper-command.v1",
                        "execution_lane": self._config.execution_lane,
                        "source_arm": self._config.source_arm,
                        "source_decision_id": (
                            source_decision.portfolio_decision_id
                        ),
                        "symbol": plan.symbol,
                        "side": plan.side,
                        "quantity": plan.quantity,
                        "limit_price": plan.limit_price,
                        "order_type": self._config.order_type,
                        "time_in_force": self._config.time_in_force,
                        "extended_hours": self._config.extended_hours,
                        "quote_id": plan.quote_id,
                        "quote_as_of": plan.quote_as_of,
                        "reason": plan.reason,
                        "settled_cash_only": True,
                        "real_order_routing": False,
                    }
                )
            )
            source_manifest_hash = canonical_hash(
                {
                    "source_decision_id": (
                        source_decision.portfolio_decision_id
                    ),
                    "source_decision_hash": (
                        source_decision.decision_hash
                    ),
                    "quote_id": plan.quote_id,
                    "quote_as_of": plan.quote_as_of,
                }
            )
            command_id = stable_id(
                "alpaca-paper-command",
                idempotency_key,
            )
            command_hash = canonical_hash(
                {
                    "command_id": command_id,
                    "binding_id": binding.binding_id,
                    "idempotency_key": idempotency_key,
                    "payload": payload,
                    "config_manifest_hash": (
                        self._paper_bundle.manifest_hash
                    ),
                    "code_version": self._code_version,
                    "source_manifest_hash": source_manifest_hash,
                }
            )
            return repository.append_command(
                command_id=command_id,
                binding_id=binding.binding_id,
                run_id=binding.run_id,
                source_decision_id=(
                    source_decision.portfolio_decision_id
                ),
                command_type="SUBMIT",
                client_order_id=client_order_id,
                broker_order_id=None,
                symbol=plan.symbol,
                side=plan.side.value,
                quantity=plan.quantity,
                limit_price=plan.limit_price,
                reason=plan.reason,
                idempotency_key=idempotency_key,
                config_manifest_hash=self._paper_bundle.manifest_hash,
                code_version=self._code_version,
                source_manifest_hash=source_manifest_hash,
                command_hash=command_hash,
                payload_json=payload,
                created_at=now,
            )

    def _append_order_result(
        self,
        *,
        binding: PaperBrokerBindingRow,
        command: PaperBrokerCommandRow,
        order: AlpacaPaperOrder,
        now: datetime,
    ) -> None:
        idempotency_key = stable_id(
            "alpaca-submit-result",
            binding.binding_id,
            order.broker_order_id,
            order.status,
            order.filled_quantity,
            order.updated_at,
        )
        with self._session_factory.begin() as session:
            if self._event_exists(
                session,
                binding_id=binding.binding_id,
                idempotency_key=idempotency_key,
            ):
                return
            self._append_event(
                session=session,
                repository=AlpacaPaperRepository(session),
                binding=binding,
                command=command,
                event_type="SUBMIT_RECONCILED",
                status=order.status,
                broker_order_id=order.broker_order_id,
                client_order_id=order.client_order_id,
                provider_event_id=None,
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.quantity,
                filled_quantity=order.filled_quantity,
                fill_price=order.filled_average_price,
                occurred_at=order.updated_at,
                available_at=now,
                provider_request_id=order.provider_request_id,
                idempotency_key=idempotency_key,
                payload=_order_payload(order),
                created_at=now,
            )

    async def _cancel_orders(
        self,
        *,
        binding: PaperBrokerBindingRow,
        source_decision: PortfolioDecisionRow,
        orders: Sequence[AlpacaPaperOrder],
        now: datetime,
        reason: str,
    ) -> None:
        for order in orders:
            command = self._ensure_cancel_command(
                binding=binding,
                source_decision=source_decision,
                order=order,
                now=now,
                reason=reason,
            )
            latest = self._latest_event_for_command(command.command_id)
            if (
                latest is not None
                and latest.event_type == "CANCEL_REQUEST_ACCEPTED"
            ):
                continue
            result = await self._client.cancel_order(
                order.broker_order_id
            )
            if result.accepted:
                self._append_cancel_accepted(
                    binding=binding,
                    command=command,
                    order=order,
                    result=result,
                    now=now,
                )

    def _ensure_cancel_command(
        self,
        *,
        binding: PaperBrokerBindingRow,
        source_decision: PortfolioDecisionRow,
        order: AlpacaPaperOrder,
        now: datetime,
        reason: str,
    ) -> PaperBrokerCommandRow:
        idempotency_key = (
            f"CANCEL:{binding.binding_id}:{order.broker_order_id}"
        )
        with self._session_factory.begin() as session:
            repository = AlpacaPaperRepository(session)
            existing = next(
                (
                    row
                    for row in repository.commands(
                        binding_id=binding.binding_id,
                        command_type="CANCEL",
                    )
                    if row.idempotency_key == idempotency_key
                ),
                None,
            )
            if existing is not None:
                return existing
            payload = _json_object(
                canonical_data(
                    {
                        "schema_version": "alpaca-paper-cancel.v1",
                        "broker_order_id": order.broker_order_id,
                        "client_order_id": order.client_order_id,
                        "source_decision_id": (
                            source_decision.portfolio_decision_id
                        ),
                        "reason": reason,
                        "real_order_routing": False,
                    }
                )
            )
            source_manifest_hash = canonical_hash(
                {
                    "broker_order_id": order.broker_order_id,
                    "client_order_id": order.client_order_id,
                    "broker_status": order.status,
                    "broker_updated_at": order.updated_at,
                    "source_decision_id": (
                        source_decision.portfolio_decision_id
                    ),
                }
            )
            command_id = stable_id(
                "alpaca-paper-command",
                idempotency_key,
            )
            command_hash = canonical_hash(
                {
                    "command_id": command_id,
                    "idempotency_key": idempotency_key,
                    "payload": payload,
                    "source_manifest_hash": source_manifest_hash,
                }
            )
            return repository.append_command(
                command_id=command_id,
                binding_id=binding.binding_id,
                run_id=binding.run_id,
                source_decision_id=(
                    source_decision.portfolio_decision_id
                ),
                command_type="CANCEL",
                client_order_id=order.client_order_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.remaining_quantity,
                limit_price=order.limit_price,
                reason=reason,
                idempotency_key=idempotency_key,
                config_manifest_hash=self._paper_bundle.manifest_hash,
                code_version=self._code_version,
                source_manifest_hash=source_manifest_hash,
                command_hash=command_hash,
                payload_json=payload,
                created_at=now,
            )

    def _append_cancel_accepted(
        self,
        *,
        binding: PaperBrokerBindingRow,
        command: PaperBrokerCommandRow,
        order: AlpacaPaperOrder,
        result: AlpacaPaperCancelResult,
        now: datetime,
    ) -> None:
        idempotency_key = stable_id(
            "alpaca-cancel-accepted",
            command.command_id,
        )
        with self._session_factory.begin() as session:
            if self._event_exists(
                session,
                binding_id=binding.binding_id,
                idempotency_key=idempotency_key,
            ):
                return
            self._append_event(
                session=session,
                repository=AlpacaPaperRepository(session),
                binding=binding,
                command=command,
                event_type="CANCEL_REQUEST_ACCEPTED",
                status="pending_cancel",
                broker_order_id=order.broker_order_id,
                client_order_id=order.client_order_id,
                provider_event_id=None,
                symbol=order.symbol,
                side=order.side.value,
                quantity=order.remaining_quantity,
                filled_quantity=order.filled_quantity,
                fill_price=order.filled_average_price,
                occurred_at=now,
                available_at=now,
                provider_request_id=result.provider_request_id,
                idempotency_key=idempotency_key,
                payload=_json_object(
                    canonical_data(
                        {
                            "cancel_request_accepted": True,
                            "terminal_cancellation_confirmed": False,
                            "broker_order_id": order.broker_order_id,
                        }
                    )
                ),
                created_at=now,
            )

    def _record_ready(
        self,
        *,
        binding: PaperBrokerBindingRow,
        now: datetime,
    ) -> None:
        idempotency_key = stable_id(
            "alpaca-reconciliation-ready",
            binding.binding_id,
            now,
        )
        with self._session_factory.begin() as session:
            self._append_event(
                session=session,
                repository=AlpacaPaperRepository(session),
                binding=binding,
                command=None,
                event_type="RECONCILIATION_READY",
                status="READY",
                broker_order_id=None,
                client_order_id=None,
                provider_event_id=None,
                symbol=None,
                side=None,
                quantity=None,
                filled_quantity=None,
                fill_price=None,
                occurred_at=now,
                available_at=now,
                provider_request_id=None,
                idempotency_key=idempotency_key,
                payload={
                    "real_order_routing": False,
                    "matched_attribution_included": False,
                },
                created_at=now,
            )

    def _record_blocked(
        self,
        *,
        run_id: str,
        now: datetime,
        code: str,
        detail: str,
    ) -> None:
        try:
            with self._session_factory.begin() as session:
                repository = AlpacaPaperRepository(session)
                binding = repository.binding_for_run(run_id)
                if binding is None:
                    return
                idempotency_key = stable_id(
                    "alpaca-reconciliation-blocked",
                    binding.binding_id,
                    now,
                    code,
                )
                self._append_event(
                    session=session,
                    repository=repository,
                    binding=binding,
                    command=None,
                    event_type="RECONCILIATION_BLOCKED",
                    status=code,
                    broker_order_id=None,
                    client_order_id=None,
                    provider_event_id=None,
                    symbol=None,
                    side=None,
                    quantity=None,
                    filled_quantity=None,
                    fill_price=None,
                    occurred_at=now,
                    available_at=now,
                    provider_request_id=None,
                    idempotency_key=idempotency_key,
                    payload={
                        "code": code,
                        "detail": detail,
                        "real_order_routing": False,
                    },
                    created_at=now,
                )
        except Exception:
            # A diagnostic record must never broaden a broker failure.
            return

    def _append_event(
        self,
        *,
        session: Session,
        repository: AlpacaPaperRepository,
        binding: PaperBrokerBindingRow,
        command: PaperBrokerCommandRow | None,
        event_type: str,
        status: str,
        broker_order_id: str | None,
        client_order_id: str | None,
        provider_event_id: str | None,
        symbol: str | None,
        side: str | None,
        quantity: Decimal | None,
        filled_quantity: Decimal | None,
        fill_price: Decimal | None,
        occurred_at: datetime,
        available_at: datetime,
        provider_request_id: str | None,
        idempotency_key: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> PaperBrokerEventRow:
        del session
        source_manifest_hash = canonical_hash(payload)
        event_id = stable_id(
            "alpaca-paper-event",
            binding.binding_id,
            idempotency_key,
        )
        event_hash = canonical_hash(
            {
                "event_id": event_id,
                "binding_id": binding.binding_id,
                "command_id": (
                    None if command is None else command.command_id
                ),
                "event_type": event_type,
                "status": status,
                "broker_order_id": broker_order_id,
                "client_order_id": client_order_id,
                "provider_event_id": provider_event_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "filled_quantity": filled_quantity,
                "fill_price": fill_price,
                "occurred_at": occurred_at,
                "available_at": available_at,
                "idempotency_key": idempotency_key,
                "source_manifest_hash": source_manifest_hash,
            }
        )
        return repository.append_event(
            event_id=event_id,
            binding_id=binding.binding_id,
            run_id=binding.run_id,
            command_id=None if command is None else command.command_id,
            event_type=event_type,
            status=status,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            provider_event_id=provider_event_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            occurred_at=occurred_at,
            available_at=available_at,
            provider_request_id=provider_request_id,
            idempotency_key=idempotency_key,
            config_manifest_hash=self._paper_bundle.manifest_hash,
            code_version=self._code_version,
            source_manifest_hash=source_manifest_hash,
            event_hash=event_hash,
            payload_json=payload,
            created_at=created_at,
        )

    def _event_exists(
        self,
        session: Session,
        *,
        binding_id: str,
        idempotency_key: str,
    ) -> bool:
        return (
            session.scalar(
                select(PaperBrokerEventRow.event_id).where(
                    PaperBrokerEventRow.binding_id == binding_id,
                    PaperBrokerEventRow.idempotency_key
                    == idempotency_key,
                )
            )
            is not None
        )

    def _latest_event_for_command(
        self,
        command_id: str,
    ) -> PaperBrokerEventRow | None:
        with self._session_factory() as session:
            return session.scalar(
                select(PaperBrokerEventRow)
                .where(PaperBrokerEventRow.command_id == command_id)
                .order_by(
                    PaperBrokerEventRow.available_at.desc(),
                    PaperBrokerEventRow.event_id.desc(),
                )
                .limit(1)
            )


def alpaca_paper_canary_status(
    session: Session,
    *,
    run_id: str,
    enabled: bool,
    config: AlpacaPaperConfigBundle | None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": enabled,
        "state": "DISABLED_NOT_CONFIGURED",
        "execution_lane": "ALPACA_PAPER_CANARY",
        "provider": "ALPACA",
        "source_arm": (
            None if config is None else config.config.source_arm.value
        ),
        "rest_base_url": (
            None if config is None else config.config.rest_base_url
        ),
        "account_bound": False,
        "account_ready": False,
        "reconciliation_status": "NOT_BOUND",
        "initial_equity_usd": None,
        "initial_cash_usd": None,
        "current_equity_usd": None,
        "current_cash_usd": None,
        "cumulative_return": None,
        "positions": [],
        "open_orders": [],
        "open_order_count": 0,
        "latest_source_decision_id": None,
        "last_sync_at": None,
        "last_error_code": None,
        "last_error_detail": None,
        "consecutive_failures": 0,
        "maximum_consecutive_failures": (
            None
            if config is None
            else config.config.maximum_consecutive_failures
        ),
        "matched_attribution_included": False,
        "real_order_routing": False,
    }
    if not enabled or config is None:
        return base
    binding = session.scalar(
        select(PaperBrokerBindingRow).where(
            PaperBrokerBindingRow.run_id == run_id
        )
    )
    if binding is None:
        return {
            **base,
            "state": "ENABLED_AWAITING_CLEAN_BINDING",
            "reconciliation_status": "NOT_BOUND",
        }
    events = tuple(
        session.scalars(
            select(PaperBrokerEventRow)
            .where(
                PaperBrokerEventRow.binding_id == binding.binding_id
            )
            .order_by(
                PaperBrokerEventRow.available_at,
                PaperBrokerEventRow.event_id,
            )
        )
    )
    account = next(
        (
            row
            for row in reversed(events)
            if row.event_type == "ACCOUNT_SNAPSHOT"
        ),
        None,
    )
    positions = next(
        (
            row
            for row in reversed(events)
            if row.event_type == "POSITIONS_SNAPSHOT"
        ),
        None,
    )
    reconciliation = next(
        (
            row
            for row in reversed(events)
            if row.event_type
            in {"RECONCILIATION_READY", "RECONCILIATION_BLOCKED"}
        ),
        None,
    )
    order_states: dict[str, PaperBrokerEventRow] = {}
    for row in events:
        if (
            row.broker_order_id is not None
            and row.event_type
            in {
                "ORDER_SNAPSHOT",
                "SUBMIT_RECONCILED",
                "CANCEL_REQUEST_ACCEPTED",
            }
        ):
            order_states[row.broker_order_id] = row
    open_orders = [
        {
            "broker_order_id": row.broker_order_id,
            "client_order_id": row.client_order_id,
            "symbol": row.symbol,
            "side": row.side,
            "quantity": (
                None if row.quantity is None else str(row.quantity)
            ),
            "filled_quantity": (
                None
                if row.filled_quantity is None
                else str(row.filled_quantity)
            ),
            "status": row.status,
        }
        for row in order_states.values()
        if row.status in ALPACA_PAPER_OPEN_STATUSES
    ]
    latest_decision = session.scalar(
        select(PortfolioDecisionRow)
        .where(
            PortfolioDecisionRow.run_id == run_id,
            PortfolioDecisionRow.arm_id == binding.source_arm_id,
            PortfolioDecisionRow.algorithm_version
            == Q1_ALGORITHM_VERSION,
        )
        .order_by(
            desc(PortfolioDecisionRow.decision_created_at),
            desc(PortfolioDecisionRow.portfolio_decision_id),
        )
        .limit(1)
    )
    current_equity = (
        None
        if account is None
        else _optional_decimal(account.payload_json.get("equity_usd"))
    )
    current_cash = (
        None
        if account is None
        else _optional_decimal(account.payload_json.get("cash_usd"))
    )
    cumulative_return = (
        None
        if current_equity is None or binding.initial_equity_usd <= ZERO
        else current_equity / binding.initial_equity_usd - ONE
    )
    blocked = (
        reconciliation is not None
        and reconciliation.event_type == "RECONCILIATION_BLOCKED"
    )
    consecutive_failures = 0
    for row in reversed(events):
        if row.event_type == "RECONCILIATION_READY":
            break
        if row.event_type == "RECONCILIATION_BLOCKED":
            consecutive_failures += 1
    failure_threshold_reached = (
        consecutive_failures
        >= config.config.maximum_consecutive_failures
    )
    return {
        **base,
        "state": (
            "BLOCKED_FAILURE_THRESHOLD"
            if blocked and failure_threshold_reached
            else "BLOCKED"
            if blocked
            else "READY"
        ),
        "source_arm": binding.source_arm_id,
        "account_bound": True,
        "account_ready": (
            False
            if account is None
            else bool(account.payload_json.get("account_ready"))
        ),
        "reconciliation_status": (
            "AWAITING_FIRST_SYNC"
            if reconciliation is None
            else reconciliation.status
        ),
        "initial_equity_usd": str(binding.initial_equity_usd),
        "initial_cash_usd": str(binding.initial_cash_usd),
        "current_equity_usd": (
            None if current_equity is None else str(current_equity)
        ),
        "current_cash_usd": (
            None if current_cash is None else str(current_cash)
        ),
        "cumulative_return": (
            None
            if cumulative_return is None
            else str(cumulative_return)
        ),
        "positions": (
            []
            if positions is None
            else positions.payload_json.get("positions", [])
        ),
        "open_orders": sorted(
            open_orders,
            key=lambda item: (
                str(item["symbol"]),
                str(item["broker_order_id"]),
            ),
        ),
        "open_order_count": len(open_orders),
        "latest_source_decision_id": (
            None
            if latest_decision is None
            else latest_decision.portfolio_decision_id
        ),
        "last_sync_at": (
            None
            if not events
            else _iso(max(_aware(row.available_at) for row in events))
        ),
        "last_error_code": (
            reconciliation.status
            if blocked and reconciliation is not None
            else None
        ),
        "last_error_detail": (
            None
            if not blocked or reconciliation is None
            else reconciliation.payload_json.get("detail")
        ),
        "consecutive_failures": consecutive_failures,
    }


def _order_payload(order: AlpacaPaperOrder) -> dict[str, Any]:
    return _json_object(
        canonical_data(
            {
                "broker_order_id": order.broker_order_id,
                "client_order_id": order.client_order_id,
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "filled_quantity": order.filled_quantity,
                "filled_average_price": order.filled_average_price,
                "limit_price": order.limit_price,
                "status": order.status,
                "submitted_at": order.submitted_at,
                "updated_at": order.updated_at,
            }
        )
    )


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except ArithmeticError:
        return None
    return result if result.is_finite() else None


def _json_object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Q1AlpacaPaperCanaryError(
            "Expected a canonical JSON object"
        )
    return cast(dict[str, Any], value)


def _aware(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")
