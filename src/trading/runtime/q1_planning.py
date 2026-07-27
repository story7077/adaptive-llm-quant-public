from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import cast

from pydantic import JsonValue

from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.q1 import Q1ArmId, Q1DecisionInputManifest, Q1StrategyDecision
from trading.domain.q1_runtime import Q1OrderIntent
from trading.domain.time import require_aware_utc
from trading.execution.order_state import Q1OrderClass
from trading.quant.allocator import TurnoverResult


class Q1PlanningError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecisionQuote:
    symbol: str
    quote_id: str
    bid: Decimal
    ask: Decimal
    available_at: datetime

    def __post_init__(self) -> None:
        require_aware_utc(self.available_at)
        if not self.symbol or not self.quote_id:
            raise Q1PlanningError("Decision quote identity is required")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise Q1PlanningError("Decision quote is not executable")

    @property
    def midpoint(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.midpoint * Decimal("10000")


@dataclass(frozen=True, slots=True)
class OrderPlanningConfig:
    quantity_increment: Decimal
    commission_rate: Decimal
    commission_waiver_notional_usd: Decimal
    delay_penalty_bps: Decimal
    commission_precision: Decimal

    def __post_init__(self) -> None:
        if self.quantity_increment <= 0:
            raise Q1PlanningError("Quantity increment must be positive")
        if (
            self.commission_rate < 0
            or self.commission_waiver_notional_usd < 0
            or self.delay_penalty_bps < 0
            or self.commission_precision <= 0
        ):
            raise Q1PlanningError(
                "Planning cost parameters must be non-negative with positive precision"
            )


@dataclass(frozen=True, slots=True)
class PlannedOrders:
    intents: tuple[Q1OrderIntent, ...]
    omitted: tuple[dict[str, str], ...]
    intent_manifest_hash: str


def build_portfolio_decision(
    *,
    run_id: str,
    arm_id: Q1ArmId,
    source_cycle_id: str,
    input_state_sequence: int,
    decision_kind: str,
    scheduled_at: datetime,
    signal_data_cutoff: datetime,
    portfolio_state_as_of: datetime,
    quote_as_of: datetime,
    decision_created_at: datetime,
    valid_until: datetime,
    current_weights: Mapping[str, Decimal],
    deterministic_target_weights: Mapping[str, Decimal],
    final_target_weights: Mapping[str, Decimal],
    expected_annualized_volatility: Decimal,
    expected_one_way_turnover: Decimal,
    used_daily_turnover_before: Decimal,
    signal_hash: str | None,
    allocation_hash: str | None,
    llm_overlay_state: str,
    llm_policy_id: str | None,
    diagnostics: dict[str, object],
    input_manifest: Q1DecisionInputManifest,
    worker_fence_token: str,
    cycle_attempt_count: int,
) -> Q1StrategyDecision:
    complete_diagnostics = {
        **diagnostics,
        "current_weights": dict(current_weights),
        "deterministic_target_weights": dict(
            deterministic_target_weights
        ),
        "final_target_weights": dict(final_target_weights),
        "expected_annualized_volatility": expected_annualized_volatility,
        "expected_one_way_turnover": expected_one_way_turnover,
        "used_daily_turnover_before": used_daily_turnover_before,
        "signal_hash": signal_hash,
        "allocation_hash": allocation_hash,
        "llm_overlay_state": llm_overlay_state,
        "llm_policy_id": llm_policy_id,
    }
    # Lease ownership and retry count fence the eventual database write, but
    # they are operational provenance rather than economic decision inputs.
    # Excluding them keeps a reclaimed cycle deterministic for identical
    # versioned market, state, code, model, and configuration inputs.
    content = {
        "run_id": run_id,
        "arm_id": arm_id,
        "algorithm_version": "q1_math_core_v1",
        "source_cycle_id": source_cycle_id,
        "input_state_sequence": input_state_sequence,
        "decision_kind": decision_kind,
        "scheduled_at": scheduled_at,
        "signal_data_cutoff": signal_data_cutoff,
        "portfolio_state_as_of": portfolio_state_as_of,
        "quote_as_of": quote_as_of,
        "decision_created_at": decision_created_at,
        "valid_until": valid_until,
        "target_weights": dict(final_target_weights),
        "diagnostics": complete_diagnostics,
        "input_manifest": input_manifest,
    }
    decision_hash = canonical_hash(content)
    return Q1StrategyDecision(
        portfolio_decision_id=stable_id(
            "q1-portfolio-decision",
            run_id,
            arm_id,
            scheduled_at,
            decision_hash,
        ),
        run_id=run_id,
        arm_id=arm_id,
        algorithm_version="q1_math_core_v1",
        source_cycle_id=source_cycle_id,
        input_state_sequence=input_state_sequence,
        decision_kind=decision_kind,
        scheduled_at=scheduled_at,
        signal_data_cutoff=signal_data_cutoff,
        portfolio_state_as_of=portfolio_state_as_of,
        quote_as_of=quote_as_of,
        decision_created_at=decision_created_at,
        valid_until=valid_until,
        input_manifest=input_manifest,
        target_weights=dict(final_target_weights),
        diagnostics=cast(
            dict[str, JsonValue],
            canonical_data(complete_diagnostics),
        ),
        worker_fence_token=worker_fence_token,
        cycle_attempt_count=cycle_attempt_count,
        config_manifest_hash=input_manifest.config_manifest_hash,
        code_version=input_manifest.code_version,
        model_version=input_manifest.model_version,
        source_manifest_hash=input_manifest.source_manifest_hash,
        decision_hash=decision_hash,
    )


def risk_approval_id(decision: Q1StrategyDecision) -> str:
    return stable_id(
        "q1-risk-approval",
        decision.portfolio_decision_id,
        decision.target_weights,
    )


def plan_normal_orders(
    *,
    decision: Q1StrategyDecision,
    turnover: TurnoverResult,
    positions: Mapping[str, Decimal],
    settled_cash_usd: Decimal,
    quotes: Mapping[str, DecisionQuote],
    source_cycle_id: str,
    input_state_sequence: int,
    valid_until: datetime,
    config: OrderPlanningConfig,
) -> PlannedOrders:
    created_at = decision.decision_created_at
    expiry = require_aware_utc(valid_until)
    if expiry != decision.valid_until:
        raise Q1PlanningError("Order expiry must match decision valid_until")
    if settled_cash_usd < 0:
        raise Q1PlanningError("Settled cash cannot be negative")
    if turnover.decision_kind.startswith("NO_"):
        return PlannedOrders(
            intents=(),
            omitted=tuple(
                {
                    "symbol": item.symbol,
                    "side": item.side,
                    "reason": item.reason,
                    "proposed_notional_usd": str(item.proposed_notional_usd),
                }
                for item in turnover.omitted_orders
            ),
            intent_manifest_hash=canonical_hash(
                {
                    "decision_id": decision.portfolio_decision_id,
                    "intents": [],
                    "omitted": turnover.omitted_orders,
                }
            ),
        )

    risk_decision_id = risk_approval_id(decision)
    remaining_settled = settled_cash_usd
    intents: list[Q1OrderIntent] = []
    omitted = [
        {
            "symbol": item.symbol,
            "side": item.side,
            "reason": item.reason,
            "proposed_notional_usd": str(item.proposed_notional_usd),
        }
        for item in turnover.omitted_orders
    ]
    for trade in sorted(
        turnover.proposed_trades,
        key=lambda item: (
            item.side != "SELL",
            item.symbol,
        ),
    ):
        quote = quotes.get(trade.symbol)
        if quote is None:
            raise Q1PlanningError(
                f"Missing decision quote for {trade.symbol}"
            )
        quantity = (trade.notional_usd / quote.midpoint).quantize(
            config.quantity_increment,
            rounding=ROUND_DOWN,
        )
        if trade.side == "SELL":
            quantity = min(
                quantity,
                positions.get(trade.symbol, Decimal("0")),
            ).quantize(config.quantity_increment, rounding=ROUND_DOWN)
        else:
            delayed_ask = quote.ask * (
                Decimal("1")
                + config.delay_penalty_bps / Decimal("10000")
            )
            conservative_unit_cost = delayed_ask * (
                Decimal("1") + config.commission_rate
            )
            available_cash = max(
                Decimal("0"),
                remaining_settled - config.commission_precision,
            )
            affordable = (
                available_cash / conservative_unit_cost
            ).quantize(config.quantity_increment, rounding=ROUND_DOWN)
            quantity = min(quantity, affordable)
        if quantity <= 0:
            omitted.append(
                {
                    "symbol": trade.symbol,
                    "side": trade.side,
                    "reason": (
                        "INSUFFICIENT_SETTLED_CASH"
                        if trade.side == "BUY"
                        else "NO_SELLABLE_QUANTITY"
                    ),
                    "proposed_notional_usd": str(trade.notional_usd),
                }
            )
            continue
        side = OrderSide(trade.side)
        identity = {
            "run_id": decision.run_id,
            "arm_id": decision.arm_id,
            "portfolio_decision_id": decision.portfolio_decision_id,
            "source_cycle_id": source_cycle_id,
            "symbol": trade.symbol,
            "side": side,
            "quantity": quantity,
            "decision_quote_id": quote.quote_id,
            "created_at": created_at,
            "valid_until": expiry,
        }
        intent_hash = canonical_hash(
            {
                **identity,
                "risk_decision_id": risk_decision_id,
                "decision_reference_price": quote.midpoint,
                "decision_spread_bps": quote.spread_bps,
                "input_state_sequence": input_state_sequence,
                "config_manifest_hash": (
                    decision.input_manifest.config_manifest_hash
                ),
                "code_version": decision.input_manifest.code_version,
                "model_version": decision.input_manifest.model_version,
                "source_manifest_hash": (
                    decision.input_manifest.source_manifest_hash
                ),
            }
        )
        order_intent_id = stable_id("q1-order-intent", intent_hash)
        intent = Q1OrderIntent(
            order_intent_id=order_intent_id,
            run_id=decision.run_id,
            arm_id=decision.arm_id,
            portfolio_decision_id=decision.portfolio_decision_id,
            risk_decision_id=risk_decision_id,
            source_cycle_id=source_cycle_id,
            input_state_sequence=input_state_sequence,
            symbol=trade.symbol,
            side=side,
            order_class=Q1OrderClass.NORMAL.value,
            quantity=quantity,
            decision_quote_id=quote.quote_id,
            decision_reference_price=quote.midpoint,
            decision_spread_bps=quote.spread_bps,
            created_at=created_at,
            valid_until=expiry,
            idempotency_key=stable_id("q1-order-intent-idem", intent_hash),
            algorithm_version="q1_math_core_v1",
            config_manifest_hash=decision.input_manifest.config_manifest_hash,
            code_version=decision.input_manifest.code_version,
            model_version=decision.input_manifest.model_version,
            source_manifest_hash=decision.input_manifest.source_manifest_hash,
            intent_hash=intent_hash,
        )
        intents.append(intent)
        if side == "BUY":
            estimated_notional = quantity * quote.ask
            estimated_commission = (
                Decimal("0")
                if estimated_notional
                <= config.commission_waiver_notional_usd
                else estimated_notional * config.commission_rate
            )
            remaining_settled -= estimated_notional + estimated_commission
    manifest_hash = canonical_hash(
        {
            "decision_id": decision.portfolio_decision_id,
            "intents": intents,
            "omitted": omitted,
        }
    )
    return PlannedOrders(
        intents=tuple(intents),
        omitted=tuple(omitted),
        intent_manifest_hash=manifest_hash,
    )


def plan_target_quantity_sell_orders(
    *,
    decision: Q1StrategyDecision,
    current_positions: Mapping[str, Decimal],
    target_quantities: Mapping[str, Decimal],
    quotes: Mapping[str, DecisionQuote],
    source_cycle_id: str,
    input_state_sequence: int,
    order_class: Q1OrderClass,
    config: OrderPlanningConfig,
) -> PlannedOrders:
    if order_class not in {
        Q1OrderClass.EMERGENCY_REDUCTION,
        Q1OrderClass.LLM_REDUCTION,
        Q1OrderClass.LIVE_MIRROR_TRANSITION,
    }:
        raise Q1PlanningError("Target-quantity planner is sell-only")
    intents: list[Q1OrderIntent] = []
    omitted: list[dict[str, str]] = []
    risk_decision_id = risk_approval_id(decision)
    for symbol, target in sorted(target_quantities.items()):
        current = current_positions.get(symbol, Decimal("0"))
        if target < 0 or target > current:
            raise Q1PlanningError("Sell target must be within current position")
        quantity = (current - target).quantize(
            config.quantity_increment,
            rounding=ROUND_DOWN,
        )
        if quantity <= 0:
            continue
        quote = quotes.get(symbol)
        if quote is None:
            raise Q1PlanningError(
                f"Missing residual-target quote for {symbol}"
            )
        identity = {
            "run_id": decision.run_id,
            "arm_id": decision.arm_id,
            "portfolio_decision_id": decision.portfolio_decision_id,
            "source_cycle_id": source_cycle_id,
            "symbol": symbol,
            "side": OrderSide.SELL,
            "quantity": quantity,
            "target_quantity": target,
            "decision_quote_id": quote.quote_id,
            "created_at": decision.decision_created_at,
            "valid_until": decision.valid_until,
            "order_class": order_class,
        }
        intent_hash = canonical_hash(
            {
                **identity,
                "risk_decision_id": risk_decision_id,
                "decision_reference_price": quote.midpoint,
                "decision_spread_bps": quote.spread_bps,
                "input_state_sequence": input_state_sequence,
                "config_manifest_hash": decision.config_manifest_hash,
                "code_version": decision.code_version,
                "model_version": decision.model_version,
                "source_manifest_hash": decision.source_manifest_hash,
            }
        )
        intents.append(
            Q1OrderIntent(
                order_intent_id=stable_id("q1-order-intent", intent_hash),
                run_id=decision.run_id,
                arm_id=decision.arm_id,
                portfolio_decision_id=decision.portfolio_decision_id,
                risk_decision_id=risk_decision_id,
                source_cycle_id=source_cycle_id,
                input_state_sequence=input_state_sequence,
                symbol=symbol,
                side=OrderSide.SELL,
                order_class=order_class.value,
                quantity=quantity,
                decision_quote_id=quote.quote_id,
                decision_reference_price=quote.midpoint,
                decision_spread_bps=quote.spread_bps,
                created_at=decision.decision_created_at,
                valid_until=decision.valid_until,
                idempotency_key=stable_id(
                    "q1-order-intent-idem",
                    intent_hash,
                ),
                algorithm_version=decision.algorithm_version,
                config_manifest_hash=decision.config_manifest_hash,
                code_version=decision.code_version,
                model_version=decision.model_version,
                source_manifest_hash=decision.source_manifest_hash,
                intent_hash=intent_hash,
            )
        )
    return PlannedOrders(
        intents=tuple(intents),
        omitted=tuple(omitted),
        intent_manifest_hash=canonical_hash(
            {
                "decision_id": decision.portfolio_decision_id,
                "intents": intents,
                "omitted": omitted,
            }
        ),
    )
