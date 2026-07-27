from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from trading.domain.contracts import Fill
from trading.domain.enums import OrderSide

ARM_IDS: tuple[str, ...] = (
    "B0-CASH",
    "B0-QQQ",
    "B0-VOL",
    "B1",
    "B2",
    "B3-RISK",
    "B3-FULL",
)


@dataclass(frozen=True, slots=True)
class ArmState:
    arm_id: str
    initial_cash_usd: Decimal
    cash_usd: Decimal
    positions: dict[str, Decimal]
    sequence: int

    def apply_fill(self, fill: Fill) -> ArmState:
        if fill.arm_id != self.arm_id:
            raise ValueError(f"Fill for {fill.arm_id} cannot mutate arm {self.arm_id}")
        positions = dict(self.positions)
        signed_quantity = fill.quantity if fill.side is OrderSide.BUY else -fill.quantity
        positions[fill.symbol] = positions.get(fill.symbol, Decimal("0")) + signed_quantity
        if positions[fill.symbol] < 0:
            raise ValueError(f"Fill would create a short position in {fill.symbol}")
        notional = fill.quantity * fill.price
        if fill.side is OrderSide.BUY:
            cash = self.cash_usd - notional - fill.commission_usd
        else:
            cash = self.cash_usd + notional - fill.commission_usd
        if cash < 0:
            raise ValueError("Fill would create negative USD cash")
        return ArmState(
            arm_id=self.arm_id,
            initial_cash_usd=self.initial_cash_usd,
            cash_usd=cash,
            positions=positions,
            sequence=self.sequence + 1,
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "arm_id": self.arm_id,
            "initial_cash_usd": str(self.initial_cash_usd),
            "cash_usd": str(self.cash_usd),
            "positions": {
                symbol: str(quantity)
                for symbol, quantity in sorted(self.positions.items())
            },
            "sequence": self.sequence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> ArmState:
        positions_payload = payload.get("positions")
        if not isinstance(positions_payload, dict):
            raise ValueError("Arm state positions payload must be an object")
        positions_mapping = cast(dict[object, object], positions_payload)
        return cls(
            arm_id=str(payload["arm_id"]),
            initial_cash_usd=Decimal(str(payload["initial_cash_usd"])),
            cash_usd=Decimal(str(payload["cash_usd"])),
            positions={
                str(symbol): Decimal(str(quantity))
                for symbol, quantity in positions_mapping.items()
            },
            sequence=int(str(payload["sequence"])),
        )


def create_arm_states(initial_cash_usd: Decimal) -> dict[str, ArmState]:
    return {
        arm_id: ArmState(
            arm_id=arm_id,
            initial_cash_usd=initial_cash_usd,
            cash_usd=initial_cash_usd,
            positions={},
            sequence=0,
        )
        for arm_id in ARM_IDS
    }


def rebuild_arm_state(
    arm_id: str, initial_cash_usd: Decimal, fills: list[Fill]
) -> ArmState:
    state = ArmState(
        arm_id=arm_id,
        initial_cash_usd=initial_cash_usd,
        cash_usd=initial_cash_usd,
        positions={},
        sequence=0,
    )
    for fill in sorted(fills, key=lambda item: (item.effective_at, item.fill_id)):
        state = state.apply_fill(fill)
    return state


def states_are_independent(states: dict[str, ArmState]) -> bool:
    position_object_ids = {id(state.positions) for state in states.values()}
    return len(position_object_ids) == len(states)
