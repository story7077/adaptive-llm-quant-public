from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from trading.domain.contracts import Fill, MarketQuote, OrderIntent
from trading.domain.enums import OrderSide
from trading.domain.hashing import stable_id

CENT = Decimal("0.01")
PRICE_PRECISION = Decimal("0.0001")
QUANTITY_PRECISION = Decimal("0.000001")


class PaperBroker:
    def __init__(
        self,
        *,
        execution_scenario_id: str,
        commission_rate: Decimal,
        commission_waiver_threshold_usd: Decimal,
        half_spread_bps: Decimal,
        delay_penalty_bps: Decimal,
    ) -> None:
        self._execution_scenario_id = execution_scenario_id
        self._commission_rate = commission_rate
        self._commission_waiver_threshold_usd = commission_waiver_threshold_usd
        self._half_spread_bps = half_spread_bps
        self._delay_penalty_bps = delay_penalty_bps

    def fill_marketable(
        self,
        intent: OrderIntent,
        *,
        next_bar_open: Decimal,
        effective_at: datetime,
        participation_fraction: Decimal = Decimal("1"),
        cumulative_order_notional_before: Decimal = Decimal("0"),
        commission_charged_before: Decimal = Decimal("0"),
    ) -> Fill:
        if intent.order_type != "MARKET":
            raise ValueError("Phase 0 PaperBroker only supports MARKET orders")
        if not Decimal("0") < participation_fraction <= Decimal("1"):
            raise ValueError("participation_fraction must be within (0, 1]")
        penalty = (self._half_spread_bps + self._delay_penalty_bps) / Decimal("10000")
        if intent.side is OrderSide.BUY:
            fill_price = next_bar_open * (Decimal("1") + penalty)
        else:
            fill_price = next_bar_open * (Decimal("1") - penalty)
        fill_price = fill_price.quantize(PRICE_PRECISION, rounding=ROUND_HALF_EVEN)
        fill_quantity = (intent.quantity * participation_fraction).quantize(
            QUANTITY_PRECISION, rounding=ROUND_DOWN
        )
        if fill_quantity <= 0:
            raise ValueError("Participation produced a zero-quantity fill")
        notional = fill_quantity * fill_price
        commission = self._incremental_commission(
            fill_notional=notional,
            cumulative_order_notional_before=cumulative_order_notional_before,
            commission_charged_before=commission_charged_before,
        )
        return Fill(
            fill_id=stable_id(
                "fill",
                intent.order_intent_id,
                str(fill_quantity),
                str(fill_price),
                self._execution_scenario_id,
            ),
            order_intent_id=intent.order_intent_id,
            arm_id=intent.arm_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=fill_quantity,
            price=fill_price,
            commission_usd=commission,
            execution_scenario_id=self._execution_scenario_id,
            effective_at=effective_at,
            created_at=effective_at,
        )

    def fill_from_quote(
        self,
        intent: OrderIntent,
        *,
        quote: MarketQuote,
        effective_at: datetime,
        max_quote_age_seconds: int,
        participation_fraction: Decimal = Decimal("1"),
        max_fill_quantity: Decimal | None = None,
        cumulative_order_notional_before: Decimal = Decimal("0"),
        commission_charged_before: Decimal = Decimal("0"),
    ) -> Fill:
        if intent.order_type != "MARKET":
            raise ValueError("PaperBroker quote execution only supports MARKET orders")
        if intent.symbol != quote.symbol:
            raise ValueError("Quote symbol differs from order intent")
        if max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if not Decimal("0") < participation_fraction <= Decimal("1"):
            raise ValueError("participation_fraction must be within (0, 1]")
        if max_fill_quantity is not None and max_fill_quantity <= 0:
            raise ValueError("max_fill_quantity must be positive")
        if quote.event_time < intent.created_at or quote.available_at < intent.created_at:
            raise ValueError("Quote predates the order intent")
        if quote.available_at > effective_at:
            raise ValueError("Quote was not available at effective_at")
        quote_age = (effective_at - quote.event_time).total_seconds()
        if quote_age < 0 or quote_age > max_quote_age_seconds:
            raise ValueError("Quote is stale at effective_at")
        if quote.bid_price <= 0 or quote.ask_price <= 0:
            raise ValueError("Quote must have positive bid and ask prices")
        if quote.ask_price < quote.bid_price:
            raise ValueError("Crossed quote cannot drive a paper fill")

        reference_price = (
            quote.ask_price if intent.side is OrderSide.BUY else quote.bid_price
        )
        available_round_lots = (
            quote.ask_size_round_lots
            if intent.side is OrderSide.BUY
            else quote.bid_size_round_lots
        )
        if available_round_lots <= 0:
            raise ValueError("Executable quote side has no displayed size")
        delay = self._delay_penalty_bps / Decimal("10000")
        fill_price = (
            reference_price * (Decimal("1") + delay)
            if intent.side is OrderSide.BUY
            else reference_price * (Decimal("1") - delay)
        ).quantize(PRICE_PRECISION, rounding=ROUND_HALF_EVEN)
        displayed_quantity = Decimal(available_round_lots * 100)
        fill_cap = displayed_quantity * participation_fraction
        if max_fill_quantity is not None:
            fill_cap = min(fill_cap, max_fill_quantity)
        fill_quantity = min(intent.quantity, fill_cap).quantize(
            QUANTITY_PRECISION, rounding=ROUND_DOWN
        )
        if fill_quantity <= 0:
            raise ValueError("Participation produced a zero-quantity fill")
        notional = fill_quantity * fill_price
        commission = self._incremental_commission(
            fill_notional=notional,
            cumulative_order_notional_before=cumulative_order_notional_before,
            commission_charged_before=commission_charged_before,
        )
        return Fill(
            fill_id=stable_id(
                "fill",
                intent.order_intent_id,
                quote.quote_id,
                str(fill_quantity),
                str(fill_price),
                self._execution_scenario_id,
            ),
            order_intent_id=intent.order_intent_id,
            arm_id=intent.arm_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=fill_quantity,
            price=fill_price,
            commission_usd=commission,
            execution_scenario_id=self._execution_scenario_id,
            effective_at=effective_at,
            created_at=effective_at,
        )

    def _incremental_commission(
        self,
        *,
        fill_notional: Decimal,
        cumulative_order_notional_before: Decimal,
        commission_charged_before: Decimal,
    ) -> Decimal:
        if cumulative_order_notional_before < 0 or commission_charged_before < 0:
            raise ValueError("Cumulative order values cannot be negative")
        cumulative_notional = cumulative_order_notional_before + fill_notional
        total_commission = (
            Decimal("0")
            if cumulative_notional <= self._commission_waiver_threshold_usd
            else (cumulative_notional * self._commission_rate).quantize(
                CENT,
                rounding=ROUND_HALF_EVEN,
            )
        )
        incremental = total_commission - commission_charged_before
        if incremental < 0:
            raise ValueError("commission_charged_before exceeds cumulative commission")
        return incremental
