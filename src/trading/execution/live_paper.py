from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from pydantic import JsonValue

from trading.data.alpaca import FEED, PROVIDER
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import Fill, MarketQuote, OrderIntent
from trading.domain.enums import MarketDataSourceKind
from trading.domain.time import require_aware_utc
from trading.execution.paper import PaperBroker
from trading.persistence.models import MarketQuoteRow


class ExecutableQuoteNotFound(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QuoteDrivenFill:
    fill: Fill
    quote_id: str
    quote_event_time: datetime
    quote_available_at: datetime
    pricing_rule: str = "BUY_ASK_SELL_BID_PLUS_DELAY_V1"


class LivePaperExecutionService:
    def __init__(
        self,
        repository: MarketDataRepository,
        broker: PaperBroker,
        *,
        max_quote_age_seconds: int,
    ) -> None:
        if max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        self._repository = repository
        self._broker = broker
        self._max_quote_age_seconds = max_quote_age_seconds

    def fill_market_order(
        self,
        intent: OrderIntent,
        *,
        effective_at: datetime,
        observed_after: datetime | None = None,
        remaining_quantity: Decimal | None = None,
        max_spread_bps: Decimal | None = None,
        participation_fraction: Decimal = Decimal("1"),
        max_fill_quantity: Decimal | None = None,
        cumulative_order_notional_before: Decimal = Decimal("0"),
        commission_charged_before: Decimal = Decimal("0"),
    ) -> QuoteDrivenFill:
        instant = require_aware_utc(effective_at, "effective_at")
        cursor = (
            intent.created_at
            if observed_after is None
            else max(
                intent.created_at,
                require_aware_utc(observed_after, "observed_after"),
            )
        )
        working_intent = (
            intent
            if remaining_quantity is None
            else intent.model_copy(update={"quantity": remaining_quantity})
        )
        row = self._repository.first_executable_quote(
            provider=PROVIDER,
            feed=FEED,
            symbol=intent.symbol,
            observed_after=cursor,
            as_of=instant,
            max_age_seconds=self._max_quote_age_seconds,
            side=intent.side,
            max_spread_bps=max_spread_bps,
        )
        if row is None:
            raise ExecutableQuoteNotFound(
                f"No executable IEX quote for {intent.symbol} after the order intent"
            )
        quote = _quote_contract(row)
        fill = self._broker.fill_from_quote(
            working_intent,
            quote=quote,
            effective_at=instant,
            max_quote_age_seconds=self._max_quote_age_seconds,
            participation_fraction=participation_fraction,
            max_fill_quantity=max_fill_quantity,
            cumulative_order_notional_before=cumulative_order_notional_before,
            commission_charged_before=commission_charged_before,
        )
        return QuoteDrivenFill(
            fill=fill,
            quote_id=quote.quote_id,
            quote_event_time=quote.event_time,
            quote_available_at=quote.available_at,
        )


def _quote_contract(row: MarketQuoteRow) -> MarketQuote:
    return MarketQuote(
        quote_id=row.quote_id,
        provider=row.provider,
        feed=row.feed,
        symbol=row.symbol,
        event_time=_aware(row.event_time),
        provider_timestamp=row.provider_timestamp,
        available_at=_aware(row.available_at),
        ingested_at=_aware(row.ingested_at),
        source_kind=MarketDataSourceKind(row.source_kind),
        bid_exchange=row.bid_exchange,
        bid_price=row.bid_price,
        bid_size_round_lots=row.bid_size_round_lots,
        ask_exchange=row.ask_exchange,
        ask_price=row.ask_price,
        ask_size_round_lots=row.ask_size_round_lots,
        conditions=list(row.conditions),
        tape=row.tape,
        payload_hash=row.payload_hash,
        raw_object_uri=row.raw_object_uri,
        payload=cast(dict[str, JsonValue], row.payload_json),
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
