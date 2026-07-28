from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.synthetic import SyntheticBar, SyntheticScenario
from trading.domain.contracts import Fill, NavSnapshot, OrderIntent
from trading.domain.enums import OrderSide
from trading.experiments.arms import ARM_IDS, ArmState
from trading.persistence.models import FillRow, NavSnapshotRow, OrderIntentRow, RunRow
from trading.runtime.pipeline import load_arm_states, load_scenario_for_run

MONEY_QUANTUM = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.0001")
PERCENT_QUANTUM = Decimal("0.0001")

ASSET_NAMES = {
    "QQQ": "Invesco QQQ",
    "SOXX": "iShares Semiconductor",
    "SOXL": "Direxion Semiconductor Bull 3X",
    "SOXS": "Direxion Semiconductor Bear 3X",
    "GLD": "SPDR Gold Shares",
}


class DashboardError(RuntimeError):
    """Base error for a missing dashboard projection."""


class DashboardRunNotFound(DashboardError):
    pass


class DashboardArmNotFound(DashboardError):
    pass


class DashboardSymbolNotFound(DashboardError):
    pass


class MarketDashboardService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def snapshot(
        self,
        *,
        run_id: str | None = None,
        arm_id: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        with self._session_factory() as session:
            run = _resolve_run(session, run_id)
            scenario = load_scenario_for_run(session, run.run_id)
            states = load_arm_states(session, run.run_id)
            if not states:
                raise DashboardRunNotFound(f"Run {run.run_id} has no materialized arm state")

            arm_ids = _ordered_arm_ids(states)
            selected_arm_id = arm_id or ("B3-RISK" if "B3-RISK" in states else arm_ids[0])
            state = states.get(selected_arm_id)
            if state is None:
                raise DashboardArmNotFound(f"Unknown arm_id {selected_arm_id} for run {run.run_id}")

            symbols = list(dict.fromkeys(bar.symbol for bar in scenario.bars))
            selected_symbol = symbol or ("QQQ" if "QQQ" in symbols else symbols[0])
            if selected_symbol not in symbols:
                raise DashboardSymbolNotFound(
                    f"Unknown symbol {selected_symbol} for run {run.run_id}"
                )

            navs = _load_latest_navs(session, run.run_id)
            if selected_arm_id not in navs:
                raise DashboardArmNotFound(f"Arm {selected_arm_id} has no NAV for run {run.run_id}")
            intents = _load_intents(session, run.run_id, selected_arm_id)
            fills = _load_fills(session, run.run_id, selected_arm_id)

        return _build_snapshot(
            run=run,
            scenario=scenario,
            states=states,
            navs=navs,
            intents=intents,
            fills=fills,
            selected_arm_id=selected_arm_id,
            selected_symbol=selected_symbol,
            arm_ids=arm_ids,
            symbols=symbols,
        )


def _resolve_run(session: Session, run_id: str | None) -> RunRow:
    if run_id is not None:
        run = session.get(RunRow, run_id)
    else:
        run = session.scalar(
            select(RunRow)
            .where(RunRow.status == "COMPLETED")
            .order_by(RunRow.started_at.desc())
            .limit(1)
        )
    if run is None:
        detail = "No completed run is available" if run_id is None else f"Unknown run_id {run_id}"
        raise DashboardRunNotFound(detail)
    return run


def _ordered_arm_ids(states: dict[str, ArmState]) -> list[str]:
    known = [arm_id for arm_id in ARM_IDS if arm_id in states]
    extras = sorted(set(states) - set(known))
    return [*known, *extras]


def _load_latest_navs(session: Session, run_id: str) -> dict[str, NavSnapshot]:
    rows = list(
        session.scalars(
            select(NavSnapshotRow)
            .where(NavSnapshotRow.run_id == run_id)
            .order_by(NavSnapshotRow.arm_id, NavSnapshotRow.as_of.desc())
        )
    )
    navs: dict[str, NavSnapshot] = {}
    for row in rows:
        if row.arm_id not in navs:
            navs[row.arm_id] = NavSnapshot.model_validate(row.payload_json)
    return navs


def _load_intents(session: Session, run_id: str, arm_id: str) -> list[OrderIntent]:
    rows = list(
        session.scalars(
            select(OrderIntentRow).where(
                OrderIntentRow.run_id == run_id,
                OrderIntentRow.arm_id == arm_id,
            )
        )
    )
    intents = [OrderIntent.model_validate(row.payload_json) for row in rows]
    return sorted(intents, key=lambda item: (item.created_at, item.order_intent_id))


def _load_fills(session: Session, run_id: str, arm_id: str) -> list[Fill]:
    rows = list(
        session.scalars(
            select(FillRow)
            .where(
                FillRow.run_id == run_id,
                FillRow.arm_id == arm_id,
            )
            .order_by(FillRow.effective_at, FillRow.fill_id)
        )
    )
    return [Fill.model_validate(row.payload_json) for row in rows]


def _build_snapshot(
    *,
    run: RunRow,
    scenario: SyntheticScenario,
    states: dict[str, ArmState],
    navs: dict[str, NavSnapshot],
    intents: list[OrderIntent],
    fills: list[Fill],
    selected_arm_id: str,
    selected_symbol: str,
    arm_ids: list[str],
    symbols: list[str],
) -> dict[str, Any]:
    final_prices = _final_prices(scenario.bars)
    nav = navs[selected_arm_id]
    state = states[selected_arm_id]
    candles = [
        _candle_payload(bar)
        for bar in sorted(
            (bar for bar in scenario.bars if bar.symbol == selected_symbol),
            key=lambda item: item.event_time,
        )
    ]
    quote = _quote_payload(selected_symbol, candles)
    positions = _position_payloads(state, fills, final_prices, nav.nav_usd)
    orders = _order_payloads(intents, fills)
    fill_payloads = [_fill_payload(fill) for fill in reversed(fills[-20:])]
    computed_market_value = sum(
        (
            quantity * final_prices[symbol]
            for symbol, quantity in state.positions.items()
            if symbol in final_prices
        ),
        Decimal("0"),
    ).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    computed_cash = state.cash_usd.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)
    computed_nav = computed_cash + computed_market_value
    total_pnl = nav.nav_usd - state.initial_cash_usd
    total_return = _ratio_percent(total_pnl, state.initial_cash_usd)
    latest_available_at = max(bar.available_at for bar in scenario.bars)

    return {
        "source": {
            "mode": "SYNTHETIC",
            "provider": "synthetic",
            "run_id": run.run_id,
            "run_status": run.status,
            "as_of": latest_available_at.isoformat(),
            "decision_time": scenario.decision_time.isoformat(),
            "freshness": "FIXTURE_NOT_LIVE",
            "candle_quality": "OPEN_CLOSE_SOURCE_HIGH_LOW_DERIVED",
            "candle_note": (
                "합성 원본에는 시가·종가만 있어 차트의 고가·저가는 결정론적으로 파생했습니다."
            ),
        },
        "filters": {
            "arms": arm_ids,
            "symbols": symbols,
            "selected_arm": selected_arm_id,
            "selected_symbol": selected_symbol,
        },
        "market": {
            "selected_symbol": selected_symbol,
            "display_name": ASSET_NAMES.get(selected_symbol, selected_symbol),
            "quote": quote,
            "candles": candles,
        },
        "portfolio": {
            "arm_id": selected_arm_id,
            "initial_cash_usd": _decimal_text(state.initial_cash_usd),
            "cash_usd": _decimal_text(state.cash_usd),
            "positions_market_value_usd": _decimal_text(nav.positions_market_value_usd),
            "nav_usd": _decimal_text(nav.nav_usd),
            "total_pnl_usd": _decimal_text(total_pnl),
            "total_return_pct": _decimal_text(total_return, PERCENT_QUANTUM),
            "position_count": len(positions),
            "sequence": state.sequence,
            "as_of": nav.as_of.isoformat(),
        },
        "positions": positions,
        "orders": list(reversed(orders[-20:])),
        "fills": fill_payloads,
        "arm_comparison": [
            _arm_summary(arm_id, states[arm_id], navs.get(arm_id)) for arm_id in arm_ids
        ],
        "paper_execution": {
            "broker": "PaperBroker",
            "execution_scenario_id": scenario.execution_scenario_id,
            "fill_reference": "NEXT_BAR_OPEN",
            "commission_rate": _decimal_text(scenario.commission_rate, Decimal("0.000001")),
            "commission_percent": _decimal_text(
                scenario.commission_rate * Decimal("100"), Decimal("0.0001")
            ),
            "commission_waiver_threshold_usd": _decimal_text(
                scenario.commission_waiver_threshold_usd
            ),
            "half_spread_bps": _decimal_text(scenario.half_spread_bps, Decimal("0.01")),
            "delay_penalty_bps": _decimal_text(scenario.delay_penalty_bps, Decimal("0.01")),
            "partial_fill_supported": True,
            "real_order_routing": False,
        },
        "reconciliation": {
            "status": "MATCHED" if computed_nav == nav.nav_usd else "MISMATCH",
            "stored_nav_usd": _decimal_text(nav.nav_usd),
            "computed_nav_usd": _decimal_text(computed_nav),
            "difference_usd": _decimal_text(nav.nav_usd - computed_nav),
        },
    }


