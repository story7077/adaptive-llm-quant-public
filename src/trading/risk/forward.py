from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Any, cast

from pydantic import JsonValue

from trading.domain.contracts import OrderIntent, PortfolioDecision, RiskDecision
from trading.domain.enums import OrderSide
from trading.domain.hashing import stable_id
from trading.experiments.arms import ArmState
from trading.portfolio.forward import CASH_SYMBOL, CORE_SYMBOL, ForwardTarget

CENT = Decimal("0.01")
WEIGHT_EPSILON = Decimal("0.00000001")


class ForwardRiskError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecisionQuote:
    symbol: str
    quote_id: str
    bid_price: Decimal
    ask_price: Decimal
    bid_size_round_lots: int
    ask_size_round_lots: int
    event_time: datetime
    available_at: datetime

    @property
    def midpoint(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return (
            (self.ask_price - self.bid_price)
            / self.midpoint
            * Decimal("10000")
        )


@dataclass(frozen=True, slots=True)
class ForwardRiskConfig:
    version: str
    transition_version: str
    core_version: str
    max_one_way_daily_turnover: Decimal
    min_order_notional_usd: Decimal
    buy_cash_reserve_fraction: Decimal
    spend_unsettled_sale_proceeds: bool
    max_single_symbol_weight: Decimal
    max_semiconductor_cluster_weight: Decimal
    max_leveraged_etf_weight: Decimal
    max_combined_leveraged_etf_weight: Decimal
    max_gross_exposure: Decimal
    soft_daily_loss_fraction: Decimal
    hard_daily_loss_fraction: Decimal
    block_entries_drawdown_fraction: Decimal
    force_reduce_drawdown_fraction: Decimal
    force_reduce_core_fraction: Decimal
    commission_rate: Decimal
    commission_waiver_threshold_usd: Decimal
    delay_penalty_bps: Decimal
    quantity_precision: Decimal
    sell_only_symbols: frozenset[str]
    entry_symbols: frozenset[str]
    semiconductor_symbols: frozenset[str]
    leveraged_symbols: frozenset[str]
    core_cap_exempt_arms: frozenset[str]


@dataclass(frozen=True, slots=True)
class ForwardOrderPlan:
    input_state_sequence: int
    portfolio_decision: PortfolioDecision
    risk_decision: RiskDecision
    intents: tuple[OrderIntent, ...]
    approved_state_payload: dict[str, Any]
    diagnostics: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ForwardLossGuard:
    state: str
    session_open_nav_usd: Decimal
    current_nav_usd: Decimal
    peak_nav_usd: Decimal
    daily_loss_fraction: Decimal
    run_drawdown_fraction: Decimal
    block_new_entries: bool
    forced_sell_budget_usd: Decimal | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "session_open_nav_usd": format(self.session_open_nav_usd, "f"),
            "current_nav_usd": format(self.current_nav_usd, "f"),
            "peak_nav_usd": format(self.peak_nav_usd, "f"),
            "daily_loss_fraction": format(self.daily_loss_fraction, "f"),
            "run_drawdown_fraction": format(self.run_drawdown_fraction, "f"),
            "block_new_entries": self.block_new_entries,
            "forced_sell_budget_usd": (
                None
                if self.forced_sell_budget_usd is None
                else format(self.forced_sell_budget_usd, "f")
            ),
        }


