from __future__ import annotations

import html
import json
import re
from datetime import datetime
from decimal import Decimal
from typing import cast

import httpx

from trading.data.alpaca import AlpacaCredentials, AlpacaHttpError, JsonObject
from trading.data.raw_store import ImmutableRawStore
from trading.domain.hashing import stable_id
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.llm.webgpt_news import NewsItem


class AlpacaNewsClient:
    def __init__(
        self,
        *,
        credentials: AlpacaCredentials,
        raw_store: ImmutableRawStore,
        base_url: str = "https://data.alpaca.markets",
        clock: Clock | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credentials = credentials
        self._raw_store = raw_store
        self._base_url = base_url.rstrip("/")
        self._clock = clock or SystemClock()
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch(
        self,
        *,
        start: datetime,
        end: datetime,
        symbols: tuple[str, ...] = (),
        max_items: int = 80,
    ) -> list[NewsItem]:
        start_at = require_aware_utc(start, "start")
        end_at = require_aware_utc(end, "end")
        if start_at > end_at:
            raise ValueError("News start must not follow end")
        if not 1 <= max_items <= 500:
            raise ValueError("max_items must be between 1 and 500")
        params: dict[str, str | int] = {
            "start": _rfc3339(start_at),
            "end": _rfc3339(end_at),
            "sort": "desc",
            "limit": min(50, max_items),
            "include_content": "true",
            "exclude_contentless": "false",
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        items: list[NewsItem] = []
        seen_tokens: set[str] = set()
        while len(items) < max_items:
            payload, available_at = await self._get(params=params)
            raw_news = payload.get("news", [])
            if not isinstance(raw_news, list):
                raise AlpacaHttpError(200, "Malformed Alpaca news payload")
            for raw in cast(list[object], raw_news):
                item = _parse_news_item(
                    _object(raw),
                    available_at=available_at,
                )
                items.append(item)
                if len(items) >= max_items:
                    break
            token = payload.get("next_page_token")
            if token is None or len(items) >= max_items:
                break
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise AlpacaHttpError(200, "Invalid Alpaca news page token")
            seen_tokens.add(token)
            params["page_token"] = token
        deduplicated = {item.source_id: item for item in items}
        return sorted(
            deduplicated.values(),
            key=lambda item: (item.available_at, item.source_id),
        )

    async def _get(
        self,
        *,
        params: dict[str, str | int],
    ) -> tuple[JsonObject, datetime]:
        response = await self._client.get(
            f"{self._base_url}/v1beta1/news",
            params=params,
            headers=self._credentials.headers,
        )
        available_at = self._clock.now()
        self._raw_store.persist(
            provider="alpaca",
            feed="news",
            channel="rest-news",
            received_at=available_at,
            content=response.content,
        )
        if response.status_code >= 400:
            raise AlpacaHttpError(response.status_code, response.text[:240])
        try:
            decoded: object = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as exc:
            raise AlpacaHttpError(response.status_code, "Invalid news JSON") from exc
        return _object(decoded), available_at


def _parse_news_item(payload: JsonObject, *, available_at: datetime) -> NewsItem:
    external_id = str(payload.get("id", "")).strip()
    if not external_id:
        raise ValueError("Alpaca news item is missing id")
    published_at = _timestamp(_required_string(payload, "created_at"))
    updated_at = _timestamp(
        str(payload.get("updated_at") or payload["created_at"])
    )
    source = str(payload.get("source") or "alpaca-news")
    url = _required_string(payload, "url")
    headline = _required_string(payload, "headline")
    body = str(payload.get("content") or payload.get("summary") or "")
    symbols_value = payload.get("symbols", [])
    symbols = (
        [str(item).upper() for item in cast(list[object], symbols_value)]
        if isinstance(symbols_value, list)
        else []
    )
    return NewsItem(
        source_id=stable_id(
            "alpaca-news",
            external_id,
            updated_at,
        ),
        source=source,
        url=url,
        headline=headline,
        published_at=published_at,
        available_at=max(require_aware_utc(available_at), published_at),
        body_excerpt=_plain_text(body)[:2500],
        symbols=list(dict.fromkeys(symbols)),
    )


def _plain_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_tags).split())


def _object(value: object) -> JsonObject:
    if not isinstance(value, dict):
        raise ValueError("Alpaca news payload item must be an object")
    return cast(JsonObject, value)


def _required_string(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Alpaca news item requires string field {key}")
    return value


def _timestamp(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return require_aware_utc(parsed)


def _rfc3339(value: datetime) -> str:
    return require_aware_utc(value).isoformat().replace("+00:00", "Z")