def _final_prices(bars: list[SyntheticBar]) -> dict[str, Decimal]:
    latest: dict[str, SyntheticBar] = {}
    for bar in bars:
        current = latest.get(bar.symbol)
        if current is None or bar.available_at > current.available_at:
            latest[bar.symbol] = bar
    return {symbol: bar.close for symbol, bar in latest.items()}


def _candle_payload(bar: SyntheticBar) -> dict[str, Any]:
    body_high = max(bar.open, bar.close)
    body_low = min(bar.open, bar.close)
    body_range = body_high - body_low
    wick = max(body_range * Decimal("0.35"), body_high * Decimal("0.0015"))
    high = (body_high + wick).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    low = max(
        MONEY_QUANTUM,
        (body_low - wick).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP),
    )
    return {
        "time": bar.event_time.isoformat(),
        "available_at": bar.available_at.isoformat(),
        "open": _decimal_text(bar.open, PRICE_QUANTUM),
        "high": _decimal_text(high, PRICE_QUANTUM),
        "low": _decimal_text(low, PRICE_QUANTUM),
        "close": _decimal_text(bar.close, PRICE_QUANTUM),
        "high_low_derived": True,
    }


def _quote_payload(symbol: str, candles: list[dict[str, Any]]) -> dict[str, Any]:
    latest = candles[-1]
    previous = candles[-2] if len(candles) > 1 else candles[-1]
    latest_close = Decimal(str(latest["close"]))
    previous_close = Decimal(str(previous["close"]))
    first_close = Decimal(str(candles[0]["close"]))
    change = latest_close - previous_close
    period_change = latest_close - first_close
    return {
        "symbol": symbol,
        "price": _decimal_text(latest_close, PRICE_QUANTUM),
        "change": _decimal_text(change, PRICE_QUANTUM),
        "change_pct": _decimal_text(_ratio_percent(change, previous_close), PERCENT_QUANTUM),
        "period_change": _decimal_text(period_change, PRICE_QUANTUM),
        "period_change_pct": _decimal_text(
            _ratio_percent(period_change, first_close), PERCENT_QUANTUM
        ),
        "period_high": _decimal_text(
            max(Decimal(str(candle["high"])) for candle in candles),
            PRICE_QUANTUM,
        ),
        "period_low": _decimal_text(
            min(Decimal(str(candle["low"])) for candle in candles),
            PRICE_QUANTUM,
        ),
        "as_of": latest["available_at"],
    }


