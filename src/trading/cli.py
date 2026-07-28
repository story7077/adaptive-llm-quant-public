from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from sqlalchemy import select, text

from trading.control.bundles import export_request_bundle, run_codex_bundle
from trading.control.contracts import AdaptivePolicyDecision
from trading.control.providers import CommanderProvider
from trading.control.service import ControlPlaneService
from trading.dashboard.live_market import LiveMarketSnapshotService
from trading.data.alpaca import (
    AlpacaCredentials,
    AlpacaRestClient,
    AlpacaStreamClient,
)
from trading.data.alpaca_reference import AlpacaReferenceClient
from trading.data.history import MarketHistoryService
from trading.data.market_repository import MarketDataRepository
from trading.data.raw_store import ImmutableRawStore
from trading.data.universe import (
    IexStreamSubscriptionPlan,
    basic_iex_stream_plan,
    market_data_symbols,
)
from trading.data.worker import AlpacaMarketWorker
from trading.domain.algorithm import (
    LEGACY_FORWARD_ALGORITHM_VERSION,
    Q1_ALGORITHM_VERSION,
    SUPPORTED_PAPER_ALGORITHM_VERSIONS,
)
from trading.domain.contracts import PolicyPatch
from trading.domain.hashing import canonical_hash
from trading.domain.time import SystemClock, require_aware_utc
from trading.execution.alpaca_paper import AlpacaPaperTradingClient
from trading.experiments.arms import ARM_IDS
from trading.llm.policy_compiler import PolicyCompiler, PolicyState
from trading.llm.webgpt_news import (
    WebGptAdapterConfig,
    WebGptAdapterError,
    WebGptNewsAdapter,
    WebGptNewsRequest,
    WebGptNewsResult,
)
from trading.persistence.db import (
    create_database_engine,
    current_revision,
    downgrade_database,
    make_session_factory,
    upgrade_database,
)
from trading.persistence.experiment_outcomes import (
    ExperimentOutcomePersistenceError,
)
from trading.persistence.meta_controller import (
    MetaControllerPersistenceError,
    MetaControllerRepository,
)
from trading.persistence.meta_oos import (
    MetaOosPersistenceError,
    MetaOosRepository,
)
from trading.persistence.models import NavSnapshotRow, RunRow, ShadowArmRow
from trading.persistence.paper import load_paper_account_spec
from trading.persistence.research import ResearchPersistenceError, ResearchRepository
from trading.persistence.research_scheduler import ResearchSchedulerRepository
from trading.replay.engine import replay_full
from trading.replay.q1 import replay_q1_run
from trading.replay.verifier import verify_ledger_arm, verify_run
from trading.research.candidate_artifact import CandidateArtifactBundleV1
from trading.research.chronological_meta_oos import (
    ChronologicalMetaOosPlanV1,
    ChronologicalMetaOosResultV1,
    MetaOosError,
    verify_chronological_meta_oos_result,
)
from trading.research.config import (
    FACTORIAL_CONFIG_FILE,
    RESEARCH_CONFIG_FILE,
    ResearchConfigBundle,
    load_research_config,
    meta_controller_parameters,
    meta_oos_evaluation_contract,
    recursive_improvement_status,
)
from trading.research.contracts import (
    ResearchCommanderKind,
    ResearchDecisionV1,
    ResearchRequestV1,
)
from trading.research.experiment_outcomes import (
    AlgorithmProposalV2,
    ExperimentOutcomeEventV1,
    ExperimentOutcomeMaturationInputV1,
    ResearchActionKind,
    ResearchExperimentActionV1,
    ResearchMemorySnapshotV1,
)
from trading.research.file_runtime import (
    ResearchFileRuntimeError,
    ResearchPlaneFileRuntime,
    atomic_write_json,
    load_json_model,
    load_research_request,
    local_artifact_label,
    resolve_local_output,
    write_cycle_decision,
)
from trading.research.host import ResearchHostError
from trading.research.lifecycle import (
    ResearchLifecycleError,
    ResearchLifecycleService,
)
from trading.research.meta_controller import (
    ResearchActionPlanV1,
    build_research_context,
)
from trading.research.promotion_evidence import (
    ChampionDesignationV1,
    PromotionEvidenceV1,
    TrustedPromotionEvaluationV1,
    TrustedShadowPerformanceSummaryV1,
)
from trading.research.v2_contracts import (
    ResearchDecisionV2,
    ResearchRequestV2,
)
from trading.research.webgpt_commander import (
    WebGptActiveResearchCommander,
    WebGptCommanderError,
)
from trading.research.webgpt_scout import (
    WebGptActiveResearchScout,
    WebGptScoutConfig,
    WebGptScoutError,
    WebScoutRequestV1,
)
from trading.runtime.news import (
    BackgroundPaperNewsRefresher,
    PaperNewsPipeline,
)
from trading.runtime.paper import PaperRuntimeService
from trading.runtime.paper_worker import PaperRuntimeWorker
from trading.runtime.pipeline import seed_demo
from trading.runtime.q1_alpaca_paper import Q1AlpacaPaperCanaryService
from trading.runtime.q1_config import (
    llm_transport_config,
    operational_config,
)
from trading.runtime.q1_cycle import Q1PaperCycleProcessor
from trading.runtime.q1_paper import Q1PaperRuntimeService
from trading.runtime.q1_provider import Q1SelectedCommanderProvider
from trading.runtime.q1_worker import Q1PaperRuntimeWorker
from trading.runtime.research_scheduler import ResearchSchedulerService
from trading.runtime.scheduler import PaperCycleStore
from trading.settings import (
    ALPACA_PAPER_CONFIG_FILE,
    Q1_CONFIG_FILE,
    ConfigBundle,
    Settings,
    load_alpaca_paper_config_bundle,
    load_config_bundle,
    load_q1_config_bundle,
)

app = typer.Typer(no_args_is_help=True, help="Adaptive LLM quant Phase 0 operations.")
config_app = typer.Typer(no_args_is_help=True)
db_app = typer.Typer(no_args_is_help=True)
seed_app = typer.Typer(no_args_is_help=True)
ingest_app = typer.Typer(no_args_is_help=True)
run_app = typer.Typer(no_args_is_help=True)
ledger_app = typer.Typer(no_args_is_help=True)
policy_app = typer.Typer(no_args_is_help=True)
shadow_app = typer.Typer(no_args_is_help=True)
stats_app = typer.Typer(no_args_is_help=True)
control_app = typer.Typer(no_args_is_help=True)
ui_app = typer.Typer(no_args_is_help=True)
market_app = typer.Typer(no_args_is_help=True)
paper_app = typer.Typer(no_args_is_help=True)
webgpt_app = typer.Typer(no_args_is_help=True)
research_app = typer.Typer(no_args_is_help=True)
research_outcome_app = typer.Typer(no_args_is_help=True)
research_memory_app = typer.Typer(no_args_is_help=True)
research_meta_policy_app = typer.Typer(no_args_is_help=True)
research_meta_oos_app = typer.Typer(no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(seed_app, name="seed")
app.add_typer(ingest_app, name="ingest")
app.add_typer(run_app, name="run")
app.add_typer(ledger_app, name="ledger")
app.add_typer(policy_app, name="policy")
app.add_typer(shadow_app, name="shadow")
app.add_typer(stats_app, name="stats")
app.add_typer(control_app, name="control")
app.add_typer(ui_app, name="ui")
app.add_typer(market_app, name="market")
app.add_typer(paper_app, name="paper")
app.add_typer(research_app, name="research")
research_app.add_typer(research_outcome_app, name="outcome")
research_app.add_typer(research_memory_app, name="memory")
research_app.add_typer(research_meta_policy_app, name="meta-policy")
research_app.add_typer(research_meta_oos_app, name="meta-oos")

EXPECTED_DATABASE_REVISION = "0017_chronological_meta_oos_v1"
app.add_typer(webgpt_app, name="webgpt")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def runtime() -> tuple[Settings, Any, Any]:
    settings = Settings.from_env(repo_root())
    config = load_config_bundle(settings.config_dir)
    engine = create_database_engine(settings.database_url)
    return settings, config, engine


def _paper_algorithm_version(value: str) -> str:
    selected = value.strip()
    if selected not in SUPPORTED_PAPER_ALGORITHM_VERSIONS:
        raise typer.BadParameter(
            f"algorithm version must be one of {SUPPORTED_PAPER_ALGORITHM_VERSIONS}"
        )
    return selected


def _require_loopback_host(value: str) -> str:
    host = value.strip()
    if host.lower() == "localhost":
        return host
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise typer.BadParameter(
            "operator UI host must be a loopback IP address or localhost"
        ) from exc
    if not address.is_loopback:
        raise typer.BadParameter(
            "operator UI host must remain loopback-only"
        )
    return host


@app.command("doctor")
def doctor() -> None:
    checks: dict[str, dict[str, Any]] = {}
    settings = Settings.from_env(repo_root())
    try:
        if settings.paper_algorithm_version == Q1_ALGORITHM_VERSION:
            selected_config = load_q1_config_bundle(settings.config_dir)
            operational_config(selected_config)
            config_files = [Q1_CONFIG_FILE, "costs.yaml"]
            load_alpaca_paper_config_bundle(settings.config_dir)
            config_files.append(ALPACA_PAPER_CONFIG_FILE)
        else:
            selected_config = load_config_bundle(settings.config_dir)
            config_files = sorted(selected_config.documents)
        checks["config"] = {
            "ok": True,
            "algorithm_version": settings.paper_algorithm_version,
            "manifest_hash": selected_config.manifest_hash,
            "files": config_files,
        }
    except Exception as exc:
        checks["config"] = {"ok": False, "detail": str(exc)}

    now = SystemClock().now()
    utc_offset = now.utcoffset()
    checks["utc_clock"] = {
        "ok": utc_offset is not None and utc_offset.total_seconds() == 0,
        "now": now.isoformat(),
    }
    try:
        engine = create_database_engine(settings.database_url)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        revision = current_revision(engine)
        checks["database"] = {
            "ok": True,
            "dialect": engine.dialect.name,
            "migration_revision": revision,
        }
        checks["migration"] = {
            "ok": revision == EXPECTED_DATABASE_REVISION,
            "expected": EXPECTED_DATABASE_REVISION,
            "actual": revision,
        }
        engine.dispose()
    except Exception as exc:
        checks["database"] = {"ok": False, "detail": str(exc)}
        checks["migration"] = {"ok": False, "detail": "Database connection unavailable"}

    try:
        settings.raw_store.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=".doctor-", dir=settings.raw_store)
        os.close(descriptor)
        Path(temp_name).unlink()
        checks["raw_store"] = {"ok": True, "path": str(settings.raw_store)}
    except Exception as exc:
        checks["raw_store"] = {"ok": False, "detail": str(exc)}

    checks["production_broker"] = {
        "ok": not settings.real_broker_enabled and not settings.production_unlock,
        "enabled": settings.real_broker_enabled,
        "production_unlock": settings.production_unlock,
    }
    checks["real_llm"] = {
        "ok": not settings.real_llm_enabled,
        "enabled": settings.real_llm_enabled,
    }
    checks["market_data"] = {
        "ok": True,
        "enabled": settings.market_data_enabled,
        "provider": "alpaca",
        "feed": "iex",
        "credentials": "CONFIGURED" if settings.has_alpaca_credentials else "AUTH_REQUIRED",
        "real_order_routing": False,
    }
    try:
        paper_canary = load_alpaca_paper_config_bundle(settings.config_dir)
        checks["alpaca_paper_canary"] = {
            "ok": (not settings.q1_alpaca_paper_enabled or settings.has_alpaca_credentials),
            "enabled": settings.q1_alpaca_paper_enabled,
            "execution_lane": paper_canary.config.execution_lane,
            "source_arm": paper_canary.config.source_arm.value,
            "rest_base_url": paper_canary.config.rest_base_url,
            "credentials": ("CONFIGURED" if settings.has_alpaca_credentials else "AUTH_REQUIRED"),
            "real_order_routing": False,
        }
    except Exception as exc:
        checks["alpaca_paper_canary"] = {
            "ok": False,
            "detail": str(exc),
            "real_order_routing": False,
        }
    checks["secret_file"] = {
        "ok": True,
        "status": "ABSENT" if not (repo_root() / ".env").exists() else "PRESENT_NOT_LOGGED",
    }
    checks["clock_drift"] = {
        "ok": True,
        "status": "SYSTEM_UTC_CLOCK_USED_PHASE0",
    }
    checks["solver"] = {
        "ok": True,
        "status": "NOT_REQUIRED",
        "detail": "The Phase 1 optimizer is disabled.",
    }
    passed = all(bool(item.get("ok")) for item in checks.values())
    _emit({"passed": passed, "checks": checks})
    if not passed:
        raise typer.Exit(1)


