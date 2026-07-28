from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast

from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.domain.contracts import Fill
from trading.domain.enums import OrderSide


@dataclass(frozen=True, slots=True)
class UnsettledReceivable:
    receivable_id: str
    source_fill_id: str
    amount_usd: Decimal
    settlement_date: date
    created_at: datetime

    def __post_init__(self) -> None:
        if self.amount_usd <= 0:
            raise ValueError("Unsettled receivable amount must be positive")
        if self.created_at.tzinfo is None:
            raise ValueError("Unsettled receivable created_at must be timezone-aware")

    def as_payload(self) -> dict[str, object]:
        return {
            "receivable_id": self.receivable_id,
            "source_fill_id": self.source_fill_id,
            "amount_usd": format(self.amount_usd, "f"),
            "settlement_date": self.settlement_date.isoformat(),
            "created_at": _aware(self.created_at).isoformat(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> UnsettledReceivable:
        return cls(
            receivable_id=str(payload["receivable_id"]),
            source_fill_id=str(payload["source_fill_id"]),
            amount_usd=Decimal(str(payload["amount_usd"])),
            settlement_date=date.fromisoformat(str(payload["settlement_date"])),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
        )


@dataclass(frozen=True, slots=True)
class Q1ArmState:
    arm_id: str
    initial_nav_usd: Decimal
    settled_cash_usd: Decimal
    unsettled_receivables: tuple[UnsettledReceivable, ...]
    positions: dict[str, Decimal]
    sequence: int
    evaluation_anchor_id: str | None
    algorithm_version: str = Q1_ALGORITHM_VERSION

    def __post_init__(self) -> None:
        if self.algorithm_version != Q1_ALGORITHM_VERSION:
            raise ValueError("Q1 arm state algorithm version mismatch")
        if self.initial_nav_usd < 0 or self.settled_cash_usd < 0:
            raise ValueError("Q1 arm cash balances cannot be negative")
        if self.sequence < 0:
            raise ValueError("Q1 arm state sequence cannot be negative")
        if any(quantity < 0 for quantity in self.positions.values()):
            raise ValueError("Q1 arm state cannot contain short positions")
        receivable_ids = [item.receivable_id for item in self.unsettled_receivables]
        if len(receivable_ids) != len(set(receivable_ids)):
            raise ValueError("Q1 arm state has duplicate receivable IDs")

    @property
    def unsettled_cash_usd(self) -> Decimal:
        return sum(
            (item.amount_usd for item in self.unsettled_receivables),
            Decimal("0"),
        )

    @property
    def total_cash_usd(self) -> Decimal:
        return self.settled_cash_usd + self.unsettled_cash_usd

    def nav(self, prices: dict[str, Decimal]) -> Decimal:
        missing = sorted(
            symbol
            for symbol, quantity in self.positions.items()
            if quantity > 0 and symbol not in prices
        )
        if missing:
            raise ValueError(f"Missing NAV prices for: {missing}")
        market_value = sum(
            (
                quantity * prices[symbol]
                for symbol, quantity in self.positions.items()
                if quantity > 0
            ),
            Decimal("0"),
        )
        return self.total_cash_usd + market_value

    def weights(self, prices: dict[str, Decimal]) -> dict[str, Decimal]:
        nav = self.nav(prices)
        if nav <= 0:
            raise ValueError("Q1 arm NAV must be positive to compute weights")
        weights = {
            symbol: quantity * prices[symbol] / nav
            for symbol, quantity in sorted(self.positions.items())
            if quantity > 0
        }
        weights["USD_CASH"] = self.total_cash_usd / nav
        return weights

    def apply_fill(
        self,
        fill: Fill,
        *,
        sell_receivable: UnsettledReceivable | None = None,
    ) -> Q1ArmState:
        if fill.arm_id != self.arm_id:
            raise ValueError(f"Fill for {fill.arm_id} cannot mutate arm {self.arm_id}")
        positions = dict(self.positions)
        current = positions.get(fill.symbol, Decimal("0"))
        notional = fill.quantity * fill.price
        if fill.side is OrderSide.BUY:
            if sell_receivable is not None:
                raise ValueError("BUY fill cannot create a settlement receivable")
            required_cash = notional + fill.commission_usd
            if required_cash > self.settled_cash_usd:
                raise ValueError("BUY fill exceeds settled cash")
            settled_cash = self.settled_cash_usd - required_cash
            positions[fill.symbol] = current + fill.quantity
            receivables = self.unsettled_receivables
        else:
            if fill.quantity > current:
                raise ValueError("SELL fill would create a short position")
            expected_receivable = notional - fill.commission_usd
            if expected_receivable <= 0:
                raise ValueError("SELL fill must create a positive net receivable")
            if sell_receivable is None:
                raise ValueError("SELL fill requires a typed settlement receivable")
            if (
                sell_receivable.source_fill_id != fill.fill_id
                or sell_receivable.amount_usd != expected_receivable
            ):
                raise ValueError("SELL receivable does not match fill economics")
            settled_cash = self.settled_cash_usd
            positions[fill.symbol] = current - fill.quantity
            receivables = (*self.unsettled_receivables, sell_receivable)
        if positions.get(fill.symbol) == 0:
            positions.pop(fill.symbol, None)
        return Q1ArmState(
            arm_id=self.arm_id,
            initial_nav_usd=self.initial_nav_usd,
            settled_cash_usd=settled_cash,
            unsettled_receivables=tuple(receivables),
            positions=positions,
            sequence=self.sequence + 1,
            evaluation_anchor_id=self.evaluation_anchor_id,
        )

    def settle(self, receivable_id: str) -> Q1ArmState:
        matched = [
            item
            for item in self.unsettled_receivables
            if item.receivable_id == receivable_id
        ]
        if not matched:
            return self
        receivable = matched[0]
        remaining = tuple(
            item
            for item in self.unsettled_receivables
            if item.receivable_id != receivable_id
        )
        return Q1ArmState(
            arm_id=self.arm_id,
            initial_nav_usd=self.initial_nav_usd,
            settled_cash_usd=self.settled_cash_usd + receivable.amount_usd,
            unsettled_receivables=remaining,
            positions=dict(self.positions),
            sequence=self.sequence + 1,
            evaluation_anchor_id=self.evaluation_anchor_id,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "schema_version": "q1_arm_state_v1",
            "algorithm_version": self.algorithm_version,
            "arm_id": self.arm_id,
            "initial_nav_usd": format(self.initial_nav_usd, "f"),
            "settled_cash_usd": format(self.settled_cash_usd, "f"),
            "unsettled_cash_usd": format(self.unsettled_cash_usd, "f"),
            "unsettled_receivables": [
                item.as_payload()
                for item in sorted(
                    self.unsettled_receivables,
                    key=lambda value: value.receivable_id,
                )
            ],
            "positions": {
                symbol: format(quantity, "f")
                for symbol, quantity in sorted(self.positions.items())
            },
            "sequence": self.sequence,
            "evaluation_anchor_id": self.evaluation_anchor_id,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Q1ArmState:
        if payload.get("schema_version") != "q1_arm_state_v1":
            raise ValueError("Not a q1_arm_state_v1 payload")
        raw_positions = payload.get("positions")
        raw_receivables = payload.get("unsettled_receivables")
        if not isinstance(raw_positions, dict) or not isinstance(
            raw_receivables,
            list,
        ):
            raise ValueError("Malformed Q1 arm state payload")
        positions = cast(dict[object, object], raw_positions)
        receivables = cast(list[object], raw_receivables)
        return cls(
            arm_id=str(payload["arm_id"]),
            initial_nav_usd=Decimal(str(payload["initial_nav_usd"])),
            settled_cash_usd=Decimal(str(payload["settled_cash_usd"])),
            unsettled_receivables=tuple(
                UnsettledReceivable.from_payload(cast(dict[str, object], item))
                for item in receivables
                if isinstance(item, dict)
            ),
            positions={
                str(symbol): Decimal(str(quantity))
                for symbol, quantity in positions.items()
            },
            sequence=int(str(payload["sequence"])),
            evaluation_anchor_id=(
                None
                if payload.get("evaluation_anchor_id") is None
                else str(payload["evaluation_anchor_id"])
            ),
            algorithm_version=str(payload["algorithm_version"]),
        )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