def evaluate_forward_loss_guard(
    *,
    state: ArmState,
    quotes: dict[str, DecisionQuote],
    session_open_nav_usd: Decimal,
    peak_nav_usd: Decimal,
    config: ForwardRiskConfig,
) -> ForwardLossGuard:
    if session_open_nav_usd <= 0:
        raise ForwardRiskError("Session-open NAV must be positive")
    current_nav = state.cash_usd + sum(
        (
            quantity * quotes[symbol].midpoint
            for symbol, quantity in state.positions.items()
            if quantity != 0
        ),
        Decimal("0"),
    )
    if current_nav <= 0:
        raise ForwardRiskError("Current arm NAV must be positive")
    effective_peak = max(peak_nav_usd, current_nav)
    if effective_peak <= 0:
        raise ForwardRiskError("Peak NAV must be positive")
    daily_loss = max(
        Decimal("0"),
        (session_open_nav_usd - current_nav) / session_open_nav_usd,
    )
    drawdown = max(
        Decimal("0"),
        (effective_peak - current_nav) / effective_peak,
    )
    forced_budget: Decimal | None = None
    if drawdown >= config.force_reduce_drawdown_fraction:
        loss_state = "FORCE_REDUCE"
        forced_budget = _forced_reduction_budget(
            state=state,
            quotes=quotes,
            current_nav=current_nav,
            config=config,
            include_core=True,
        )
    elif daily_loss >= config.hard_daily_loss_fraction:
        loss_state = "HARD_STOP"
        forced_budget = _forced_reduction_budget(
            state=state,
            quotes=quotes,
            current_nav=current_nav,
            config=config,
            include_core=True,
        )
    elif drawdown >= config.block_entries_drawdown_fraction:
        loss_state = "HARD_STOP"
    elif daily_loss >= config.soft_daily_loss_fraction:
        loss_state = "SOFT_STOP"
    else:
        loss_state = "NORMAL"
    return ForwardLossGuard(
        state=loss_state,
        session_open_nav_usd=session_open_nav_usd,
        current_nav_usd=current_nav,
        peak_nav_usd=effective_peak,
        daily_loss_fraction=daily_loss,
        run_drawdown_fraction=drawdown,
        block_new_entries=loss_state != "NORMAL",
        forced_sell_budget_usd=forced_budget,
    )


