from __future__ import annotations

import asyncio

# pyright: reportUnusedFunction=false
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from trading.control.bundles import export_request_bundle
from trading.control.contracts import AdaptivePolicyDecision
from trading.control.providers import CommanderProvider
from trading.control.service import (
    ControlPlaneError,
    ControlPlaneService,
    DecisionConflict,
    NoProviderSelected,
    RequestNotFound,
    SelectionConflict,
)
from trading.dashboard.live_market import LiveMarketError, LiveMarketSnapshotService
from trading.dashboard.service import (
    DashboardError,
    DashboardRunNotFound,
    MarketDashboardService,
)
from trading.data.alpaca import (
    FEED,
    PROVIDER,
    AlpacaCredentials,
    AlpacaRestClient,
    AlpacaStreamClient,
)
from trading.data.alpaca_reference import AlpacaReferenceClient
from trading.data.history import MarketHistoryService
from trading.data.market_repository import MarketDataRepository
from trading.data.raw_store import ImmutableRawStore
from trading.data.universe import basic_iex_stream_plan, market_data_symbols
from trading.data.worker import AlpacaMarketWorker
from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.domain.enums import MarketConnectionState
from trading.domain.time import SystemClock
from trading.execution.alpaca_paper import AlpacaPaperTradingClient
from trading.persistence.db import create_database_engine, make_session_factory
from trading.persistence.factorial import FactorialPaperExperimentRepository
from trading.persistence.meta_controller import MetaControllerRepository
from trading.persistence.meta_oos import MetaOosRepository
from trading.persistence.paper import load_paper_account_spec
from trading.persistence.prospective import ProspectiveCandidateRepository
from trading.persistence.prospective_evaluation import (
    ProspectiveEvaluationRepository,
)
from trading.persistence.prospective_outcomes import (
    ProspectiveOutcomeRepository,
)
from trading.persistence.research import (
    ResearchPersistenceError,
    ResearchRepository,
)
from trading.persistence.research_scheduler import ResearchSchedulerRepository
from trading.persistence.research_shadow import (
    ResearchShadowRuntimeRepository,
)
from trading.research.config import (
    ResearchConfigBundle,
    load_research_config,
    recursive_improvement_status,
)
from trading.research.contracts import ResearchCommanderKind
from trading.research.lifecycle import (
    ResearchLifecycleError,
    ResearchLifecycleService,
)
from trading.research.prospective import load_prospective_candidate_config
from trading.research.prospective_evaluation import (
    load_prospective_evaluation_config,
)
from trading.research.prospective_outcomes import (
    load_prospective_outcome_config,
)
from trading.runtime.commander import OperationalRiskCommander
from trading.runtime.forward_paper import ForwardPaperTradingService
from trading.runtime.news import (
    BackgroundPaperNewsRefresher,
    PaperNewsPipeline,
)
from trading.runtime.paper import PaperRuntimeError, PaperRuntimeService
from trading.runtime.paper_worker import PaperRuntimeWorker
from trading.runtime.prospective_candidate import (
    prospective_candidate_status,
    resolve_prospective_challenger_id,
)
from trading.runtime.prospective_evaluation import (
    prospective_evaluation_status,
)
from trading.runtime.prospective_outcomes import prospective_outcome_status
from trading.runtime.q1_alpaca_paper import Q1AlpacaPaperCanaryService
from trading.runtime.q1_config import llm_transport_config
from trading.runtime.q1_cycle import Q1PaperCycleProcessor
from trading.runtime.q1_paper import Q1PaperRuntimeError, Q1PaperRuntimeService
from trading.runtime.q1_provider import Q1SelectedCommanderProvider
from trading.runtime.q1_worker import Q1PaperRuntimeWorker
from trading.runtime.research_scheduler import ResearchSchedulerService
from trading.runtime.scheduler import PaperCycleStore
from trading.settings import (
    Settings,
    load_alpaca_paper_config_bundle,
    load_config_bundle,
    load_q1_config_bundle,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderSelectionBody(ApiModel):
    provider: CommanderProvider
    expected_version: int = Field(ge=0)


class RequestBody(ApiModel):
    arm_scope: Literal["B3-RISK", "B3-FULL"]
    scope_id: str = Field(default="legacy_global", min_length=1, max_length=80)
    context: dict[str, JsonValue] = Field(default_factory=dict)
    as_of: datetime | None = None
    data_available_cutoff: datetime | None = None


class DecisionBody(ApiModel):
    provider: CommanderProvider
    output: dict[str, Any]


class ResearchCommanderSelectionBody(ApiModel):
    commander: ResearchCommanderKind
    expected_version: int = Field(ge=0)


class ResearchPromotionEvaluationBody(ApiModel):
    challenger_id: str = Field(min_length=1, max_length=160)


class ResearchPromotionApprovalBody(ApiModel):
    challenger_id: str = Field(min_length=1, max_length=160)
    approved_by: str = Field(min_length=1, max_length=120)


class ResearchChampionDesignationBody(ApiModel):
    challenger_id: str = Field(min_length=1, max_length=160)
    expected_current_version: str = Field(min_length=1, max_length=80)
    designated_by: str = Field(min_length=1, max_length=120)
    idempotency_key: str = Field(min_length=1, max_length=160)


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    status_only: bool = False,
) -> FastAPI:
    active_settings = settings or Settings.from_env(_repo_root())
    engine: Engine | None = None
    if session_factory is None:
        engine = create_database_engine(active_settings.database_url)
        session_factory = make_session_factory(engine)
    config = load_config_bundle(active_settings.config_dir)
    q1_mode = (
        active_settings.paper_algorithm_version == Q1_ALGORITHM_VERSION
    )
    q1_config = (
        load_q1_config_bundle(active_settings.config_dir)
        if q1_mode
        else None
    )
    alpaca_paper_config = (
        load_alpaca_paper_config_bundle(active_settings.config_dir)
        if q1_mode
        else None
    )
    service = ControlPlaneService(session_factory)
    research_repository = ResearchRepository(session_factory)
    meta_controller_repository = MetaControllerRepository(session_factory)
    research_lifecycle = ResearchLifecycleService(
        repository=research_repository
    )
    research_config = load_research_config(active_settings.config_dir)
    prospective_config = load_prospective_candidate_config(
        active_settings.config_dir
    )
    prospective_outcome_config = load_prospective_outcome_config(
        active_settings.config_dir
    )
    prospective_evaluation_config = (
        load_prospective_evaluation_config(active_settings.config_dir)
    )
    research_scheduler = ResearchSchedulerService(
        repository=ResearchSchedulerRepository(session_factory),
        config=research_config,
    )
    factorial_repository = FactorialPaperExperimentRepository(session_factory)
    prospective_repository = ProspectiveCandidateRepository(session_factory)
    prospective_outcome_repository = ProspectiveOutcomeRepository(
        session_factory
    )
    prospective_evaluation_repository = (
        ProspectiveEvaluationRepository(session_factory)
    )
    dashboard_service = MarketDashboardService(session_factory)
    live_market_service = LiveMarketSnapshotService(
        session_factory,
        settings=active_settings,
        config=config,
    )
    market_repository = MarketDataRepository(session_factory)
    legacy_paper_service = (
        None
        if q1_mode
        else PaperRuntimeService(
            session_factory,
            config=config,
            workspace_root=_repo_root(),
        )
    )
    q1_paper_service = (
        Q1PaperRuntimeService(
            session_factory,
            config=q1_config,
            workspace_root=_repo_root(),
            alpaca_paper_enabled=(
                active_settings.q1_alpaca_paper_enabled
            ),
            alpaca_paper_config=alpaca_paper_config,
        )
        if q1_config is not None
        else None
    )
    forward_trading = (
        None
        if q1_mode
        else ForwardPaperTradingService(
            session_factory,
            config=config,
            max_quote_age_seconds=active_settings.market_quote_stale_seconds,
        )
    )
    paper_cycles = PaperCycleStore(session_factory)
    account_quote_symbols = (
        ()
        if active_settings.paper_account_file is None
        else tuple(
            position.symbol
            for position in load_paper_account_spec(
                active_settings.paper_account_file
            ).positions
        )
    )
    stream_plan = basic_iex_stream_plan(
        config,
        required_quote_symbols=account_quote_symbols,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        if status_only:
            _app.state.q1_worker_state = "STATUS_ONLY_EXTERNAL_RUNTIME"
            try:
                yield
            finally:
                if engine is not None:
                    engine.dispose()
            return
        market_stop: asyncio.Event | None = None
        market_task: asyncio.Task[None] | None = None
        paper_stop: asyncio.Event | None = None
        paper_task: asyncio.Task[None] | None = None
        history_client: AlpacaRestClient | None = None
        history_stop: asyncio.Event | None = None
        history_task: asyncio.Task[None] | None = None
        if q1_mode:
            _app.state.q1_worker_state = (
                "STATUS_READY_AWAITING_Q1_WORKER_INTEGRATION"
                if active_settings.paper_runtime_enabled
                else "STATUS_ONLY_PAPER_RUNTIME_DISABLED"
            )
        initial_state = (
            MarketConnectionState.STOPPED
            if not active_settings.market_data_enabled
            else (
                MarketConnectionState.STOPPED
                if active_settings.has_alpaca_credentials
                else MarketConnectionState.AUTH_REQUIRED
            )
        )
        await asyncio.to_thread(
            market_repository.ensure_status,
            provider=PROVIDER,
            feed=FEED,
            state=initial_state,
            now=SystemClock().now(),
        )
        if active_settings.market_data_enabled and active_settings.has_alpaca_credentials:
            credentials = AlpacaCredentials(
                key_id=active_settings.alpaca_key_id or "",
                secret_key=active_settings.alpaca_secret_key or "",
            )
            raw_store = ImmutableRawStore(active_settings.raw_store)
            history_client = AlpacaRestClient(
                credentials=credentials,
                raw_store=raw_store,
                base_url=active_settings.alpaca_data_url,
            )
            history_service = MarketHistoryService(
                repository=market_repository,
                client=history_client,
                symbols=market_data_symbols(config),
            )
            try:
                initial_history = await history_service.backfill_daily()
            except Exception:
                await history_client.aclose()
                history_client = None
                raise
            _app.state.history_last_error = None
            _app.state.history_last_refresh = {
                "fetched": initial_history.fetched,
                "inserted": initial_history.inserted,
                "at": initial_history.end.isoformat(),
            }
            history_stop = asyncio.Event()
            history_task = asyncio.create_task(
                _periodic_history_backfill(
                    history_service,
                    history_stop,
                    app=_app,
                    interval=timedelta(hours=6),
                ),
                name="alpaca-adjusted-history-refresh",
            )
            _app.state.history_task = history_task
            worker = AlpacaMarketWorker(
                repository=market_repository,
                rest_client=AlpacaRestClient(
                    credentials=credentials,
                    raw_store=raw_store,
                    base_url=active_settings.alpaca_data_url,
                ),
                stream_client=AlpacaStreamClient(
                    credentials=credentials,
                    symbols=market_data_symbols(config),
                    raw_store=raw_store,
                    trade_symbols=stream_plan.trades,
                    quote_symbols=stream_plan.quotes,
                    bar_symbols=stream_plan.bars,
                    updated_bar_symbols=stream_plan.updated_bars,
                    url=active_settings.alpaca_stream_url,
                ),
                symbols=market_data_symbols(config),
                heartbeat_interval_seconds=active_settings.market_heartbeat_seconds,
            )
            market_stop = asyncio.Event()
            market_task = asyncio.create_task(
                worker.run_forever(market_stop),
                name="alpaca-iex-market-data",
            )
            _app.state.market_task = market_task
            if active_settings.paper_runtime_enabled:
                if q1_mode:
                    if (
                        q1_paper_service is None
                        or q1_config is None
                        or active_settings.paper_account_file is None
                    ):
                        raise Q1PaperRuntimeError(
                            "Q1 paper services require a configured account file"
                        )
                    q1_news_pipeline: PaperNewsPipeline | None = None
                    if active_settings.webgpt_enabled:
                        if not active_settings.real_llm_enabled:
                            raise Q1PaperRuntimeError(
                                "Q1 WebGPT analysis requires the explicit "
                                "real LLM gate"
                            )
                        q1_news_pipeline = PaperNewsPipeline(
                            session_factory,
                            settings=active_settings,
                            symbols=market_data_symbols(config),
                            repo_root=_repo_root(),
                        )
                        _app.state.webgpt_readiness = (
                            await asyncio.to_thread(
                                q1_news_pipeline.doctor
                            )
                        )

                    q1_news_refresher = (
                        None
                        if q1_news_pipeline is None
                        else BackgroundPaperNewsRefresher(
                            q1_news_pipeline
                        )
                    )

                    q1_worker = Q1PaperRuntimeWorker(
                        settings=active_settings,
                        config=q1_config,
                        paper=q1_paper_service,
                        cycles=paper_cycles,
                        processor=Q1PaperCycleProcessor(
                            session_factory,
                            runtime=q1_paper_service,
                            account_file=(
                                active_settings.paper_account_file
                            ),
                            workspace_root=_repo_root(),
                            llm_overlay_provider=(
                                Q1SelectedCommanderProvider(
                                    session_factory,
                                    settings=active_settings,
                                    transport_config=llm_transport_config(
                                        q1_config
                                    ),
                                    repo_root=_repo_root(),
                                )
                                if active_settings.real_llm_enabled
                                else None
                            ),
                            llm_news_refresher=(
                                q1_news_refresher
                            ),
                        ),
                        reference_client=AlpacaReferenceClient(
                            credentials=credentials,
                            raw_store=raw_store,
                            trading_base_url=(
                                active_settings.alpaca_trading_url
                            ),
                            data_base_url=active_settings.alpaca_data_url,
                        ),
                        alpaca_paper_canary=(
                            Q1AlpacaPaperCanaryService(
                                session_factory,
                                q1_config=q1_config,
                                paper_config=alpaca_paper_config,
                                client=AlpacaPaperTradingClient(
                                    credentials=credentials,
                                    base_url=(
                                        alpaca_paper_config.config.rest_base_url
                                    ),
                                    timeout_seconds=(
                                        alpaca_paper_config.config.request_timeout_seconds
                                    ),
                                    reconciliation_lookup_attempts=(
                                        alpaca_paper_config.config.reconciliation_lookup_attempts
                                    ),
                                    reconciliation_lookup_interval_seconds=(
                                        alpaca_paper_config.config.reconciliation_lookup_interval_seconds
                                    ),
                                ),
                                workspace_root=_repo_root(),
                            )
                            if (
                                active_settings.q1_alpaca_paper_enabled
                                and alpaca_paper_config is not None
                            )
                            else None
                        ),
                    )
                    paper_stop = asyncio.Event()
                    paper_task = asyncio.create_task(
                        q1_worker.run_forever(paper_stop),
                        name="q1-math-core-paper-runtime",
                    )
                    _app.state.paper_task = paper_task
                    _app.state.q1_worker_state = "Q1_WORKER_RUNNING"
                else:
                    if legacy_paper_service is None or forward_trading is None:
                        raise PaperRuntimeError(
                            "Legacy paper services are unavailable"
                        )
                news_pipeline: PaperNewsPipeline | None = None
                commander_pipeline: OperationalRiskCommander | None = None
                if not q1_mode and active_settings.webgpt_enabled:
                    if legacy_paper_service is None:
                        raise PaperRuntimeError(
                            "Legacy paper service is unavailable"
                        )
                    if not active_settings.real_llm_enabled:
                        raise PaperRuntimeError(
                            "WebGPT paper analysis requires the explicit real LLM gate"
                        )
                    news_pipeline = PaperNewsPipeline(
                        session_factory,
                        settings=active_settings,
                        symbols=market_data_symbols(config),
                        repo_root=_repo_root(),
                    )
                    commander_pipeline = OperationalRiskCommander(
                        session_factory,
                        settings=active_settings,
                        paper=legacy_paper_service,
                        news=news_pipeline,
                        repo_root=_repo_root(),
                    )
                    _app.state.webgpt_readiness = await asyncio.to_thread(
                        news_pipeline.doctor
                    )
                if not q1_mode:
                    if (
                        legacy_paper_service is None
                        or forward_trading is None
                    ):
                        raise PaperRuntimeError(
                            "Legacy paper services are unavailable"
                        )
                    paper_worker = PaperRuntimeWorker(
                        settings=active_settings,
                        config=config,
                        paper=legacy_paper_service,
                        cycles=paper_cycles,
                        reference_client=AlpacaReferenceClient(
                            credentials=credentials,
                            raw_store=raw_store,
                            trading_base_url=(
                                active_settings.alpaca_trading_url
                            ),
                            data_base_url=active_settings.alpaca_data_url,
                        ),
                        news_runner=(
                            None
                            if news_pipeline is None
                            else news_pipeline.run
                        ),
                        commander_runner=(
                            None
                            if commander_pipeline is None
                            else commander_pipeline.run
                        ),
                        forward_trading=forward_trading,
                    )
                    await asyncio.to_thread(paper_worker.initialize)
                    paper_stop = asyncio.Event()
                    paper_task = asyncio.create_task(
                        paper_worker.run_forever(paper_stop),
                        name="forward-paper-runtime",
                    )
                    _app.state.paper_task = paper_task

                def record_paper_failure(task: asyncio.Task[None]) -> None:
                    if task.cancelled():
                        return
                    error = task.exception()
                    if error is None:
                        return
                    if q1_mode:
                        _app.state.q1_worker_state = "Q1_WORKER_FAILED"
                    _app.state.paper_failure_task = asyncio.create_task(
                        asyncio.to_thread(
                            paper_cycles.heartbeat,
                            run_id=active_settings.paper_run_id,
                            state="FAILED",
                            now=SystemClock().now(),
                            error_code=type(error).__name__.upper(),
                            error_detail=str(error),
                        )
                    )

                if paper_task is None:
                    raise PaperRuntimeError(
                        "Paper runtime task was not created"
                    )
                paper_task.add_done_callback(record_paper_failure)
        try:
            yield
        finally:
            if history_task is not None and history_stop is not None:
                history_stop.set()
                history_task.cancel()
                with suppress(asyncio.CancelledError):
                    await history_task
            if history_client is not None:
                await history_client.aclose()
            if paper_task is not None and paper_stop is not None:
                paper_stop.set()
                paper_task.cancel()
                with suppress(asyncio.CancelledError):
                    await paper_task
            if market_task is not None and market_stop is not None:
                market_stop.set()
                market_task.cancel()
                with suppress(asyncio.CancelledError):
                    await market_task
            if engine is not None:
                engine.dispose()

    app = FastAPI(
        title="Adaptive LLM Quant Control",
        version="0.2.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.control_service = service
    app.state.dashboard_service = dashboard_service
    app.state.live_market_service = live_market_service
    app.state.paper_service = (
        q1_paper_service if q1_mode else legacy_paper_service
    )
    app.state.paper_cycles = paper_cycles
    app.state.database_engine = engine
    app.state.webgpt_readiness = None
    app.state.history_last_refresh = None
    app.state.history_last_error = None
    app.state.q1_worker_state = (
        "STATUS_ONLY_PAPER_RUNTIME_DISABLED"
        if q1_mode
        else "NOT_APPLICABLE"
    )
    app.state.status_only = status_only

    if status_only:

        @app.middleware("http")
        async def reject_status_only_writes(
            request: Request,
            call_next: Any,
        ) -> Any:
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                return _json_error(
                    405,
                    "status-only operator UI is read-only",
                )
            return await call_next(request)

    @app.exception_handler(ControlPlaneError)
    async def control_plane_error_handler(_request: Any, exc: ControlPlaneError) -> Any:
        status_code = 400
        if isinstance(exc, RequestNotFound):
            status_code = 404
        elif isinstance(
            exc,
            (SelectionConflict, DecisionConflict, NoProviderSelected),
        ):
            status_code = 409
        return _json_error(status_code, str(exc))

    @app.exception_handler(DashboardError)
    async def dashboard_error_handler(_request: Any, exc: DashboardError) -> Any:
        status_code = 404 if isinstance(exc, DashboardRunNotFound) else 400
        return _json_error(status_code, str(exc))

    @app.exception_handler(LiveMarketError)
    async def live_market_error_handler(_request: Any, exc: LiveMarketError) -> Any:
        return _json_error(400, str(exc))

    @app.exception_handler(PaperRuntimeError)
    async def paper_runtime_error_handler(_request: Any, exc: PaperRuntimeError) -> Any:
        return _json_error(409, str(exc))

    @app.exception_handler(Q1PaperRuntimeError)
    async def q1_paper_runtime_error_handler(
        _request: Any,
        exc: Q1PaperRuntimeError,
    ) -> Any:
        return _json_error(409, str(exc))

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (_repo_root() / "src" / "trading" / "ui" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/api/control/status")
    def control_status() -> dict[str, Any]:
        return service.status(scope_id=active_settings.paper_run_id)

    @app.get("/api/paper/status")
    def paper_status() -> dict[str, Any]:
        if q1_mode:
            if q1_paper_service is None:
                raise Q1PaperRuntimeError("Q1 paper status service is unavailable")
            payload = q1_paper_service.status(active_settings.paper_run_id)
        else:
            if legacy_paper_service is None:
                raise PaperRuntimeError("Legacy paper status service is unavailable")
            payload = legacy_paper_service.status(active_settings.paper_run_id)
        scheduler_status = paper_cycles.status(
            active_settings.paper_run_id
        )
        payload["scheduler"] = scheduler_status
        payload["runtime_enabled"] = active_settings.paper_runtime_enabled
        payload["operator_surface_mode"] = (
            "STATUS_ONLY_READ_ONLY" if status_only else "MANAGED_RUNTIME"
        )
        selection = service.current_selection()
        if status_only:
            payload["process"] = _status_only_process_status(
                scheduler_status,
                now=SystemClock().now(),
                stale_after_seconds=max(
                    30,
                    active_settings.paper_poll_seconds * 4,
                ),
            )
        else:
            paper_task = getattr(app.state, "paper_task", None)
            payload["process"] = {
                "task_running": (
                    isinstance(paper_task, asyncio.Task)
                    and not paper_task.done()
                ),
                "worker_state": (
                    app.state.q1_worker_state
                    if q1_mode
                    else "LEGACY_UI_LIFESPAN_WORKER"
                ),
                "managed_by_this_process": True,
            }
        payload["ai"] = {
            "webgpt_enabled": active_settings.webgpt_enabled,
            "real_llm_enabled": active_settings.real_llm_enabled,
            "webgpt_readiness": app.state.webgpt_readiness,
            "selected_commander": (
                None if selection is None else selection.provider.value
            ),
        }
        return payload

    @app.get("/api/research/status")
    def research_status() -> dict[str, Any]:
        factorial_status = _factorial_status(
            factorial_repository,
            research_config=research_config,
        )
        persisted_status = research_repository.status()
        meta_controller_status = meta_controller_repository.status()
        portfolio_sharpe_status = (
            research_repository.portfolio_sharpe().status()
        )
        meta_oos_status = MetaOosRepository(session_factory).status()
        prospective_status = prospective_candidate_status(
            prospective_repository,
            config=prospective_config,
        )
        prospective_challenger_id = resolve_prospective_challenger_id(
            prospective_status=prospective_status,
            persisted_status=persisted_status,
        )
        return {
            **persisted_status,
            "recursive_improvement": recursive_improvement_status(
                research_config,
                experiment_outcome_ledger=(
                    persisted_status["experiment_outcome_ledger"]
                ),
                meta_controller_ledger=meta_controller_status,
                portfolio_sharpe_ledger=portfolio_sharpe_status,
                meta_oos_ledger=meta_oos_status,
            ),
            "research_plane_version": (
                research_config.config.algorithm_version
            ),
            "operational_algorithm": {
                "algorithm_version": (
                    active_settings.paper_algorithm_version
                ),
                "mutation_policy": "VERSIONED_CHALLENGER_ONLY",
            },
            "web_scout": {
                "required_model": "GPT-5.6 Sol Pro",
                "required_reasoning": "xhigh",
                "access_path": "CHATGPT_WEB_AGBROWSE",
                "status": "LOCAL_RUNTIME_REQUIRED",
            },
            "available_data_catalog": {
                "asset_classes": ["US_EQUITY", "US_ETF"],
                "hardcoded_symbol_allowlist": False,
            },
            "factorial_arms": factorial_status["required_arms"],
            "factorial_experiment": factorial_status,
            "scheduler": research_scheduler.status(),
            "prospective_candidate": prospective_status,
            "prospective_outcomes": prospective_outcome_status(
                prospective_outcome_repository,
                config=prospective_outcome_config,
                challenger_id=prospective_challenger_id,
            ),
            "prospective_evaluation": prospective_evaluation_status(
                prospective_evaluation_repository,
                config=prospective_evaluation_config,
                research_repository=research_repository,
                challenger_id=prospective_challenger_id,
            ),
            "shadow_runtime": ResearchShadowRuntimeRepository(
                session_factory
            ).status(),
            "operator_surface_mode": (
                "STATUS_ONLY_READ_ONLY"
                if status_only
                else "MANAGED_RUNTIME"
            ),
            "real_order_routing": False,
        }

    @app.get("/api/research/factorial/status")
    def research_factorial_status(
        run_id: str | None = None,
    ) -> dict[str, Any]:
        return _factorial_status(
            factorial_repository,
            research_config=research_config,
            run_id=run_id,
        )

    @app.post("/api/research/commander")
    def research_select_commander(
        body: ResearchCommanderSelectionBody,
    ) -> dict[str, Any]:
        now = SystemClock().now()
        try:
            selection = research_repository.select_commander(
                body.commander,
                config_hash=research_config.manifest_hash,
                effective_at=now,
                created_at=now,
                expected_version=body.expected_version,
            )
        except ResearchPersistenceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "selection": selection.model_dump(mode="json"),
            "real_order_routing": False,
        }

    @app.post("/api/research/promotion/evaluate")
    def research_evaluate_promotion(
        body: ResearchPromotionEvaluationBody,
    ) -> dict[str, Any]:
        try:
            result = research_lifecycle.evaluate_trusted_promotion(
                challenger_id=body.challenger_id,
                contract=(
                    research_config.config.promotion.evaluation_contract
                ),
                created_at=SystemClock().now(),
            )
        except ResearchLifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "created": result.created,
            "challenger_id": body.challenger_id,
            "status": result.status.value,
            "evidence": result.evidence.model_dump(mode="json"),
            "evaluation": result.evaluation.model_dump(mode="json"),
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    @app.post("/api/research/promotion/approve")
    def research_approve_promotion(
        body: ResearchPromotionApprovalBody,
    ) -> dict[str, Any]:
        try:
            result = research_lifecycle.approve_trusted_promotion(
                challenger_id=body.challenger_id,
                approved_by=body.approved_by,
                created_at=SystemClock().now(),
            )
        except ResearchLifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "created": result.created,
            "challenger_id": body.challenger_id,
            "status": result.status.value,
            "manual_approval": result.decision.model_dump(mode="json"),
            "champion_designated": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    @app.post("/api/research/champion/designate")
    def research_designate_champion(
        body: ResearchChampionDesignationBody,
    ) -> dict[str, Any]:
        try:
            result = research_lifecycle.designate_champion(
                challenger_id=body.challenger_id,
                expected_current_version=body.expected_current_version,
                designated_by=body.designated_by,
                idempotency_key=body.idempotency_key,
                designated_at=SystemClock().now(),
            )
        except ResearchLifecycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "created": result.created,
            "challenger_id": body.challenger_id,
            "status": result.status.value,
            "current_champion": result.designation.model_dump(mode="json"),
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }

    @app.get("/api/trading/dashboard")
    def trading_dashboard(
        run_id: str | None = None,
        arm_id: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        return dashboard_service.snapshot(
            run_id=run_id,
            arm_id=arm_id,
            symbol=symbol,
        )

    @app.get("/api/market/snapshot")
    def market_snapshot(
        symbol: str | None = None,
        timeframe: str = "1Min",
        limit: int = Query(120, ge=1, le=500),
    ) -> dict[str, Any]:
        snapshot = live_market_service.snapshot(
            symbol=symbol,
            timeframe=timeframe,
            limit=limit,
        )
        snapshot["history_refresh"] = history_refresh_status(
            enabled=active_settings.market_data_enabled,
            configured=active_settings.has_alpaca_credentials,
            last_refresh=app.state.history_last_refresh,
            last_error=app.state.history_last_error,
        )
        return snapshot

    @app.get("/api/control/schema")
    def output_schema() -> dict[str, Any]:
        return AdaptivePolicyDecision.model_json_schema()

    @app.post("/api/control/provider")
    def select_provider(body: ProviderSelectionBody) -> dict[str, Any]:
        selection, changed = service.select_provider(
            body.provider,
            expected_version=body.expected_version,
        )
        return {
            "changed": changed,
            "selection": selection.model_dump(mode="json"),
        }

    @app.post("/api/control/requests")
    def create_request(body: RequestBody) -> dict[str, Any]:
        request = service.create_request(
            arm_scope=body.arm_scope,
            scope_id=body.scope_id,
            context=body.context,
            as_of=body.as_of,
            data_available_cutoff=body.data_available_cutoff,
        )
        commander_dir = active_settings.commander_dir or (_repo_root().parent / "stock-commander")
        exported = export_request_bundle(request, commander_dir=commander_dir)
        prompt = exported.prompt_file.read_text(encoding="utf-8")
        return {
            "request": request.model_dump(mode="json"),
            "prompt": prompt,
            "output_schema": AdaptivePolicyDecision.model_json_schema(),
            "bundle": exported.as_payload(),
        }

    @app.get("/api/control/requests/{request_id}")
    def get_request(request_id: str) -> dict[str, Any]:
        request = service.get_request(request_id)
        return {"request": request.model_dump(mode="json")}

    @app.post("/api/control/requests/{request_id}/decision")
    def submit_decision(request_id: str, body: DecisionBody) -> dict[str, Any]:
        receipt = service.submit_decision(
            request_id=request_id,
            provider=body.provider,
            output=body.output,
        )
        return {"receipt": receipt.model_dump(mode="json")}

    return app


async def _periodic_history_backfill(
    service: MarketHistoryService,
    stop: asyncio.Event,
    *,
    app: FastAPI,
    interval: timedelta,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval.total_seconds())
        except TimeoutError:
            try:
                result = await service.backfill_daily()
            except Exception as exc:
                app.state.history_last_error = {
                    "error_code": type(exc).__name__.upper(),
                    "detail": str(exc)[:500],
                    "at": SystemClock().now().isoformat(),
                }
            else:
                app.state.history_last_error = None
                app.state.history_last_refresh = {
                    "fetched": result.fetched,
                    "inserted": result.inserted,
                    "at": result.end.isoformat(),
                }


def history_refresh_status(
    *,
    enabled: bool,
    configured: bool,
    last_refresh: dict[str, Any] | None,
    last_error: dict[str, Any] | None,
) -> dict[str, Any]:
    if not enabled:
        status = "DISABLED"
    elif not configured:
        status = "AUTH_REQUIRED"
    elif last_error is not None:
        status = "ERROR"
    elif last_refresh is not None:
        status = "READY"
    else:
        status = "PENDING"
    return {
        "status": status,
        "last_success": (
            None
            if last_refresh is None
            else {
                "fetched": last_refresh.get("fetched"),
                "inserted": last_refresh.get("inserted"),
                "at": last_refresh.get("at"),
            }
        ),
        "last_error": (
            None
            if last_error is None
            else {
                "error_code": last_error.get("error_code"),
                "at": last_error.get("at"),
            }
        ),
    }


def _factorial_status(
    repository: FactorialPaperExperimentRepository,
    *,
    research_config: ResearchConfigBundle,
    run_id: str | None = None,
) -> dict[str, Any]:
    matched = research_config.factorial.matched_conditions
    return repository.status(
        run_id=run_id,
        minimum_common_sessions=(
            research_config.config.shadow.minimum_forward_sessions
        ),
        schedule_timezone=research_config.config.schedule.timezone,
        scheduled_time=(
            research_config.config.schedule.daily_aggregation_time
        ),
        expected_config_manifest_hash=research_config.manifest_hash,
        decision_schedule_version=matched.decision_schedule_version,
        execution_scenario_version=matched.execution_scenario_version,
        cost_model_version=matched.cost_model_version,
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_error(status_code: int, detail: str) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})


def _status_only_process_status(
    scheduler_status: dict[str, Any],
    *,
    now: datetime,
    stale_after_seconds: int,
) -> dict[str, Any]:
    runtime = scheduler_status.get("runtime")
    if not isinstance(runtime, dict):
        return {
            "task_running": False,
            "worker_state": "EXTERNAL_RUNTIME_NOT_INITIALIZED",
            "managed_by_this_process": False,
            "status_source": "PERSISTED_RUNTIME_HEARTBEAT",
            "heartbeat_fresh": False,
        }
    typed_runtime = cast(dict[str, Any], runtime)
    state = typed_runtime.get("state")
    raw_heartbeat = typed_runtime.get("heartbeat_at")
    heartbeat: datetime | None = None
    if isinstance(raw_heartbeat, str):
        try:
            parsed = datetime.fromisoformat(
                raw_heartbeat.replace("Z", "+00:00")
            )
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            heartbeat = parsed
    heartbeat_fresh = (
        heartbeat is not None
        and heartbeat <= now
        and now - heartbeat <= timedelta(seconds=stale_after_seconds)
    )
    state_name = state if isinstance(state, str) and state else "UNKNOWN"
    terminal = state_name in {"FAILED", "STOPPED"}
    if terminal:
        worker_state = f"EXTERNAL_RUNTIME_{state_name}"
    elif not heartbeat_fresh:
        worker_state = "EXTERNAL_RUNTIME_HEARTBEAT_STALE"
    else:
        worker_state = f"EXTERNAL_RUNTIME_{state_name}"
    return {
        "task_running": heartbeat_fresh and not terminal,
        "worker_state": worker_state,
        "managed_by_this_process": False,
        "status_source": "PERSISTED_RUNTIME_HEARTBEAT",
        "heartbeat_fresh": heartbeat_fresh,
    }


app = create_app()
