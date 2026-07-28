from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock, Thread
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from trading.data.alpaca import AlpacaCredentials
from trading.data.alpaca_news import AlpacaNewsClient
from trading.data.raw_store import ImmutableRawStore
from trading.domain.contracts import (
    AssetImpactAssessment,
    NewsEvent,
    NewsFact,
    model_payload,
)
from trading.domain.enums import EventDirection, Horizon, OrdinalBucket
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.time import Clock, SystemClock
from trading.llm.webgpt_news import (
    AssetDirection,
    NewsHorizon,
    WebGptAdapterConfig,
    WebGptNewsAdapter,
    WebGptNewsRequest,
    WebGptNewsResult,
)
from trading.persistence.models import NewsEventRow, PaperCycleRow, SourceRecordRow
from trading.settings import Settings


class PaperNewsPipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        symbols: tuple[str, ...],
        repo_root: Path,
        lookback: timedelta = timedelta(hours=4),
        context_max_age: timedelta = timedelta(hours=8),
        clock: Clock | None = None,
    ) -> None:
        if not settings.has_alpaca_credentials:
            raise ValueError("Alpaca credentials are required for the news pipeline")
        self._session_factory = session_factory
        self._settings = settings
        self._symbols = symbols
        self._repo_root = repo_root
        self._lookback = lookback
        self._context_max_age = context_max_age
        self._clock = clock or SystemClock()
        self._adapter = WebGptNewsAdapter(
            WebGptAdapterConfig.from_env(repo_root)
        )

    def doctor(self) -> dict[str, Any]:
        return dict(self._adapter.doctor())

    async def run(
        self,
        cycle: PaperCycleRow,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        requested_cutoff = context["data_available_cutoff"]
        if not isinstance(requested_cutoff, datetime):
            raise ValueError("NEWS cycle context requires a datetime cutoff")
        if not self._settings.real_llm_enabled:
            raise RuntimeError("TRADING_REAL_LLM_ENABLED is required for WebGPT analysis")
        stored = self._stored_cycle_output(
            cycle_id=cycle.cycle_id,
            run_id=self._settings.paper_run_id,
        )
        if stored is not None:
            return stored
        request = self._prepared_request_for_cycle(cycle.cycle_id)
        if request is None:
            client = AlpacaNewsClient(
                credentials=AlpacaCredentials(
                    key_id=self._settings.alpaca_key_id or "",
                    secret_key=self._settings.alpaca_secret_key or "",
                ),
                raw_store=ImmutableRawStore(self._settings.raw_store),
                base_url=self._settings.alpaca_data_url,
            )
            try:
                news_items = await client.fetch(
                    start=requested_cutoff - self._lookback,
                    end=requested_cutoff,
                    max_items=40,
                )
            finally:
                await client.aclose()
            if not news_items:
                completed_at = self._clock.now()
                return {
                    "status": "NO_NEW_ARTICLES",
                    "news_event_id": None,
                    "article_count": 0,
                    "data_available_cutoff": completed_at,
                }

            analysis_as_of = max(
                requested_cutoff,
                self._clock.now(),
                *(item.available_at for item in news_items),
            )
            request_id = stable_id(
                "webgpt-news-request",
                cycle.cycle_id,
                [item.source_id for item in news_items],
            )
            request = WebGptNewsRequest(
                request_id=request_id,
                created_at=analysis_as_of,
                analysis_as_of=analysis_as_of,
                symbols=list(self._symbols),
                news_items=news_items,
                market_context={
                    "paper_run_id": self._settings.paper_run_id,
                    "cycle_id": cycle.cycle_id,
                    "cycle_scheduled_at": cycle.scheduled_at.isoformat(),
                    "data_available_cutoff": (
                        requested_cutoff.isoformat()
                    ),
                    "market_data_provider": "alpaca",
                    "equity_feed": "iex",
                    "real_order_routing": False,
                },
            )
            self._persist_prepared_request(
                cycle_id=cycle.cycle_id,
                request=request,
            )
        news_items = request.news_items
        existing = self._result_for_request(request.request_id)
        result = (
            existing
            if existing is not None
            else await asyncio.to_thread(self._adapter.analyze, request)
        )
        completed_at = self._clock.now()
        news_event_id = stable_id(
            "news-analysis",
            request.request_id,
            canonical_hash(result),
        )
        typed_events = self._typed_news_events(
            request=request,
            result=result,
            completed_at=completed_at,
        )
        with self._session_factory.begin() as session:
            if session.get(NewsEventRow, news_event_id) is None:
                session.add(
                    NewsEventRow(
                        news_event_id=news_event_id,
                        as_of=completed_at,
                        payload_json={
                            "provider": "WEBGPT_5_6_SOL_XHIGH",
                            "request": request.model_dump(mode="json"),
                            "analysis": result.model_dump(mode="json"),
                            "analysis_completed_at": completed_at.isoformat(),
                        },
                        output_hash=canonical_hash(result),
                    )
                )
            for event in typed_events:
                if session.get(NewsEventRow, event.news_event_id) is None:
                    session.add(
                        NewsEventRow(
                            news_event_id=event.news_event_id,
                            as_of=event.as_of,
                            payload_json=model_payload(event),
                            output_hash=event.output_hash,
                        )
                    )
        return {
            "status": "ANALYZED",
            "news_event_id": news_event_id,
            "evidence_event_ids": [
                event.news_event_id
                for event in typed_events
            ],
            "article_count": len(news_items),
            "event_count": len(result.events),
            "overall_direction": result.overall_direction.value,
            "overall_confidence": result.overall_confidence,
            "analysis_summary": result.analysis_summary,
            "data_available_cutoff": completed_at,
        }

    def _typed_news_events(
        self,
        *,
        request: WebGptNewsRequest,
        result: WebGptNewsResult,
        completed_at: datetime,
    ) -> tuple[NewsEvent, ...]:
        raw_cutoff = request.market_context.get(
            "data_available_cutoff"
        )
        try:
            source_cutoff = (
                request.analysis_as_of
                if not isinstance(raw_cutoff, str)
                else datetime.fromisoformat(
                    raw_cutoff.replace("Z", "+00:00")
                )
            )
        except ValueError:
            source_cutoff = request.analysis_as_of
        source_cutoff = min(source_cutoff, completed_at)
        context_manifest_hash = canonical_hash(request)
        prompt_hash = canonical_hash(
            {
                "request_id": request.request_id,
                "schema_version": request.schema_version,
                "context_manifest_hash": context_manifest_hash,
            }
        )
        events: list[NewsEvent] = []
        for analyzed in result.events:
            content = {
                "request_id": request.request_id,
                "analyzed_event": analyzed,
                "analysis_as_of": result.analysis_as_of,
                "completed_at": completed_at,
                "data_available_cutoff": source_cutoff,
                "context_manifest_hash": context_manifest_hash,
            }
            output_hash = canonical_hash(content)
            event_id = stable_id(
                "webgpt-typed-news-event",
                request.request_id,
                analyzed.event_id,
                output_hash,
            )
            events.append(
                NewsEvent(
                    news_event_id=event_id,
                    schema_version="news_event_v2",
                    model_run_id=request.request_id,
                    as_of=completed_at,
                    data_available_cutoff=source_cutoff,
                    source_event_ids=list(analyzed.source_ids),
                    event_type=(
                        f"WEBGPT_{analyzed.direction.value}_"
                        f"{analyzed.horizon.value}"
                    ),
                    actors=[],
                    facts=[
                        NewsFact(
                            statement=analyzed.canonical_summary,
                            source_id=source_id,
                            certainty=analyzed.confidence,
                            is_official_source=False,
                        )
                        for source_id in analyzed.source_ids
                    ],
                    impacts=[
                        AssetImpactAssessment(
                            symbol_or_factor=impact.symbol,
                            direction=_event_direction(
                                impact.direction
                            ),
                            severity_bucket=_ordinal_bucket(
                                impact.magnitude
                            ),
                            horizon=_domain_horizon(
                                analyzed.horizon
                            ),
                            transmission_channels=[
                                impact.transmission_summary
                            ],
                            raw_confidence=impact.confidence,
                        )
                        for impact in analyzed.asset_impacts
                    ],
                    novelty_bucket=_ordinal_bucket(
                        analyzed.novelty
                    ),
                    contradiction_source_ids=[],
                    invalidation_conditions=[],
                    expires_at=completed_at + self._context_max_age,
                    prompt_hash=prompt_hash,
                    context_manifest_hash=context_manifest_hash,
                    output_hash=output_hash,
                    created_at=completed_at,
                )
            )
        return tuple(events)

    def _stored_cycle_output(
        self,
        *,
        cycle_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(NewsEventRow)
                    .order_by(desc(NewsEventRow.as_of))
                    .limit(500)
                )
            )
        for row in rows:
            request_value = row.payload_json.get("request")
            analysis_value = row.payload_json.get("analysis")
            if not isinstance(request_value, dict) or not isinstance(
                analysis_value,
                dict,
            ):
                continue
            request = cast(dict[str, Any], request_value)
            market_context_value = request.get("market_context")
            if not isinstance(market_context_value, dict):
                continue
            market_context = cast(dict[str, Any], market_context_value)
            if (
                market_context.get("paper_run_id") != run_id
                or market_context.get("cycle_id") != cycle_id
            ):
                continue
            analysis = WebGptNewsResult.model_validate(analysis_value)
            news_items = request.get("news_items", [])
            article_count = (
                len(cast(list[Any], news_items))
                if isinstance(news_items, list)
                else 0
            )
            return {
                "status": "ANALYZED",
                "news_event_id": row.news_event_id,
                "article_count": article_count,
                "event_count": len(analysis.events),
                "overall_direction": analysis.overall_direction.value,
                "overall_confidence": analysis.overall_confidence,
                "analysis_summary": analysis.analysis_summary,
                "data_available_cutoff": row.as_of,
                "idempotent_replay": True,
            }
        return None

    def _prepared_request_for_cycle(
        self,
        cycle_id: str,
    ) -> WebGptNewsRequest | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(SourceRecordRow).where(
                    SourceRecordRow.provider == "webgpt-news-request",
                    SourceRecordRow.external_id == cycle_id,
                    SourceRecordRow.revision == 1,
                )
            )
        if row is None:
            return None
        content = row.payload_json.get("content")
        if not isinstance(content, dict):
            raise ValueError("Stored WebGPT request source is malformed")
        return WebGptNewsRequest.model_validate(content)

    def _persist_prepared_request(
        self,
        *,
        cycle_id: str,
        request: WebGptNewsRequest,
    ) -> None:
        source_id = stable_id("webgpt-news-request-source", cycle_id)
        content_hash = canonical_hash(request)
        with self._session_factory.begin() as session:
            existing = session.get(SourceRecordRow, source_id)
            if existing is not None:
                if existing.content_hash != content_hash:
                    raise ValueError("WebGPT cycle request changed after preparation")
                return
            session.add(
                SourceRecordRow(
                    source_id=source_id,
                    provider="webgpt-news-request",
                    external_id=cycle_id,
                    revision=1,
                    published_at=request.created_at,
                    available_at=request.created_at,
                    content_hash=content_hash,
                    payload_json={"content": request.model_dump(mode="json")},
                )
            )

    def recent_context(
        self,
        *,
        as_of: datetime,
        run_id: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        lower_bound = as_of - self._context_max_age
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(NewsEventRow)
                    .where(
                        NewsEventRow.as_of <= as_of,
                        NewsEventRow.as_of >= lower_bound,
                    )
                    .order_by(desc(NewsEventRow.as_of))
                    .limit(max(limit * 10, 100))
                )
            )
        context: list[dict[str, Any]] = []
        for row in rows:
            request_value = row.payload_json.get("request", {})
            if not isinstance(request_value, dict):
                continue
            request = cast(dict[str, Any], request_value)
            market_context_value = request.get("market_context", {})
            if not isinstance(market_context_value, dict):
                continue
            market_context = cast(dict[str, Any], market_context_value)
            if market_context.get("paper_run_id") != run_id:
                continue
            context.append({
                "news_event_id": row.news_event_id,
                "available_at": row.as_of.isoformat(),
                "output_hash": row.output_hash,
                "analysis": row.payload_json.get("analysis", {}),
            })
            if len(context) >= limit:
                break
        return context

    def _result_for_request(self, request_id: str) -> WebGptNewsResult | None:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(NewsEventRow).order_by(desc(NewsEventRow.as_of)).limit(100)
                )
            )
        for row in rows:
            request = row.payload_json.get("request")
            analysis = row.payload_json.get("analysis")
            request_payload = (
                cast(dict[str, Any], request)
                if isinstance(request, dict)
                else {}
            )
            if request_payload.get("request_id") == request_id:
                if not isinstance(analysis, dict):
                    raise ValueError("Stored WebGPT news analysis is malformed")
                return WebGptNewsResult.model_validate(analysis)
        return None