def build_forward_order_plan(
    *,
    run_id: str,
    cycle_id: str,
    state: ArmState,
    target: ForwardTarget,
    quotes: dict[str, DecisionQuote],
    decision_time: datetime,
    intent_created_at: datetime,
    valid_until: datetime,
    input_snapshot_hash: str,
    session_open_nav_usd: Decimal,
    today_buy_notional_usd: Decimal,
    today_sell_notional_usd: Decimal,
    unsettled_sale_proceeds_usd: Decimal,
    config: ForwardRiskConfig,
    loss_state: str = "NORMAL",
    block_new_entries: bool = False,
    forced_sell_budget_usd: Decimal | None = None,
    forced_target_quantities: dict[str, Decimal] | None = None,
    allow_transition_sells: bool = True,
) -> ForwardOrderPlan:
    if state.arm_id != target.arm_id:
        raise ForwardRiskError("Arm state and forward target differ")
    if session_open_nav_usd <= 0:
        raise ForwardRiskError("Session-open NAV must be positive")
    if unsettled_sale_proceeds_usd < 0:
        raise ForwardRiskError("Unsettled sale proceeds cannot be negative")
    required_symbols = {
        symbol for symbol, quantity in state.positions.items() if quantity != 0
    }
    required_symbols.add(CORE_SYMBOL)
    missing = sorted(required_symbols - set(quotes))
    if missing:
        raise ForwardRiskError(f"Decision quote bundle is incomplete: {missing}")

    current_values = {
        symbol: quantity * quotes[symbol].midpoint
        for symbol, quantity in state.positions.items()
        if quantity != 0
    }
    nav = state.cash_usd + sum(current_values.values(), Decimal("0"))
    if nav <= 0:
        raise ForwardRiskError("Current arm NAV must be positive")
    previous_weights = {
        symbol: float(value / nav)
        for symbol, value in sorted(current_values.items())
    }
    previous_weights[CASH_SYMBOL] = float(state.cash_usd / nav)
    desired_values = {
        symbol: nav * Decimal(str(weight))
        for symbol, weight in target.target_weights.items()
        if symbol != CASH_SYMBOL and weight > 0
    }
    forced_targets = {
        symbol: max(Decimal("0"), Decimal(quantity))
        for symbol, quantity in (forced_target_quantities or {}).items()
    }
    unknown_forced_symbols = sorted(set(forced_targets) - set(quotes))
    if unknown_forced_symbols:
        raise ForwardRiskError(
            f"Forced target quote bundle is incomplete: {unknown_forced_symbols}"
        )
    for symbol, target_quantity in forced_targets.items():
        desired_values[symbol] = min(
            desired_values.get(symbol, Decimal("0")),
            target_quantity * quotes[symbol].midpoint,
        )
    desired_turnover = _one_way_turnover(
        current_values=current_values,
        desired_values=desired_values,
        nav=nav,
    )

    sell_budget = max(
        Decimal("0"),
        session_open_nav_usd * config.max_one_way_daily_turnover
        - today_sell_notional_usd,
    )
    if not allow_transition_sells:
        sell_budget = Decimal("0")
    if forced_sell_budget_usd is not None and forced_sell_budget_usd < 0:
        raise ForwardRiskError("forced_sell_budget_usd cannot be negative")
    forced_sell_residual_usd = sum(
        (
            max(
                Decimal("0"),
                state.positions.get(symbol, Decimal("0")) - target_quantity,
            )
            * quotes[symbol].bid_price
            for symbol, target_quantity in forced_targets.items()
        ),
        Decimal("0"),
    )
    sell_budget = max(
        sell_budget,
        forced_sell_residual_usd,
        forced_sell_budget_usd or Decimal("0"),
    )
    buy_budget = max(
        Decimal("0"),
        session_open_nav_usd * config.max_one_way_daily_turnover
        - today_buy_notional_usd,
    )

    sell_orders = _sell_orders(
        state=state,
        current_values=current_values,
        desired_values=desired_values,
        quotes=quotes,
        budget=sell_budget,
        forced_target_quantities=forced_targets,
        allow_transition_sells=allow_transition_sells,
        config=config,
    )
    blocked_entries = block_new_entries or _qqq_entry_is_blocked(
        target.blocked_new_entries
    )
    buy_orders = _buy_orders(
        state=state,
        current_values=current_values,
        desired_values=desired_values,
        quotes=quotes,
        budget=buy_budget,
        unsettled_sale_proceeds_usd=(
            Decimal("0")
            if config.spend_unsettled_sale_proceeds
            else unsettled_sale_proceeds_usd
        ),
        blocked_entries=blocked_entries,
        config=config,
    )
    planned = (*sell_orders, *buy_orders)
    projected_cash, projected_positions, estimated_cost = _project_state(
        state=state,
        orders=planned,
        quotes=quotes,
        config=config,
    )
    if projected_cash < 0:
        raise ForwardRiskError("Forward plan would create negative USD cash")
    projected_nav = projected_cash + sum(
        (
            quantity * quotes[symbol].midpoint
            for symbol, quantity in projected_positions.items()
            if quantity != 0
        ),
        Decimal("0"),
    )
    if projected_nav <= 0:
        raise ForwardRiskError("Forward plan would create non-positive NAV")
    approved_weights = {
        symbol: float(quantity * quotes[symbol].midpoint / projected_nav)
        for symbol, quantity in sorted(projected_positions.items())
        if quantity != 0
    }
    approved_weights[CASH_SYMBOL] = float(projected_cash / projected_nav)
    approved_weights = _normalize_weights(approved_weights)

    rejection_reasons = _risk_rejections(
        arm_id=state.arm_id,
        current_values=current_values,
        current_nav=nav,
        projected_positions=projected_positions,
        projected_nav=projected_nav,
        unavoidable_cost_usd=estimated_cost,
        allow_cost_epsilon=(
            bool(planned)
            and all(order["side"] is OrderSide.SELL for order in planned)
        ),
        quotes=quotes,
        config=config,
    )
    portfolio_id = stable_id(
        "forward-portfolio",
        run_id,
        cycle_id,
        state.arm_id,
        state.sequence,
        input_snapshot_hash,
    )
    portfolio = PortfolioDecision(
        portfolio_decision_id=portfolio_id,
        arm_id=state.arm_id,
        decision_time=decision_time,
        core_portfolio_version=config.core_version,
        policy_version=target.policy_version,
        forecast_ids=[],
        input_snapshot_hash=input_snapshot_hash,
        previous_weights=previous_weights,
        target_weights_pre_risk=target.target_weights,
        expected_net_return_bps=0.0,
        expected_annualized_vol=(
            target.core_forecast.annualized_vol
            * target.target_weights.get(CORE_SYMBOL, 0.0)
        ),
        expected_cvar_975=0.0,
        expected_turnover=float(desired_turnover),
        expected_cost_usd=estimated_cost,
        optimizer_status=(
            "FORWARD_BASELINE_TRANSITION"
            if planned
            else "NO_TRADE_WITHIN_THRESHOLD"
        ),
        solver_name="DETERMINISTIC_B0_FORWARD_V1",
        solver_diagnostics={
            "target_reason": target.target_reason,
            "core_forecast_version": target.core_forecast.version,
            "core_forecast_vol": target.core_forecast.annualized_vol,
            "core_qqq_weight": target.core_forecast.qqq_weight,
            "policy_risk_multiplier": target.policy_risk_multiplier,
            "blocked_new_entries": cast(
                JsonValue,
                sorted(target.blocked_new_entries),
            ),
            "loss_state": loss_state,
            "transition_version": config.transition_version,
        },
        created_at=intent_created_at,
    )
    risk_id = stable_id("forward-risk", portfolio_id, config.version)
    approved = not rejection_reasons
    risk = RiskDecision(
        risk_decision_id=risk_id,
        portfolio_decision_id=portfolio_id,
        approved=approved,
        approved_target_weights=approved_weights if approved else {},
        rejected_reasons=rejection_reasons,
        forced_reduction_actions=(
            [
                {
                    "action": (
                        "FORCE_REDUCE"
                        if loss_state == "FORCE_REDUCE"
                        else (
                            "HARD_STOP_REDUCE"
                            if loss_state == "HARD_STOP"
                            else "GRANDFATHERED_ACCOUNT_TRANSITION"
                        )
                    ),
                    "loss_state": loss_state,
                    "transition_version": config.transition_version,
                    "sell_only": [
                        item["symbol"]
                        for item in planned
                        if item["side"] is OrderSide.SELL
                    ],
                    "target_quantities": {
                        symbol: format(quantity, "f")
                        for symbol, quantity in sorted(forced_targets.items())
                    },
                }
            ]
            if forced_targets
            else (
                [
                    {
                        "action": "GRANDFATHERED_ACCOUNT_TRANSITION",
                        "loss_state": loss_state,
                        "transition_version": config.transition_version,
                        "sell_only": [
                            item["symbol"]
                            for item in planned
                            if item["side"] is OrderSide.SELL
                        ],
                        "target_quantities": {},
                    }
                ]
                if sell_orders
                else []
            )
        ),
        risk_config_version=config.version,
        market_data_age_seconds=max(
            (
                (intent_created_at - quote.event_time).total_seconds()
                for quote in quotes.values()
            ),
            default=0.0,
        ),
        created_at=intent_created_at,
    )
    intents = (
        _order_intents(
            run_id=run_id,
            cycle_id=cycle_id,
            arm_id=state.arm_id,
            state_sequence=state.sequence,
            portfolio=portfolio,
            risk=risk,
            orders=planned,
            created_at=intent_created_at,
            valid_until=valid_until,
        )
        if approved
        else ()
    )
    return ForwardOrderPlan(
        input_state_sequence=state.sequence,
        portfolio_decision=portfolio,
        risk_decision=risk,
        intents=intents,
        approved_state_payload={
            "cash_usd": format(projected_cash, "f"),
            "positions": {
                symbol: format(quantity, "f")
                for symbol, quantity in sorted(projected_positions.items())
                if quantity != 0
            },
            "projected_nav_usd": format(projected_nav, "f"),
        },
        diagnostics={
            "desired_turnover": format(desired_turnover, "f"),
            "sell_budget_usd": format(sell_budget, "f"),
            "buy_budget_usd": format(buy_budget, "f"),
            "unsettled_sale_proceeds_usd": format(
                unsettled_sale_proceeds_usd,
                "f",
            ),
            "estimated_cost_usd": format(estimated_cost, "f"),
            "blocked_entries": blocked_entries,
            "loss_state": loss_state,
            "forced_sell_budget_usd": (
                None
                if forced_sell_budget_usd is None
                else format(forced_sell_budget_usd, "f")
            ),
            "forced_sell_residual_usd": format(
                forced_sell_residual_usd,
                "f",
            ),
            "forced_target_quantities": {
                symbol: format(quantity, "f")
                for symbol, quantity in sorted(forced_targets.items())
            },
            "order_count": len(intents),
        },
    )


