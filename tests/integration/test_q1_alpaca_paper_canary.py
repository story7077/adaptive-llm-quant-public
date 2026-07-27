from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash
from trading.domain.q1 import (
    PointInTimeSourceReference,
    Q1DecisionInputManifest,
    Q1StrategyDecision,
)
from trading.execution.alpaca_paper import (
    AlpacaPaperAccount,
    AlpacaPaperCancelResult,
    AlpacaPaperFillActivity,
    AlpacaPaperOrder,
    AlpacaPaperOrderRequest,
    AlpacaPaperPosition,
)
from trading.persistence.alpaca_paper import AlpacaPaperRepository
from trading.persistence.models import (
    ArmStateSnapshotRow,
    FillRow,
    MarketCalendarSessionRow,
    MarketQuoteRow,
    MarketStreamStatusRow,
    MatchedAttributionResultRow,
    NavSnapshotRow,
    PaperBrokerBindingRow,
    PaperBrokerCommandRow,
    PaperBrokerEventRow,
    PortfolioDecisionRow,
    RunRow,
)
from trading.runtime.provenance import workspace_code_version
from trading.runtime.q1_alpaca_paper import Q1AlpacaPaperCanaryService
from trading.settings import (
    load_alpaca_paper_config_bundle,
    load_q1_config_bundle,
)

RUN_ID = "alpaca-paper-canary-test"
DECISION_ID = "alpaca-paper-canary-decision"
BINDING_ID = "alpaca-paper-canary-binding"
COMMAND_ID = "alpaca-paper-canary-command"
CLIENT_ORDER_ID = "q1p-test-client-order"
BROKER_ORDER_ID = "alpaca-paper-order-1"
HASH = "b" * 64
NOW = datetime(2026, 7, 27, 14, 1, tzinfo=UTC)
SYNC_AT = datetime(2026, 7, 27, 14, 5, tzinfo=UTC)
SESSION_OPEN = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
SESSION_CLOSE = datetime(2026, 7, 27, 20, 0, tzinfo=UTC)


class _FakePaperClient:
    def __init__(
        self,
        *,
        positions: tuple[AlpacaPaperPosition, ...] = (),
        orders: tuple[AlpacaPaperOrder, ...] = (),
        fills: tuple[AlpacaPaperFillActivity, ...] = (),
    ) -> None:
        self.account = AlpacaPaperAccount(
            account_id="sensitive-paper-account-id",
            status="ACTIVE",
            currency="USD",
            equity=Decimal("100000"),
            cash=Decimal("100000"),
            buying_power=Decimal("200000"),
            account_blocked=False,
            trading_blocked=False,
            trade_suspended_by_user=False,
            provider_request_id="provider-account-request",
        )
        self.positions = positions
        self.orders = orders
        self.fills = fills
        self.ensure_requests: list[AlpacaPaperOrderRequest] = []
        self.cancel_requests: list[str] = []
        self.closed = False

    async def get_account(self) -> AlpacaPaperAccount:
        return self.account

    async def list_positions(self) -> tuple[AlpacaPaperPosition, ...]:
        return self.positions

    async def list_orders(
        self,
        *,
        status: str = "all",
        limit: int = 500,
    ) -> tuple[AlpacaPaperOrder, ...]:
        assert status == "all"
        assert limit <= 500
        return self.orders

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> AlpacaPaperOrder | None:
        return next(
            (
                order
                for order in self.orders
                if order.client_order_id == client_order_id
            ),
            None,
        )

    async def ensure_submitted(
        self,
        request: AlpacaPaperOrderRequest,
    ) -> AlpacaPaperOrder:
        self.ensure_requests.append(request)
        order = AlpacaPaperOrder(
            broker_order_id=f"broker-{len(self.ensure_requests)}",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            filled_quantity=Decimal("0"),
            filled_average_price=None,
            limit_price=request.limit_price,
            status="accepted",
            submitted_at=SYNC_AT,
            updated_at=SYNC_AT,
            provider_request_id="provider-submit-request",
        )
        self.orders = (*self.orders, order)
        return order

    async def cancel_order(
        self,
        broker_order_id: str,
    ) -> AlpacaPaperCancelResult:
        self.cancel_requests.append(broker_order_id)
        return AlpacaPaperCancelResult(
            broker_order_id=broker_order_id,
            accepted=True,
            provider_request_id="provider-cancel-request",
        )

    async def list_fill_activities(
        self,
        *,
        after: datetime | None = None,
        page_size: int = 100,
    ) -> tuple[AlpacaPaperFillActivity, ...]:
        del after
        assert page_size <= 100
        return self.fills

    async def aclose(self) -> None:
        self.closed = True


