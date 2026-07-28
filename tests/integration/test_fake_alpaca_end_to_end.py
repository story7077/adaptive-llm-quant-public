from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType

import httpx
from sqlalchemy import func, select

from trading.dashboard.live_market import LiveMarketSnapshotService
from trading.data.alpaca import (
    AlpacaCredentials,
    AlpacaRestClient,
    AlpacaStreamClient,
)
from trading.data.market_repository import MarketDataRepository
from trading.data.raw_store import ImmutableRawStore
from trading.data.worker import AlpacaMarketWorker
from trading.domain.contracts import OrderIntent
from trading.domain.enums import OrderSide
from trading.domain.time import FrozenClock
from trading.execution.live_paper import LivePaperExecutionService
from trading.execution.paper import PaperBroker
from trading.persistence.models import MarketBarRow, MarketQuoteRow, MarketTradeEventRow
from trading.settings import Settings


def test_fake_alpaca_flows_from_rest_and_websocket_to_ui_and_paper_broker(
    sqlite_database,
    config_bundle,
    repository_root: Path,
    tmp_path: Path,
) -> None:
    database_url, _, factory = sqlite_database
    now = datetime(2026, 7, 27, 15, 0, 5, tzinfo=UTC)
    clock = FrozenClock(now)
    stop = asyncio.Event()
    socket = FakeAlpacaSocket(stop)
    raw_store = ImmutableRawStore(tmp_path / "raw")
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_rest_handler))
    credentials = AlpacaCredentials("test-key", "test-secret")
    rest = AlpacaRestClient(
        credentials=credentials,
        raw_store=raw_store,
        clock=clock,
        client=http_client,
    )
    stream = AlpacaStreamClient(
        credentials=credentials,
        symbols=("SOXL",),
        raw_store=raw_store,
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, 5, 500000, tzinfo=UTC)),
        connection_factory=lambda _url: FakeConnection(socket),
    )
    repository = MarketDataRepository(factory)
    worker = AlpacaMarketWorker(
        repository=repository,
        rest_client=rest,
        stream_client=stream,
        symbols=("SOXL",),
        clock=clock,
    )

    async def run_pipeline() -> None:
        async def stop_after_data() -> None:
            await socket.data_delivered.wait()
            await asyncio.sleep(0.05)
            stop.set()

        await asyncio.gather(worker.run_forever(stop), stop_after_data())
        await http_client.aclose()

    asyncio.run(run_pipeline())

    settings = Settings(
        database_url=database_url,
        config_dir=repository_root / "config",
        raw_store=tmp_path / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
        alpaca_key_id="test-key",
        alpaca_secret_key="test-secret",
        market_bar_stale_seconds=120,
    )
    snapshot = LiveMarketSnapshotService(
        factory,
        settings=settings,
        config=config_bundle,
        clock=FrozenClock(datetime(2026, 7, 27, 15, 0, 6, tzinfo=UTC)),
    ).snapshot(symbol="SOXL")

    assert snapshot["source"]["data_status"] == "LIVE"
    assert snapshot["market"]["candles"][-1]["close"] == "42.330000000000"
    assert snapshot["market"]["candles"][-1]["source_kind"] == "STREAM_UPDATE"
    assert snapshot["market"]["quote"]["price"] == "42.350000000000"
    assert snapshot["market"]["quote"]["ask"] == "42.360000000000"
    assert snapshot["paper_input"]["ready"] is True
    assert len(list((tmp_path / "raw").rglob("*.json"))) >= 4
    with factory() as session:
        assert session.scalar(select(func.count(MarketBarRow.bar_id))) == 2
        assert session.scalar(select(func.count(MarketQuoteRow.quote_id))) == 2
        assert session.scalar(select(func.count(MarketTradeEventRow.trade_event_id))) == 2

    intent = OrderIntent(
        order_intent_id="order_live_soxl",
        arm_id="B3-RISK",
        portfolio_decision_id="pdec_live",
        risk_decision_id="rdec_live",
        symbol="SOXL",
        side=OrderSide.BUY,
        order_type="MARKET",
        quantity=Decimal("2"),
        limit_price=None,
        time_in_force="DAY",
        session="REGULAR",
        client_order_id="client_live_soxl",
        idempotency_key="idem_live_soxl",
        created_at=datetime(2026, 7, 27, 15, 0, 3, tzinfo=UTC),
    )
    broker = PaperBroker(
        execution_scenario_id="alpaca_iex_quote_v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    execution = LivePaperExecutionService(
        repository,
        broker,
        max_quote_age_seconds=15,
    ).fill_market_order(
        intent,
        effective_at=datetime(2026, 7, 27, 15, 0, 6, tzinfo=UTC),
    )

    assert execution.quote_id == snapshot["paper_input"]["quote_id"]
    assert execution.fill.price == Decimal("42.3642")
    assert execution.fill.quantity == Decimal("2.000")
    assert execution.fill.execution_scenario_id == "alpaca_iex_quote_v1"
    assert settings.real_broker_enabled is False
    assert settings.production_unlock is False
    assert replace(settings, market_data_enabled=False).market_data_enabled is False


