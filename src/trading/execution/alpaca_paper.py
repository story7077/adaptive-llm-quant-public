from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from urllib.parse import urlsplit

import httpx

from trading.data.alpaca import AlpacaCredentials, JsonObject
from trading.domain.enums import OrderSide
from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALPACA_PAPER_ALLOWED_SYMBOLS = frozenset({"QQQ", "SOXX"})
ALPACA_PAPER_OPEN_STATUSES = frozenset(
    {
        "accepted",
        "pending_new",
        "new",
        "partially_filled",
        "pending_cancel",
        "pending_replace",
        "accepted_for_bidding",
        "stopped",
        "suspended",
        "calculated",
        "held",
    }
)
ALPACA_PAPER_TERMINAL_STATUSES = frozenset(
    {
        "filled",
        "canceled",
        "expired",
        "rejected",
        "replaced",
        "done_for_day",
    }
)


class AlpacaPaperTradingError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.status_code = status_code
        self.provider_request_id = provider_request_id
        self.retryable = retryable
        super().__init__(message)


class AlpacaPaperOrderConflict(AlpacaPaperTradingError):
    pass


@dataclass(frozen=True, slots=True)
class AlpacaPaperAccount:
    account_id: str = field(repr=False)
    status: str
    currency: str
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    account_blocked: bool
    trading_blocked: bool
    trade_suspended_by_user: bool
    provider_request_id: str | None

    @property
    def account_id_hash(self) -> str:
        return canonical_hash({"alpaca_paper_account_id": self.account_id})

    @property
    def is_trading_ready(self) -> bool:
        return (
            self.status.upper() in {"ACTIVE", "PAPER_ONLY"}
            and not self.account_blocked
            and not self.trading_blocked
            and not self.trade_suspended_by_user
            and self.currency == "USD"
            and self.equity > 0
        )


@dataclass(frozen=True, slots=True)
class AlpacaPaperPosition:
    symbol: str
    quantity: Decimal
    market_value: Decimal
    average_entry_price: Decimal
    side: str


@dataclass(frozen=True, slots=True)
class AlpacaPaperOrderRequest:
    symbol: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    client_order_id: str
    order_type: str = "limit"
    time_in_force: str = "day"
    extended_hours: bool = False

    def __post_init__(self) -> None:
        if self.symbol not in ALPACA_PAPER_ALLOWED_SYMBOLS:
            raise ValueError("Alpaca Paper canary supports only QQQ and SOXX")
        if self.quantity <= 0 or self.limit_price <= 0:
            raise ValueError("Alpaca Paper order quantity and limit price must be positive")
        if self.order_type != "limit" or self.time_in_force != "day":
            raise ValueError("Alpaca Paper canary requires limit/day orders")
        if self.extended_hours:
            raise ValueError("Alpaca Paper canary does not use extended-hours routing")
        if not self.client_order_id or len(self.client_order_id) > 128:
            raise ValueError("Alpaca client_order_id must contain at most 128 characters")

    def payload(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "qty": format(self.quantity, "f"),
            "side": self.side.value.lower(),
            "type": self.order_type,
            "time_in_force": self.time_in_force,
            "limit_price": format(self.limit_price, "f"),
            "extended_hours": self.extended_hours,
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True, slots=True)
class AlpacaPaperOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    filled_quantity: Decimal
    filled_average_price: Decimal | None
    limit_price: Decimal | None
    status: str
    submitted_at: datetime
    updated_at: datetime
    provider_request_id: str | None

    @property
    def remaining_quantity(self) -> Decimal:
        return max(Decimal("0"), self.quantity - self.filled_quantity)

    @property
    def is_open(self) -> bool:
        return self.status in ALPACA_PAPER_OPEN_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self.status in ALPACA_PAPER_TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class AlpacaPaperFillActivity:
    activity_id: str
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    executed_at: datetime
    provider_request_id: str | None


@dataclass(frozen=True, slots=True)
class AlpacaPaperCancelResult:
    broker_order_id: str
    accepted: bool
    provider_request_id: str | None