def _seed_run_only(session) -> None:
    session.add(
        RunRow(
            run_id=RUN_ID,
            mode="PAPER",
            experiment_version="q1_math_core_v1",
            config_manifest_hash=HASH,
            code_commit="test-code",
            started_at=NOW - timedelta(minutes=1),
            ended_at=None,
            status="RUNNING",
            result_manifest={
                "real_order_routing": False,
                "alpaca_paper_canary": True,
            },
            result_hash=None,
        )
    )
    session.flush()


def _seed_run_and_decision(session) -> None:
    _seed_run_only(session)
    session.add(
        PortfolioDecisionRow(
            portfolio_decision_id=DECISION_ID,
            run_id=RUN_ID,
            arm_id="Q1-LLM",
            source_cycle_id=None,
            input_state_sequence=1,
            decision_time=NOW - timedelta(seconds=5),
            algorithm_version="q1_math_core_v1",
            scheduled_at=NOW - timedelta(minutes=1),
            signal_data_cutoff=NOW - timedelta(minutes=1),
            portfolio_state_as_of=NOW - timedelta(seconds=5),
            quote_as_of=NOW - timedelta(seconds=5),
            decision_created_at=NOW - timedelta(seconds=5),
            valid_until=NOW + timedelta(minutes=19),
            calendar_session_id=None,
            config_manifest_hash=HASH,
            code_version="test-code",
            model_version="q1-llm-test",
            source_manifest_hash=HASH,
            input_manifest_hash=HASH,
            payload_json={
                "decision_kind": "STRATEGIC_LLM_OVERLAY",
                "target_weights": {
                    "QQQ": "0.5",
                    "SOXX": "0",
                    "USD_CASH": "0.5",
                },
            },
            decision_hash=canonical_hash({"decision_id": DECISION_ID}),
        )
    )
    session.flush()


def _seed_service_run(session, repository_root: Path) -> None:
    q1_config = load_q1_config_bundle(repository_root / "config")
    session.add(
        RunRow(
            run_id=RUN_ID,
            mode="PAPER",
            experiment_version="q1_math_core_v1",
            config_manifest_hash=q1_config.manifest_hash,
            code_commit=workspace_code_version(repository_root),
            started_at=NOW - timedelta(minutes=1),
            ended_at=None,
            status="RUNNING",
            result_manifest={"real_order_routing": False},
            result_hash=None,
        )
    )
    session.flush()


def _service(
    factory,
    *,
    repository_root: Path,
    client: _FakePaperClient,
) -> Q1AlpacaPaperCanaryService:
    return Q1AlpacaPaperCanaryService(
        factory,
        q1_config=load_q1_config_bundle(repository_root / "config"),
        paper_config=load_alpaca_paper_config_bundle(
            repository_root / "config"
        ),
        client=client,
        workspace_root=repository_root,
    )