def _position_payloads(
    state: ArmState,
    fills: list[Fill],
    final_prices: dict[str, Decimal],
    nav_usd: Decimal,
) -> list[dict[str, Any]]:
    inventory = _inventory_costs(fills)
    positions: list[dict[str, Any]] = []
    for symbol, quantity in sorted(state.positions.items()):
        if quantity == 0:
            continue
        current_price = final_prices[symbol]
        market_value = quantity * current_price
        remaining_cost, average_cost, realized_pnl = inventory.get(
            symbol,
            (Decimal("0"), None, Decimal("0")),
        )
        unrealized = market_value - remaining_cost
        positions.append(
            {
                "symbol": symbol,
                "display_name": ASSET_NAMES.get(symbol, symbol),
                "quantity": _decimal_text(quantity, Decimal("0.001")),
                "average_cost": (
                    None if average_cost is None else _decimal_text(average_cost, PRICE_QUANTUM)
                ),
                "current_price": _decimal_text(current_price, PRICE_QUANTUM),
                "market_value_usd": _decimal_text(market_value),
                "cost_basis_usd": _decimal_text(remaining_cost),
                "unrealized_pnl_usd": _decimal_text(unrealized),
                "unrealized_return_pct": (
                    None
                    if remaining_cost == 0
                    else _decimal_text(
                        _ratio_percent(unrealized, remaining_cost),
                        PERCENT_QUANTUM,
                    )
                ),
                "realized_pnl_usd": _decimal_text(realized_pnl),
                "weight_pct": _decimal_text(
                    _ratio_percent(market_value, nav_usd),
                    PERCENT_QUANTUM,
                ),
            }
        )
    return sorted(
        positions,
        key=lambda item: Decimal(str(item["market_value_usd"])),
        reverse=True,
    )


