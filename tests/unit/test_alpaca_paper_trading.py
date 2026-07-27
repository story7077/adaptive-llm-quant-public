from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from trading.data.alpaca import AlpacaCredentials
from trading.domain.enums import OrderSide
from trading.execution.alpaca_paper import (
    AlpacaPaperOrderConflict,
    AlpacaPaperOrderRequest,
    AlpacaPaperTradingClient,
    AlpacaPaperTradingError,
    deterministic_client_order_id,
)
from trading.settings import (
    ConfigurationError,
    Settings,
    load_alpaca_paper_config_bundle,
)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
KEY_ID = "paper-test-key"
SECRET_KEY = "paper-test-secret"
CLIENT_ORDER_ID = "q1-test-client-order"
BROKER_ORDER_ID = "broker-order-1"


def _order_payload(
    *,
    status: str = "accepted",
    filled_quantity: str = "0",
    filled_avg_price: str | None = None,
) -> dict[str, object]:
    return {
        "id": BROKER_ORDER_ID,
        "client_order_id": CLIENT_ORDER_ID,
        "symbol": "QQQ",
        "side": "buy",
        "qty": "2",
        "filled_qty": filled_quantity,
        "status": status,
        "submitted_at": "2026-07-27T14:01:00Z",
        "updated_at": "2026-07-27T14:01:01Z",
        "filled_avg_price": filled_avg_price,
        "limit_price": "500.20",
    }


def _request() -> AlpacaPaperOrderRequest:
    return AlpacaPaperOrderRequest(
        symbol="QQQ",
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        limit_price=Decimal("500.20"),
        client_order_id=CLIENT_ORDER_ID,
    )


def _client(
    handler: httpx.MockTransport,
) -> tuple[AlpacaPaperTradingClient, httpx.AsyncClient]:
    http_client = httpx.AsyncClient(transport=handler)
    return (
        AlpacaPaperTradingClient(
            credentials=AlpacaCredentials(KEY_ID, SECRET_KEY),
            client=http_client,
            reconciliation_lookup_attempts=1,
            reconciliation_lookup_interval_seconds=0.001,
        ),
        http_client,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.alpaca.markets",
        "http://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets.evil.example",
        "https://paper-api.alpaca.markets/v2",
        "https://user@paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets:443",
    ],
)
def test_paper_client_rejects_every_non_allowlisted_origin(
    base_url: str,
) -> None:
    with pytest.raises(ValueError, match="paper-api\\.alpaca\\.markets"):
        AlpacaPaperTradingClient(
            credentials=AlpacaCredentials(KEY_ID, SECRET_KEY),
            base_url=base_url,
        )


def test_paper_client_rejects_redirect_following_transport() -> None:
    http_client = httpx.AsyncClient(follow_redirects=True)
    try:
        with pytest.raises(ValueError, match="redirect"):
            AlpacaPaperTradingClient(
                credentials=AlpacaCredentials(KEY_ID, SECRET_KEY),
                client=http_client,
            )
    finally:
        asyncio.run(http_client.aclose())


def test_deterministic_client_order_id_is_stable_bounded_and_intent_specific() -> None:
    first = deterministic_client_order_id(
        run_id="q1-run-" + "x" * 300,
        source_decision_id="q1-intent-" + "y" * 300,
        symbol="QQQ",
        side=OrderSide.BUY,
    )
    replay = deterministic_client_order_id(
        run_id="q1-run-" + "x" * 300,
        source_decision_id="q1-intent-" + "y" * 300,
        symbol="QQQ",
        side=OrderSide.BUY,
    )
    another_intent = deterministic_client_order_id(
        run_id="q1-run-" + "x" * 300,
        source_decision_id="q1-intent-other",
        symbol="QQQ",
        side=OrderSide.BUY,
    )

    assert first == replay
    assert first != another_intent
    assert len(first) <= 128
    assert first.isascii()
    assert all(character.isalnum() or character in "-_" for character in first)


@pytest.mark.parametrize(
    ("symbol", "quantity", "client_order_id"),
    [
        ("SOXS", Decimal("1"), CLIENT_ORDER_ID),
        ("NVDA", Decimal("1"), CLIENT_ORDER_ID),
        ("QQQ", Decimal("0"), CLIENT_ORDER_ID),
        ("SOXX", Decimal("-1"), CLIENT_ORDER_ID),
        ("QQQ", Decimal("1"), "x" * 129),
    ],
)
def test_order_request_is_limited_to_q1_paper_contract(
    symbol: str,
    quantity: Decimal,
    client_order_id: str,
) -> None:
    with pytest.raises(ValueError):
        AlpacaPaperOrderRequest(
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=quantity,
            limit_price=Decimal("500.20"),
            client_order_id=client_order_id,
        )