def _sell_orders(
    *,
    state: ArmState,
    current_values: dict[str, Decimal],
    desired_values: dict[str, Decimal],
    quotes: dict[str, DecisionQuote],
    budget: Decimal,
    forced_target_quantities: dict[str, Decimal],
    allow_transition_sells: bool,
    config: ForwardRiskConfig,
) -> tuple[dict[str, Any], ...]:
    candidates: list[tuple[int, int, Decimal, str, Decimal]] = []
    for symbol, current_value in current_values.items():
        current_quantity = state.positions[symbol]
        if symbol in forced_target_quantities:
            maximum_quantity = max(
                Decimal("0"),
                current_quantity - forced_target_quantities[symbol],
            )
            if maximum_quantity <= 0:
                continue
            priority = (
                0
                if symbol in config.leveraged_symbols
                else (
                    1
                    if symbol in config.semiconductor_symbols
                    else (2 if symbol == CORE_SYMBOL else 3)
                )
            )
            candidates.append(
                (
                    0,
                    priority,
                    -(maximum_quantity * quotes[symbol].bid_price),
                    symbol,
                    maximum_quantity,
                )
            )
            continue
        if not allow_transition_sells:
            continue
        surplus = max(
            Decimal("0"),
            current_value - desired_values.get(symbol, Decimal("0")),
        )
        if surplus <= 0:
            continue
        priority = (
            0
            if symbol in config.leveraged_symbols
            else (
                1
                if (
                    symbol in config.semiconductor_symbols
                    and symbol in config.sell_only_symbols
                )
                else 2
            )
        )
        maximum_quantity = min(
            current_quantity,
            _floor_quantity(
                surplus / quotes[symbol].bid_price,
                config.quantity_precision,
            ),
        )
        candidates.append(
            (1, priority, -surplus, symbol, maximum_quantity)
        )
    remaining = budget
    orders: list[dict[str, Any]] = []
    for _, _, negative_surplus, symbol, maximum_quantity in sorted(candidates):
        if remaining < config.min_order_notional_usd:
            break
        quote = quotes[symbol]
        notional = min(-negative_surplus, remaining)
        quantity = min(
            maximum_quantity,
            _floor_quantity(notional / quote.bid_price, config.quantity_precision),
        )
        expected_notional = quantity * quote.bid_price
        if quantity <= 0 or expected_notional < config.min_order_notional_usd:
            continue
        orders.append(
            {
                "symbol": symbol,
                "side": OrderSide.SELL,
                "quantity": quantity,
                "decision_quote_id": quote.quote_id,
            }
        )
        remaining -= expected_notional
    return tuple(orders)