@config_app.command("validate")
def validate_config(
    all_configs: bool = typer.Option(False, "--all", help="Validate all immutable configs."),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit paper algorithm config to validate.",
    ),
) -> None:
    settings = Settings.from_env(repo_root())
    selected = _paper_algorithm_version(algorithm_version)
    if all_configs:
        legacy = load_config_bundle(settings.config_dir)
        q1 = load_q1_config_bundle(settings.config_dir)
        alpaca_paper = load_alpaca_paper_config_bundle(settings.config_dir)
        research = load_research_config(settings.config_dir)
        operational_config(q1)
        _emit(
            {
                "valid": True,
                "algorithm_versions": list(SUPPORTED_PAPER_ALGORITHM_VERSIONS),
                "manifest_hash": canonical_hash(
                    {
                        LEGACY_FORWARD_ALGORITHM_VERSION: (legacy.manifest_hash),
                        Q1_ALGORITHM_VERSION: q1.manifest_hash,
                        "ALPACA_PAPER_CANARY": (alpaca_paper.manifest_hash),
                        "ADAPTIVE_RESEARCH_PLANE_V1": (research.manifest_hash),
                    }
                ),
                "files": sorted(
                    {
                        *legacy.documents,
                        Q1_CONFIG_FILE,
                        "costs.yaml",
                        ALPACA_PAPER_CONFIG_FILE,
                        RESEARCH_CONFIG_FILE,
                        FACTORIAL_CONFIG_FILE,
                    }
                ),
                "configs": {
                    LEGACY_FORWARD_ALGORITHM_VERSION: {
                        "manifest_hash": legacy.manifest_hash,
                        "files": sorted(legacy.documents),
                    },
                    Q1_ALGORITHM_VERSION: {
                        "manifest_hash": q1.manifest_hash,
                        "files": [Q1_CONFIG_FILE, "costs.yaml"],
                    },
                    "ALPACA_PAPER_CANARY": {
                        "manifest_hash": alpaca_paper.manifest_hash,
                        "files": [ALPACA_PAPER_CONFIG_FILE],
                    },
                    "ADAPTIVE_RESEARCH_PLANE_V1": {
                        "manifest_hash": research.manifest_hash,
                        "files": [
                            RESEARCH_CONFIG_FILE,
                            FACTORIAL_CONFIG_FILE,
                        ],
                    },
                },
            }
        )
        return
    if selected == Q1_ALGORITHM_VERSION:
        q1 = load_q1_config_bundle(settings.config_dir)
        operational_config(q1)
        manifest_hash = q1.manifest_hash
        files = [Q1_CONFIG_FILE, "costs.yaml"]
    else:
        legacy = load_config_bundle(settings.config_dir)
        manifest_hash = legacy.manifest_hash
        files = sorted(legacy.documents)
    _emit(
        {
            "valid": True,
            "algorithm_version": selected,
            "manifest_hash": manifest_hash,
            "files": files,
        }
    )


@db_app.command("upgrade")
def db_upgrade() -> None:
    settings = Settings.from_env(repo_root())
    upgrade_database(settings.database_url)
    engine = create_database_engine(settings.database_url)
    revision = current_revision(engine)
    engine.dispose()
    _emit({"upgraded": revision == EXPECTED_DATABASE_REVISION, "revision": revision})


@db_app.command("downgrade")
def db_downgrade(
    revision: str = typer.Option(..., "--revision", help="Development-only target revision."),
) -> None:
    settings = Settings.from_env(repo_root())
    if not _is_local_database(settings.database_url):
        raise typer.BadParameter("Downgrade is limited to a local development database")
    downgrade_database(settings.database_url, revision)
    _emit({"downgraded": True, "revision": revision})


@seed_app.command("demo")
def seed_demo_command() -> None:
    settings, config, engine = runtime()
    factory = make_session_factory(engine)
    manifest, result_hash, created = seed_demo(
        settings=settings,
        config=config,
        session_factory=factory,
    )
    engine.dispose()
    _emit(
        {
            "run_id": manifest["run_id"],
            "created": created,
            "result_hash": result_hash,
            "arm_count": len(manifest["arms"]),
        }
    )


@ingest_app.command("synthetic")
def ingest_synthetic(
    scenario: str = typer.Option("demo", "--scenario"),
) -> None:
    if scenario != "demo":
        raise typer.BadParameter("Phase 0 only provides the demo synthetic scenario")
    seed_demo_command()


@run_app.command("cycle")
def run_cycle(
    run_id: str = typer.Option(..., "--run-id"),
    at: str | None = typer.Option(None, "--at"),
) -> None:
    if run_id != "demo_run":
        raise typer.BadParameter("Phase 0 only provides run_id=demo_run")
    if at is not None:
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        require_aware_utc(parsed, "at")
    seed_demo_command()


@app.command("replay")
def replay(
    run_id: str = typer.Option(..., "--run-id"),
    mode: str = typer.Option("full", "--mode"),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit algorithm version; existing replay semantics remain legacy by default.",
    ),
) -> None:
    if mode.lower() != "full":
        raise typer.BadParameter("Phase 0 implements FULL replay only")
    selected = _paper_algorithm_version(algorithm_version)
    _, _, engine = runtime()
    factory = make_session_factory(engine)
    if selected == Q1_ALGORITHM_VERSION:
        q1_result = replay_q1_run(factory, run_id)
        engine.dispose()
        _emit(q1_result.as_payload())
        if not q1_result.passed:
            raise typer.Exit(1)
        return
    result = replay_full(factory, run_id)
    engine.dispose()
    _emit(
        {
            "run_id": result.run_id,
            "mode": result.mode,
            "result_hash": result.result_hash,
            "arm_count": len(result.manifest["arms"]),
        }
    )


