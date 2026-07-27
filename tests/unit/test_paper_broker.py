from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading.data.alpaca import parse_stream_message
from trading.domain.contracts import MarketQuote, OrderIntent
from trading.domain.enums import OrderSide
from trading.execution.paper import PaperBroker


def order(quantity: str = "10") -> OrderIntent:
    return OrderIntent(
        order_intent_id="order_test",
        arm_id="B1",
        portfolio_decision_id="pdec_test",
        risk_decision_id="rdec_test",
        symbol="QQQ",
        side=OrderSide.BUY,
        order_type="MARKET",
        quantity=Decimal(quantity),
        limit_price=None,
        time_in_force="DAY",
        session="REGULAR",
        client_order_id="client_test",
        idempotency_key="idem_test",
        created_at=datetime(2026, 7, 20, 19, 45, tzinfo=UTC),
    )


def test_buy_fill_is_not_better_than_next_open() -> None:
    broker = PaperBroker(
        execution_scenario_id="test",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    fill = broker.fill_marketable(
        order(),
        next_bar_open=Decimal("500"),
        effective_at=datetime(2026, 7, 21, 19, 44, tzinfo=UTC),
    )
    assert fill.price > Decimal("500")
    assert fill.commission_usd == Decimal("5.00")


def test_partial_fill_never_exceeds_intent() -> None:
    broker = PaperBroker(
        execution_scenario_id="test",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    intent = order("10")
    fill = broker.fill_marketable(
        intent,
        next_bar_open=Decimal("500"),
        effective_at=datetime(2026, 7, 21, 19, 44, tzinfo=UTC),
        participation_fraction=Decimal("0.5"),
    )
    assert fill.quantity == Decimal("5.000")
    assert fill.quantity < intent.quantity


def test_quote_fill_uses_ask_without_adding_half_spread_twice() -> None:
    broker = PaperBroker(
        execution_scenario_id="iex_quote_v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    intent = order()
    quote = parse_stream_message(
        {
            "T": "q",
            "S": "QQQ",
            "bx": "V",
            "bp": "500.00",
            "bs": 2,
            "ax": "V",
            "ap": "500.10",
            "as": 3,
            "c": [],
            "t": "2026-07-20T19:45:01Z",
            "z": "C",
        },
        available_at=datetime(2026, 7, 20, 19, 45, 2, tzinfo=UTC),
        raw_object_uri="raw://quote",
    )
    assert isinstance(quote, MarketQuote)
    fill = broker.fill_from_quote(
        intent,
        quote=quote,
        effective_at=datetime(2026, 7, 20, 19, 45, 3, tzinfo=UTC),
        max_quote_age_seconds=15,
    )
    assert fill.price == Decimal("500.1500")
    assert fill.price < Decimal("500.35")


def test_quote_fill_rejects_stale_crossed_and_empty_side_quotes() -> None:
    broker = PaperBroker(
        execution_scenario_id="iex_quote_v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    intent = order()
    quote = _live_quote()

    with pytest.raises(ValueError, match="stale"):
        broker.fill_from_quote(
            intent,
            quote=quote,
            effective_at=datetime(2026, 7, 20, 19, 45, 30, tzinfo=UTC),
            max_quote_age_seconds=15,
        )
    with pytest.raises(ValueError, match="Crossed"):
        broker.fill_from_quote(
            intent,
            quote=quote.model_copy(
                update={"bid_price": Decimal("500.20"), "ask_price": Decimal("500.10")}
            ),
            effective_at=datetime(2026, 7, 20, 19, 45, 3, tzinfo=UTC),
            max_quote_age_seconds=15,
        )
    with pytest.raises(ValueError, match="displayed size"):
        broker.fill_from_quote(
            intent,
            quote=quote.model_copy(update={"ask_size_round_lots": 0}),
            effective_at=datetime(2026, 7, 20, 19, 45, 3, tzinfo=UTC),
            max_quote_age_seconds=15,
        )


def test_quote_fill_is_capped_by_displayed_iex_round_lots() -> None:
    broker = PaperBroker(
        execution_scenario_id="iex_quote_v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    fill = broker.fill_from_quote(
        order("1000"),
        quote=_live_quote(),
        effective_at=datetime(2026, 7, 20, 19, 45, 3, tzinfo=UTC),
        max_quote_age_seconds=15,
    )
    assert fill.quantity == Decimal("300.000")


def test_quote_participation_caps_displayed_liquidity_not_small_order_quantity() -> None:
    broker = PaperBroker(
        execution_scenario_id="iex_quote_v1",
        commission_rate=Decimal("0.001"),
        commission_waiver_threshold_usd=Decimal("10"),
        half_spread_bps=Decimal("4"),
        delay_penalty_bps=Decimal("1"),
    )
    fill = broker.fill_from_quote(
        order("5"),
        quote=_live_quote(),
        effective_at=datetime(2026, 7, 20, 19, 45, 3, tzinfo=UTC),
        max_quote_age_seconds=15,
        participation_fraction=Decimal("0.10"),
    )

    assert fill.quantity == Decimal("5.000000")


def _live_quote() -> MarketQuote:
    quote = parse_stream_message(
        {
            "T": "q",
            "S": "QQQ",
            "bx": "V",
            "bp": "500.00",
            "bs": 2,
            "ax": "V",
            "ap": "500.10",
            "as": 3,
            "c": [],
            "t": "2026-07-20T19:45:01Z",
            "z": "C",
        },
        available_at=datetime(2026, 7, 20, 19, 45, 2, tzinfo=UTC),
        raw_object_uri="raw://quote",
    )
    assert isinstance(quote, MarketQuote)
    return quote