def _seed_service_decision_and_market(
    session,
    *,
    repository_root: Path,
    now: datetime,
) -> None:
    q1_config = load_q1_config_bundle(repository_root / "config")
    code_version = workspace_code_version(repository_root)
    calendar_session_id = (
        f"alpaca-paper-calendar-{now.date().isoformat()}"
    )
    quote_id = f"alpaca-paper-quote-{now.isoformat()}"
    source_hash = canonical_hash(
        {
            "calendar_session_id": calendar_session_id,
            "quote_id": quote_id,
            "now": now,
        }
    )
    calendar_available_at = SESSION_OPEN - timedelta(days=7)
    session.add(
        MarketCalendarSessionRow(
            calendar_session_id=calendar_session_id,
            algorithm_version="q1_math_core_v1",
            calendar_version="alpaca_market_calendar_v1",
            session_date=date(2026, 7, 27),
            open_at=SESSION_OPEN,
            close_at=SESSION_CLOSE,
            source="test-calendar",
            available_at=calendar_available_at,
            config_manifest_hash=q1_config.manifest_hash,
            code_version=code_version,
            model_version="test-calendar",
            source_manifest_hash=source_hash,
            session_hash=canonical_hash(
                {
                    "open_at": SESSION_OPEN,
                    "close_at": SESSION_CLOSE,
                }
            ),
            payload_json={"early_close": False},
            created_at=calendar_available_at,
        )
    )
    quote_at = now - timedelta(seconds=2)
    session.add(
        MarketQuoteRow(
            quote_id=quote_id,
            provider="alpaca",
            feed="iex",
            symbol="QQQ",
            event_time=quote_at,
            provider_timestamp=quote_at.isoformat(),
            available_at=quote_at,
            ingested_at=quote_at,
            source_kind="STREAM_QUOTE",
            bid_exchange="V",
            bid_price=Decimal("499.90"),
            bid_size_round_lots=10,
            ask_exchange="V",
            ask_price=Decimal("500.10"),
            ask_size_round_lots=10,
            conditions=[],
            tape="C",
            payload_hash=canonical_hash(
                {
                    "quote_id": quote_id,
                    "bid": "499.90",
                    "ask": "500.10",
                }
            ),
            raw_object_uri=None,
            payload_json={"source": "test"},
        )
    )
    session.add(
        MarketStreamStatusRow(
            provider="alpaca",
            feed="iex",
            state="CONNECTED",
            connected_at=SESSION_OPEN,
            disconnected_at=None,
            last_message_at=quote_at,
            last_bar_at=None,
            last_quote_at=quote_at,
            last_trade_at=None,
            reconnect_count=0,
            consecutive_failures=0,
            last_error_code=None,
            last_error_detail=None,
            updated_at=quote_at,
        )
    )
    manifest = Q1DecisionInputManifest(
        calendar_session_id=calendar_session_id,
        source_bars=(),
        quotes=(
            PointInTimeSourceReference(
                record_id=quote_id,
                available_at=quote_at,
            ),
        ),
        config_manifest_hash=q1_config.manifest_hash,
        code_version=code_version,
        model_version="q1-llm-test",
        source_manifest_hash=source_hash,
        manifest_hash=canonical_hash(
            {
                "calendar_session_id": calendar_session_id,
                "quote_id": quote_id,
            }
        ),
    )
    decision_created_at = now - timedelta(seconds=1)
    decision = Q1StrategyDecision(
        portfolio_decision_id=DECISION_ID,
        run_id=RUN_ID,
        arm_id="Q1-LLM",
        source_cycle_id="service-test-cycle",
        input_state_sequence=1,
        decision_kind="STRATEGIC_LLM_OVERLAY",
        scheduled_at=now - timedelta(minutes=5),
        signal_data_cutoff=now - timedelta(minutes=5),
        portfolio_state_as_of=now - timedelta(seconds=3),
        quote_as_of=quote_at,
        decision_created_at=decision_created_at,
        valid_until=now + timedelta(minutes=20),
        input_manifest=manifest,
        target_weights={
            "QQQ": Decimal("0.5"),
            "SOXX": Decimal("0"),
            "USD_CASH": Decimal("0.5"),
        },
        diagnostics={"test": True},
        worker_fence_token="service-test-worker",
        cycle_attempt_count=1,
        config_manifest_hash=q1_config.manifest_hash,
        code_version=code_version,
        model_version="q1-llm-test",
        source_manifest_hash=source_hash,
        decision_hash=canonical_hash(
            {
                "portfolio_decision_id": DECISION_ID,
                "target_weights": {
                    "QQQ": "0.5",
                    "SOXX": "0",
                    "USD_CASH": "0.5",
                },
            }
        ),
    )
    session.add(
        PortfolioDecisionRow(
            portfolio_decision_id=decision.portfolio_decision_id,
            run_id=decision.run_id,
            arm_id=decision.arm_id.value,
            source_cycle_id=None,
            input_state_sequence=decision.input_state_sequence,
            decision_time=decision.decision_created_at,
            algorithm_version=decision.algorithm_version,
            scheduled_at=decision.scheduled_at,
            signal_data_cutoff=decision.signal_data_cutoff,
            portfolio_state_as_of=decision.portfolio_state_as_of,
            quote_as_of=decision.quote_as_of,
            decision_created_at=decision.decision_created_at,
            valid_until=decision.valid_until,
            calendar_session_id=calendar_session_id,
            config_manifest_hash=decision.config_manifest_hash,
            code_version=decision.code_version,
            model_version=decision.model_version,
            source_manifest_hash=decision.source_manifest_hash,
            input_manifest_hash=decision.input_manifest.manifest_hash,
            payload_json=decision.model_dump(mode="json"),
            decision_hash=decision.decision_hash,
        )
    )
    session.flush()