def _buy_orders(
    *,
    state: ArmState,
    current_values: dict[str, Decimal],
    desired_values: dict[str, Decimal],
    quotes: dict[str, DecisionQuote],
    budget: Decimal,
    unsettled_sale_proceeds_usd: Decimal,
    blocked_entries: bool,
    config: ForwardRiskConfig,
) -> tuple[dict[str, Any], ...]:
    if blocked_entries:
        return ()
    spendable_cash = max(
        Decimal("0"),
        (state.cash_usd - unsettled_sale_proceeds_usd)
        * (Decimal("1") - config.buy_cash_reserve_fraction),
    )
    remaining = min(budget, spendable_cash)
    orders: list[dict[str, Any]] = []
    for symbol in sorted(desired_values):
        if symbol in config.sell_only_symbols or symbol not in config.entry_symbols:
            continue
        deficit = max(
            Decimal("0"),
            desired_values[symbol] - current_values.get(symbol, Decimal("0")),
        )
        if deficit < config.min_order_notional_usd:
            continue
        quote = quotes[symbol]
        delayed_ask = quote.ask_price * (
            Decimal("1") + config.delay_penalty_bps / Decimal("10000")
        )
        gross_budget = min(deficit, remaining)
        quantity = _floor_quantity(
            gross_budget
            / (delayed_ask * (Decimal("1") + config.commission_rate)),
            config.quantity_precision,
        )
        expected_notional = quantity * delayed_ask
        commission = _commission(expected_notional, config)
        expected_cash = expected_notional + commission
        while quantity > 0 and expected_cash > remaining:
            quantity = _floor_quantity(
                quantity - config.quantity_precision,
                config.quantity_precision,
            )
            expected_notional = quantity * delayed_ask
            commission = _commission(expected_notional, config)
            expected_cash = expected_notional + commission
        if quantity <= 0 or expected_notional < config.min_order_notional_usd:
            continue
        orders.append(
            {
                "symbol": symbol,
                "side": OrderSide.BUY,
                "quantity": quantity,
                "decision_quote_id": quote.quote_id,
            }
        )
        remaining -= expected_cash
    return tuple(orders)


