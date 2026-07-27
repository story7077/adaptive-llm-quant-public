from __future__ import annotations

from dataclasses import dataclass

FACTORIAL_EXPERIMENT_VERSION = "ai_guard_factorial_v1"
FACTORIAL_ARM_IDS = (
    "B0-VOL",
    "B3-GUARD",
    "B3-AI",
    "B3-AI-GUARD",
)


@dataclass(frozen=True, slots=True)
class FactorialArmContract:
    arm_id: str
    deterministic_loss_guard: bool
    operational_risk_commander: bool
    independent_cash_positions_orders_ledger: bool = True
    real_order_routing: bool = False


def factorial_arm_contracts() -> tuple[FactorialArmContract, ...]:
    return (
        FactorialArmContract(
            arm_id="B0-VOL",
            deterministic_loss_guard=False,
            operational_risk_commander=False,
        ),
        FactorialArmContract(
            arm_id="B3-GUARD",
            deterministic_loss_guard=True,
            operational_risk_commander=False,
        ),
        FactorialArmContract(
            arm_id="B3-AI",
            deterministic_loss_guard=False,
            operational_risk_commander=True,
        ),
        FactorialArmContract(
            arm_id="B3-AI-GUARD",
            deterministic_loss_guard=True,
            operational_risk_commander=True,
        ),
    )
