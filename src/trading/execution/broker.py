from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from trading.domain.contracts import OrderIntent


class BrokerOrder(Protocol):
    @property
    def broker_order_id(self) -> str: ...


class Broker(Protocol):
    async def submit_order(self, intent: OrderIntent) -> BrokerOrder: ...

    async def cancel_order(self, broker_order_id: str) -> None: ...

    async def list_orders(self, arm_or_account_id: str) -> Sequence[BrokerOrder]: ...


class ProductionBrokerDisabled(RuntimeError):
    pass


class DisabledProductionBroker:
    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        del intent
        raise ProductionBrokerDisabled("Production broker is disabled before Phase 5 unlock")

    async def cancel_order(self, broker_order_id: str) -> None:
        del broker_order_id
        raise ProductionBrokerDisabled("Production broker is disabled before Phase 5 unlock")

    async def list_orders(self, arm_or_account_id: str) -> Sequence[BrokerOrder]:
        del arm_or_account_id
        raise ProductionBrokerDisabled("Production broker is disabled before Phase 5 unlock")