def _foreign_open_order() -> AlpacaPaperOrder:
    return AlpacaPaperOrder(
        broker_order_id="foreign-broker-order",
        client_order_id="foreign-client-order",
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        filled_average_price=None,
        limit_price=Decimal("500"),
        status="new",
        submitted_at=SYNC_AT,
        updated_at=SYNC_AT,
        provider_request_id="provider-foreign-order",
    )


def _foreign_position() -> AlpacaPaperPosition:
    return AlpacaPaperPosition(
        symbol="NVDA",
        quantity=Decimal("1"),
        market_value=Decimal("200"),
        average_entry_price=Decimal("190"),
        side="long",
    )


def _internal_counts(session) -> dict[str, int]:
    return {
        "fills": (
            session.scalar(select(func.count()).select_from(FillRow))
            or 0
        ),
        "states": (
            session.scalar(
                select(func.count()).select_from(ArmStateSnapshotRow)
            )
            or 0
        ),
        "matched": (
            session.scalar(
                select(func.count()).select_from(
                    MatchedAttributionResultRow
                )
            )
            or 0
        ),
    }


def _append_broker_records(repository: AlpacaPaperRepository) -> None:
    binding_payload = {
        "execution_lane": "ALPACA_PAPER_CANARY",
        "source_arm_id": "Q1-LLM",
        "real_order_routing": False,
    }
    binding_hash = canonical_hash(binding_payload)
    repository.create_binding(
        binding_id=BINDING_ID,
        run_id=RUN_ID,
        source_arm_id="Q1-LLM",
        account_id_hash=canonical_hash({"paper_account": "test"}),
        initial_equity_usd=Decimal("100000"),
        initial_cash_usd=Decimal("100000"),
        config_manifest_hash=HASH,
        code_version="test-code",
        binding_hash=binding_hash,
        payload_json=binding_payload,
        created_at=NOW,
    )
    command_payload = {
        "command_type": "SUBMIT",
        "symbol": "QQQ",
        "side": "BUY",
        "quantity": "2",
        "limit_price": "500.20",
        "client_order_id": CLIENT_ORDER_ID,
    }
    command_hash = canonical_hash(command_payload)
    repository.append_command(
        command_id=COMMAND_ID,
        binding_id=BINDING_ID,
        run_id=RUN_ID,
        source_decision_id=DECISION_ID,
        command_type="SUBMIT",
        client_order_id=CLIENT_ORDER_ID,
        broker_order_id=None,
        symbol="QQQ",
        side="BUY",
        quantity=Decimal("2"),
        limit_price=Decimal("500.20"),
        reason="TARGET_DELTA",
        idempotency_key="submit-q1p-test-client-order",
        config_manifest_hash=HASH,
        code_version="test-code",
        source_manifest_hash=HASH,
        command_hash=command_hash,
        payload_json=command_payload,
        created_at=NOW,
    )
    fill_payload = {
        "provider_event_id": "fill-activity-1",
        "broker_order_id": BROKER_ORDER_ID,
        "filled_quantity": "0.75",
        "fill_price": "500.125",
    }
    event_hash = canonical_hash(fill_payload)
    repository.append_event(
        event_id="alpaca-paper-fill-event",
        binding_id=BINDING_ID,
        run_id=RUN_ID,
        command_id=COMMAND_ID,
        event_type="FILL",
        status="partially_filled",
        broker_order_id=BROKER_ORDER_ID,
        client_order_id=CLIENT_ORDER_ID,
        provider_event_id="fill-activity-1",
        symbol="QQQ",
        side="BUY",
        quantity=Decimal("0.75"),
        filled_quantity=Decimal("0.75"),
        fill_price=Decimal("500.125"),
        occurred_at=NOW + timedelta(seconds=1),
        available_at=NOW + timedelta(seconds=2),
        provider_request_id="alpaca-request-1",
        idempotency_key="fill-activity-1",
        config_manifest_hash=HASH,
        code_version="test-code",
        source_manifest_hash=HASH,
        event_hash=event_hash,
        payload_json=fill_payload,
        created_at=NOW + timedelta(seconds=2),
    )


