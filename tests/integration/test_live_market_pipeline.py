from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from trading.dashboard.live_market import LiveMarketSnapshotService
from trading.data.alpaca import parse_stream_message
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import MarketBar, MarketQuote, MarketTradeEvent
from trading.domain.enums import MarketConnectionState
from trading.domain.time import FrozenClock
from trading.ui.app import create_app, history_refresh_status


def test_append_only_market_data_dedupes_and_projects_latest_bar_revision(
    sqlite_database,
    config_bundle,
    repository_root,
) -> None:
    _, engine, factory = sqlite_database
    repository = MarketDataRepository(factory)
    original = _bar(close="42.20", available_minute=31, message_type="b")
    updated = _bar(close="42.30", available_minute=32, message_type="u")
    quote = _quote()
    trade = _trade()

    first = repository.append(
        bars=[original, updated],
        quotes=[quote],
        trades=[trade],
    )
    duplicate = repository.append(
        bars=[original, updated],
        quotes=[quote],
        trades=[trade],
    )
    assert first.total == 4
    assert duplicate.total == 0

    before_update = repository.latest_bars(
        provider="alpaca",
        feed="iex",
        symbol="SOXL",
        timeframe="1Min",
        as_of=datetime(2026, 7, 27, 14, 31, 30, tzinfo=UTC),
        limit=10,
    )
    after_update = repository.latest_bars(
        provider="alpaca",
        feed="iex",
        symbol="SOXL",
        timeframe="1Min",
        as_of=datetime(2026, 7, 27, 14, 32, 30, tzinfo=UTC),
        limit=10,
    )
    assert before_update[-1].close == Decimal("42.200000000000")
    assert after_update[-1].close == Decimal("42.300000000000")
    assert len(after_update) == 1

    settings = replace(
        _settings(repository_root, sqlite_database[0]),
        alpaca_key_id="test-key",
        alpaca_secret_key="test-secret",
        market_bar_stale_seconds=300,
    )
    service = LiveMarketSnapshotService(
        factory,
        settings=settings,
        config=config_bundle,
        clock=FrozenClock(datetime(2026, 7, 27, 14, 32, 5, tzinfo=UTC)),
    )
    snapshot = service.snapshot(symbol="SOXL")
    assert snapshot["source"]["coverage"] == "SINGLE_EXCHANGE"
    assert snapshot["source"]["candle_quality"] == "NATIVE_OHLCV"
    assert snapshot["source"]["connection_state"] == "STOPPED"
    assert snapshot["source"]["data_status"] == "LIVE"
    assert snapshot["market"]["candles"][-1]["close"] == "42.300000000000"
    assert snapshot["market"]["candles"][-1]["high_low_derived"] is False
    assert snapshot["paper_input"]["ready"] is True
    assert snapshot["market"]["quote"]["bid_size_shares"] == 300

    repository.transition(
        provider="alpaca",
        feed="iex",
        state=MarketConnectionState.CONNECTED,
        now=datetime(2026, 7, 27, 14, 33, tzinfo=UTC),
    )
    repository.heartbeat(
        provider="alpaca",
        feed="iex",
        now=datetime(2026, 7, 27, 14, 39, 50, tzinfo=UTC),
    )
    stale = service.snapshot(
        symbol="SOXL",
        as_of=datetime(2026, 7, 27, 14, 40, tzinfo=UTC),
    )
    assert stale["source"]["connection_state"] == "CONNECTED"
    assert stale["source"]["data_status"] == "STALE"
    assert stale["paper_input"]["reason"] == "STALE_QUOTE"

    dead_worker = service.snapshot(
        symbol="SOXL",
        as_of=datetime(2026, 7, 27, 14, 40, 30, tzinfo=UTC),
    )
    assert dead_worker["source"]["connection_state"] == "DISCONNECTED"
    assert dead_worker["source"]["heartbeat_age_seconds"] == 40.0

    with (
        engine.connect() as connection,
        connection.begin(),
        pytest.raises(DBAPIError, match="append-only"),
    ):
        connection.execute(
            text(
                "UPDATE market_bars SET close=1 "
                f"WHERE bar_id='{original.bar_id}'"
            )
        )