def _inventory_costs(
    fills: list[Fill],
) -> dict[str, tuple[Decimal, Decimal | None, Decimal]]:
    quantity_by_symbol: defaultdict[str, Decimal] = defaultdict(Decimal)
    cost_by_symbol: defaultdict[str, Decimal] = defaultdict(Decimal)
    realized_by_symbol: defaultdict[str, Decimal] = defaultdict(Decimal)
    for fill in sorted(fills, key=lambda item: (item.effective_at, item.fill_id)):
        quantity = quantity_by_symbol[fill.symbol]
        cost = cost_by_symbol[fill.symbol]
        if fill.side is OrderSide.BUY:
            quantity_by_symbol[fill.symbol] = quantity + fill.quantity
            cost_by_symbol[fill.symbol] = cost + fill.quantity * fill.price + fill.commission_usd
            continue
        if quantity <= 0 or fill.quantity > quantity:
            raise DashboardError(f"Unsupported short inventory while projecting {fill.symbol}")
        average_cost = cost / quantity
        released_cost = average_cost * fill.quantity
        quantity_by_symbol[fill.symbol] = quantity - fill.quantity
        cost_by_symbol[fill.symbol] = cost - released_cost
        realized_by_symbol[fill.symbol] += (
            fill.quantity * fill.price - fill.commission_usd - released_cost
        )

    symbols = set(quantity_by_symbol) | set(realized_by_symbol)
    return {
        symbol: (
            cost_by_symbol[symbol],
            (
                None
                if quantity_by_symbol[symbol] == 0
                else cost_by_symbol[symbol] / quantity_by_symbol[symbol]
            ),
            realized_by_symbol[symbol],
        )
        for symbol in symbols
    }


def _order_payloads(
    intents: list[OrderIntent],
    fills: list[Fill],
) -> list[dict[str, Any]]:
    fills_by_order: defaultdict[str, list[Fill]] = defaultdict(list)
    for fill in fills:
        fills_by_order[fill.order_intent_id].append(fill)

    payloads: list[dict[str, Any]] = []
    for intent in intents:
        order_fills = fills_by_order[intent.order_intent_id]
        filled_quantity = sum(
            (fill.quantity for fill in order_fills),
            Decimal("0"),
        )
        fill_notional = sum(
            (fill.quantity * fill.price for fill in order_fills),
            Decimal("0"),
        )
        if filled_quantity >= intent.quantity:
            status = "FILLED"
        elif filled_quantity > 0:
            status = "PARTIAL"
        else:
            status = "OPEN"
        payloads.append(
            {
                "order_intent_id": intent.order_intent_id,
                "symbol": intent.symbol,
                "side": intent.side.value,
                "order_type": intent.order_type,
                "requested_quantity": _decimal_text(intent.quantity, Decimal("0.001")),
                "filled_quantity": _decimal_text(filled_quantity, Decimal("0.001")),
                "average_fill_price": (
                    None
                    if filled_quantity == 0
                    else _decimal_text(
                        fill_notional / filled_quantity,
                        PRICE_QUANTUM,
                    )
                ),
                "status": status,
                "created_at": intent.created_at.isoformat(),
                "idempotency_key": intent.idempotency_key,
            }
        )
    return payloads


def _fill_payload(fill: Fill) -> dict[str, Any]:
    notional = fill.quantity * fill.price
    return {
        "fill_id": fill.fill_id,
        "order_intent_id": fill.order_intent_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": _decimal_text(fill.quantity, Decimal("0.001")),
        "price": _decimal_text(fill.price, PRICE_QUANTUM),
        "notional_usd": _decimal_text(notional),
        "commission_usd": _decimal_text(fill.commission_usd),
        "effective_at": fill.effective_at.isoformat(),
        "execution_scenario_id": fill.execution_scenario_id,
    }


def _arm_summary(
    arm_id: str,
    state: ArmState,
    nav: NavSnapshot | None,
) -> dict[str, Any]:
    if nav is None:
        return {
            "arm_id": arm_id,
            "nav_usd": None,
            "total_pnl_usd": None,
            "total_return_pct": None,
            "position_count": len(
                [quantity for quantity in state.positions.values() if quantity != 0]
            ),
        }
    pnl = nav.nav_usd - state.initial_cash_usd
    return {
        "arm_id": arm_id,
        "nav_usd": _decimal_text(nav.nav_usd),
        "total_pnl_usd": _decimal_text(pnl),
        "total_return_pct": _decimal_text(
            _ratio_percent(pnl, state.initial_cash_usd),
            PERCENT_QUANTUM,
        ),
        "position_count": len([quantity for quantity in state.positions.values() if quantity != 0]),
    }


def _ratio_percent(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        return Decimal("0")
    return numerator / denominator * Decimal("100")


def _decimal_text(
    value: Decimal,
    quantum: Decimal = MONEY_QUANTUM,
) -> str:
    return format(value.quantize(quantum, rounding=ROUND_HALF_UP), "f")