def test_canary_fill_is_idempotent_and_never_becomes_internal_q1_fill(
    sqlite_database,
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_run_and_decision(session)
        repository = AlpacaPaperRepository(session)
        _append_broker_records(repository)
        _append_broker_records(repository)

    with factory() as session:
        broker_counts = {
            "bindings": session.scalar(
                select(func.count()).select_from(PaperBrokerBindingRow)
            ),
            "commands": session.scalar(
                select(func.count()).select_from(PaperBrokerCommandRow)
            ),
            "events": session.scalar(
                select(func.count()).select_from(PaperBrokerEventRow)
            ),
        }
        internal_counts = {
            "fills": session.scalar(
                select(func.count()).select_from(FillRow)
            ),
            "states": session.scalar(
                select(func.count()).select_from(ArmStateSnapshotRow)
            ),
            "matched": session.scalar(
                select(func.count()).select_from(
                    MatchedAttributionResultRow
                )
            ),
        }
        event = session.scalar(select(PaperBrokerEventRow))

    assert broker_counts == {
        "bindings": 1,
        "commands": 1,
        "events": 1,
    }
    assert internal_counts == {
        "fills": 0,
        "states": 0,
        "matched": 0,
    }
    assert event is not None
    assert event.event_type == "FILL"
    assert event.provider_event_id == "fill-activity-1"
    assert event.filled_quantity == Decimal("0.75")


def test_persistence_boundaries_recursively_reject_credential_payloads(
    sqlite_database,
) -> None:
    _database_url, _engine, factory = sqlite_database
    sensitive_keys = (
        "alpaca_secret_key",
        "api_key",
        "credentials",
        "authorization",
        "password",
        "access_token",
    )

    with factory.begin() as session:
        _seed_run_and_decision(session)
        repository = AlpacaPaperRepository(session)
        binding_values = {
            "binding_id": BINDING_ID,
            "run_id": RUN_ID,
            "source_arm_id": "Q1-LLM",
            "account_id_hash": canonical_hash(
                {"paper_account": "test"}
            ),
            "initial_equity_usd": Decimal("100000"),
            "initial_cash_usd": Decimal("100000"),
            "config_manifest_hash": HASH,
            "code_version": "test-code",
            "binding_hash": canonical_hash({"binding": BINDING_ID}),
            "created_at": NOW,
        }
        for key in sensitive_keys:
            with pytest.raises(ValueError):
                repository.create_binding(
                    **binding_values,
                    payload_json={
                        "audit": {
                            "nested": {
                                key: "must-never-be-persisted",
                            }
                        }
                    },
                )
            assert session.scalar(
                select(func.count()).select_from(PaperBrokerBindingRow)
            ) == 0

        repository.create_binding(
            **binding_values,
            payload_json={
                "execution_lane": "ALPACA_PAPER_CANARY",
                "real_order_routing": False,
            },
        )
        session.flush()
        command_values = {
            "command_id": COMMAND_ID,
            "binding_id": BINDING_ID,
            "run_id": RUN_ID,
            "source_decision_id": DECISION_ID,
            "command_type": "SUBMIT",
            "client_order_id": CLIENT_ORDER_ID,
            "broker_order_id": None,
            "symbol": "QQQ",
            "side": "BUY",
            "quantity": Decimal("2"),
            "limit_price": Decimal("500.20"),
            "reason": "TARGET_DELTA",
            "idempotency_key": "submit-q1p-test-client-order",
            "config_manifest_hash": HASH,
            "code_version": "test-code",
            "source_manifest_hash": HASH,
            "command_hash": canonical_hash({"command": COMMAND_ID}),
            "created_at": NOW,
        }
        for key in sensitive_keys:
            with pytest.raises(ValueError):
                repository.append_command(
                    **command_values,
                    payload_json={
                        "audit": {
                            "nested": {
                                key: "must-never-be-persisted",
                            }
                        }
                    },
                )
            assert session.scalar(
                select(func.count()).select_from(PaperBrokerCommandRow)
            ) == 0

        repository.append_command(
            **command_values,
            payload_json={
                "symbol": "QQQ",
                "client_order_id": CLIENT_ORDER_ID,
            },
        )
        session.flush()
        event_values = {
            "event_id": "alpaca-paper-fill-event",
            "binding_id": BINDING_ID,
            "run_id": RUN_ID,
            "command_id": COMMAND_ID,
            "event_type": "FILL_ACTIVITY",
            "status": "partially_filled",
            "broker_order_id": BROKER_ORDER_ID,
            "client_order_id": CLIENT_ORDER_ID,
            "provider_event_id": "fill-activity-1",
            "symbol": "QQQ",
            "side": "BUY",
            "quantity": Decimal("0.75"),
            "filled_quantity": Decimal("0.75"),
            "fill_price": Decimal("500.125"),
            "occurred_at": NOW + timedelta(seconds=1),
            "available_at": NOW + timedelta(seconds=2),
            "provider_request_id": "alpaca-request-1",
            "idempotency_key": "fill-activity-1",
            "config_manifest_hash": HASH,
            "code_version": "test-code",
            "source_manifest_hash": HASH,
            "event_hash": canonical_hash(
                {"event": "fill-activity-1"}
            ),
            "created_at": NOW + timedelta(seconds=2),
        }
        for key in sensitive_keys:
            with pytest.raises(ValueError):
                repository.append_event(
                    **event_values,
                    payload_json={
                        "audit": {
                            "nested": {
                                key: "must-never-be-persisted",
                            }
                        }
                    },
                )
            assert session.scalar(
                select(func.count()).select_from(PaperBrokerEventRow)
            ) == 0


def test_service_clean_first_bind_without_decision_records_ready_snapshots_only(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_service_run(session, repository_root)
    client = _FakePaperClient()
    service = _service(
        factory,
        repository_root=repository_root,
        client=client,
    )

    output = asyncio.run(service.sync(RUN_ID, now=SYNC_AT))

    with factory() as session:
        event_types = tuple(
            session.scalars(
                select(PaperBrokerEventRow.event_type).order_by(
                    PaperBrokerEventRow.created_at,
                    PaperBrokerEventRow.event_id,
                )
            )
        )
        binding_count = (
            session.scalar(
                select(func.count()).select_from(PaperBrokerBindingRow)
            )
            or 0
        )
        command_count = (
            session.scalar(
                select(func.count()).select_from(PaperBrokerCommandRow)
            )
            or 0
        )
        internal = _internal_counts(session)

    assert output["state"] == "READY"
    assert output["reconciliation_status"] == "READY"
    assert output["account_bound"] is True
    assert output["account_ready"] is True
    assert output["matched_attribution_included"] is False
    assert output["real_order_routing"] is False
    assert binding_count == 1
    assert command_count == 0
    assert set(event_types) == {
        "ACCOUNT_SNAPSHOT",
        "POSITIONS_SNAPSHOT",
        "RECONCILIATION_READY",
    }
    assert len(event_types) == 3
    assert client.ensure_requests == []
    assert client.cancel_requests == []
    assert internal == {"fills": 0, "states": 0, "matched": 0}


@pytest.mark.parametrize(
    ("foreign_kind", "expected_code"),
    [
        ("position", "FOREIGN_OR_UNSAFE_POSITION"),
        ("order", "FOREIGN_OPEN_ORDER"),
    ],
)
def test_service_blocks_foreign_account_state_without_submitting(
    sqlite_database,
    repository_root: Path,
    foreign_kind: str,
    expected_code: str,
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_service_run(session, repository_root)
    client = _FakePaperClient()
    service = _service(
        factory,
        repository_root=repository_root,
        client=client,
    )
    first = asyncio.run(service.sync(RUN_ID, now=SYNC_AT))
    assert first["state"] == "READY"
    if foreign_kind == "position":
        client.positions = (_foreign_position(),)
    else:
        client.orders = (_foreign_open_order(),)

    output = asyncio.run(
        service.sync(
            RUN_ID,
            now=SYNC_AT + timedelta(minutes=1),
        )
    )

    with factory() as session:
        internal = _internal_counts(session)
        command_count = (
            session.scalar(
                select(func.count()).select_from(PaperBrokerCommandRow)
            )
            or 0
        )

    assert output["state"] == "BLOCKED"
    assert output["reconciliation_status"] == expected_code
    assert output["last_error_code"] == expected_code
    assert client.ensure_requests == []
    assert client.cancel_requests == []
    assert command_count == 0
    assert internal == {"fills": 0, "states": 0, "matched": 0}


def test_valid_source_decision_routes_only_to_paper_canary_lane(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_service_run(session, repository_root)
        _seed_service_decision_and_market(
            session,
            repository_root=repository_root,
            now=SYNC_AT,
        )
    client = _FakePaperClient()
    service = _service(
        factory,
        repository_root=repository_root,
        client=client,
    )

    output = asyncio.run(service.sync(RUN_ID, now=SYNC_AT))

    with factory() as session:
        commands = tuple(session.scalars(select(PaperBrokerCommandRow)))
        events = tuple(session.scalars(select(PaperBrokerEventRow)))
        internal = _internal_counts(session)

    assert output["state"] == "READY"
    assert output["matched_attribution_included"] is False
    assert output["real_order_routing"] is False
    assert len(client.ensure_requests) == 1
    request = client.ensure_requests[0]
    assert request.symbol == "QQQ"
    assert request.side is OrderSide.BUY
    assert request.quantity == Decimal("100")
    assert request.limit_price == Decimal("500.21")
    assert len(commands) == 1
    assert commands[0].command_type == "SUBMIT"
    assert commands[0].source_decision_id == DECISION_ID
    assert commands[0].client_order_id == request.client_order_id
    assert any(
        event.event_type == "SUBMIT_RECONCILED"
        and event.broker_order_id == "broker-1"
        for event in events
    )
    assert internal == {"fills": 0, "states": 0, "matched": 0}


def test_soft_stop_cancels_owned_buy_without_touching_internal_orders(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_service_run(session, repository_root)
        _seed_service_decision_and_market(
            session,
            repository_root=repository_root,
            now=SYNC_AT,
        )
    client = _FakePaperClient()
    service = _service(
        factory,
        repository_root=repository_root,
        client=client,
    )
    asyncio.run(service.sync(RUN_ID, now=SYNC_AT))
    assert len(client.ensure_requests) == 1
    with factory.begin() as session:
        session.add(
            NavSnapshotRow(
                nav_snapshot_id="alpaca-paper-soft-stop-nav",
                run_id=RUN_ID,
                arm_id="Q1-LLM",
                source_cycle_id=None,
                quote_manifest_hash=HASH,
                algorithm_version="q1_math_core_v1",
                config_manifest_hash=(
                    load_q1_config_bundle(
                        repository_root / "config"
                    ).manifest_hash
                ),
                code_version=workspace_code_version(repository_root),
                model_version="q1-risk-test",
                source_manifest_hash=HASH,
                as_of=SYNC_AT + timedelta(seconds=30),
                nav_usd=Decimal("99000"),
                payload_json={"risk_state": "SOFT_STOP"},
            )
        )

    output = asyncio.run(
        service.sync(
            RUN_ID,
            now=SYNC_AT + timedelta(minutes=1),
        )
    )

    with factory() as session:
        broker_commands = tuple(
            session.scalars(
                select(PaperBrokerCommandRow).order_by(
                    PaperBrokerCommandRow.created_at
                )
            )
        )
        internal = _internal_counts(session)

    assert output["state"] == "READY"
    assert client.cancel_requests == ["broker-1"]
    assert len(client.ensure_requests) == 1
    assert [row.command_type for row in broker_commands] == [
        "SUBMIT",
        "CANCEL",
    ]
    assert internal == {"fills": 0, "states": 0, "matched": 0}


def test_service_never_creates_buy_after_no_risk_increase_cutoff(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    after_cutoff = datetime(2026, 7, 27, 17, 5, tzinfo=UTC)
    with factory.begin() as session:
        _seed_service_run(session, repository_root)
        _seed_service_decision_and_market(
            session,
            repository_root=repository_root,
            now=after_cutoff,
        )
    client = _FakePaperClient()
    service = _service(
        factory,
        repository_root=repository_root,
        client=client,
    )

    output = asyncio.run(service.sync(RUN_ID, now=after_cutoff))

    with factory() as session:
        command_count = (
            session.scalar(
                select(func.count()).select_from(PaperBrokerCommandRow)
            )
            or 0
        )
        internal = _internal_counts(session)

    assert output["state"] == "READY"
    assert client.ensure_requests == []
    assert command_count == 0
    assert internal == {"fills": 0, "states": 0, "matched": 0}


def test_service_status_exposes_only_sanitized_canary_fields(
    sqlite_database,
    repository_root: Path,
) -> None:
    _database_url, _engine, factory = sqlite_database
    with factory.begin() as session:
        _seed_service_run(session, repository_root)
    client = _FakePaperClient()
    service = _service(
        factory,
        repository_root=repository_root,
        client=client,
    )
    asyncio.run(service.sync(RUN_ID, now=SYNC_AT))

    status = service.status(RUN_ID)
    rendered = json.dumps(status, sort_keys=True)

    assert status["state"] == "READY"
    assert status["source_arm"] == "Q1-LLM"
    assert status["account_bound"] is True
    assert status["real_order_routing"] is False
    assert status["matched_attribution_included"] is False
    assert "sensitive-paper-account-id" not in rendered
    assert "provider-account-request" not in rendered
    assert "account_id" not in status
    assert "account_id_hash" not in status
    assert "provider_request_id" not in status
    assert "credentials" not in rendered.lower()
    assert "authorization" not in rendered.lower()