def _project_state(
    *,
    state: ArmState,
    orders: tuple[dict[str, Any], ...],
    quotes: dict[str, DecisionQuote],
    config: ForwardRiskConfig,
) -> tuple[Decimal, dict[str, Decimal], Decimal]:
    cash = state.cash_usd
    positions = dict(state.positions)
    total_cost = Decimal("0")
    delay = config.delay_penalty_bps / Decimal("10000")
    for order in orders:
        symbol = str(order["symbol"])
        side = order["side"]
        quantity = Decimal(str(order["quantity"]))
        quote = quotes[symbol]
        price = (
            quote.ask_price * (Decimal("1") + delay)
            if side is OrderSide.BUY
            else quote.bid_price * (Decimal("1") - delay)
        )
        notional = quantity * price
        commission = _commission(notional, config)
        crossing_and_delay_cost = quantity * abs(price - quote.midpoint)
        total_cost += commission + crossing_and_delay_cost
        if side is OrderSide.BUY:
            cash -= notional + commission
            positions[symbol] = positions.get(symbol, Decimal("0")) + quantity
        else:
            if quantity > positions.get(symbol, Decimal("0")):
                raise ForwardRiskError(f"SELL would short {symbol}")
            cash += notional - commission
            positions[symbol] = positions.get(symbol, Decimal("0")) - quantity
    return cash, positions, total_cost


def _forced_reduction_budget(
    *,
    state: ArmState,
    quotes: dict[str, DecisionQuote],
    current_nav: Decimal,
    config: ForwardRiskConfig,
    include_core: bool,
) -> Decimal:
    """Return the sell-only notional needed for the typed 12% drawdown response."""
    targets = build_forced_reduction_targets(
        state=state,
        quotes=quotes,
        current_nav=current_nav,
        config=config,
        include_core=include_core,
    )
    return sum(
        (
            max(
                Decimal("0"),
                state.positions.get(symbol, Decimal("0")) - target_quantity,
            )
            * quotes[symbol].midpoint
            for symbol, target_quantity in targets.items()
        ),
        Decimal("0"),
    )


def build_forced_reduction_targets(
    *,
    state: ArmState,
    quotes: dict[str, DecisionQuote],
    current_nav: Decimal,
    config: ForwardRiskConfig,
    include_core: bool = True,
) -> dict[str, Decimal]:
    """Freeze typed quantity targets for one hard-loss episode."""
    if current_nav <= 0:
        raise ForwardRiskError("Current NAV must be positive")
    targets: dict[str, Decimal] = {}
    for symbol, quantity in state.positions.items():
        if quantity > 0 and symbol in config.leveraged_symbols:
            targets[symbol] = Decimal("0")

    semiconductor_positions = {
        symbol: quantity
        for symbol, quantity in state.positions.items()
        if (
            quantity > 0
            and symbol in config.semiconductor_symbols
            and symbol not in config.leveraged_symbols
        )
    }
    semiconductor_value = sum(
        (
            quantity * quotes[symbol].midpoint
            for symbol, quantity in semiconductor_positions.items()
        ),
        Decimal("0"),
    )
    semiconductor_cap = current_nav * config.max_semiconductor_cluster_weight
    if semiconductor_value > semiconductor_cap:
        retained_fraction = semiconductor_cap / semiconductor_value
        for symbol, quantity in semiconductor_positions.items():
            targets[symbol] = _floor_quantity(
                quantity * retained_fraction,
                config.quantity_precision,
            )

    core_quantity = state.positions.get(CORE_SYMBOL, Decimal("0"))
    if include_core and core_quantity > 0:
        targets[CORE_SYMBOL] = _floor_quantity(
            core_quantity * (Decimal("1") - config.force_reduce_core_fraction),
            config.quantity_precision,
        )
    return targets