def _rest_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["APCA-API-KEY-ID"] == "test-key"
    if request.url.path == "/v2/stocks/bars":
        return httpx.Response(
            200,
            json={
                "bars": {
                    "SOXL": [
                        {
                            "t": "2026-07-27T14:59:00Z",
                            "o": 42.10,
                            "h": 42.35,
                            "l": 42.00,
                            "c": 42.20,
                            "v": 1200,
                            "vw": 42.18,
                            "n": 73,
                        }
                    ]
                },
                "next_page_token": None,
            },
        )
    if request.url.path == "/v2/stocks/quotes/latest":
        return httpx.Response(
            200,
            json={
                "quotes": {
                    "SOXL": {
                        "t": "2026-07-27T15:00:00Z",
                        "bx": "V",
                        "bp": 42.29,
                        "bs": 3,
                        "ax": "V",
                        "ap": 42.31,
                        "as": 4,
                        "c": [],
                        "z": "C",
                    }
                }
            },
        )
    if request.url.path == "/v2/stocks/trades/latest":
        return httpx.Response(
            200,
            json={
                "trades": {
                    "SOXL": {
                        "t": "2026-07-27T15:00:00Z",
                        "i": 100,
                        "x": "V",
                        "p": 42.30,
                        "s": 100,
                        "c": [],
                        "z": "C",
                    }
                }
            },
        )
    return httpx.Response(404, json={"message": "unexpected test path"})


class FakeAlpacaSocket:
    def __init__(self, stop: asyncio.Event) -> None:
        self._stop = stop
        self.data_delivered = asyncio.Event()
        quote = (
            '{"T":"q","S":"SOXL","bx":"V","bp":42.34,"bs":3,'
            '"ax":"V","ap":42.36,"as":4,"c":[],"t":'
            '"2026-07-27T15:00:04Z","z":"C"}'
        )
        self._responses = [
            '[{"T":"success","msg":"connected"}]',
            '[{"T":"success","msg":"authenticated"}]',
            (
                '[{"T":"subscription","trades":["SOXL"],"quotes":["SOXL"],'
                '"bars":["SOXL"],"updatedBars":["SOXL"]}]'
            ),
            (
                '[{"T":"u","S":"SOXL","o":42.10,"h":42.40,"l":42.00,'
                '"c":42.33,"v":1300,"vw":42.24,"n":81,'
                '"t":"2026-07-27T14:59:00Z"},'
                f"{quote},{quote},"
                '{"T":"t","S":"SOXL","i":101,"x":"V","p":42.35,"s":100,'
                '"c":[],"t":"2026-07-27T15:00:04Z","z":"C"}]'
            ),
        ]
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str:
        if self._responses:
            response = self._responses.pop(0)
            if not self._responses:
                self.data_delivered.set()
            return response
        await self._stop.wait()
        return "[]"


class FakeConnection(AbstractAsyncContextManager[FakeAlpacaSocket]):
    def __init__(self, socket: FakeAlpacaSocket) -> None:
        self._socket = socket

    async def __aenter__(self) -> FakeAlpacaSocket:
        return self._socket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None