def test_submit_uses_paper_endpoint_exact_body_and_auth_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=_order_payload(),
            headers={"x-request-id": "alpaca-request-1"},
        )

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> object:
        try:
            return await client.submit_order(_request())
        finally:
            await http_client.aclose()

    snapshot = asyncio.run(run())

    assert len(seen) == 1
    submitted = seen[0]
    assert submitted.method == "POST"
    assert str(submitted.url) == f"{PAPER_BASE_URL}/v2/orders"
    assert submitted.headers["APCA-API-KEY-ID"] == KEY_ID
    assert submitted.headers["APCA-API-SECRET-KEY"] == SECRET_KEY
    assert submitted.headers["content-type"].startswith("application/json")
    assert json.loads(submitted.content) == {
        "symbol": "QQQ",
        "side": "buy",
        "qty": "2",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "500.20",
        "extended_hours": False,
        "client_order_id": CLIENT_ORDER_ID,
    }
    assert snapshot.broker_order_id == BROKER_ORDER_ID
    assert snapshot.client_order_id == CLIENT_ORDER_ID
    assert snapshot.side is OrderSide.BUY
    assert snapshot.quantity == Decimal("2")
    assert snapshot.filled_quantity == 0
    assert snapshot.status == "accepted"
    assert snapshot.filled_average_price is None
    assert snapshot.provider_request_id == "alpaca-request-1"


def test_lookup_by_client_id_can_reuse_existing_order_without_posting() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.url.params["client_order_id"] == CLIENT_ORDER_ID
        return httpx.Response(200, json=_order_payload(status="new"))

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> object:
        try:
            return await client.ensure_submitted(_request())
        finally:
            await http_client.aclose()

    snapshot = asyncio.run(run())

    assert snapshot.broker_order_id == BROKER_ORDER_ID
    assert snapshot.status == "new"
    assert methods == ["GET"]


def test_existing_client_id_with_different_order_contract_is_rejected() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        payload = _order_payload(status="new")
        payload["qty"] = "3"
        return httpx.Response(200, json=payload)

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> None:
        try:
            with pytest.raises(
                AlpacaPaperOrderConflict,
                match=r"client_order_id|another order",
            ):
                await client.ensure_submitted(_request())
        finally:
            await http_client.aclose()

    asyncio.run(run())

    assert methods == ["GET"]


def test_ambiguous_submit_is_reconciled_by_client_id_without_blind_retry() -> None:
    calls: list[tuple[str, str]] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            get_count += 1
            if get_count == 1:
                return httpx.Response(404, json={"message": "order not found"})
            return httpx.Response(200, json=_order_payload(status="accepted"))
        raise httpx.ReadTimeout("ambiguous submit timeout", request=request)

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> object:
        try:
            return await client.ensure_submitted(_request())
        finally:
            await http_client.aclose()

    snapshot = asyncio.run(run())

    assert snapshot.broker_order_id == BROKER_ORDER_ID
    assert calls == [
        ("GET", "/v2/orders:by_client_order_id"),
        ("POST", "/v2/orders"),
        ("GET", "/v2/orders:by_client_order_id"),
    ]


def test_unknown_5xx_outcome_is_looked_up_and_never_blindly_retried() -> None:
    posts: list[bytes] = []
    calls: list[tuple[str, str]] = []
    get_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            get_count += 1
            return httpx.Response(404, json={"message": "order not found"})
        posts.append(request.content)
        return httpx.Response(503, json={"message": "temporarily unavailable"})

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> None:
        try:
            with pytest.raises(AlpacaPaperTradingError, match="503"):
                await client.ensure_submitted(_request())
        finally:
            await http_client.aclose()

    asyncio.run(run())

    assert calls == [
        ("GET", "/v2/orders:by_client_order_id"),
        ("POST", "/v2/orders"),
        ("GET", "/v2/orders:by_client_order_id"),
    ]
    assert len(posts) == 1
    assert get_count == 2


def test_cancel_acceptance_is_not_confused_with_terminal_cancellation() -> None:
    get_count = 0
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        methods.append(request.method)
        if request.method == "DELETE":
            assert request.url.path == f"/v2/orders/{BROKER_ORDER_ID}"
            return httpx.Response(204)
        get_count += 1
        assert request.url.params["client_order_id"] == CLIENT_ORDER_ID
        status = "pending_cancel" if get_count == 1 else "canceled"
        return httpx.Response(200, json=_order_payload(status=status))

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> tuple[object, object]:
        try:
            cancellation = await client.cancel_order(BROKER_ORDER_ID)
            assert cancellation.accepted is True
            assert cancellation.broker_order_id == BROKER_ORDER_ID
            pending = await client.get_order_by_client_id(CLIENT_ORDER_ID)
            terminal = await client.get_order_by_client_id(CLIENT_ORDER_ID)
            assert pending is not None
            assert terminal is not None
            return pending, terminal
        finally:
            await http_client.aclose()

    pending, terminal = asyncio.run(run())

    assert pending.status == "pending_cancel"
    assert terminal.status == "canceled"
    assert methods == ["DELETE", "GET", "GET"]


