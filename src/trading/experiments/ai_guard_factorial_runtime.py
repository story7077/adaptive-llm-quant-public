from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from trading.domain.contracts import Fill, LedgerEntry, OrderIntent
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash, stable_id
from trading.experiments.ai_guard_factorial import (
    FACTORIAL_ARM_IDS,
    FactorialArmContract,
    factorial_arm_contracts,
)
from trading.experiments.arms import ArmState
from trading.ledger.journal import (
    capital_entry,
    fill_entry,
    rebuild_holdings,
    validate_entries,
)

_ONE = Decimal("1")
_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class FactorialOrderState:
    intent: OrderIntent
    remaining_quantity: Decimal
    valid_until: datetime

    def __post_init__(self) -> None:
        if self.remaining_quantity <= 0:
            raise ValueError("remaining order quantity must be positive")
        if self.remaining_quantity > self.intent.quantity:
            raise ValueError("remaining quantity exceeds original intent")
        if self.valid_until.tzinfo is None:
            raise ValueError("factorial order validity must be timezone-aware")
        if self.valid_until <= self.intent.created_at:
            raise ValueError(
                "factorial order validity must follow intent creation"
            )


@dataclass(frozen=True, slots=True)
class FactorialPaperArm:
    contract: FactorialArmContract
    portfolio: ArmState
    pending_orders: tuple[FactorialOrderState, ...]
    fills: tuple[Fill, ...]
    ledger: tuple[LedgerEntry, ...]
    latest_nav_usd: Decimal
    common_market_manifest_hash: str
    forecast_hash: str
    policy_version: str
    decision_schedule_version: str
    execution_scenario_version: str
    cost_model_version: str
    starting_capital_usd: Decimal
    config_manifest_hash: str
    real_order_routing: bool = False

    def __post_init__(self) -> None:
        if self.portfolio.arm_id != self.contract.arm_id:
            raise ValueError("factorial arm contract/state mismatch")
        if self.real_order_routing:
            raise ValueError("factorial research arms are paper-only")
        if self.starting_capital_usd <= 0:
            raise ValueError("factorial starting capital must be positive")
        if self.portfolio.initial_cash_usd != self.starting_capital_usd:
            raise ValueError("factorial starting capital/state mismatch")
        if any(
            not value.strip()
            for value in (
                self.policy_version,
                self.decision_schedule_version,
                self.execution_scenario_version,
                self.cost_model_version,
            )
        ):
            raise ValueError("factorial version bindings must not be blank")
        if re.fullmatch(r"[a-f0-9]{64}", self.config_manifest_hash) is None:
            raise ValueError("factorial config manifest hash must be SHA-256")
        if any(
            re.fullmatch(r"[a-f0-9]{64}", value) is None
            for value in (
                self.common_market_manifest_hash,
                self.forecast_hash,
            )
        ):
            raise ValueError(
                "factorial market and forecast hashes must be SHA-256"
            )
        if any(order.intent.arm_id != self.contract.arm_id for order in self.pending_orders):
            raise ValueError("pending order belongs to another factorial arm")
        if any(fill.arm_id != self.contract.arm_id for fill in self.fills):
            raise ValueError("fill belongs to another factorial arm")
        if any(
            fill.execution_scenario_id != self.execution_scenario_version
            for fill in self.fills
        ):
            raise ValueError("factorial fill execution scenario mismatch")
        validate_entries(list(self.ledger))
        ledger_cash, ledger_positions = rebuild_holdings(list(self.ledger))
        if (
            ledger_cash != self.portfolio.cash_usd
            or ledger_positions != self.portfolio.positions
            or self.portfolio.sequence != len(self.fills)
        ):
            raise ValueError(
                "factorial portfolio does not reconcile to fills and ledger"
            )