def resolve_forced_reduction_targets(
    *,
    guard: ForwardLossGuard,
    latched_targets: dict[str, Decimal] | None,
    state: ArmState,
    quotes: dict[str, DecisionQuote],
    config: ForwardRiskConfig,
    loss_controls_applied: bool,
) -> dict[str, Decimal] | None:
    """Reuse an active hard-loss latch or create one only at a force threshold."""
    if (
        not loss_controls_applied
        or guard.state not in {"HARD_STOP", "FORCE_REDUCE"}
    ):
        return None
    if latched_targets is not None:
        return dict(latched_targets)
    if (
        guard.forced_sell_budget_usd is None
        or guard.forced_sell_budget_usd <= 0
    ):
        return None
    return build_forced_reduction_targets(
        state=state,
        quotes=quotes,
        current_nav=guard.current_nav_usd,
        config=config,
    )


def _risk_rejections(
    *,
    arm_id: str,
    current_values: dict[str, Decimal],
    current_nav: Decimal,
    projected_positions: dict[str, Decimal],
    projected_nav: Decimal,
    unavoidable_cost_usd: Decimal,
    allow_cost_epsilon: bool,
    quotes: dict[str, DecisionQuote],
    config: ForwardRiskConfig,
) -> list[str]:
    reasons: list[str] = []
    cost_weight_epsilon = (
        unavoidable_cost_usd / current_nav
        if current_nav > 0 and allow_cost_epsilon
        else Decimal("0")
    )
    if any(quantity < 0 for quantity in projected_positions.values()):
        reasons.append("SHORT_POSITION_PROHIBITED")
    projected_values = {
        symbol: quantity * quotes[symbol].midpoint
        for symbol, quantity in projected_positions.items()
        if quantity != 0
    }
    current_weights = {
        symbol: value / current_nav for symbol, value in current_values.items()
    }
    projected_weights = {
        symbol: value / projected_nav for symbol, value in projected_values.items()
    }
    for symbol, post in projected_weights.items():
        if symbol == CORE_SYMBOL and arm_id in config.core_cap_exempt_arms:
            continue
        pre = current_weights.get(symbol, Decimal("0"))
        if not _grandfather_allows(
            pre,
            post,
            config.max_single_symbol_weight,
            extra_epsilon=cost_weight_epsilon,
        ):
            reasons.append(f"SINGLE_SYMBOL_LIMIT:{symbol}")

    pre_semi = sum(
        (
            current_weights.get(symbol, Decimal("0"))
            for symbol in config.semiconductor_symbols
        ),
        Decimal("0"),
    )
    post_semi = sum(
        (
            projected_weights.get(symbol, Decimal("0"))
            for symbol in config.semiconductor_symbols
        ),
        Decimal("0"),
    )
    if not _grandfather_allows(
        pre_semi,
        post_semi,
        config.max_semiconductor_cluster_weight,
        extra_epsilon=cost_weight_epsilon,
    ):
        reasons.append("SEMICONDUCTOR_CLUSTER_LIMIT")

    for symbol in config.leveraged_symbols:
        pre = current_weights.get(symbol, Decimal("0"))
        post = projected_weights.get(symbol, Decimal("0"))
        if not _grandfather_allows(
            pre,
            post,
            config.max_leveraged_etf_weight,
            extra_epsilon=cost_weight_epsilon,
        ):
            reasons.append(f"LEVERAGED_SYMBOL_LIMIT:{symbol}")
    pre_leverage = sum(
        (
            current_weights.get(symbol, Decimal("0"))
            for symbol in config.leveraged_symbols
        ),
        Decimal("0"),
    )
    post_leverage = sum(
        (
            projected_weights.get(symbol, Decimal("0"))
            for symbol in config.leveraged_symbols
        ),
        Decimal("0"),
    )
    if not _grandfather_allows(
        pre_leverage,
        post_leverage,
        config.max_combined_leveraged_etf_weight,
        extra_epsilon=cost_weight_epsilon,
    ):
        reasons.append("COMBINED_LEVERAGED_LIMIT")
    gross = sum(projected_weights.values(), Decimal("0"))
    if gross > config.max_gross_exposure + WEIGHT_EPSILON:
        reasons.append("MAX_GROSS_EXPOSURE")
    return reasons


