from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx

from trading.data.alpaca import AlpacaCredentials
from trading.data.alpaca_news import AlpacaNewsClient
from trading.data.raw_store import ImmutableRawStore
from trading.domain.time import FrozenClock
from trading.llm.webgpt_news import NewsItem


def test_news_received_after_query_cutoff_is_retained_with_honest_availability(
    tmp_path: Path,
) -> None:
    requested_end = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    received_at = datetime(2026, 7, 27, 14, 0, 1, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["end"] == "2026-07-27T14:00:00Z"
        assert request.url.params["sort"] == "desc"
        return httpx.Response(
            200,
            json={
                "news": [
                    {
                        "id": 42,
                        "created_at": "2026-07-27T13:59:00Z",
                        "updated_at": "2026-07-27T13:59:30Z",
                        "source": "benzinga",
                        "url": "https://example.com/news/42",
                        "headline": "Semiconductor market update",
                        "summary": "A bounded provider summary.",
                        "symbols": ["SOXX"],
                    }
                ],
                "next_page_token": None,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    news = AlpacaNewsClient(
        credentials=AlpacaCredentials("test-key", "test-secret"),
        raw_store=ImmutableRawStore(tmp_path),
        clock=FrozenClock(received_at),
        client=client,
    )

    async def run() -> list[NewsItem]:
        result = await news.fetch(
            start=datetime(2026, 7, 27, 13, 0, tzinfo=UTC),
            end=requested_end,
            max_items=10,
        )
        await client.aclose()
        return result

    items = asyncio.run(run())

    assert len(items) == 1
    assert items[0].available_at == received_at