def initialize_factorial_paper_arms(
    *,
    starting_capital_usd: Decimal,
    effective_at: datetime,
    common_market_manifest_hash: str,
    forecast_hash: str,
    policy_version: str,
    decision_schedule_version: str,
    execution_scenario_version: str,
    cost_model_version: str,
    config_manifest_hash: str,
) -> dict[str, FactorialPaperArm]:
    if starting_capital_usd <= 0:
        raise ValueError("starting capital must be positive")
    arms: dict[str, FactorialPaperArm] = {}
    for contract in factorial_arm_contracts():
        portfolio = ArmState(
            arm_id=contract.arm_id,
            initial_cash_usd=starting_capital_usd,
            cash_usd=starting_capital_usd,
            positions={},
            sequence=0,
        )
        entry = capital_entry(
            contract.arm_id,
            starting_capital_usd,
            effective_at,
        )
        arms[contract.arm_id] = FactorialPaperArm(
            contract=contract,
            portfolio=portfolio,
            pending_orders=(),
            fills=(),
            ledger=(entry,),
            latest_nav_usd=starting_capital_usd,
            common_market_manifest_hash=common_market_manifest_hash,
            forecast_hash=forecast_hash,
            policy_version=policy_version,
            decision_schedule_version=decision_schedule_version,
            execution_scenario_version=execution_scenario_version,
            cost_model_version=cost_model_version,
            starting_capital_usd=starting_capital_usd,
            config_manifest_hash=config_manifest_hash,
        )
    if tuple(arms) != FACTORIAL_ARM_IDS:
        raise ValueError("factorial arm initialization order changed")
    if len({id(arm.portfolio.positions) for arm in arms.values()}) != len(arms):
        raise ValueError("factorial positions must be independent")
    return arms


def factorial_target_weights(
    *,
    base_weights: dict[str, Decimal],
    guard_risk_multiplier: Decimal,
    ai_risk_multiplier: Decimal,
) -> dict[str, dict[str, Decimal]]:
    _validate_base_weights(base_weights)
    for name, multiplier in (
        ("guard", guard_risk_multiplier),
        ("ai", ai_risk_multiplier),
    ):
        if multiplier < 0 or multiplier > 1:
            raise ValueError(f"{name} risk multiplier must be within [0, 1]")
    treatment_multiplier = {
        "B0-VOL": _ONE,
        "B3-GUARD": guard_risk_multiplier,
        "B3-AI": ai_risk_multiplier,
        "B3-AI-GUARD": guard_risk_multiplier * ai_risk_multiplier,
    }
    targets = {
        arm_id: _scale_risky_weights(base_weights, multiplier)
        for arm_id, multiplier in treatment_multiplier.items()
    }
    for arm_id, target in targets.items():
        for symbol, base_weight in base_weights.items():
            if symbol != "USD_CASH" and target[symbol] > base_weight:
                raise ValueError(f"{arm_id} treatment increased risky exposure")
    return targets


