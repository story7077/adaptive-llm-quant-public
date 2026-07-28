from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from trading.domain.time import FrozenClock
from trading.llm.webgpt_news import WebGptNewsRequest, WebGptNewsResult
from trading.persistence.models import NewsEventRow, PaperCycleRow
from trading.runtime.news import PaperNewsPipeline
from trading.settings import Settings


def _settings(repository_root, *, real_llm_enabled: bool) -> Settings:
    return Settings(
        database_url="sqlite+pysqlite://",
        config_dir=repository_root / "config",
        raw_store=repository_root / ".local" / "test-raw",
        real_broker_enabled=False,
        real_llm_enabled=real_llm_enabled,
        production_unlock=False,
        alpaca_key_id="test-key",
        alpaca_secret_key="test-secret",
        paper_run_id="paper-a",
        webgpt_enabled=True,
    )


def test_news_pipeline_refuses_model_call_when_real_llm_gate_is_off(
    sqlite_database,
    repository_root,
) -> None:
    _, _, factory = sqlite_database
    cutoff = datetime(2026, 7, 27, 14, 0, tzinfo=UTC)
    pipeline = PaperNewsPipeline(
        factory,
        settings=_settings(repository_root, real_llm_enabled=False),
        symbols=("SOXX",),
        repo_root=repository_root,
        clock=FrozenClock(cutoff),
    )
    cycle = PaperCycleRow(
        cycle_id="news-cycle",
        run_id="paper-a",
        cycle_kind="NEWS",
        scheduled_at=cutoff,
    )

    with pytest.raises(RuntimeError, match="TRADING_REAL_LLM_ENABLED"):
        asyncio.run(
            pipeline.run(
                cycle,
                {"data_available_cutoff": cutoff},
            )
        )


def test_recent_news_context_is_scoped_to_run_and_bounded_by_age(
    sqlite_database,
    repository_root,
) -> None:
    _, _, factory = sqlite_database
    now = datetime(2026, 7, 27, 18, 0, tzinfo=UTC)
    settings = _settings(repository_root, real_llm_enabled=True)
    pipeline = PaperNewsPipeline(
        factory,
        settings=settings,
        symbols=("SOXX",),
        repo_root=repository_root,
        context_max_age=timedelta(hours=8),
        clock=FrozenClock(now),
    )

    def payload(run_id: str) -> dict[str, object]:
        return {
            "request": {"market_context": {"paper_run_id": run_id}},
            "analysis": {"analysis_summary": run_id},
        }

    with factory.begin() as session:
        session.add_all(
            [
                NewsEventRow(
                    news_event_id="paper-a-recent",
                    as_of=now - timedelta(hours=1),
                    payload_json=payload("paper-a"),
                    output_hash="a" * 64,
                ),
                NewsEventRow(
                    news_event_id="paper-b-recent",
                    as_of=now - timedelta(minutes=30),
                    payload_json=payload("paper-b"),
                    output_hash="b" * 64,
                ),
                NewsEventRow(
                    news_event_id="paper-a-stale",
                    as_of=now - timedelta(hours=9),
                    payload_json=payload("paper-a"),
                    output_hash="c" * 64,
                ),
            ]
        )

    context = pipeline.recent_context(as_of=now, run_id="paper-a")

    assert [item["news_event_id"] for item in context] == ["paper-a-recent"]


def test_webgpt_analysis_is_projected_to_typed_q1_evidence(
    sqlite_database,
    repository_root,
) -> None:
    _, _, factory = sqlite_database
    cutoff = datetime(2026, 7, 27, 16, 0, tzinfo=UTC)
    completed_at = cutoff + timedelta(minutes=2)
    pipeline = PaperNewsPipeline(
        factory,
        settings=_settings(repository_root, real_llm_enabled=True),
        symbols=("QQQ", "SOXX"),
        repo_root=repository_root,
        clock=FrozenClock(completed_at),
    )
    request = WebGptNewsRequest.model_validate(
        {
            "request_id": "q1-news-request",
            "created_at": completed_at,
            "analysis_as_of": completed_at,
            "symbols": ["QQQ", "SOXX"],
            "news_items": [
                {
                    "source_id": "licensed-source-1",
                    "source": "Licensed feed",
                    "url": "https://example.com/news/1",
                    "headline": "Bounded market event",
                    "published_at": cutoff - timedelta(minutes=10),
                    "available_at": cutoff - timedelta(minutes=9),
                    "body_excerpt": "Licensed excerpt.",
                    "symbols": ["SOXX"],
                }
            ],
            "market_context": {
                "paper_run_id": "paper-a",
                "data_available_cutoff": cutoff.isoformat(),
            },
        }
    )
    result = WebGptNewsResult.model_validate(
        {
            "request_id": request.request_id,
            "analysis_as_of": completed_at,
            "overall_direction": "RISK_OFF",
            "overall_confidence": 0.7,
            "events": [
                {
                    "event_id": "event-1",
                    "canonical_summary": "A bounded risk event occurred.",
                    "direction": "RISK_OFF",
                    "horizon": "ONE_TO_THREE_DAYS",
                    "confidence": 0.7,
                    "novelty": 0.6,
                    "source_ids": ["licensed-source-1"],
                    "source_urls": ["https://example.com/news/1"],
                    "asset_impacts": [
                        {
                            "symbol": "SOXX",
                            "direction": "BEARISH",
                            "magnitude": 0.6,
                            "confidence": 0.7,
                            "transmission_summary": "Chip beta is exposed.",
                        }
                    ],
                    "counterevidence": [],
                }
            ],
            "uncertainties": [],
            "data_gaps": [],
            "analysis_summary": "Risk-off evidence.",
        }
    )

    typed = pipeline._typed_news_events(
        request=request,
        result=result,
        completed_at=completed_at,
    )

    assert len(typed) == 1
    assert typed[0].data_available_cutoff == cutoff
    assert typed[0].created_at == completed_at
    assert typed[0].source_event_ids == ["licensed-source-1"]
    assert typed[0].impacts[0].symbol_or_factor == "SOXX"
    assert typed[0].output_hash
