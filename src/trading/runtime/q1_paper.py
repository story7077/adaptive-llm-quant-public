from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.algorithm import Q1_ALGORITHM_VERSION
from trading.domain.hashing import canonical_data, canonical_hash, stable_id
from trading.domain.q1 import (
    MarketCalendarSession,
    Q1ArmId,
    Q1StrategyDecision,
    StrategyDailyResult,
)
from trading.domain.time import Clock, SystemClock, require_aware_utc
from trading.evaluation.matched import (
    DailyEvaluationObservation,
    EvaluationConfig,
    evaluate_performance,
)
from trading.execution.order_state import pending_orders
from trading.persistence.models import (
    MarketCalendarSessionRow,
    MatchedAttributionResultRow,
    NavSnapshotRow,
    PortfolioDecisionRow,
    RunRow,
    ShadowArmRow,
    StrategyDailyResultRow,
    StrategyEvaluationAnchorRow,
)
from trading.persistence.paper import PaperBootstrapService, ProvisionedPaperAccount
from trading.persistence.q1 import (
    MarketCalendarSessionRepository,
    RiskEpisodeRepository,
)
from trading.persistence.q1_runtime import (
    latest_arm_state,
    load_q1_order_book,
)
from trading.quant.config import Q1MathConfig, parse_q1_math_config
from trading.runtime.provenance import workspace_code_version
from trading.runtime.q1_alpaca_paper import (
    alpaca_paper_canary_status,
)
from trading.runtime.q1_config import evaluation_config
from trading.runtime.q1_scheduler import (
    Q1SessionSchedule,
    VersionedMarketSession,
    normal_order_valid_until,
)
from trading.settings import AlpacaPaperConfigBundle, Q1ConfigBundle

Q1_ARM_IDS = tuple(item.value for item in Q1ArmId)
Q1_STRATEGY_ARM_IDS = (
    Q1ArmId.B0_CASH.value,
    Q1ArmId.B0_QQQ.value,
    Q1ArmId.B0_VOL.value,
    Q1ArmId.Q1_DET.value,
    Q1ArmId.Q1_LLM.value,
)
Q1_RISK_ARM_IDS = (
    Q1ArmId.LIVE_MIRROR.value,
    Q1ArmId.Q1_DET.value,
    Q1ArmId.Q1_LLM.value,
)
Q1_MODEL_VERSION = "q1_runtime_v1"


class Q1PaperRuntimeError(RuntimeError):
    pass