@app.command("verify")
def verify(
    run_id: str = typer.Option(..., "--run-id"),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit algorithm version; existing verify semantics remain legacy by default.",
    ),
) -> None:
    selected = _paper_algorithm_version(algorithm_version)
    _, _, engine = runtime()
    factory = make_session_factory(engine)
    if selected == Q1_ALGORITHM_VERSION:
        q1_result = replay_q1_run(factory, run_id)
        engine.dispose()
        _emit(q1_result.as_payload())
        if not q1_result.passed:
            raise typer.Exit(1)
        return
    report = verify_run(factory, run_id)
    engine.dispose()
    _emit(report.as_payload())
    if not report.passed:
        raise typer.Exit(1)


@ledger_app.command("verify")
def ledger_verify(arm: str = typer.Option(..., "--arm")) -> None:
    if arm not in ARM_IDS:
        raise typer.BadParameter(f"Unknown shadow arm: {arm}")
    _, _, engine = runtime()
    report = verify_ledger_arm(make_session_factory(engine), arm)
    engine.dispose()
    _emit(report)
    if not report["balanced"]:
        raise typer.Exit(1)


@policy_app.command("compile")
def policy_compile(
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False)],
    arm: Annotated[str, typer.Option("--arm")],
) -> None:
    payload = json.loads(file.read_text(encoding="utf-8"))
    patch = PolicyPatch.model_validate(payload)
    if patch.arm_scope != arm:
        raise typer.BadParameter("Patch arm_scope differs from --arm")
    state = PolicyCompiler().compile(
        patch,
        PolicyState.default(arm),
        now=patch.effective_from,
        shadow_mode=True,
    )
    _emit(
        {
            "accepted": True,
            "policy_hash": canonical_hash(state.as_payload()),
            "policy": state.as_payload(),
        }
    )


@shadow_app.command("status")
def shadow_status(run_id: str = typer.Option(..., "--run-id")) -> None:
    _, _, engine = runtime()
    factory = make_session_factory(engine)
    with factory() as session:
        run = session.get(RunRow, run_id)
        if run is None:
            raise typer.BadParameter(f"Unknown run_id: {run_id}")
        arms = list(
            session.scalars(
                select(ShadowArmRow)
                .where(ShadowArmRow.run_id == run_id)
                .order_by(ShadowArmRow.arm_id)
            )
        )
        navs = {
            row.arm_id: str(row.nav_usd)
            for row in session.scalars(
                select(NavSnapshotRow).where(NavSnapshotRow.run_id == run_id)
            )
        }
    engine.dispose()
    _emit(
        {
            "run_id": run_id,
            "status": run.status,
            "result_hash": run.result_hash,
            "arms": [{"arm_id": row.arm_id, "nav_usd": navs.get(row.arm_id)} for row in arms],
        }
    )


@stats_app.command("calculate")
def stats_calculate(experiment: str = typer.Option(..., "--experiment")) -> None:
    _emit(
        {
            "experiment": experiment,
            "status": "NOT_REQUIRED_PHASE0",
            "detail": "Promotion statistics start in Phase 4.",
        }
    )


@control_app.command("select")
def control_select(
    provider: Annotated[CommanderProvider, typer.Option("--provider")],
    expected_version: Annotated[
        int | None,
        typer.Option("--expected-version", min=0),
    ] = None,
) -> None:
    _, _, engine = runtime()
    service = ControlPlaneService(make_session_factory(engine))
    selection, changed = service.select_provider(
        provider,
        expected_version=expected_version,
    )
    engine.dispose()
    _emit(
        {
            "changed": changed,
            "selection": selection.model_dump(mode="json"),
        }
    )


@control_app.command("status")
def control_status() -> None:
    settings, _, engine = runtime()
    payload = ControlPlaneService(make_session_factory(engine)).status(
        scope_id=settings.paper_run_id
    )
    engine.dispose()
    _emit(payload)


@control_app.command("schema")
def control_schema() -> None:
    _emit(AdaptivePolicyDecision.model_json_schema())


