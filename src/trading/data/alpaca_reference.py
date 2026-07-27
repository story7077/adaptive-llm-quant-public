from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from zoneinfo import ZoneInfo

import httpx

from trading.data.alpaca import AlpacaCredentials, AlpacaHttpError, JsonObject
from trading.data.raw_store import ImmutableRawStore
from trading.domain.hashing import canonical_hash
from trading.domain.time import Clock, SystemClock

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class MarketSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    payload_hash: str
    available_at: datetime
    raw_object_uri: str


@dataclass(frozen=True, slots=True)
class CorporateActionRevision:
    action_id: str
    action_type: str
    symbol: str
    process_date: date | None
    available_at: datetime
    payload_hash: str
    payload: JsonObject
    raw_object_uri: str


class AlpacaReferenceClient:
    """Read-only reference-data client. It never sends account or order mutations."""

    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        raw_store: ImmutableRawStore,
        trading_base_url: str = "https://paper-api.alpaca.markets",
        data_base_url: str = "https://data.alpaca.markets",
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._raw_store = raw_store
        self._trading_base_url = trading_base_url.rstrip("/")
        self._data_base_url = data_base_url.rstrip("/")
        self._clock = clock or SystemClock()
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_calendar(self, *, start: date, end: date) -> list[MarketSession]:
        if start > end:
            raise ValueError("Calendar start must not follow end")
        payload, available_at, raw_uri = await self._get(
            f"{self._trading_base_url}/v2/calendar",
            params={
                "start": start.isoformat(),
                "end": end.isoformat(),
                "date_type": "TRADING",
            },
            channel="calendar",
        )
        if not isinstance(payload, list):
            raise AlpacaHttpError(200, "Malformed calendar payload")
        return [
            _parse_market_session(
                _object(item),
                available_at=available_at,
                raw_object_uri=raw_uri,
            )
            for item in cast(list[object], payload)
        ]

    async def fetch_corporate_actions(
        self,
        *,
        symbols: tuple[str, ...],
        start: date,
        end: date,
    ) -> list[CorporateActionRevision]:
        if not symbols:
            return []
        if start > end:
            raise ValueError("Corporate-action start must not follow end")
        params: dict[str, str | int] = {
            "symbols": ",".join(symbols),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "region": "us",
            "sort": "asc",
            "limit": 1000,
        }
        actions: list[CorporateActionRevision] = []
        seen_tokens: set[str] = set()
        while True:
            payload, available_at, raw_uri = await self._get(
                f"{self._data_base_url}/v1/corporate-actions",
                params=params,
                channel="corporate-actions",
            )
            if not isinstance(payload, dict):
                raise AlpacaHttpError(200, "Malformed corporate-actions payload")
            root = cast(dict[str, object], payload)
            for action_type, items in root.items():
                if action_type == "next_page_token" or items is None:
                    continue
                if not isinstance(items, list):
                    continue
                for raw_item in cast(list[object], items):
                    item = _object(raw_item)
                    actions.append(
                        _parse_corporate_action(
                            action_type=action_type,
                            payload=item,
                            available_at=available_at,
                            raw_object_uri=raw_uri,
                        )
                    )
            token = root.get("next_page_token")
            if token is None:
                return actions
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise AlpacaHttpError(200, "Invalid corporate-action page token")
            seen_tokens.add(token)
            params["page_token"] = token

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, str | int],
        channel: str,
    ) -> tuple[object, datetime, str]:
        response = await self._client.get(
            url,
            params=params,
            headers=self._credentials.headers,
        )
        available_at = self._clock.now()
        raw_uri = self._raw_store.persist(
            provider="alpaca",
            feed="reference",
            channel=channel,
            received_at=available_at,
            content=response.content,
        )
        if response.status_code >= 400:
            raise AlpacaHttpError(response.status_code, response.text[:240])
        try:
            payload: object = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise AlpacaHttpError(response.status_code, "Invalid JSON response") from exc
        return payload, available_at, raw_uri


def _parse_market_session(
    payload: JsonObject,
    *,
    available_at: datetime,
    raw_object_uri: str,
) -> MarketSession:
    session_date = date.fromisoformat(_required_string(payload, "date"))
    open_at = _calendar_time(session_date, _required_string(payload, "open"))
    close_at = _calendar_time(session_date, _required_string(payload, "close"))
    if close_at <= open_at:
        raise ValueError("Market session close must follow open")
    return MarketSession(
        session_date=session_date,
        open_at=open_at,
        close_at=close_at,
        payload_hash=canonical_hash(payload),
        available_at=available_at.astimezone(UTC),
        raw_object_uri=raw_object_uri,
    )


def _parse_corporate_action(
    *,
    action_type: str,
    payload: JsonObject,
    available_at: datetime,
    raw_object_uri: str,
) -> CorporateActionRevision:
    action_id = _required_string(payload, "id")
    symbol = str(payload.get("symbol") or payload.get("initiating_symbol") or "").upper()
    if not symbol:
        raise ValueError("Corporate action is missing a symbol")
    raw_process_date = payload.get("process_date")
    process_date = None if raw_process_date is None else date.fromisoformat(str(raw_process_date))
    return CorporateActionRevision(
        action_id=action_id,
        action_type=action_type,
        symbol=symbol,
        process_date=process_date,
        available_at=available_at.astimezone(UTC),
        payload_hash=canonical_hash(payload),
        payload=payload,
        raw_object_uri=raw_object_uri,
    )


def _calendar_time(session_date: date, raw: str) -> datetime:
    parsed = datetime.strptime(raw, "%H:%M").time()
    return datetime.combine(session_date, parsed, tzinfo=NEW_YORK).astimezone(UTC)


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Alpaca reference item must be an object")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Alpaca reference item requires string field {key}")
    return value