class BackgroundPaperNewsRefresher:
    """Launch one non-blocking WebGPT news refresh per Q1 cycle."""

    def __init__(self, pipeline: PaperNewsPipeline) -> None:
        self._pipeline = pipeline
        self._lock = Lock()
        self._in_flight: set[str] = set()
        self._last_status = "IDLE"

    def __call__(
        self,
        cycle: PaperCycleRow,
        cutoff: datetime,
    ) -> dict[str, object]:
        with self._lock:
            if cycle.cycle_id in self._in_flight:
                return {
                    "status": "ALREADY_RUNNING",
                    "cycle_id": cycle.cycle_id,
                }
            self._in_flight.add(cycle.cycle_id)
            self._last_status = "RUNNING"
        Thread(
            target=self._run,
            args=(cycle, cutoff),
            name="q1-webgpt-news-refresh",
            daemon=True,
        ).start()
        return {
            "status": "STARTED",
            "cycle_id": cycle.cycle_id,
        }

    @property
    def last_status(self) -> str:
        with self._lock:
            return self._last_status

    def _run(
        self,
        cycle: PaperCycleRow,
        cutoff: datetime,
    ) -> None:
        status = "COMPLETED"
        try:
            asyncio.run(
                self._pipeline.run(
                    cycle,
                    {
                        "data_available_cutoff": cutoff,
                    },
                )
            )
        except Exception:
            status = "FAILED"
        finally:
            with self._lock:
                self._in_flight.discard(cycle.cycle_id)
                self._last_status = status


def _event_direction(value: AssetDirection) -> EventDirection:
    if value is AssetDirection.BULLISH:
        return EventDirection.POSITIVE
    if value is AssetDirection.BEARISH:
        return EventDirection.NEGATIVE
    return EventDirection.NEUTRAL


def _domain_horizon(value: NewsHorizon) -> Horizon:
    return (
        Horizon.H4
        if value is NewsHorizon.INTRADAY
        else Horizon.H5D
    )


def _ordinal_bucket(value: float) -> OrdinalBucket:
    if value <= 0:
        return OrdinalBucket.NONE
    if value < 0.25:
        return OrdinalBucket.LOW
    if value < 0.50:
        return OrdinalBucket.MEDIUM
    if value < 0.75:
        return OrdinalBucket.HIGH
    return OrdinalBucket.EXTREME