class Q1PaperRunConflict(Q1PaperRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Q1PaperRunInitialization:
    run_id: str
    account_spec_id: str
    created: bool
    state: str
    algorithm_version: str = Q1_ALGORITHM_VERSION
    real_order_routing: bool = False


class Q1PaperRuntimeService:
    """Versioned q1 paper runtime facade.

    Cycle mutation lives in ``Q1PaperCycleProcessor``. This facade owns
    initialization, immutable calendar registration, and read-only status.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        config: Q1ConfigBundle,
        workspace_root: Path,
        clock: Clock | None = None,
        alpaca_paper_enabled: bool = False,
        alpaca_paper_config: AlpacaPaperConfigBundle | None = None,
    ) -> None:
        if alpaca_paper_enabled and alpaca_paper_config is None:
            raise Q1PaperRuntimeError(
                "Enabled Alpaca Paper status requires its versioned config"
            )
        self._session_factory = session_factory
        self._config = config
        self._workspace_root = workspace_root
        self._clock = clock or SystemClock()
        self._alpaca_paper_enabled = alpaca_paper_enabled
        self._alpaca_paper_config = alpaca_paper_config
        self._math_config: Q1MathConfig = parse_q1_math_config(config.document)
        self._schedule = Q1SessionSchedule.from_document(config.document)
        self._bootstrap = PaperBootstrapService(session_factory)

    @property
    def math_config(self) -> Q1MathConfig:
        return self._math_config

    @property
    def schedule(self) -> Q1SessionSchedule:
        return self._schedule

    @property
    def schedule_timezone(self) -> ZoneInfo:
        return ZoneInfo(str(self._config.document["schedule"]["timezone"]))

    @property
    def config(self) -> Q1ConfigBundle:
        return self._config

    def initialize(
        self,
        *,
        run_id: str,
        account_file: Path,
        now: datetime | None = None,
    ) -> Q1PaperRunInitialization:
        instant = self._now(now)
        account = self._bootstrap.provision_from_file(account_file, now=instant)
        code_version = workspace_code_version(self._workspace_root)
        created = False
        with self._session_factory.begin() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                run = RunRow(
                    run_id=run_id,
                    mode="PAPER",
                    experiment_version=Q1_ALGORITHM_VERSION,
                    config_manifest_hash=self._config.manifest_hash,
                    code_commit=code_version,
                    started_at=instant,
                    ended_at=None,
                    status="PENDING_BOOTSTRAP",
                    result_manifest={
                        "schema_version": "q1-paper-run.v1",
                        "algorithm_version": Q1_ALGORITHM_VERSION,
                        "account_spec_id": account.account_spec_id,
                        "evaluation_anchor_state": "AWAITING_FIRST_1000_ET_DECISION",
                        "strategy_arm_initialization": "CASH_ONLY_COMMON_T0_NAV",
                        "hold_live_mirror_initialization": "INHERITED_ACCOUNT",
                        "active_strategy_universe": [
                            "QQQ",
                            "SOXX",
                            "USD_CASH",
                        ],
                        "disabled": [
                            "SOXS",
                            "SHORT_SELLING",
                            "MARGIN",
                            "LEVERAGE",
                            "OPTIONS",
                            "INDIVIDUAL_STOCK_ALPHA",
                        ],
                        "matched_comparisons": [
                            "Q1-DET - B0-VOL",
                            "Q1-LLM - Q1-DET",
                        ],
                        "real_order_routing": False,
                    },
                    result_hash=None,
                )
                session.add(run)
                created = True
            else:
                self._validate_existing_run(
                    run,
                    account=account,
                    code_version=code_version,
                )
            for arm_id in Q1_ARM_IDS:
                arm_instance_id = stable_id("q1-arm", run_id, arm_id)
                if session.get(ShadowArmRow, arm_instance_id) is None:
                    session.add(
                        ShadowArmRow(
                            arm_instance_id=arm_instance_id,
                            run_id=run_id,
                            arm_id=arm_id,
                            created_at=instant,
                        )
                    )
        return Q1PaperRunInitialization(
            run_id=run_id,
            account_spec_id=account.account_spec_id,
            created=created,
            state=str(self.status(run_id)["state"]),
        )

    def register_calendar_session(
        self,
        session: VersionedMarketSession,
        *,
        now: datetime | None = None,
    ) -> MarketCalendarSession:
        instant = self._now(now)
        if session.calendar_version != self._calendar_version:
            raise Q1PaperRuntimeError("Market calendar version mismatch")
        source_manifest_hash = canonical_hash(
            {
                "calendar_version": session.calendar_version,
                "session_date": session.session_date,
                "open_at": session.open_at,
                "close_at": session.close_at,
                "source_payload_hash": session.source_payload_hash,
                "available_at": session.source_available_at,
            }
        )
        session_hash = canonical_hash(
            {
                "algorithm_version": Q1_ALGORITHM_VERSION,
                "calendar_version": session.calendar_version,
                "session_date": session.session_date,
                "open_at": session.open_at,
                "close_at": session.close_at,
                "source_manifest_hash": source_manifest_hash,
                "config_manifest_hash": self._config.manifest_hash,
            }
        )
        record = MarketCalendarSession(
            calendar_session_id=session.calendar_session_id,
            calendar_version=session.calendar_version,
            session_date=session.session_date,
            open_at=session.open_at,
            close_at=session.close_at,
            source="ALPACA_CALENDAR_API_PIT",
            available_at=session.source_available_at,
            session_hash=session_hash,
            created_at=max(instant, session.source_available_at),
            config_manifest_hash=self._config.manifest_hash,
            code_version=workspace_code_version(self._workspace_root),
            model_version=Q1_MODEL_VERSION,
            source_manifest_hash=source_manifest_hash,
        )
        with self._session_factory.begin() as database_session:
            MarketCalendarSessionRepository(database_session).append(record)
        return record

    def calendar_session(
        self,
        *,
        session_date: date,
        cutoff: datetime,
    ) -> VersionedMarketSession | None:
        instant = require_aware_utc(cutoff)
        with self._session_factory() as session:
            row = MarketCalendarSessionRepository(session).for_date_as_of(
                calendar_version=self._calendar_version,
                session_date=session_date,
                cutoff=instant,
            )
            return None if row is None else _versioned_market_session(row)

    def status(self, run_id: str) -> dict[str, Any]:
        status_now = self._clock.now()
        status_session_date = status_now.astimezone(
            self.schedule_timezone
        ).date()
        with self._session_factory() as session:
            run = session.get(RunRow, run_id)
            if run is None:
                return {
                    "run_id": run_id,
                    "state": "NOT_INITIALIZED",
                    "algorithm_version": Q1_ALGORITHM_VERSION,
                    "alpaca_paper_canary": alpaca_paper_canary_status(
                        session,
                        run_id=run_id,
                        enabled=self._alpaca_paper_enabled,
                        config=self._alpaca_paper_config,
                    ),
                    "real_order_routing": False,
                }
            if run.experiment_version != Q1_ALGORITHM_VERSION:
                raise Q1PaperRunConflict(
                    f"Run {run_id!r} belongs to {run.experiment_version}"
                )
            anchor = session.scalar(
                select(StrategyEvaluationAnchorRow).where(
                    StrategyEvaluationAnchorRow.run_id == run_id
                )
            )
            latest_calendar = session.scalar(
                select(MarketCalendarSessionRow)
                .where(
                    MarketCalendarSessionRow.calendar_version
                    == self._calendar_version,
                    MarketCalendarSessionRow.session_date
                    <= status_session_date,
                    MarketCalendarSessionRow.available_at <= status_now,
                )
                .order_by(
                    MarketCalendarSessionRow.session_date.desc(),
                    MarketCalendarSessionRow.available_at.desc(),
                )
                .limit(1)
            )
            if latest_calendar is None:
                latest_calendar = session.scalar(
                    select(MarketCalendarSessionRow)
                    .where(
                        MarketCalendarSessionRow.calendar_version
                        == self._calendar_version,
                        MarketCalendarSessionRow.available_at <= status_now,
                    )
                    .order_by(
                        MarketCalendarSessionRow.session_date,
                        MarketCalendarSessionRow.available_at.desc(),
                    )
                    .limit(1)
                )
            states = {
                arm_id: latest_arm_state(
                    session,
                    run_id=run_id,
                    arm_id=arm_id,
                )
                for arm_id in Q1_ARM_IDS
            }
            latest_navs = _latest_navs(session, run_id)
            latest_decisions = _latest_decisions(session, run_id)
            latest_strategic_decisions = _latest_strategic_decisions(
                session,
                run_id,
            )
            order_book = load_q1_order_book(session, run_id=run_id)
            pending = pending_orders(order_book.descriptors, order_book.events)
            risk_repository = RiskEpisodeRepository(session)
            active_episodes = {
                arm_id: risk_repository.active(
                    run_id=run_id,
                    arm_id=arm_id,
                )
                for arm_id in Q1_RISK_ARM_IDS
            }
            matched = list(
                session.scalars(
                    select(MatchedAttributionResultRow)
                    .where(MatchedAttributionResultRow.run_id == run_id)
                    .order_by(
                        MatchedAttributionResultRow.comparison,
                        desc(MatchedAttributionResultRow.through_session_date),
                    )
                )
            )
            latest_matched: dict[str, MatchedAttributionResultRow] = {}
            for row in matched:
                latest_matched.setdefault(row.comparison, row)
            performance = _performance_status(
                session,
                run_id=run_id,
                config=evaluation_config(self._config),
            )
            calendar_payload = _calendar_status(
                latest_calendar,
                schedule=self._schedule,
            )
            arms = {
                arm_id: _arm_status(
                    arm_id=arm_id,
                    state=states[arm_id],
                    nav=latest_navs.get(arm_id),
                    decision=latest_decisions.get(arm_id),
                    signal_decision=latest_strategic_decisions.get(arm_id),
                    pending=[
                        item
                        for item in pending
                        if item.order.arm_id == arm_id
                    ],
                    active_episode=active_episodes.get(arm_id),
                    status_now=status_now,
                )
                for arm_id in Q1_ARM_IDS
            }
            return {
                "run_id": run_id,
                "state": run.status,
                "algorithm_version": Q1_ALGORITHM_VERSION,
                "evaluation_anchor": (
                    None
                    if anchor is None
                    else {
                        "evaluation_anchor_id": anchor.evaluation_anchor_id,
                        "common_t0_at": _iso(anchor.common_t0_at),
                        "initial_nav_usd": str(anchor.initial_nav_usd),
                        "calendar_session_id": anchor.calendar_session_id,
                        "anchor_hash": anchor.anchor_hash,
                    }
                ),
                "active_strategy_universe": ["QQQ", "SOXX", "USD_CASH"],
                "hold_and_live_mirror_reported_separately": True,
                "arms": arms,
                "session_calendar": calendar_payload,
                "matched_comparisons": {
                    "alpha": _matched_status(
                        latest_matched.get("Q1_DET_MINUS_B0_VOL")
                    ),
                    "llm": _matched_status(
                        latest_matched.get("Q1_LLM_MINUS_Q1_DET")
                    ),
                    "minimum_common_out_of_sample_sessions": (
                        self._math_config.evaluation.minimum_common_out_of_sample_sessions
                    ),
                },
                "performance": performance,
                "llm_overlay_state": (
                    arms[Q1ArmId.Q1_LLM.value]["llm_overlay_state"]
                ),
                "research_only": [
                    "T1",
                    "R1",
                    "X1",
                    "CONSTITUENT_BREADTH",
                    "INDIVIDUAL_STOCK_SELECTION",
                    "INTRADAY_ALPHA",
                ],
                "disabled": {
                    "SOXS": True,
                    "short_selling": True,
                    "margin": True,
                    "leverage": True,
                    "options": True,
                    "individual_stock_alpha": True,
                },
                "alpaca_paper_canary": alpaca_paper_canary_status(
                    session,
                    run_id=run_id,
                    enabled=self._alpaca_paper_enabled,
                    config=self._alpaca_paper_config,
                ),
                "real_order_routing": False,
            }

    @property
    def _calendar_version(self) -> str:
        return str(self._config.document["market_calendar_version"])

    def _validate_existing_run(
        self,
        run: RunRow,
        *,
        account: ProvisionedPaperAccount,
        code_version: str,
    ) -> None:
        if run.mode != "PAPER" or run.experiment_version != Q1_ALGORITHM_VERSION:
            raise Q1PaperRunConflict(
                f"Run {run.run_id!r} is not a q1 paper run"
            )
        manifest = run.result_manifest or {}
        if manifest.get("account_spec_id") != account.account_spec_id:
            raise Q1PaperRunConflict("Q1 run is bound to another account snapshot")
        if run.config_manifest_hash != self._config.manifest_hash:
            raise Q1PaperRunConflict("Q1 run config manifest changed")
        if run.code_commit != code_version:
            raise Q1PaperRunConflict("Q1 run code version changed")
        if manifest.get("real_order_routing") is not False:
            raise Q1PaperRunConflict("Q1 run routing safety invariant is missing")

    def _now(self, value: datetime | None) -> datetime:
        return self._clock.now() if value is None else require_aware_utc(value)


def _versioned_market_session(
    row: MarketCalendarSessionRow,
) -> VersionedMarketSession:
    payload = row.payload_json
    return VersionedMarketSession(
        calendar_session_id=row.calendar_session_id,
        calendar_version=row.calendar_version,
        session_date=row.session_date,
        open_at=_aware(row.open_at),
        close_at=_aware(row.close_at),
        source_payload_hash=str(
            payload.get("source_manifest_hash", row.source_manifest_hash)
        ),
        source_available_at=_aware(row.available_at),
    )


def _latest_navs(
    session: Session,
    run_id: str,
) -> dict[str, NavSnapshotRow]:
    rows = list(
        session.scalars(
            select(NavSnapshotRow)
            .where(
                NavSnapshotRow.run_id == run_id,
                NavSnapshotRow.algorithm_version == Q1_ALGORITHM_VERSION,
            )
            .order_by(
                NavSnapshotRow.arm_id,
                NavSnapshotRow.as_of.desc(),
                NavSnapshotRow.nav_snapshot_id.desc(),
            )
        )
    )
    latest: dict[str, NavSnapshotRow] = {}
    for row in rows:
        latest.setdefault(row.arm_id, row)
    return latest


def _latest_decisions(
    session: Session,
    run_id: str,
) -> dict[str, Q1StrategyDecision]:
    rows = list(
        session.scalars(
            select(PortfolioDecisionRow)
            .where(
                PortfolioDecisionRow.run_id == run_id,
                PortfolioDecisionRow.algorithm_version
                == Q1_ALGORITHM_VERSION,
            )
            .order_by(
                PortfolioDecisionRow.arm_id,
                PortfolioDecisionRow.decision_created_at.desc(),
                PortfolioDecisionRow.portfolio_decision_id.desc(),
            )
        )
    )
    latest: dict[str, Q1StrategyDecision] = {}
    for row in rows:
        latest.setdefault(
            row.arm_id,
            Q1StrategyDecision.model_validate(row.payload_json),
        )
    return latest


def _latest_strategic_decisions(
    session: Session,
    run_id: str,
) -> dict[str, Q1StrategyDecision]:
    rows = list(
        session.scalars(
            select(PortfolioDecisionRow)
            .where(
                PortfolioDecisionRow.run_id == run_id,
                PortfolioDecisionRow.algorithm_version
                == Q1_ALGORITHM_VERSION,
            )
            .order_by(
                PortfolioDecisionRow.arm_id,
                PortfolioDecisionRow.decision_created_at.desc(),
                PortfolioDecisionRow.portfolio_decision_id.desc(),
            )
        )
    )
    latest: dict[str, Q1StrategyDecision] = {}
    for row in rows:
        decision = Q1StrategyDecision.model_validate(row.payload_json)
        if decision.decision_kind not in {
            "NORMAL_REBALANCE",
            "NO_TRADE_BELOW_BAND",
            "NO_EXECUTABLE_ORDERS",
        }:
            continue
        latest.setdefault(
            row.arm_id,
            decision,
        )
    return latest


def _arm_status(
    *,
    arm_id: str,
    state: Any,
    nav: NavSnapshotRow | None,
    decision: Q1StrategyDecision | None,
    signal_decision: Q1StrategyDecision | None,
    pending: list[Any],
    active_episode: Any,
    status_now: datetime,
) -> dict[str, Any]:
    nav_payload = {} if nav is None else nav.payload_json
    decision_weights = (
        None
        if decision is None
        else {
            symbol: str(value)
            for symbol, value in decision.target_weights.items()
        }
    )
    return {
        "initialization": (
            "INHERITED_ACCOUNT"
            if arm_id in {Q1ArmId.HOLD.value, Q1ArmId.LIVE_MIRROR.value}
            else "CASH_ONLY_COMMON_T0_NAV"
        ),
        "comparison_group": (
            "HOLD_SEPARATE"
            if arm_id == Q1ArmId.HOLD.value
            else "LIVE_MIRROR_SEPARATE"
            if arm_id == Q1ArmId.LIVE_MIRROR.value
            else "STRATEGY"
        ),
        "sequence": None if state is None else state.sequence,
        "positions": (
            {}
            if state is None
            else {
                symbol: str(quantity)
                for symbol, quantity in sorted(state.positions.items())
            }
        ),
        "settled_cash_usd": (
            None if state is None else str(state.settled_cash_usd)
        ),
        "unsettled_receivables_usd": (
            None if state is None else str(state.unsettled_cash_usd)
        ),
        "nav_usd": None if nav is None else str(nav.nav_usd),
        "actual_weights": nav_payload.get("actual_weights"),
        "target_weights": decision_weights,
        "latest_signal_diagnostics": (
            None
            if signal_decision is None
            else signal_decision.diagnostics
        ),
        "llm_overlay_state": _llm_overlay_status(
            arm_id=arm_id,
            decision=decision,
            status_now=status_now,
        ),
        "risk_state": (
            str(nav_payload.get("risk_state", "NORMAL"))
            if active_episode is None
            else active_episode.latest_event.severity
        ),
        "active_risk_episode": (
            None
            if active_episode is None
            else {
                "risk_episode_id": active_episode.episode.risk_episode_id,
                "severity": active_episode.latest_event.severity,
                "target_generation": active_episode.latest_event.target_generation,
                "targets": {
                    row.symbol: str(row.target_quantity)
                    for row in active_episode.targets
                },
            }
        ),
        "pending_orders": [
            {
                "order_intent_id": item.order.order_intent_id,
                "symbol": item.order.symbol,
                "side": item.order.side.value,
                "order_class": item.order.order_class.value,
                "remaining_quantity": str(item.remaining_quantity),
                "latest_status": item.status.value,
                "valid_until": _iso(item.order.valid_until),
            }
            for item in pending
        ],
    }


def _llm_overlay_status(
    *,
    arm_id: str,
    decision: Q1StrategyDecision | None,
    status_now: datetime,
) -> str:
    if arm_id != Q1ArmId.Q1_LLM.value:
        return "NOT_APPLICABLE"
    if decision is None:
        return "NO_POLICY"
    state = str(
        decision.diagnostics.get(
            "llm_overlay_state",
            "NO_CHANGE",
        )
    )
    if state != "ACTIVE":
        return state
    raw_expiry = decision.diagnostics.get("llm_policy_expiry_time")
    if not isinstance(raw_expiry, str):
        policy = decision.diagnostics.get("llm_policy")
        if isinstance(policy, dict):
            raw_expiry = policy.get("expiry_time")
    if not isinstance(raw_expiry, str):
        return state
    try:
        expiry = _aware(
            datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
        )
    except ValueError:
        return state
    return (
        "EXPIRED_AWAITING_NEXT_REBALANCE"
        if status_now >= expiry
        else state
    )


def _calendar_status(
    row: MarketCalendarSessionRow | None,
    *,
    schedule: Q1SessionSchedule,
) -> dict[str, Any] | None:
    if row is None:
        return None
    session = _versioned_market_session(row)
    return {
        "calendar_session_id": row.calendar_session_id,
        "calendar_version": row.calendar_version,
        "session_date": row.session_date.isoformat(),
        "open_at": _iso(row.open_at),
        "close_at": _iso(row.close_at),
        "normal_order_cutoff": _iso(
            normal_order_valid_until(session, schedule=schedule)
        ),
        "source": row.source,
        "available_at": _iso(row.available_at),
    }


def _matched_status(
    row: MatchedAttributionResultRow | None,
) -> dict[str, Any]:
    if row is None:
        return {
            "state": "AWAITING_COMMON_SESSIONS",
            "common_valid_sessions": 0,
            "promotion_ready": False,
        }
    return {
        "state": (
            "MINIMUM_SAMPLE_REACHED_MANUAL_REVIEW_REQUIRED"
            if row.promotion_ready
            else "AWAITING_COMMON_SESSIONS"
        ),
        "comparison": row.comparison,
        "left_arm_id": row.left_arm_id,
        "right_arm_id": row.right_arm_id,
        "through_session_date": row.through_session_date.isoformat(),
        "common_valid_sessions": row.common_valid_sessions,
        "mean_daily_difference": str(row.mean_daily_difference),
        "annualized_difference": str(row.annualized_difference),
        "newey_west_lag": row.newey_west_lag,
        "newey_west_standard_error": str(row.newey_west_standard_error),
        "bootstrap_seed": row.bootstrap_seed,
        "bootstrap_interval": [
            str(row.bootstrap_lower),
            str(row.bootstrap_upper),
        ],
        "promotion_ready": row.promotion_ready,
        "automatic_profitability_claim": False,
        "automatic_statistical_significance_claim": False,
        "promotion_is_manual": True,
    }


def _performance_status(
    session: Session,
    *,
    run_id: str,
    config: EvaluationConfig,
) -> dict[str, Any]:
    rows = tuple(
        session.scalars(
            select(StrategyDailyResultRow)
            .where(
                StrategyDailyResultRow.run_id == run_id,
                StrategyDailyResultRow.algorithm_version
                == Q1_ALGORITHM_VERSION,
            )
            .order_by(
                StrategyDailyResultRow.arm_id,
                StrategyDailyResultRow.session_date,
            )
        )
    )
    observations: dict[Q1ArmId, list[DailyEvaluationObservation]] = {}
    latest: dict[Q1ArmId, StrategyDailyResult] = {}
    for row in rows:
        result = StrategyDailyResult.model_validate(row.payload_json)
        observations.setdefault(result.arm_id, []).append(
            DailyEvaluationObservation(
                session_date=result.session_date,
                arm_id=result.arm_id,
                net_daily_return=result.net_daily_return,
                daily_turnover=result.daily_turnover,
                commissions_usd=result.commissions_usd,
                spread_cost_usd=result.spread_cost_usd,
                delay_cost_usd=result.delay_cost_usd,
                sensitivity_5bp_usd=result.sensitivity_5bp_usd,
                sensitivity_10bp_usd=result.sensitivity_10bp_usd,
                cash_weight=result.cash_weight,
                qqq_weight=result.qqq_weight,
                soxx_weight=result.soxx_weight,
                risk_episode_active=(
                    result.active_risk_episode_count > 0
                ),
                llm_reduction_active=(
                    result.active_llm_reduction_count > 0
                ),
            )
        )
        latest[result.arm_id] = result
    payload: dict[str, Any] = {}
    for arm_id, arm_observations in observations.items():
        metrics = evaluate_performance(arm_observations, config)
        latest_result = latest[arm_id]
        values = canonical_data(asdict(metrics))
        if not isinstance(values, dict):
            raise Q1PaperRuntimeError(
                "Q1 performance metrics did not serialize to an object"
            )
        payload[arm_id.value] = {
            **values,
            "latest_session_date": (
                latest_result.session_date.isoformat()
            ),
            "latest_daily_return": str(
                latest_result.net_daily_return
            ),
            "latest_daily_turnover": str(
                latest_result.daily_turnover
            ),
            "latest_cumulative_return": str(
                latest_result.cumulative_return
            ),
        }
    return payload


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat().replace("+00:00", "Z")
