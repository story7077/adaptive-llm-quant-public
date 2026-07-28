from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal

from trading.domain.enums import OrderSide


@dataclass(frozen=True, slots=True)
class Q1ExecutionConfig:
    displayed_participation: Decimal
    adv_participation: Decimal
    delay_penalty_bps: Decimal
    guard_min_bps: Decimal
    guard_max_bps: Decimal
    guard_spread_multiplier: Decimal
    quantity_precision: Decimal
    price_precision: Decimal
    commission_rate: Decimal
    commission_waiver_threshold_usd: Decimal
    commission_precision: Decimal
    sensitivity_bps: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        if not Decimal("0") < self.displayed_participation <= Decimal("1"):
            raise ValueError("Displayed participation must be within (0, 1]")
        if not Decimal("0") < self.adv_participation <= Decimal("1"):
            raise ValueError("ADV participation must be within (0, 1]")
        if (
            self.delay_penalty_bps < 0
            or self.guard_min_bps <= 0
            or self.guard_max_bps < self.guard_min_bps
            or self.guard_spread_multiplier <= 0
        ):
            raise ValueError("Invalid Q1 execution penalty or guard parameters")
        if self.quantity_precision <= 0 or self.price_precision <= 0:
            raise ValueError("Q1 execution precision must be positive")
        if (
            self.commission_rate < 0
            or self.commission_waiver_threshold_usd < 0
            or self.commission_precision <= 0
        ):
            raise ValueError("Q1 commission parameters cannot be negative")
        if any(value < 0 for value in self.sensitivity_bps):
            raise ValueError("Q1 sensitivity values cannot be negative")


@dataclass(frozen=True, slots=True)
class Q1FillEconomics:
    quantity: Decimal
    price: Decimal
    commission_usd: Decimal
    cumulative_notional_usd: Decimal
    cumulative_commission_usd: Decimal
    base_execution_cost_usd: Decimal
    sensitivity_costs_usd: dict[str, Decimal]
    guard_bps: Decimal
    adverse_move_bps: Decimal


class Q1PriceGuardViolation(RuntimeError):
    def __init__(self, *, guard_bps: Decimal, adverse_move_bps: Decimal) -> None:
        self.guard_bps = guard_bps
        self.adverse_move_bps = adverse_move_bps
        super().__init__(
            f"Decision price guard exceeded: {adverse_move_bps} bps > {guard_bps} bps"
        )


def dynamic_guard_bps(
    decision_spread_bps: Decimal,
    *,
    config: Q1ExecutionConfig,
) -> Decimal:
    if decision_spread_bps < 0:
        raise ValueError("Decision spread cannot be negative")
    return min(
        config.guard_max_bps,
        max(
            config.guard_min_bps,
            config.guard_spread_multiplier * decision_spread_bps,
        ),
    )


def build_q1_fill_economics(
    *,
    side: OrderSide,
    remaining_quantity: Decimal,
    bid_price: Decimal,
    ask_price: Decimal,
    executable_side_quantity: Decimal,
    remaining_adv_capacity: Decimal,
    decision_reference_price: Decimal,
    decision_spread_bps: Decimal,
    cumulative_notional_before: Decimal,
    cumulative_commission_before: Decimal,
    config: Q1ExecutionConfig,
) -> Q1FillEconomics:
    if remaining_quantity <= 0:
        raise ValueError("Remaining order quantity must be positive")
    if (
        bid_price <= 0
        or ask_price <= 0
        or ask_price < bid_price
        or executable_side_quantity <= 0
        or remaining_adv_capacity <= 0
        or decision_reference_price <= 0
    ):
        raise ValueError("Q1 fill inputs must describe an executable quote")
    if cumulative_notional_before < 0 or cumulative_commission_before < 0:
        raise ValueError("Q1 cumulative order economics cannot be negative")

    reference = ask_price if side is OrderSide.BUY else bid_price
    adverse_move = (
        (reference - decision_reference_price) / decision_reference_price
        if side is OrderSide.BUY
        else (decision_reference_price - reference) / decision_reference_price
    )
    adverse_move_bps = max(Decimal("0"), adverse_move * Decimal("10000"))
    guard_bps = dynamic_guard_bps(decision_spread_bps, config=config)
    if adverse_move_bps > guard_bps:
        raise Q1PriceGuardViolation(
            guard_bps=guard_bps,
            adverse_move_bps=adverse_move_bps,
        )

    displayed_cap = executable_side_quantity * config.displayed_participation
    quantity = min(
        remaining_quantity,
        displayed_cap,
        remaining_adv_capacity,
    ).quantize(config.quantity_precision, rounding=ROUND_DOWN)
    if quantity <= 0:
        raise ValueError("Q1 execution caps produced a zero-quantity fill")

    delay = config.delay_penalty_bps / Decimal("10000")
    raw_price = (
        reference * (Decimal("1") + delay)
        if side is OrderSide.BUY
        else reference * (Decimal("1") - delay)
    )
    price = raw_price.quantize(config.price_precision, rounding=ROUND_HALF_EVEN)
    fill_notional = quantity * price
    cumulative_notional = cumulative_notional_before + fill_notional
    total_commission = (
        Decimal("0")
        if cumulative_notional <= config.commission_waiver_threshold_usd
        else (cumulative_notional * config.commission_rate).quantize(
            config.commission_precision,
            rounding=ROUND_HALF_EVEN,
        )
    )
    commission = total_commission - cumulative_commission_before
    if commission < 0:
        raise ValueError("Cumulative commission exceeds configured order commission")

    midpoint = (bid_price + ask_price) / Decimal("2")
    price_cost = (
        (price - midpoint) * quantity
        if side is OrderSide.BUY
        else (midpoint - price) * quantity
    )
    base_cost = max(Decimal("0"), price_cost) + commission
    sensitivity = {
        f"plus_{format(value, 'f')}_bps": (
            base_cost + fill_notional * value / Decimal("10000")
        )
        for value in config.sensitivity_bps
    }
    return Q1FillEconomics(
        quantity=quantity,
        price=price,
        commission_usd=commission,
        cumulative_notional_usd=cumulative_notional,
        cumulative_commission_usd=total_commission,
        base_execution_cost_usd=base_cost,
        sensitivity_costs_usd=sensitivity,
        guard_bps=guard_bps,
        adverse_move_bps=adverse_move_bps,
    )