def plan_factorial_rebalance(
    *,
    arms: dict[str, FactorialPaperArm],
    targets: dict[str, dict[str, Decimal]],
    prices: dict[str, Decimal],
    created_at: datetime,
    valid_until: datetime,
    decision_scope: str,
    minimum_notional_usd: Decimal,
) -> dict[str, FactorialPaperArm]:
    if set(arms) != set(FACTORIAL_ARM_IDS) or set(targets) != set(FACTORIAL_ARM_IDS):
        raise ValueError("factorial rebalance requires all four matched arms")
    if valid_until <= created_at:
        raise ValueError("factorial order validity must follow creation")
    if minimum_notional_usd < 0:
        raise ValueError("minimum notional cannot be negative")
    if any(arm.pending_orders for arm in arms.values()):
        raise ValueError(
            "factorial rebalance cannot replace unresolved paper orders"
        )
    market_hashes = {arm.common_market_manifest_hash for arm in arms.values()}
    forecast_hashes = {arm.forecast_hash for arm in arms.values()}
    matched_conditions = {
        (
            arm.decision_schedule_version,
            arm.execution_scenario_version,
            arm.cost_model_version,
            arm.starting_capital_usd,
            arm.config_manifest_hash,
            arm.policy_version,
        )
        for arm in arms.values()
    }
    if (
        len(market_hashes) != 1
        or len(forecast_hashes) != 1
        or len(matched_conditions) != 1
    ):
        raise ValueError(
            "factorial arms must share matched market, forecast, schedule, "
            "execution, cost, capital, config, and policy inputs"
        )
    planned: dict[str, FactorialPaperArm] = {}
    for arm_id in FACTORIAL_ARM_IDS:
        arm = arms[arm_id]
        target = targets[arm_id]
        nav = _nav(arm.portfolio, prices)
        orders: list[FactorialOrderState] = []
        for symbol in sorted(set(target) - {"USD_CASH"}):
            price = prices.get(symbol)
            if price is None or price <= 0:
                raise ValueError(f"missing positive price for {symbol}")
            current_quantity = arm.portfolio.positions.get(symbol, _ZERO)
            target_quantity = (
                nav * target[symbol] / price
            ).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            difference = target_quantity - current_quantity
            if difference == 0:
                continue
            side = OrderSide.BUY if difference > 0 else OrderSide.SELL
            quantity = abs(difference)
            if quantity * price < minimum_notional_usd:
                continue
            order_id = stable_id(
                "factorial-order",
                decision_scope,
                arm_id,
                symbol,
                side.value,
                str(quantity),
            )
            intent = OrderIntent(
                order_intent_id=order_id,
                arm_id=arm_id,
                portfolio_decision_id=stable_id(
                    "factorial-decision",
                    decision_scope,
                    arm_id,
                ),
                risk_decision_id=stable_id(
                    "factorial-risk",
                    decision_scope,
                    arm_id,
                ),
                symbol=symbol,
                side=side,
                order_type="PAPER_MARKETABLE_LIMIT",
                quantity=quantity,
                limit_price=None,
                time_in_force="DAY",
                session="REGULAR",
                client_order_id=order_id,
                idempotency_key=stable_id("factorial-idempotency", order_id),
                created_at=created_at,
            )
            orders.append(
                FactorialOrderState(
                    intent=intent,
                    remaining_quantity=quantity,
                    valid_until=valid_until,
                )
            )
        planned[arm_id] = FactorialPaperArm(
            contract=arm.contract,
            portfolio=arm.portfolio,
            pending_orders=tuple(orders),
            fills=arm.fills,
            ledger=arm.ledger,
            latest_nav_usd=nav,
            common_market_manifest_hash=arm.common_market_manifest_hash,
            forecast_hash=arm.forecast_hash,
            policy_version=arm.policy_version,
            decision_schedule_version=arm.decision_schedule_version,
            execution_scenario_version=arm.execution_scenario_version,
            cost_model_version=arm.cost_model_version,
            starting_capital_usd=arm.starting_capital_usd,
            config_manifest_hash=arm.config_manifest_hash,
        )
    return planned