def test_market_snapshot_api_keeps_live_market_separate_from_synthetic_portfolio(
    seeded_demo,
) -> None:
    settings, _, factory, _, _ = seeded_demo
    repository = MarketDataRepository(factory)
    repository.append(
        bars=[_bar(close="42.20", available_minute=31, message_type="b")],
        quotes=[_quote()],
        trades=[_trade()],
    )
    with TestClient(create_app(settings=settings, session_factory=factory)) as client:
        live = client.get("/api/market/snapshot", params={"symbol": "SOXL", "limit": 30})
        synthetic = client.get(
            "/api/trading/dashboard",
            params={"run_id": "demo_run", "symbol": "QQQ"},
        )
        invalid = client.get(
            "/api/market/snapshot",
            params={"symbol": "USD_CASH"},
        )

    assert live.status_code == 200
    assert live.json()["source"]["provider"] == "alpaca"
    assert live.json()["source"]["connection_state"] == "AUTH_REQUIRED"
    assert live.json()["filters"]["selected_symbol"] == "SOXL"
    assert live.json()["history_refresh"] == {
        "status": "AUTH_REQUIRED",
        "last_success": None,
        "last_error": None,
    }
    assert synthetic.status_code == 200
    assert synthetic.json()["source"]["mode"] == "SYNTHETIC"
    assert invalid.status_code == 400


def test_history_refresh_status_exposes_only_sanitized_failure_metadata() -> None:
    status = history_refresh_status(
        enabled=True,
        configured=True,
        last_refresh={
            "fetched": 242,
            "inserted": 2,
            "at": "2026-07-29T00:24:06+00:00",
            "detail": "success metadata must also remain bounded",
        },
        last_error={
            "error_code": "RUNTIMEERROR",
            "detail": "credential-shaped detail must remain private",
            "at": "2026-07-29T06:24:06+00:00",
        },
    )

    assert status == {
        "status": "ERROR",
        "last_success": {
            "fetched": 242,
            "inserted": 2,
            "at": "2026-07-29T00:24:06+00:00",
        },
        "last_error": {
            "error_code": "RUNTIMEERROR",
            "at": "2026-07-29T06:24:06+00:00",
        },
    }


def _bar(
    *,
    close: str,
    available_minute: int,
    message_type: str,
) -> MarketBar:
    event = parse_stream_message(
        {
            "T": message_type,
            "S": "SOXL",
            "o": "42.10",
            "h": "42.40",
            "l": "42.00",
            "c": close,
            "v": 1200,
            "vw": "42.22",
            "n": 73,
            "t": "2026-07-27T14:30:00.123456789Z",
        },
        available_at=datetime(2026, 7, 27, 14, available_minute, tzinfo=UTC),
        raw_object_uri=f"raw://bar-{available_minute}",
    )
    assert isinstance(event, MarketBar)
    return event


def _quote() -> MarketQuote:
    event = parse_stream_message(
        {
            "T": "q",
            "S": "SOXL",
            "bx": "V",
            "bp": "42.29",
            "bs": 3,
            "ax": "V",
            "ap": "42.31",
            "as": 5,
            "c": [],
            "t": "2026-07-27T14:32:00Z",
            "z": "C",
        },
        available_at=datetime(2026, 7, 27, 14, 32, 1, tzinfo=UTC),
        raw_object_uri="raw://quote",
    )
    assert isinstance(event, MarketQuote)
    return event


def _trade() -> MarketTradeEvent:
    event = parse_stream_message(
        {
            "T": "t",
            "S": "SOXL",
            "i": 991,
            "x": "V",
            "p": "42.30",
            "s": 100,
            "c": [],
            "t": "2026-07-27T14:32:00Z",
            "z": "C",
        },
        available_at=datetime(2026, 7, 27, 14, 32, 1, tzinfo=UTC),
        raw_object_uri="raw://trade",
    )
    assert isinstance(event, MarketTradeEvent)
    return event


def _settings(repository_root, database_url):
    from trading.settings import Settings

    return Settings(
        database_url=database_url,
        config_dir=repository_root / "config",
        raw_store=repository_root / "data" / "raw",
        real_broker_enabled=False,
        real_llm_enabled=False,
        production_unlock=False,
    )