def _grandfather_allows(
    pre: Decimal,
    post: Decimal,
    cap: Decimal,
    *,
    extra_epsilon: Decimal = Decimal("0"),
) -> bool:
    allowed = cap if pre <= cap else pre
    return post <= allowed + WEIGHT_EPSILON + extra_epsilon


def _order_intents(
    *,
    run_id: str,
    cycle_id: str,
    arm_id: str,
    state_sequence: int,
    portfolio: PortfolioDecision,
    risk: RiskDecision,
    orders: tuple[dict[str, Any], ...],
    created_at: datetime,
    valid_until: datetime,
) -> tuple[OrderIntent, ...]:
    intents: list[OrderIntent] = []
    for order in orders:
        symbol = str(order["symbol"])
        side = order["side"]
        order_id = stable_id(
            "forward-order",
            run_id,
            cycle_id,
            arm_id,
            state_sequence,
            symbol,
            side.value,
        )
        intents.append(
            OrderIntent(
                order_intent_id=order_id,
                arm_id=arm_id,
                portfolio_decision_id=portfolio.portfolio_decision_id,
                risk_decision_id=risk.risk_decision_id,
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=Decimal(str(order["quantity"])),
                limit_price=None,
                time_in_force="DAY",
                session="REGULAR",
                client_order_id=stable_id("paper-client", order_id),
                idempotency_key=stable_id("paper-order-idem", order_id),
                created_at=created_at,
            )
        )
    return tuple(intents)


def _one_way_turnover(
    *,
    current_values: dict[str, Decimal],
    desired_values: dict[str, Decimal],
    nav: Decimal,
) -> Decimal:
    symbols = set(current_values) | set(desired_values)
    buys = sum(
        (
            max(
                Decimal("0"),
                desired_values.get(symbol, Decimal("0"))
                - current_values.get(symbol, Decimal("0")),
            )
            for symbol in symbols
        ),
        Decimal("0"),
    )
    sells = sum(
        (
            max(
                Decimal("0"),
                current_values.get(symbol, Decimal("0"))
                - desired_values.get(symbol, Decimal("0")),
            )
            for symbol in symbols
        ),
        Decimal("0"),
    )
    return max(buys, sells) / nav


def _commission(notional: Decimal, config: ForwardRiskConfig) -> Decimal:
    if notional <= config.commission_waiver_threshold_usd:
        return Decimal("0")
    return (notional * config.commission_rate).quantize(
        CENT,
        rounding=ROUND_HALF_EVEN,
    )


def _floor_quantity(value: Decimal, precision: Decimal) -> Decimal:
    return value.quantize(precision, rounding=ROUND_DOWN)


def _qqq_entry_is_blocked(blocked_targets: frozenset[str]) -> bool:
    return bool(
        blocked_targets
        & {
            "SYMBOL:QQQ",
            "FACTOR:US_EQUITY_BETA",
            "FACTOR:US_TECH_BETA",
        }
    )


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(weights.values())
    if total <= 0:
        raise ForwardRiskError("Approved weights have no positive mass")
    normalized = {symbol: value / total for symbol, value in weights.items()}
    cash = normalized.get(CASH_SYMBOL, 0.0)
    residual = 1.0 - sum(normalized.values())
    normalized[CASH_SYMBOL] = cash + residual
    return normalized