def test_status_reconciliation_preserves_broker_fill_totals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/orders"
        assert request.url.params["status"] == "all"
        return httpx.Response(
            200,
            json=[
                _order_payload(
                    status="partially_filled",
                    filled_quantity="0.75",
                    filled_avg_price="500.125",
                )
            ],
        )

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> tuple[object, ...]:
        try:
            return await client.list_orders(status="all")
        finally:
            await http_client.aclose()

    orders = asyncio.run(run())

    assert len(orders) == 1
    order = orders[0]
    assert order.broker_order_id == BROKER_ORDER_ID
    assert order.status == "partially_filled"
    assert order.filled_quantity == Decimal("0.75")
    assert order.filled_average_price == Decimal("500.125")


def test_fill_activity_reconciliation_preserves_stable_activity_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/account/activities/FILL"
        assert request.url.params["after"] == "2026-07-27T14:00:00+00:00"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "fill-activity-1",
                    "order_id": BROKER_ORDER_ID,
                    "symbol": "QQQ",
                    "side": "buy",
                    "qty": "0.75",
                    "price": "500.125",
                    "transaction_time": "2026-07-27T14:01:02Z",
                }
            ],
            headers={"x-request-id": "fill-request-1"},
        )

    client, http_client = _client(httpx.MockTransport(handler))

    async def run() -> tuple[object, ...]:
        try:
            return await client.list_fill_activities(
                after=datetime(2026, 7, 27, 14, 0, tzinfo=UTC),
            )
        finally:
            await http_client.aclose()

    fills = asyncio.run(run())

    assert len(fills) == 1
    fill = fills[0]
    assert fill.activity_id == "fill-activity-1"
    assert fill.broker_order_id == BROKER_ORDER_ID
    assert fill.symbol == "QQQ"
    assert fill.side is OrderSide.BUY
    assert fill.quantity == Decimal("0.75")
    assert fill.price == Decimal("500.125")
    assert fill.executed_at == datetime(2026, 7, 27, 14, 1, 2, tzinfo=UTC)
    assert fill.provider_request_id == "fill-request-1"


def test_credentials_never_appear_in_repr_logs_or_http_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    credentials = AlpacaCredentials(KEY_ID, SECRET_KEY)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "message": (
                    f"authentication failed for {KEY_ID} using {SECRET_KEY}"
                )
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = AlpacaPaperTradingClient(
        credentials=credentials,
        client=http_client,
    )

    async def run() -> None:
        try:
            with pytest.raises(Exception, match="401") as raised:
                await client.submit_order(_request())
            rendered = str(raised.value)
            assert KEY_ID not in rendered
            assert SECRET_KEY not in rendered
        finally:
            await http_client.aclose()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())

    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert KEY_ID not in repr(credentials)
    assert SECRET_KEY not in repr(credentials)
    assert KEY_ID not in repr(client)
    assert SECRET_KEY not in repr(client)
    assert KEY_ID not in rendered_logs
    assert SECRET_KEY not in rendered_logs


def test_versioned_canary_config_keeps_live_routing_unavailable(
    repository_root: Path,
) -> None:
    bundle = load_alpaca_paper_config_bundle(repository_root / "config")

    assert bundle.config.rest_base_url == PAPER_BASE_URL
    assert bundle.config.execution_lane == "ALPACA_PAPER_CANARY"
    assert bundle.config.source_arm.value == "Q1-LLM"
    assert bundle.config.allowed_symbols == ("QQQ", "SOXX")
    assert bundle.document["real_order_routing"] is False


def test_enabled_canary_settings_require_paper_host_and_credentials(
    tmp_path: Path,
) -> None:
    common = {
        "database_url": "sqlite+pysqlite:///:memory:",
        "config_dir": tmp_path,
        "raw_store": tmp_path / "raw",
        "real_broker_enabled": False,
        "real_llm_enabled": False,
        "production_unlock": False,
        "q1_alpaca_paper_enabled": True,
        "alpaca_key_id": KEY_ID,
        "alpaca_secret_key": SECRET_KEY,
    }

    with pytest.raises(ConfigurationError, match="paper-api host"):
        Settings(
            **common,
            alpaca_trading_url="https://api.alpaca.markets",
        )
    missing_secret = dict(common)
    missing_secret["alpaca_secret_key"] = None
    with pytest.raises(ConfigurationError, match="credentials"):
        Settings(**missing_secret)

    configured = Settings(**common)
    assert KEY_ID not in repr(configured)
    assert SECRET_KEY not in repr(configured)