def validate_alpaca_paper_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if (
        normalized != ALPACA_PAPER_BASE_URL
        or parsed.scheme != "https"
        or parsed.hostname != "paper-api.alpaca.markets"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Alpaca Paper routing requires exactly "
            "https://paper-api.alpaca.markets"
        )
    return normalized


def deterministic_client_order_id(
    *,
    run_id: str,
    source_decision_id: str,
    symbol: str,
    side: OrderSide,
) -> str:
    digest = canonical_hash(
        {
            "lane": "ALPACA_PAPER_CANARY",
            "run_id": run_id,
            "source_decision_id": source_decision_id,
            "symbol": symbol,
            "side": side.value,
        }
    )[:32]
    return f"q1p-{digest}"


class AlpacaPaperTradingClient:
    """Strict Paper-only Trading API client.

    The client never accepts a configurable live host, never follows redirects,
    and never includes credentials in persisted response objects or exceptions.
    """

    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        base_url: str = ALPACA_PAPER_BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: Decimal | int | float = 10,
        reconciliation_lookup_attempts: int = 3,
        reconciliation_lookup_interval_seconds: Decimal | int | float = 2,
    ) -> None:
        if reconciliation_lookup_attempts <= 0:
            raise ValueError("reconciliation_lookup_attempts must be positive")
        if float(reconciliation_lookup_interval_seconds) <= 0:
            raise ValueError(
                "reconciliation_lookup_interval_seconds must be positive"
            )
        if client is not None and client.follow_redirects:
            raise ValueError(
                "Alpaca Paper client must not follow HTTP redirects"
            )
        self._credentials = credentials
        self._base_url = validate_alpaca_paper_base_url(base_url)
        self._reconciliation_lookup_attempts = reconciliation_lookup_attempts
        self._reconciliation_lookup_interval_seconds = float(
            reconciliation_lookup_interval_seconds
        )
        self._client = client or httpx.AsyncClient(
            timeout=float(timeout_seconds),
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_account(self) -> AlpacaPaperAccount:
        payload, request_id = await self._request_json("GET", "/v2/account")
        account = _object(payload, "account")
        return AlpacaPaperAccount(
            account_id=_required_string(account, "id"),
            status=_required_string(account, "status").upper(),
            currency=_required_string(account, "currency").upper(),
            equity=_required_decimal(account, "equity"),
            cash=_required_decimal(account, "cash"),
            buying_power=_required_decimal(account, "buying_power"),
            account_blocked=_required_bool(account, "account_blocked"),
            trading_blocked=_required_bool(account, "trading_blocked"),
            trade_suspended_by_user=_required_bool(
                account,
                "trade_suspended_by_user",
            ),
            provider_request_id=request_id,
        )

    async def list_positions(self) -> tuple[AlpacaPaperPosition, ...]:
        payload, _ = await self._request_json("GET", "/v2/positions")
        if not isinstance(payload, list):
            raise AlpacaPaperTradingError("Malformed Alpaca Paper positions response")
        return tuple(
            _parse_position(_object(item, "position"))
            for item in cast(list[object], payload)
        )

    async def list_orders(
        self,
        *,
        status: str = "all",
        limit: int = 500,
    ) -> tuple[AlpacaPaperOrder, ...]:
        if status not in {"open", "closed", "all"}:
            raise ValueError("Alpaca order status filter must be open, closed, or all")
        if not 1 <= limit <= 500:
            raise ValueError("Alpaca order history limit must be between 1 and 500")
        payload, request_id = await self._request_json(
            "GET",
            "/v2/orders",
            params={"status": status, "limit": limit, "direction": "asc"},
        )
        if not isinstance(payload, list):
            raise AlpacaPaperTradingError("Malformed Alpaca Paper orders response")
        return tuple(
            _parse_order(
                _object(item, "order"),
                provider_request_id=request_id,
            )
            for item in cast(list[object], payload)
        )

    async def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> AlpacaPaperOrder | None:
        try:
            payload, request_id = await self._request_json(
                "GET",
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_order_id},
            )
        except AlpacaPaperTradingError as exc:
            if exc.status_code == 404:
                return None
            raise
        return _parse_order(
            _object(payload, "order"),
            provider_request_id=request_id,
        )

    async def submit_order(
        self,
        request: AlpacaPaperOrderRequest,
    ) -> AlpacaPaperOrder:
        payload, request_id = await self._request_json(
            "POST",
            "/v2/orders",
            json_payload=request.payload(),
        )
        return _parse_order(
            _object(payload, "order"),
            provider_request_id=request_id,
        )

    async def ensure_submitted(
        self,
        request: AlpacaPaperOrderRequest,
    ) -> AlpacaPaperOrder:
        existing = await self.get_order_by_client_id(
            request.client_order_id
        )
        if existing is not None:
            _require_same_order(existing, request)
            return existing
        try:
            submitted = await self.submit_order(request)
        except AlpacaPaperTradingError as exc:
            if not exc.retryable and exc.status_code not in {409, 422}:
                raise
            for attempt in range(self._reconciliation_lookup_attempts):
                recovered = await self.get_order_by_client_id(
                    request.client_order_id
                )
                if recovered is not None:
                    _require_same_order(recovered, request)
                    return recovered
                if attempt + 1 < self._reconciliation_lookup_attempts:
                    await asyncio.sleep(
                        self._reconciliation_lookup_interval_seconds
                    )
            raise
        _require_same_order(submitted, request)
        return submitted

    async def cancel_order(
        self,
        broker_order_id: str,
    ) -> AlpacaPaperCancelResult:
        response = await self._request(
            "DELETE",
            f"/v2/orders/{broker_order_id}",
        )
        if response.status_code == 204:
            return AlpacaPaperCancelResult(
                broker_order_id=broker_order_id,
                accepted=True,
                provider_request_id=response.headers.get("X-Request-ID"),
            )
        self._raise_http_error(response)
        raise AssertionError("unreachable")

    async def list_fill_activities(
        self,
        *,
        after: datetime | None = None,
        page_size: int = 100,
    ) -> tuple[AlpacaPaperFillActivity, ...]:
        if not 1 <= page_size <= 100:
            raise ValueError(
                "Alpaca fill activity page size must be between 1 and 100"
            )
        params: dict[str, str | int] = {
            "direction": "asc",
            "page_size": page_size,
        }
        if after is not None:
            params["after"] = require_aware_utc(after).isoformat()
        payload, request_id = await self._request_json(
            "GET",
            "/v2/account/activities/FILL",
            params=params,
        )
        if not isinstance(payload, list):
            raise AlpacaPaperTradingError(
                "Malformed Alpaca Paper fill-activity response"
            )
        return tuple(
            _parse_fill_activity(
                _object(item, "fill activity"),
                provider_request_id=request_id,
            )
            for item in cast(list[object], payload)
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_payload: dict[str, object] | None = None,
    ) -> tuple[object, str | None]:
        response = await self._request(
            method,
            path,
            params=params,
            json_payload=json_payload,
        )
        if response.status_code >= 400:
            self._raise_http_error(response)
        try:
            payload: object = json.loads(
                response.text,
                parse_float=Decimal,
            )
        except json.JSONDecodeError as exc:
            raise AlpacaPaperTradingError(
                "Alpaca Paper returned invalid JSON",
                status_code=response.status_code,
                provider_request_id=response.headers.get("X-Request-ID"),
            ) from exc
        return payload, response.headers.get("X-Request-ID")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_payload: dict[str, object] | None = None,
    ) -> httpx.Response:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Alpaca Paper request path must be absolute")
        try:
            return await self._client.request(
                method,
                f"{self._base_url}{path}",
                params=params,
                json=json_payload,
                headers=self._credentials.headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise AlpacaPaperTradingError(
                "Alpaca Paper request outcome is unknown",
                retryable=True,
            ) from exc

    @staticmethod
    def _raise_http_error(response: httpx.Response) -> None:
        request_id = response.headers.get("X-Request-ID")
        raise AlpacaPaperTradingError(
            f"Alpaca Paper request failed ({response.status_code})",
            status_code=response.status_code,
            provider_request_id=request_id,
            retryable=response.status_code in {408, 409, 429}
            or response.status_code >= 500,
        )


def _require_same_order(
    order: AlpacaPaperOrder,
    request: AlpacaPaperOrderRequest,
) -> None:
    if (
        order.client_order_id != request.client_order_id
        or order.symbol != request.symbol
        or order.side is not request.side
        or order.quantity != request.quantity
        or order.limit_price != request.limit_price
    ):
        raise AlpacaPaperOrderConflict(
            "Existing Alpaca Paper client_order_id belongs to another order"
        )


def _parse_position(payload: JsonObject) -> AlpacaPaperPosition:
    return AlpacaPaperPosition(
        symbol=_required_string(payload, "symbol").upper(),
        quantity=_required_decimal(payload, "qty"),
        market_value=_required_decimal(payload, "market_value"),
        average_entry_price=_required_decimal(payload, "avg_entry_price"),
        side=_required_string(payload, "side").lower(),
    )


def _parse_order(
    payload: JsonObject,
    *,
    provider_request_id: str | None,
) -> AlpacaPaperOrder:
    status = _required_string(payload, "status").lower()
    if (
        status not in ALPACA_PAPER_OPEN_STATUSES
        and status not in ALPACA_PAPER_TERMINAL_STATUSES
    ):
        raise AlpacaPaperTradingError(
            f"Unsupported Alpaca Paper order status {status!r}"
        )
    return AlpacaPaperOrder(
        broker_order_id=_required_string(payload, "id"),
        client_order_id=_required_string(payload, "client_order_id"),
        symbol=_required_string(payload, "symbol").upper(),
        side=OrderSide(_required_string(payload, "side").upper()),
        quantity=_required_decimal(payload, "qty"),
        filled_quantity=_required_decimal(payload, "filled_qty"),
        filled_average_price=_optional_decimal(payload, "filled_avg_price"),
        limit_price=_optional_decimal(payload, "limit_price"),
        status=status,
        submitted_at=_required_datetime(payload, "submitted_at"),
        updated_at=_required_datetime(payload, "updated_at"),
        provider_request_id=provider_request_id,
    )


def _parse_fill_activity(
    payload: JsonObject,
    *,
    provider_request_id: str | None,
) -> AlpacaPaperFillActivity:
    return AlpacaPaperFillActivity(
        activity_id=_required_string(payload, "id"),
        broker_order_id=_required_string(payload, "order_id"),
        symbol=_required_string(payload, "symbol").upper(),
        side=OrderSide(_required_string(payload, "side").upper()),
        quantity=_required_decimal(payload, "qty"),
        price=_required_decimal(payload, "price"),
        executed_at=_required_datetime(payload, "transaction_time"),
        provider_request_id=provider_request_id,
    )


def _object(value: object, label: str) -> JsonObject:
    if not isinstance(value, dict):
        raise AlpacaPaperTradingError(f"Malformed Alpaca Paper {label} response")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AlpacaPaperTradingError(
            f"Alpaca Paper response requires string field {key}"
        )
    return value


def _required_decimal(payload: JsonObject, key: str) -> Decimal:
    value = payload.get(key)
    if value is None:
        raise AlpacaPaperTradingError(
            f"Alpaca Paper response requires decimal field {key}"
        )
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise AlpacaPaperTradingError(
            f"Alpaca Paper response has invalid decimal field {key}"
        ) from exc
    if not result.is_finite():
        raise AlpacaPaperTradingError(
            f"Alpaca Paper response has non-finite decimal field {key}"
        )
    return result


def _optional_decimal(
    payload: JsonObject,
    key: str,
) -> Decimal | None:
    value = payload.get(key)
    if value is None:
        return None
    return _required_decimal(payload, key)


def _required_bool(payload: JsonObject, key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise AlpacaPaperTradingError(
            f"Alpaca Paper response requires boolean field {key}"
        )
    return value


def _required_datetime(payload: JsonObject, key: str) -> datetime:
    value = _required_string(payload, key)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AlpacaPaperTradingError(
            f"Alpaca Paper response has invalid timestamp field {key}"
        ) from exc
    return require_aware_utc(parsed).astimezone(UTC)