@control_app.command("request")
def control_request(
    arm: str = typer.Option("B3-RISK", "--arm"),
    context_file: Annotated[
        Path | None,
        typer.Option("--context-file", exists=True, dir_okay=False),
    ] = None,
) -> None:
    context: dict[str, Any] = {}
    if context_file is not None:
        loaded = json.loads(context_file.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise typer.BadParameter("Context JSON root must be an object")
        context = cast(dict[str, Any], loaded)
    settings, _, engine = runtime()
    service = ControlPlaneService(make_session_factory(engine))
    request = service.create_request(
        arm_scope=arm,
        scope_id=settings.paper_run_id,
        context=context,
    )
    commander_dir = settings.commander_dir or (repo_root().parent / "stock-commander")
    bundle = export_request_bundle(request, commander_dir=commander_dir)
    engine.dispose()
    _emit(
        {
            "request": request.model_dump(mode="json"),
            "bundle": bundle.as_payload(),
        }
    )


@control_app.command("submit")
def control_submit(
    request_id: Annotated[str, typer.Option("--request-id")],
    file: Annotated[Path, typer.Option("--file", exists=True, dir_okay=False)],
    provider: Annotated[
        CommanderProvider | None,
        typer.Option("--provider"),
    ] = None,
) -> None:
    payload = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise typer.BadParameter("Decision JSON root must be an object")
    _, _, engine = runtime()
    service = ControlPlaneService(make_session_factory(engine))
    active = service.current_selection()
    resolved_provider = provider or (None if active is None else active.provider)
    if resolved_provider is None:
        engine.dispose()
        raise typer.BadParameter("Select a commander provider first")
    receipt = service.submit_decision(
        request_id=request_id,
        provider=resolved_provider,
        output=cast(dict[str, Any], payload),
    )
    engine.dispose()
    _emit({"receipt": receipt.model_dump(mode="json")})


@control_app.command("run-codex")
def control_run_codex(
    request_id: str = typer.Option(..., "--request-id"),
    timeout_seconds: int = typer.Option(900, "--timeout-seconds", min=30),
) -> None:
    settings, _, engine = runtime()
    if not settings.real_llm_enabled:
        engine.dispose()
        raise typer.BadParameter(
            "Set TRADING_REAL_LLM_ENABLED=true before an explicitly requested model run"
        )
    service = ControlPlaneService(make_session_factory(engine))
    request = service.get_request(request_id)
    if request.provider is not CommanderProvider.CODEX_SOL_MAX:
        engine.dispose()
        raise typer.BadParameter("The request is not bound to Codex Sol Max")
    commander_dir = settings.commander_dir or (repo_root().parent / "stock-commander")
    bundle = export_request_bundle(request, commander_dir=commander_dir)
    output = run_codex_bundle(bundle, timeout_seconds=timeout_seconds)
    receipt = service.submit_decision(
        request_id=request_id,
        provider=CommanderProvider.CODEX_SOL_MAX,
        output=output,
    )
    engine.dispose()
    _emit(
        {
            "receipt": receipt.model_dump(mode="json"),
            "output_file": str(bundle.output_file),
        }
    )


@research_app.command("select")
def research_select(
    commander: Annotated[
        ResearchCommanderKind,
        typer.Option("--commander"),
    ],
    expected_version: Annotated[
        int | None,
        typer.Option("--expected-version", min=0),
    ] = None,
) -> None:
    settings, _, engine = runtime()
    research_config = load_research_config(settings.config_dir)
    repository = ResearchRepository(make_session_factory(engine))
    now = datetime.now(UTC)
    selection = repository.select_commander(
        commander,
        config_hash=research_config.manifest_hash,
        effective_at=now,
        created_at=now,
        expected_version=expected_version,
    )
    engine.dispose()
    _emit(
        {
            "selection": selection.model_dump(mode="json"),
            "real_order_routing": False,
        }
    )


@research_app.command("status")
def research_status() -> None:
    settings, _, engine = runtime()
    _require_research_paper_only(settings)
    factory = make_session_factory(engine)
    research_config = load_research_config(settings.config_dir)
    research_repository = ResearchRepository(factory)
    persisted_status = research_repository.status()
    meta_controller_status = MetaControllerRepository(factory).status()
    portfolio_sharpe_status = research_repository.portfolio_sharpe().status()
    meta_oos_status = MetaOosRepository(factory).status()
    payload = {
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
        "scheduler": ResearchSchedulerService(
            repository=ResearchSchedulerRepository(factory),
            config=research_config,
        ).status(),
        "real_order_routing": False,
    }
    engine.dispose()
    _emit(payload)


@research_outcome_app.command("mature")
def research_outcome_mature(
    input_file: Annotated[
        Path | None,
        typer.Option("--input", exists=True, dir_okay=False),
    ] = None,
    as_of: Annotated[
        str | None,
        typer.Option("--as-of"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--commit"),
    ] = True,
) -> None:
    """List due outcomes or append one host-validated maturity event."""

    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        repository = ResearchRepository(
            make_session_factory(engine)
        ).experiment_outcomes()
        if input_file is None:
            instant = _research_timestamp(as_of, "--as-of")
            due = repository.due_experiments(as_of=instant)
            result = {
                "mode": "READ_ONLY_DUE_OUTCOMES",
                "as_of": instant.isoformat().replace("+00:00", "Z"),
                "due_experiment_ids": [
                    item.experiment_id for item in due
                ],
                "due_action_hashes": [item.action_hash for item in due],
                "dry_run": True,
                "real_order_routing": False,
            }
        else:
            maturation = load_json_model(
                input_file,
                ExperimentOutcomeMaturationInputV1,
            )
            if dry_run:
                event, already_exists = repository.prepare_outcome(maturation)
                created = False
            else:
                event, created = repository.append_outcome(maturation)
                already_exists = not created
            result = {
                "mode": "DRY_RUN" if dry_run else "COMMIT",
                "event": event.model_dump(mode="json"),
                "created": created,
                "already_exists": already_exists,
                "dry_run": dry_run,
                "real_order_routing": False,
            }
    except (ExperimentOutcomePersistenceError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(result)


@research_memory_app.command("materialize")
def research_memory_materialize(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of"),
    ] = None,
    data_available_cutoff: Annotated[
        str | None,
        typer.Option("--data-available-cutoff"),
    ] = None,
    created_at: Annotated[
        str | None,
        typer.Option("--created-at"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--commit"),
    ] = True,
) -> None:
    """Materialize a point-in-time snapshot from the trusted outcome ledger."""

    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    instant = _research_timestamp(as_of, "--as-of")
    cutoff = (
        instant
        if data_available_cutoff is None
        else _research_timestamp(
            data_available_cutoff,
            "--data-available-cutoff",
        )
    )
    created = _research_timestamp(created_at, "--created-at")
    engine = create_database_engine(settings.database_url)
    try:
        repository = ResearchRepository(
            make_session_factory(engine)
        ).experiment_outcomes()
        snapshot, persisted = repository.materialize_memory(
            as_of=instant,
            data_available_cutoff=cutoff,
            created_at=created,
            persist=not dry_run,
        )
        result = {
            "mode": "DRY_RUN" if dry_run else "COMMIT",
            "snapshot": snapshot.model_dump(mode="json"),
            "persisted": persisted,
            "dry_run": dry_run,
            "real_order_routing": False,
        }
    except (ExperimentOutcomePersistenceError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(result)


@research_meta_policy_app.command("build")
def research_meta_policy_build(
    snapshot_id: Annotated[
        str,
        typer.Option("--snapshot-id", min=1, max=160),
    ],
    research_cycle_id: Annotated[
        str,
        typer.Option("--research-cycle-id", min=1, max=160),
    ],
    regime_cluster_id: Annotated[
        str,
        typer.Option("--regime-cluster-id", min=1, max=160),
    ],
    failure_cluster_id: Annotated[
        str,
        typer.Option("--failure-cluster-id", min=1, max=160),
    ],
    portfolio_exposure_cluster_id: Annotated[
        str,
        typer.Option("--portfolio-exposure-cluster-id", min=1, max=160),
    ],
    maximum_total_submissions: Annotated[
        int,
        typer.Option("--maximum-total-submissions", min=1),
    ],
    idempotency_key: Annotated[
        str,
        typer.Option("--idempotency-key", min=1, max=160),
    ],
    actions: Annotated[
        str,
        typer.Option(
            "--actions",
            help="Comma-separated ResearchActionKind values; defaults to all typed actions.",
        ),
    ] = "",
    generated_at: Annotated[
        str | None,
        typer.Option("--generated-at"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--commit"),
    ] = True,
) -> None:
    """Build a deterministic action ranking from one immutable memory snapshot."""

    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    config = load_research_config(settings.config_dir)
    selected_actions = _parse_research_action_kinds(actions)
    context = build_research_context(
        regime_cluster_id=regime_cluster_id,
        failure_cluster_id=failure_cluster_id,
        portfolio_exposure_cluster_id=portfolio_exposure_cluster_id,
    )
    engine = create_database_engine(settings.database_url)
    try:
        repository = MetaControllerRepository(make_session_factory(engine))
        plan, persisted = repository.build_plan(
            research_cycle_id=research_cycle_id,
            snapshot_id=snapshot_id,
            context=context,
            parameters=meta_controller_parameters(config),
            config_hash=config.manifest_hash,
            available_action_kinds=selected_actions,
            maximum_total_submissions=maximum_total_submissions,
            idempotency_key=idempotency_key,
            generated_at=_research_timestamp(
                generated_at,
                "--generated-at",
            ),
            persist=not dry_run,
        )
        result = {
            "mode": "DRY_RUN" if dry_run else "COMMIT",
            "plan": plan.model_dump(mode="json"),
            "persisted": persisted,
            "dry_run": dry_run,
            "automatic_execution_enabled": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    except (
        ExperimentOutcomePersistenceError,
        MetaControllerPersistenceError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(result)


@research_meta_oos_app.command("plan")
def research_meta_oos_plan(
    plan_file: Annotated[
        Path,
        typer.Option("--input", exists=True, dir_okay=False),
    ],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--commit"),
    ] = True,
) -> None:
    """Validate or persist one immutable chronological outer-audit plan."""

    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    config = load_research_config(settings.config_dir)
    engine = create_database_engine(settings.database_url)
    try:
        plan = load_json_model(plan_file, ChronologicalMetaOosPlanV1)
        _validate_meta_oos_plan_config(plan, config)
        persisted = False
        if not dry_run:
            persisted = MetaOosRepository(
                make_session_factory(engine)
            ).store_plan(
                plan,
                meta_oos_evaluation_contract(config),
            )
        result = {
            "mode": "DRY_RUN" if dry_run else "COMMIT",
            "plan": plan.model_dump(mode="json"),
            "persisted": persisted,
            "outer_audit_opened": False,
            "automatic_execution_enabled": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    except (
        MetaOosError,
        MetaOosPersistenceError,
        ResearchFileRuntimeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(result)


@research_meta_oos_app.command("run")
def research_meta_oos_run(
    plan_id: Annotated[
        str,
        typer.Option("--plan-id", min=1, max=160),
    ],
    idempotency_key: Annotated[
        str,
        typer.Option("--idempotency-key", max=160),
    ] = "",
    created_at: Annotated[
        str | None,
        typer.Option("--created-at"),
    ] = None,
    expires_at: Annotated[
        str | None,
        typer.Option("--expires-at"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--commit"),
    ] = True,
) -> None:
    """Inspect readiness or reserve one run for the trusted private service."""

    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    config = load_research_config(settings.config_dir)
    meta_config = config.config.recursive_improvement.meta_oos
    now = _research_timestamp(created_at, "--created-at")
    expiry = (
        now + timedelta(hours=meta_config.reservation_ttl_hours)
        if expires_at is None
        else _research_timestamp(expires_at, "--expires-at")
    )
    engine = create_database_engine(settings.database_url)
    try:
        repository = MetaOosRepository(make_session_factory(engine))
        plan = repository.plan(plan_id)
        if plan is None:
            raise MetaOosPersistenceError("unknown meta-OOS plan")
        _validate_meta_oos_plan_config(plan, config)
        reservation = repository.reservation(plan_id)
        created = False
        if not dry_run:
            if not idempotency_key.strip():
                raise MetaOosPersistenceError(
                    "--idempotency-key is required with --commit"
                )
            reservation, created = repository.reserve_outer_audit(
                plan_id=plan_id,
                idempotency_key=idempotency_key,
                maximum_dataset_uses=(
                    meta_config.maximum_outer_audit_uses_per_dataset
                ),
                maximum_ttl_hours=meta_config.reservation_ttl_hours,
                created_at=now,
                expires_at=expiry,
            )
        result = {
            "mode": "DRY_RUN" if dry_run else "COMMIT",
            "status": (
                "DRY_RUN_READY"
                if dry_run
                else "RESERVED_AWAITING_TRUSTED_ENVIRONMENT"
            ),
            "plan_id": plan.plan_id,
            "plan_hash": plan.plan_hash,
            "audit_mode": plan.audit_mode.value,
            "reservation": (
                None
                if reservation is None
                else reservation.model_dump(mode="json")
            ),
            "reservation_created": created,
            "trusted_service_entrypoint": (
                "trading.research.chronological_meta_oos."
                "run_chronological_meta_oos"
            ),
            "raw_audit_input_accepted_by_cli": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    except (MetaOosPersistenceError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(result)


@research_meta_oos_app.command("verify")
def research_meta_oos_verify(
    plan_id: Annotated[
        str,
        typer.Option("--plan-id", min=1, max=160),
    ],
    result_file: Annotated[
        Path | None,
        typer.Option("--result", exists=True, dir_okay=False),
    ] = None,
) -> None:
    """Read and hash-verify one bounded aggregate result without writing."""

    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    config = load_research_config(settings.config_dir)
    contract = meta_oos_evaluation_contract(config)
    engine = create_database_engine(settings.database_url)
    try:
        repository = MetaOosRepository(make_session_factory(engine))
        plan = repository.plan(plan_id)
        if plan is None:
            raise MetaOosPersistenceError("unknown meta-OOS plan")
        result = (
            repository.result(plan_id)
            if result_file is None
            else load_json_model(
                result_file,
                ChronologicalMetaOosResultV1,
            )
        )
        if result is None:
            raise MetaOosPersistenceError("meta-OOS result is not available")
        verify_chronological_meta_oos_result(
            plan=plan,
            evaluation_contract=contract,
            result=result,
        )
        payload = {
            "verified": True,
            "read_only": True,
            "plan_id": plan.plan_id,
            "plan_hash": plan.plan_hash,
            "result_id": result.result_id,
            "result_hash": result.result_hash,
            "adaptive_system_pass": result.adaptive_system_pass,
            "synthetic_fixture_is_performance_evidence": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    except (
        MetaOosError,
        MetaOosPersistenceError,
        ResearchFileRuntimeError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(payload)


@research_app.command("schedule-plan")
def research_schedule_plan(
    as_of: Annotated[
        str | None,
        typer.Option("--as-of"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        service = ResearchSchedulerService(
            repository=ResearchSchedulerRepository(make_session_factory(engine)),
            config=load_research_config(settings.config_dir),
        )
        result = service.plan(
            as_of=_research_timestamp(as_of, "--as-of"),
        )
    finally:
        engine.dispose()
    _emit(result)


@research_app.command("schedule-work")
def research_schedule_work(
    worker_id: Annotated[
        str,
        typer.Option("--worker-id", min=1, max=120),
    ] = "research-scheduler-cli",
    as_of: Annotated[
        str | None,
        typer.Option("--as-of"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        service = ResearchSchedulerService(
            repository=ResearchSchedulerRepository(make_session_factory(engine)),
            config=load_research_config(settings.config_dir),
        )
        result = service.tick(
            worker_id=worker_id,
            as_of=_research_timestamp(as_of, "--as-of"),
        )
    finally:
        engine.dispose()
    _emit(result)


@research_app.command("shadow-summary-record")
def research_shadow_summary_record(
    summary_file: Annotated[
        Path,
        typer.Option("--summary", exists=True, dir_okay=False),
    ],
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        summary = load_json_model(
            summary_file,
            TrustedShadowPerformanceSummaryV1,
        )
        result = ResearchLifecycleService(
            repository=ResearchRepository(make_session_factory(engine))
        ).record_shadow_performance_summary(summary)
    except (
        ResearchFileRuntimeError,
        ResearchLifecycleError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": result.created,
            "challenger_id": result.challenger_id,
            "status": result.status.value,
            "summary_hash": result.artifact_hash,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    )


@research_app.command("candidate-artifact-register")
def research_candidate_artifact_register(
    artifact_file: Annotated[
        Path,
        typer.Option("--artifact", exists=True, dir_okay=False),
    ],
    registered_at: Annotated[
        str | None,
        typer.Option("--registered-at"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        bundle = load_json_model(
            artifact_file,
            CandidateArtifactBundleV1,
        )
        result = ResearchLifecycleService(
            repository=ResearchRepository(make_session_factory(engine))
        ).register_candidate_artifact(
            bundle,
            created_at=_research_timestamp(
                registered_at,
                "--registered-at",
            ),
        )
    except (
        ResearchFileRuntimeError,
        ResearchLifecycleError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": result.created,
            "challenger_id": result.challenger_id,
            "status": result.status.value,
            "candidate_artifact_hash": result.artifact_hash,
            "real_order_routing": False,
        }
    )


@research_app.command("promotion-evaluate")
def research_promotion_evaluate(
    challenger_id: Annotated[str, typer.Option("--challenger-id")],
    evaluated_at: Annotated[
        str | None,
        typer.Option("--evaluated-at"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        research_config = load_research_config(settings.config_dir)
        result = ResearchLifecycleService(
            repository=ResearchRepository(make_session_factory(engine))
        ).evaluate_trusted_promotion(
            challenger_id=challenger_id,
            contract=research_config.config.promotion.evaluation_contract,
            created_at=_research_timestamp(
                evaluated_at,
                "--evaluated-at",
            ),
        )
    except (ResearchLifecycleError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": result.created,
            "challenger_id": challenger_id,
            "status": result.status.value,
            "evidence": result.evidence.model_dump(mode="json"),
            "evaluation": result.evaluation.model_dump(mode="json"),
            "eligible_requires_manual_approval": (result.status.value == "PROMOTION_ELIGIBLE"),
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    )


@research_app.command("promotion-approve")
def research_promotion_approve(
    challenger_id: Annotated[str, typer.Option("--challenger-id")],
    approved_by: Annotated[str, typer.Option("--approved-by")],
    approved_at: Annotated[
        str | None,
        typer.Option("--approved-at"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        result = ResearchLifecycleService(
            repository=ResearchRepository(make_session_factory(engine))
        ).approve_trusted_promotion(
            challenger_id=challenger_id,
            approved_by=approved_by,
            created_at=_research_timestamp(approved_at, "--approved-at"),
        )
    except (ResearchLifecycleError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": result.created,
            "challenger_id": challenger_id,
            "status": result.status.value,
            "manual_approval": result.decision.model_dump(mode="json"),
            "champion_designated": False,
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    )


@research_app.command("champion-designate")
def research_champion_designate(
    challenger_id: Annotated[str, typer.Option("--challenger-id")],
    expected_current_version: Annotated[
        str,
        typer.Option("--expected-current-version"),
    ],
    designated_by: Annotated[str, typer.Option("--designated-by")],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key")],
    designated_at: Annotated[
        str | None,
        typer.Option("--designated-at"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        result = ResearchLifecycleService(
            repository=ResearchRepository(make_session_factory(engine))
        ).designate_champion(
            challenger_id=challenger_id,
            expected_current_version=expected_current_version,
            designated_by=designated_by,
            idempotency_key=idempotency_key,
            designated_at=_research_timestamp(
                designated_at,
                "--designated-at",
            ),
        )
    except (ResearchLifecycleError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": result.created,
            "challenger_id": challenger_id,
            "status": result.status.value,
            "current_champion": result.designation.model_dump(mode="json"),
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    )


@research_app.command("schema")
def research_schema() -> None:
    _emit(
        {
            "request": ResearchRequestV1.model_json_schema(),
            "decision": ResearchDecisionV1.model_json_schema(),
            "research_request_v2": ResearchRequestV2.model_json_schema(),
            "research_decision_v2": ResearchDecisionV2.model_json_schema(),
            "research_action_plan_v1": (
                ResearchActionPlanV1.model_json_schema()
            ),
            "candidate_artifact": (CandidateArtifactBundleV1.model_json_schema()),
            "algorithm_proposal_v2": AlgorithmProposalV2.model_json_schema(),
            "experiment_outcome_event_v1": (
                ExperimentOutcomeEventV1.model_json_schema()
            ),
            "experiment_outcome_maturation_input_v1": (
                ExperimentOutcomeMaturationInputV1.model_json_schema()
            ),
            "research_experiment_action_v1": (
                ResearchExperimentActionV1.model_json_schema()
            ),
            "research_memory_snapshot_v1": (
                ResearchMemorySnapshotV1.model_json_schema()
            ),
            "trusted_shadow_summary": (TrustedShadowPerformanceSummaryV1.model_json_schema()),
            "promotion_evidence": PromotionEvidenceV1.model_json_schema(),
            "trusted_promotion_evaluation": (TrustedPromotionEvaluationV1.model_json_schema()),
            "champion_designation": ChampionDesignationV1.model_json_schema(),
            "automatic_promotion_enabled": False,
            "real_order_routing": False,
        }
    )


@research_app.command("scout")
def research_scout(
    request_file: Annotated[
        Path,
        typer.Option("--request", exists=True, dir_okay=False),
    ],
    output_file: Annotated[
        Path | None,
        typer.Option(
            "--output",
            dir_okay=False,
            help="Optional normalized evidence output; must remain local/ignored.",
        ),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    if not settings.real_llm_enabled:
        raise typer.BadParameter(
            "Set TRADING_REAL_LLM_ENABLED=true before an explicitly requested Web Scout run"
        )
    try:
        request = load_json_model(request_file, WebScoutRequestV1)
        config = WebGptScoutConfig.from_env()
        artifact_root = resolve_local_output(
            config.artifact_root,
            repository_root=repo_root(),
        )
        raw_object_root = resolve_local_output(
            config.raw_object_root,
            repository_root=repo_root(),
        )
        config = replace(
            config,
            artifact_root=artifact_root,
            raw_object_root=raw_object_root,
        )
        bundle = WebGptActiveResearchScout(config).scout(request)
        result_file = (
            artifact_root
            / request.research_cycle_id
            / request.request_id
            / request.role
            / "result.json"
        )
        if output_file is not None:
            result_file = resolve_local_output(
                output_file,
                repository_root=repo_root(),
            )
            atomic_write_json(result_file, bundle)
    except (ResearchFileRuntimeError, WebGptScoutError, ValueError) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    _emit(
        {
            "ok": True,
            "request_id": request.request_id,
            "research_cycle_id": request.research_cycle_id,
            "evidence_bundle_hash": canonical_hash(bundle),
            "source_count": len(bundle.sources),
            "claim_count": len(bundle.claims),
            "output": local_artifact_label(
                result_file,
                repository_root=repo_root(),
            ),
            "model_family": bundle.model_family,
            "reasoning_profile": bundle.reasoning_profile,
            "real_order_routing": False,
        }
    )


@research_app.command("commander-run")
def research_commander_run(
    request_file: Annotated[
        Path,
        typer.Option("--request", exists=True, dir_okay=False),
    ],
    bundle_root: Annotated[
        Path,
        typer.Option(
            "--bundle-root",
            file_okay=False,
            help="Local ignored prepared-cycle root.",
        ),
    ] = Path(".local/research/runs"),
    prior_conversation_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--prior-conversation-id",
            help="Conversation ID that this fresh role invocation must not reuse.",
        ),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    if not settings.real_llm_enabled:
        raise typer.BadParameter(
            "Set TRADING_REAL_LLM_ENABLED=true before an explicitly requested "
            "WebGPT Research Commander run"
        )
    engine = create_database_engine(settings.database_url)
    try:
        request = load_research_request(request_file)
        config = WebGptScoutConfig.from_env()
        artifact_root = resolve_local_output(
            config.artifact_root,
            repository_root=repo_root(),
        )
        raw_object_root = resolve_local_output(
            config.raw_object_root,
            repository_root=repo_root(),
        )
        resolved_bundle_root = resolve_local_output(
            bundle_root,
            repository_root=repo_root(),
        )
        config = replace(
            config,
            artifact_root=artifact_root,
            raw_object_root=raw_object_root,
        )
        repository = ResearchRepository(make_session_factory(engine))
        commander = WebGptActiveResearchCommander(
            config=config,
            selection_provider=repository.current_selection,
        )
        decision = commander.command(
            request,
            prior_conversation_ids=prior_conversation_ids or (),
        )
        current_selection = repository.current_selection()
        if current_selection is None:
            raise ResearchFileRuntimeError(
                "Research Commander selection disappeared before cycle output"
            )
        output_path, created = write_cycle_decision(
            bundle_root=resolved_bundle_root,
            request=request,
            decision=decision,
            current_selection=current_selection,
        )
    except (
        ResearchFileRuntimeError,
        ResearchPersistenceError,
        WebGptCommanderError,
        WebGptScoutError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "completed": True,
            "created": created,
            "request_id": decision.request_id,
            "research_cycle_id": decision.research_cycle_id,
            "decision": decision.decision.value,
            "decision_hash": decision.output_hash,
            "output": local_artifact_label(
                output_path,
                repository_root=repo_root(),
            ),
            "model_family": "GPT-5.6 Sol Pro",
            "reasoning_profile": "xhigh",
            "api_fallback_used": False,
            "real_order_routing": False,
        }
    )


@research_app.command("cycle-prepare")
def research_cycle_prepare(
    request_file: Annotated[
        Path,
        typer.Option("--request", exists=True, dir_okay=False),
    ],
    bundle_root: Annotated[
        Path,
        typer.Option(
            "--bundle-root",
            file_okay=False,
            help="Local ignored request-bundle root.",
        ),
    ] = Path(".local/research/runs"),
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    resolved_root = resolve_local_output(
        bundle_root,
        repository_root=repo_root(),
    )
    try:
        file_runtime = ResearchPlaneFileRuntime(
            repository=ResearchRepository(make_session_factory(engine)),
            bundle_root=resolved_root,
        )
        request, cycle_root = file_runtime.prepare_cycle(request_file)
    except (
        ResearchFileRuntimeError,
        ResearchHostError,
        ResearchPersistenceError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "prepared": True,
            "request_id": request.request_id,
            "research_cycle_id": request.research_cycle_id,
            "context_manifest_hash": request.context_manifest_hash,
            "bundle": local_artifact_label(
                cycle_root,
                repository_root=repo_root(),
            ),
            "real_order_routing": False,
        }
    )


@research_app.command("evidence-import")
def research_evidence_import(
    request_file: Annotated[
        Path,
        typer.Option("--request", exists=True, dir_okay=False),
    ],
    evidence_file: Annotated[
        Path,
        typer.Option("--evidence", exists=True, dir_okay=False),
    ],
    imported_at: Annotated[
        str | None,
        typer.Option("--imported-at"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        file_runtime = ResearchPlaneFileRuntime(
            repository=ResearchRepository(make_session_factory(engine)),
            bundle_root=resolve_local_output(
                Path(".local/research/runs"),
                repository_root=repo_root(),
            ),
        )
        evidence, created = file_runtime.import_evidence(
            request_file=request_file,
            evidence_file=evidence_file,
            imported_at=_research_timestamp(imported_at, "--imported-at"),
        )
    except (
        ResearchFileRuntimeError,
        ResearchHostError,
        ResearchPersistenceError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": created,
            "research_cycle_id": evidence.research_cycle_id,
            "evidence_bundle_hash": canonical_hash(evidence),
            "source_count": len(evidence.sources),
            "claim_count": len(evidence.claims),
            "real_order_routing": False,
        }
    )


@research_app.command("decision-import")
def research_decision_import(
    request_file: Annotated[
        Path,
        typer.Option("--request", exists=True, dir_okay=False),
    ],
    decision_file: Annotated[
        Path,
        typer.Option("--decision", exists=True, dir_okay=False),
    ],
    catalog_file: Annotated[
        Path,
        typer.Option("--catalog", exists=True, dir_okay=False),
    ],
    evidence_file: Annotated[
        Path,
        typer.Option("--evidence", exists=True, dir_okay=False),
    ],
    received_at: Annotated[
        str | None,
        typer.Option("--received-at"),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        file_runtime = ResearchPlaneFileRuntime(
            repository=ResearchRepository(make_session_factory(engine)),
            bundle_root=resolve_local_output(
                Path(".local/research/runs"),
                repository_root=repo_root(),
            ),
        )
        decision, proposal_id = file_runtime.import_decision(
            request_file=request_file,
            decision_file=decision_file,
            catalog_file=catalog_file,
            evidence_file=evidence_file,
            received_at=_research_timestamp(received_at, "--received-at"),
        )
    except (
        ResearchFileRuntimeError,
        ResearchHostError,
        ResearchPersistenceError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "accepted": True,
            "request_id": decision.request_id,
            "research_cycle_id": decision.research_cycle_id,
            "decision": decision.decision.value,
            "decision_hash": decision.output_hash,
            "proposal_id": proposal_id,
            "real_order_routing": False,
        }
    )


@research_app.command("challenger-register")
def research_challenger_register(
    decision_file: Annotated[
        Path,
        typer.Option("--decision", exists=True, dir_okay=False),
    ],
    manifest_file: Annotated[
        Path,
        typer.Option("--manifest", exists=True, dir_okay=False),
    ],
) -> None:
    settings = Settings.from_env(repo_root())
    _require_research_paper_only(settings)
    engine = create_database_engine(settings.database_url)
    try:
        file_runtime = ResearchPlaneFileRuntime(
            repository=ResearchRepository(make_session_factory(engine)),
            bundle_root=resolve_local_output(
                Path(".local/research/runs"),
                repository_root=repo_root(),
            ),
        )
        manifest, proposal, created = file_runtime.register_challenger(
            decision_file=decision_file,
            manifest_file=manifest_file,
        )
    except (
        ResearchFileRuntimeError,
        ResearchHostError,
        ResearchPersistenceError,
        ValueError,
    ) as exc:
        raise typer.BadParameter(_safe_research_error(exc)) from None
    finally:
        engine.dispose()
    _emit(
        {
            "created": created,
            "challenger_id": manifest.challenger_id,
            "strategy_id": manifest.strategy_id,
            "strategy_version": manifest.strategy_version,
            "proposal_id": proposal.proposal_id,
            "manifest_hash": manifest.manifest_hash,
            "real_order_routing": False,
        }
    )


@webgpt_app.command("doctor")
def webgpt_doctor() -> None:
    adapter = WebGptNewsAdapter(WebGptAdapterConfig.from_env(repo_root()))
    try:
        _emit(adapter.doctor())
    except WebGptAdapterError as exc:
        _emit({"ok": False, "error": {"code": exc.code, "detail": exc.detail}})
        raise typer.Exit(1) from None


@webgpt_app.command("schema")
def webgpt_schema() -> None:
    _emit(
        {
            "request": WebGptNewsRequest.model_json_schema(),
            "result": WebGptNewsResult.model_json_schema(),
        }
    )


@webgpt_app.command("analyze")
def webgpt_analyze(
    request_file: Annotated[
        Path,
        typer.Option("--request-file", exists=True, dir_okay=False),
    ],
    artifact_root: Annotated[
        Path | None,
        typer.Option("--artifact-root", file_okay=False),
    ] = None,
    timeout_seconds: Annotated[
        int | None,
        typer.Option("--timeout-seconds", min=30),
    ] = None,
) -> None:
    settings = Settings.from_env(repo_root())
    if not settings.real_llm_enabled:
        raise typer.BadParameter(
            "Set TRADING_REAL_LLM_ENABLED=true before an explicitly requested WebGPT run"
        )
    payload = json.loads(request_file.read_text(encoding="utf-8"))
    request = WebGptNewsRequest.model_validate(payload)
    config = WebGptAdapterConfig.from_env(repo_root())
    if artifact_root is not None:
        config = replace(config, artifact_root=artifact_root.resolve())
    if timeout_seconds is not None:
        config = replace(config, poll_timeout_seconds=timeout_seconds)
    adapter = WebGptNewsAdapter(config)
    try:
        result = adapter.analyze(request)
    except WebGptAdapterError as exc:
        _emit({"ok": False, "error": {"code": exc.code, "detail": exc.detail}})
        raise typer.Exit(1) from None
    _emit(
        {
            "ok": True,
            "request_id": result.request_id,
            "result": result.model_dump(mode="json"),
            "artifact_dir": str(config.resolved_artifact_root / result.request_id),
        }
    )


@ui_app.command("serve")
def ui_serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit paper algorithm shown by the operator UI.",
    ),
) -> None:
    host = _require_loopback_host(host)
    import uvicorn

    from trading.ui.app import create_app

    settings = Settings.from_env(repo_root())
    active = replace(
        settings,
        paper_algorithm_version=_paper_algorithm_version(algorithm_version),
    )
    uvicorn.run(
        create_app(settings=active),
        host=host,
        port=port,
        log_level="info",
    )


@paper_app.command("init")
def paper_init(
    run_id: str | None = typer.Option(None, "--run-id"),
    account_file: Annotated[
        Path | None,
        typer.Option("--account-file", dir_okay=False),
    ] = None,
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit paper algorithm version; legacy remains the default.",
    ),
) -> None:
    selected = _paper_algorithm_version(algorithm_version)
    settings = replace(
        Settings.from_env(repo_root()),
        paper_algorithm_version=selected,
    )
    engine = create_database_engine(settings.database_url)
    resolved_run_id = run_id or settings.paper_run_id
    resolved_account = account_file or settings.paper_account_file
    if resolved_account is None:
        engine.dispose()
        raise typer.BadParameter("A paper account file is required")
    factory = make_session_factory(engine)
    if selected == Q1_ALGORITHM_VERSION:
        q1_config = load_q1_config_bundle(settings.config_dir)
        alpaca_paper_config = load_alpaca_paper_config_bundle(settings.config_dir)
        result = Q1PaperRuntimeService(
            factory,
            config=q1_config,
            workspace_root=repo_root(),
            alpaca_paper_enabled=settings.q1_alpaca_paper_enabled,
            alpaca_paper_config=alpaca_paper_config,
        ).initialize(
            run_id=resolved_run_id,
            account_file=resolved_account,
        )
    else:
        legacy_config = load_config_bundle(settings.config_dir)
        result = PaperRuntimeService(
            factory,
            config=legacy_config,
            workspace_root=repo_root(),
        ).initialize(
            run_id=resolved_run_id,
            account_file=resolved_account,
        )
    engine.dispose()
    _emit(
        {
            "run_id": result.run_id,
            "account_spec_id": result.account_spec_id,
            "created": result.created,
            "state": result.state,
            "algorithm_version": selected,
            "real_order_routing": False,
        }
    )


@paper_app.command("status")
def paper_status(
    run_id: str | None = typer.Option(None, "--run-id"),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit paper algorithm version; legacy remains the default.",
    ),
) -> None:
    selected = _paper_algorithm_version(algorithm_version)
    settings = replace(
        Settings.from_env(repo_root()),
        paper_algorithm_version=selected,
    )
    engine = create_database_engine(settings.database_url)
    resolved_run_id = run_id or settings.paper_run_id
    factory = make_session_factory(engine)
    if selected == Q1_ALGORITHM_VERSION:
        alpaca_paper_config = load_alpaca_paper_config_bundle(settings.config_dir)
        payload = Q1PaperRuntimeService(
            factory,
            config=load_q1_config_bundle(settings.config_dir),
            workspace_root=repo_root(),
            alpaca_paper_enabled=settings.q1_alpaca_paper_enabled,
            alpaca_paper_config=alpaca_paper_config,
        ).status(resolved_run_id)
    else:
        payload = PaperRuntimeService(
            factory,
            config=load_config_bundle(settings.config_dir),
            workspace_root=repo_root(),
        ).status(resolved_run_id)
    payload["scheduler"] = PaperCycleStore(factory).status(resolved_run_id)
    engine.dispose()
    _emit(payload)


@paper_app.command("tick")
def paper_tick(
    run_id: str | None = typer.Option(None, "--run-id"),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit paper algorithm version; legacy remains the default.",
    ),
) -> None:
    selected = _paper_algorithm_version(algorithm_version)
    settings = Settings.from_env(repo_root())
    resolved_settings = replace(
        settings,
        paper_runtime_enabled=True,
        paper_run_id=run_id or settings.paper_run_id,
        paper_algorithm_version=selected,
    )
    if resolved_settings.paper_account_file is None:
        raise typer.BadParameter("A paper account file is required")
    engine = create_database_engine(resolved_settings.database_url)
    credentials = _alpaca_credentials(resolved_settings)
    factory = make_session_factory(engine)
    raw_store = ImmutableRawStore(resolved_settings.raw_store)
    reference = AlpacaReferenceClient(
        credentials=credentials,
        raw_store=raw_store,
        trading_base_url=resolved_settings.alpaca_trading_url,
        data_base_url=resolved_settings.alpaca_data_url,
    )
    alpaca_paper_canary: Q1AlpacaPaperCanaryService | None = None
    if selected == Q1_ALGORITHM_VERSION:
        q1_config = load_q1_config_bundle(resolved_settings.config_dir)
        alpaca_paper_config = load_alpaca_paper_config_bundle(resolved_settings.config_dir)
        market_config = load_config_bundle(resolved_settings.config_dir)
        q1_paper = Q1PaperRuntimeService(
            factory,
            config=q1_config,
            workspace_root=repo_root(),
            alpaca_paper_enabled=(resolved_settings.q1_alpaca_paper_enabled),
            alpaca_paper_config=alpaca_paper_config,
        )
        if resolved_settings.q1_alpaca_paper_enabled:
            alpaca_paper_canary = Q1AlpacaPaperCanaryService(
                factory,
                q1_config=q1_config,
                paper_config=alpaca_paper_config,
                client=AlpacaPaperTradingClient(
                    credentials=credentials,
                    base_url=alpaca_paper_config.config.rest_base_url,
                    timeout_seconds=(alpaca_paper_config.config.request_timeout_seconds),
                    reconciliation_lookup_attempts=(
                        alpaca_paper_config.config.reconciliation_lookup_attempts
                    ),
                    reconciliation_lookup_interval_seconds=(
                        alpaca_paper_config.config.reconciliation_lookup_interval_seconds
                    ),
                ),
                workspace_root=repo_root(),
            )
        q1_news_pipeline: PaperNewsPipeline | None = None
        if resolved_settings.webgpt_enabled:
            if not resolved_settings.real_llm_enabled:
                engine.dispose()
                raise typer.BadParameter("Q1 WebGPT analysis requires TRADING_REAL_LLM_ENABLED")
            q1_news_pipeline = PaperNewsPipeline(
                factory,
                settings=resolved_settings,
                symbols=market_data_symbols(market_config),
                repo_root=repo_root(),
            )
            q1_news_pipeline.doctor()

        q1_news_refresher = (
            None if q1_news_pipeline is None else BackgroundPaperNewsRefresher(q1_news_pipeline)
        )

        worker: PaperRuntimeWorker | Q1PaperRuntimeWorker = Q1PaperRuntimeWorker(
            settings=resolved_settings,
            config=q1_config,
            paper=q1_paper,
            cycles=PaperCycleStore(factory),
            processor=Q1PaperCycleProcessor(
                factory,
                runtime=q1_paper,
                account_file=resolved_settings.paper_account_file,
                workspace_root=repo_root(),
                llm_overlay_provider=(
                    Q1SelectedCommanderProvider(
                        factory,
                        settings=resolved_settings,
                        transport_config=llm_transport_config(q1_config),
                        repo_root=repo_root(),
                    )
                    if resolved_settings.real_llm_enabled
                    else None
                ),
                llm_news_refresher=(q1_news_refresher),
            ),
            reference_client=reference,
            alpaca_paper_canary=alpaca_paper_canary,
        )
    else:
        legacy_config = load_config_bundle(resolved_settings.config_dir)
        worker = PaperRuntimeWorker(
            settings=resolved_settings,
            config=legacy_config,
            paper=PaperRuntimeService(
                factory,
                config=legacy_config,
                workspace_root=repo_root(),
            ),
            cycles=PaperCycleStore(factory),
            reference_client=reference,
        )

    async def run() -> dict[str, Any]:
        try:
            worker.initialize()
            await worker.sync_calendar()
            result = await asyncio.to_thread(worker.tick)
            if isinstance(worker, Q1PaperRuntimeWorker):
                result["alpaca_paper_canary"] = await worker.sync_alpaca_paper()
            return result
        finally:
            await reference.aclose()
            if alpaca_paper_canary is not None:
                await alpaca_paper_canary.aclose()

    try:
        payload = asyncio.run(run())
    finally:
        engine.dispose()
    _emit(payload)


@paper_app.command("serve")
def paper_serve(
    run_id: str | None = typer.Option(None, "--run-id"),
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    algorithm_version: str = typer.Option(
        LEGACY_FORWARD_ALGORITHM_VERSION,
        "--algorithm-version",
        help="Explicit paper algorithm version; legacy remains the default.",
    ),
    enable_ai: bool = typer.Option(
        False,
        "--enable-ai",
        help=(
            "Enable the fail-closed WebGPT news analyst and Codex Sol Max "
            "policy commander for this process."
        ),
    ),
) -> None:
    host = _require_loopback_host(host)
    import uvicorn

    from trading.ui.app import create_app

    settings = Settings.from_env(repo_root())
    selected = _paper_algorithm_version(algorithm_version)
    active = replace(
        settings,
        paper_runtime_enabled=True,
        paper_run_id=run_id or settings.paper_run_id,
        paper_algorithm_version=selected,
        webgpt_enabled=settings.webgpt_enabled or enable_ai,
        real_llm_enabled=settings.real_llm_enabled or enable_ai,
    )
    uvicorn.run(create_app(settings=active), host=host, port=port, log_level="info")


@market_app.command("status")
def market_status(
    symbol: str = typer.Option("SOXL", "--symbol"),
) -> None:
    settings, config, engine = runtime()
    factory = make_session_factory(engine)
    snapshot = LiveMarketSnapshotService(
        factory,
        settings=settings,
        config=config,
    ).snapshot(symbol=symbol, limit=1)
    engine.dispose()
    _emit(snapshot)


@market_app.command("backfill")
def market_backfill(
    lookback_minutes: int = typer.Option(2880, "--lookback-minutes", min=2),
) -> None:
    settings, config, engine = runtime()
    if not settings.market_data_enabled:
        engine.dispose()
        raise typer.BadParameter("Set TRADING_MARKET_DATA_ENABLED=true")
    credentials = _alpaca_credentials(settings)
    raw_store = ImmutableRawStore(settings.raw_store)
    stream_plan = _stream_plan(settings, config)
    rest_client = AlpacaRestClient(
        credentials=credentials,
        raw_store=raw_store,
        base_url=settings.alpaca_data_url,
    )
    worker = AlpacaMarketWorker(
        repository=MarketDataRepository(make_session_factory(engine)),
        rest_client=rest_client,
        stream_client=AlpacaStreamClient(
            credentials=credentials,
            symbols=market_data_symbols(config),
            raw_store=raw_store,
            trade_symbols=stream_plan.trades,
            quote_symbols=stream_plan.quotes,
            bar_symbols=stream_plan.bars,
            updated_bar_symbols=stream_plan.updated_bars,
            url=settings.alpaca_stream_url,
        ),
        symbols=market_data_symbols(config),
        heartbeat_interval_seconds=settings.market_heartbeat_seconds,
    )

    async def run() -> dict[str, Any]:
        try:
            result = await worker.backfill_once(lookback=timedelta(minutes=lookback_minutes))
            return {
                "start": result.start.isoformat(),
                "end": result.end.isoformat(),
                "inserted": {
                    "bars": result.counts.bars,
                    "quotes": result.counts.quotes,
                    "trades": result.counts.trades,
                },
            }
        finally:
            await rest_client.aclose()

    try:
        payload = asyncio.run(run())
    finally:
        engine.dispose()
    _emit(payload)


@market_app.command("history")
def market_history(
    lookback_days: int = typer.Option(500, "--lookback-days", min=100, max=3650),
) -> None:
    settings, config, engine = runtime()
    credentials = _alpaca_credentials(settings)
    raw_store = ImmutableRawStore(settings.raw_store)
    rest_client = AlpacaRestClient(
        credentials=credentials,
        raw_store=raw_store,
        base_url=settings.alpaca_data_url,
    )
    service = MarketHistoryService(
        repository=MarketDataRepository(make_session_factory(engine)),
        client=rest_client,
        symbols=market_data_symbols(config),
    )

    async def run() -> dict[str, Any]:
        try:
            result = await service.backfill_daily(lookback_days=lookback_days)
            return {
                "timeframe": result.timeframe,
                "start": result.start.isoformat(),
                "end": result.end.isoformat(),
                "fetched": result.fetched,
                "inserted": result.inserted,
            }
        finally:
            await rest_client.aclose()

    try:
        payload = asyncio.run(run())
    finally:
        engine.dispose()
    _emit(payload)


@market_app.command("calendar")
def market_calendar(
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
) -> None:
    settings, _, engine = runtime()
    credentials = _alpaca_credentials(settings)
    client = AlpacaReferenceClient(
        credentials=credentials,
        raw_store=ImmutableRawStore(settings.raw_store),
        trading_base_url=settings.alpaca_trading_url,
        data_base_url=settings.alpaca_data_url,
    )

    async def run() -> dict[str, Any]:
        try:
            sessions = await client.fetch_calendar(
                start=date.fromisoformat(start),
                end=date.fromisoformat(end),
            )
            return {
                "sessions": [
                    {
                        "date": item.session_date.isoformat(),
                        "open_at": item.open_at.isoformat(),
                        "close_at": item.close_at.isoformat(),
                        "payload_hash": item.payload_hash,
                    }
                    for item in sessions
                ]
            }
        finally:
            await client.aclose()

    try:
        payload = asyncio.run(run())
    finally:
        engine.dispose()
    _emit(payload)


@market_app.command("corporate-actions")
def market_corporate_actions(
    start: str = typer.Option(..., "--start"),
    end: str = typer.Option(..., "--end"),
) -> None:
    settings, config, engine = runtime()
    credentials = _alpaca_credentials(settings)
    client = AlpacaReferenceClient(
        credentials=credentials,
        raw_store=ImmutableRawStore(settings.raw_store),
        trading_base_url=settings.alpaca_trading_url,
        data_base_url=settings.alpaca_data_url,
    )

    async def run() -> dict[str, Any]:
        try:
            actions = await client.fetch_corporate_actions(
                symbols=market_data_symbols(config),
                start=date.fromisoformat(start),
                end=date.fromisoformat(end),
            )
            return {
                "actions": [
                    {
                        "action_id": item.action_id,
                        "action_type": item.action_type,
                        "symbol": item.symbol,
                        "process_date": (
                            None if item.process_date is None else item.process_date.isoformat()
                        ),
                        "available_at": item.available_at.isoformat(),
                        "payload_hash": item.payload_hash,
                    }
                    for item in actions
                ]
            }
        finally:
            await client.aclose()

    try:
        payload = asyncio.run(run())
    finally:
        engine.dispose()
    _emit(payload)


@market_app.command("stream")
def market_stream() -> None:
    settings, config, engine = runtime()
    if not settings.market_data_enabled:
        engine.dispose()
        raise typer.BadParameter("Set TRADING_MARKET_DATA_ENABLED=true")
    credentials = _alpaca_credentials(settings)
    raw_store = ImmutableRawStore(settings.raw_store)
    stream_plan = _stream_plan(settings, config)
    worker = AlpacaMarketWorker(
        repository=MarketDataRepository(make_session_factory(engine)),
        rest_client=AlpacaRestClient(
            credentials=credentials,
            raw_store=raw_store,
            base_url=settings.alpaca_data_url,
        ),
        stream_client=AlpacaStreamClient(
            credentials=credentials,
            symbols=market_data_symbols(config),
            raw_store=raw_store,
            trade_symbols=stream_plan.trades,
            quote_symbols=stream_plan.quotes,
            bar_symbols=stream_plan.bars,
            updated_bar_symbols=stream_plan.updated_bars,
            url=settings.alpaca_stream_url,
        ),
        symbols=market_data_symbols(config),
        heartbeat_interval_seconds=settings.market_heartbeat_seconds,
    )
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


def _is_local_database(database_url: str) -> bool:
    normalized = database_url.lower()
    return normalized.startswith("sqlite") or any(
        host in normalized for host in ("127.0.0.1", "localhost")
    )


def _stream_plan(
    settings: Settings,
    config: ConfigBundle,
) -> IexStreamSubscriptionPlan:
    required = (
        ()
        if settings.paper_account_file is None
        else tuple(
            position.symbol
            for position in load_paper_account_spec(settings.paper_account_file).positions
        )
    )
    return basic_iex_stream_plan(config, required_quote_symbols=required)


def _alpaca_credentials(settings: Settings) -> AlpacaCredentials:
    if not settings.has_alpaca_credentials:
        raise typer.BadParameter(
            "Set APCA_API_KEY_ID and APCA_API_SECRET_KEY for Alpaca IEX market data"
        )
    return AlpacaCredentials(
        key_id=settings.alpaca_key_id or "",
        secret_key=settings.alpaca_secret_key or "",
    )


def _research_timestamp(value: str | None, option_name: str) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return require_aware_utc(parsed, option_name)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{option_name} must be an ISO-8601 timestamp with a UTC offset"
        ) from exc


def _parse_research_action_kinds(
    value: str,
) -> tuple[ResearchActionKind, ...]:
    raw = tuple(item.strip() for item in value.split(",") if item.strip())
    if not raw:
        return tuple(
            action
            for action in ResearchActionKind
            if action is not ResearchActionKind.UNKNOWN_LEGACY
        )
    try:
        actions = tuple(ResearchActionKind(item) for item in raw)
    except ValueError as exc:
        raise typer.BadParameter(
            "--actions contains an unknown ResearchActionKind"
        ) from exc
    if (
        ResearchActionKind.UNKNOWN_LEGACY in actions
        or len(set(actions)) != len(actions)
    ):
        raise typer.BadParameter(
            "--actions must contain unique typed non-legacy actions"
        )
    return actions


def _validate_meta_oos_plan_config(
    plan: ChronologicalMetaOosPlanV1,
    config: ResearchConfigBundle,
) -> None:
    configured = config.config.recursive_improvement.meta_oos
    contract = meta_oos_evaluation_contract(config)
    if (
        plan.plan_version != configured.plan_version
        or plan.evaluation_contract_hash != contract.contract_hash
        or len(plan.epochs) < configured.minimum_epochs
        or len(plan.epochs) > configured.maximum_epochs
        or plan.outer_audit_budget_ordinal
        > configured.maximum_outer_audit_uses_per_dataset
        or any(
            epoch.candidate_generation_budget
            > configured.maximum_candidate_generation_budget_per_epoch
            or epoch.oos_budget
            > configured.maximum_oos_budget_per_epoch
            for epoch in plan.epochs
        )
    ):
        raise ValueError("meta-OOS plan violates versioned configuration")


def _require_research_paper_only(settings: Settings) -> None:
    if settings.real_broker_enabled or settings.production_unlock:
        raise typer.BadParameter(
            "Research Plane requires both real broker routing and production unlock "
            "to remain disabled"
        )


def _safe_research_error(exc: Exception) -> str:
    if isinstance(exc, WebGptScoutError):
        return f"{exc.code}: Web Scout failed closed"
    if isinstance(exc, ValueError):
        return "versioned Research artifact validation failed"
    detail = " ".join(str(exc).split())
    return detail[:500] or exc.__class__.__name__


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