def apply_factorial_fill(
    arm: FactorialPaperArm,
    *,
    fill: Fill,
    prices: dict[str, Decimal],
) -> FactorialPaperArm:
    matching = next(
        (
            order
            for order in arm.pending_orders
            if order.intent.order_intent_id == fill.order_intent_id
        ),
        None,
    )
    if matching is None:
        raise ValueError("fill has no pending order in this factorial arm")
    if fill.arm_id != arm.contract.arm_id:
        raise ValueError("fill belongs to another factorial arm")
    if fill.execution_scenario_id != arm.execution_scenario_version:
        raise ValueError("fill execution scenario is outside the matched contract")
    if fill.side is not matching.intent.side or fill.symbol != matching.intent.symbol:
        raise ValueError("fill does not match its factorial order")
    if fill.effective_at < matching.intent.created_at:
        raise ValueError("factorial fill predates its order")
    if fill.effective_at > matching.valid_until:
        raise ValueError("factorial fill is after order expiry")
    if fill.created_at < fill.effective_at:
        raise ValueError("factorial fill creation predates its effective time")
    if fill.quantity > matching.remaining_quantity:
        raise ValueError("factorial fill exceeds remaining quantity")
    updated_portfolio = arm.portfolio.apply_fill(fill)
    remaining = matching.remaining_quantity - fill.quantity
    pending = tuple(
        order
        for order in arm.pending_orders
        if order.intent.order_intent_id != fill.order_intent_id
    )
    if remaining > 0:
        pending = (
            *pending,
            FactorialOrderState(
                intent=matching.intent,
                remaining_quantity=remaining,
                valid_until=matching.valid_until,
            ),
        )
    ledger = (*arm.ledger, fill_entry(fill))
    validate_entries(list(ledger))
    return FactorialPaperArm(
        contract=arm.contract,
        portfolio=updated_portfolio,
        pending_orders=pending,
        fills=(*arm.fills, fill),
        ledger=ledger,
        latest_nav_usd=_nav(updated_portfolio, prices),
        common_market_manifest_hash=arm.common_market_manifest_hash,
        forecast_hash=arm.forecast_hash,
        policy_version=arm.policy_version,
        decision_schedule_version=arm.decision_schedule_version,
        execution_scenario_version=arm.execution_scenario_version,
        cost_model_version=arm.cost_model_version,
        starting_capital_usd=arm.starting_capital_usd,
        config_manifest_hash=arm.config_manifest_hash,
    )


def factorial_state_hash(arms: dict[str, FactorialPaperArm]) -> str:
    return canonical_hash(
        {
            arm_id: {
                "portfolio": arm.portfolio.as_payload(),
                "pending_orders": [
                    {
                        "order_intent_id": order.intent.order_intent_id,
                        "remaining_quantity": str(order.remaining_quantity),
                        "valid_until": order.valid_until.isoformat(),
                    }
                    for order in arm.pending_orders
                ],
                "fill_ids": [fill.fill_id for fill in arm.fills],
                "ledger_transaction_ids": [
                    entry.transaction.ledger_transaction_id for entry in arm.ledger
                ],
                "latest_nav_usd": str(arm.latest_nav_usd),
                "common_market_manifest_hash": arm.common_market_manifest_hash,
                "forecast_hash": arm.forecast_hash,
                "policy_version": arm.policy_version,
                "decision_schedule_version": arm.decision_schedule_version,
                "execution_scenario_version": arm.execution_scenario_version,
                "cost_model_version": arm.cost_model_version,
                "starting_capital_usd": str(arm.starting_capital_usd),
                "config_manifest_hash": arm.config_manifest_hash,
                "real_order_routing": arm.real_order_routing,
            }
            for arm_id, arm in sorted(arms.items())
        }
    )


def _validate_base_weights(base_weights: dict[str, Decimal]) -> None:
    if "USD_CASH" not in base_weights:
        raise ValueError("base weights require USD_CASH")
    if any(weight < 0 for weight in base_weights.values()):
        raise ValueError("base weights must be long-only")
    if abs(sum(base_weights.values(), _ZERO) - _ONE) > Decimal("0.0000000001"):
        raise ValueError("base weights must sum to one")


def _scale_risky_weights(
    base_weights: dict[str, Decimal],
    multiplier: Decimal,
) -> dict[str, Decimal]:
    result = {
        symbol: weight * multiplier
        for symbol, weight in base_weights.items()
        if symbol != "USD_CASH"
    }
    result["USD_CASH"] = _ONE - sum(result.values(), _ZERO)
    return result


def _nav(portfolio: ArmState, prices: dict[str, Decimal]) -> Decimal:
    missing = sorted(set(portfolio.positions) - set(prices))
    if missing:
        raise ValueError("missing NAV prices: " + ",".join(missing))
    return portfolio.cash_usd + sum(
        (
            quantity * prices[symbol]
            for symbol, quantity in portfolio.positions.items()
        ),
        _ZERO,
    )
