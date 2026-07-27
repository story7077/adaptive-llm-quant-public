from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.q1 import (
    Q1ArmId,
    RiskEpisode,
    RiskEpisodeEvent,
    RiskEpisodeEventType,
    RiskSeverity,
    RiskTarget,
)
from trading.domain.time import require_aware_utc


class Q1RiskError(ValueError):
    """Raised when a deterministic risk transition cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class RiskEngineConfig:
    version: str
    annualization_sessions: Decimal
    soft_sigma_multiple: Decimal
    hard_sigma_multiple: Decimal
    soft_daily_floor: Decimal
    soft_daily_ceiling: Decimal
    hard_daily_floor: Decimal
    hard_daily_ceiling: Decimal
    soft_drawdown_threshold: Decimal
    hard_drawdown_threshold: Decimal
    critical_drawdown_threshold: Decimal
    q1_hard_gross_cap: Decimal
    q1_hard_soxx_weight_cap: Decimal
    live_mirror_semiconductor_weight_cap: Decimal
    release_daily_loss_soft_fraction: Decimal
    release_drawdown_threshold: Decimal
    release_consecutive_valid_checks: int
    quantity_precision: Decimal
    leveraged_symbols: frozenset[str]
    semiconductor_symbols: frozenset[str]

    def __post_init__(self) -> None:
        fractions = (
            self.soft_daily_floor,
            self.soft_daily_ceiling,
            self.hard_daily_floor,
            self.hard_daily_ceiling,
            self.soft_drawdown_threshold,
            self.hard_drawdown_threshold,
            self.critical_drawdown_threshold,
            self.q1_hard_gross_cap,
            self.q1_hard_soxx_weight_cap,
            self.live_mirror_semiconductor_weight_cap,
            self.release_daily_loss_soft_fraction,
            self.release_drawdown_threshold,
        )
        if not self.version:
            raise Q1RiskError("Risk config version is required")
        if self.annualization_sessions <= 0:
            raise Q1RiskError("Annualization sessions must be positive")
        if self.soft_sigma_multiple < 0 or self.hard_sigma_multiple < 0:
            raise Q1RiskError("Sigma multiples cannot be negative")
        if any(value < 0 or value > 1 for value in fractions):
            raise Q1RiskError("Risk fractions must be within [0, 1]")
        if self.soft_daily_floor > self.soft_daily_ceiling:
            raise Q1RiskError("Soft daily threshold bounds are inverted")
        if self.hard_daily_floor > self.hard_daily_ceiling:
            raise Q1RiskError("Hard daily threshold bounds are inverted")
        if self.soft_drawdown_threshold >= self.hard_drawdown_threshold:
            raise Q1RiskError("Soft drawdown must be below hard drawdown")
        if self.hard_drawdown_threshold >= self.critical_drawdown_threshold:
            raise Q1RiskError("Hard drawdown must be below critical drawdown")
        if self.release_consecutive_valid_checks <= 0:
            raise Q1RiskError("Release check count must be positive")
        if self.quantity_precision <= 0:
            raise Q1RiskError("Quantity precision must be positive")


@dataclass(frozen=True, slots=True)
class RiskQuote:
    symbol: str
    quote_id: str
    midpoint: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or not self.quote_id:
            raise Q1RiskError("Risk quote identity is required")
        if self.midpoint <= 0:
            raise Q1RiskError("Risk quote midpoint must be positive")


@dataclass(frozen=True, slots=True)
class RiskCheckInput:
    arm_id: Q1ArmId
    calendar_session_id: str
    scheduled_at: datetime
    decision_created_at: datetime
    positions: Mapping[str, Decimal]
    settled_cash_usd: Decimal
    unsettled_receivables_usd: Decimal
    quotes: Mapping[str, RiskQuote]
    session_open_nav_usd: Decimal
    running_peak_nav_usd: Decimal
    portfolio_annualized_vol: Decimal | None
    reconciliation_ok: bool
    critical_reconciliation_condition: bool
    reconciliation_status: str = "OK"

    def __post_init__(self) -> None:
        require_aware_utc(self.scheduled_at)
        require_aware_utc(self.decision_created_at)
        if not self.calendar_session_id:
            raise Q1RiskError("Calendar-session ID is required")
        if self.decision_created_at < self.scheduled_at:
            raise Q1RiskError("Risk decision cannot be created before scheduled_at")
        if self.settled_cash_usd < 0 or self.unsettled_receivables_usd < 0:
            raise Q1RiskError("Cash balances cannot be negative")
        if self.session_open_nav_usd <= 0 or self.running_peak_nav_usd <= 0:
            raise Q1RiskError("Session-open and running-peak NAV must be positive")
        if self.portfolio_annualized_vol is not None and self.portfolio_annualized_vol < 0:
            raise Q1RiskError("Portfolio volatility cannot be negative")
        if any(quantity < 0 for quantity in self.positions.values()):
            raise Q1RiskError("Q1 risk engine prohibits short positions")
        if not self.reconciliation_status:
            raise Q1RiskError("Reconciliation status is required")
        if self.reconciliation_ok != (
            self.reconciliation_status == "OK"
        ):
            raise Q1RiskError(
                "Reconciliation status and ok flag are inconsistent"
            )


@dataclass(frozen=True, slots=True)
class RiskMetrics:
    current_nav_usd: Decimal
    daily_loss: Decimal
    run_drawdown: Decimal
    daily_sigma: Decimal | None
    soft_daily_threshold: Decimal
    hard_daily_threshold: Decimal
    indicated_severity: RiskSeverity


@dataclass(frozen=True, slots=True)
class RiskEpisodeProvenance:
    run_id: str
    config_manifest_hash: str
    code_version: str
    model_version: str
    source_manifest_hash: str
    worker_fence_token: str
    cycle_attempt_count: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.run_id,
                self.config_manifest_hash,
                self.code_version,
                self.model_version,
                self.source_manifest_hash,
                self.worker_fence_token,
            )
        ):
            raise Q1RiskError("Complete risk-episode provenance is required")
        if self.cycle_attempt_count <= 0:
            raise Q1RiskError("Cycle attempt count must be positive")


@dataclass(frozen=True, slots=True)
class RiskTransition:
    effective_severity: RiskSeverity
    block_new_buys: bool
    cancel_pending_buys: bool
    active_episode: RiskEpisode | None
    new_episode: RiskEpisode | None
    new_events: tuple[RiskEpisodeEvent, ...]
    executable_residual_targets: tuple[RiskTarget, ...]
    required_quote_symbols: frozenset[str]
    release_allows_automatic_buys: bool = False


def evaluate_risk_check(
    check: RiskCheckInput,
    config: RiskEngineConfig,
) -> RiskMetrics:
    """Calculate loss-only risk state from held assets; no QQQ history is needed."""
    held_symbols = {
        symbol for symbol, quantity in check.positions.items() if quantity > 0
    }
    missing = sorted(held_symbols - set(check.quotes))
    if missing:
        raise Q1RiskError(f"Fresh quotes missing for held risk assets: {missing}")
    current_nav = (
        check.settled_cash_usd
        + check.unsettled_receivables_usd
        + sum(
            (
                quantity * check.quotes[symbol].midpoint
                for symbol, quantity in check.positions.items()
                if quantity > 0
            ),
            Decimal("0"),
        )
    )
    if current_nav <= 0:
        raise Q1RiskError("Current NAV must be positive")
    daily_loss = max(
        Decimal("0"),
        (check.session_open_nav_usd - current_nav) / check.session_open_nav_usd,
    )
    effective_peak = max(check.running_peak_nav_usd, current_nav)
    run_drawdown = max(
        Decimal("0"),
        (effective_peak - current_nav) / effective_peak,
    )
    daily_sigma = (
        None
        if check.portfolio_annualized_vol is None
        else check.portfolio_annualized_vol / config.annualization_sessions.sqrt()
    )
    sigma_for_threshold = daily_sigma or Decimal("0")
    soft_daily = _clip(
        config.soft_sigma_multiple * sigma_for_threshold,
        config.soft_daily_floor,
        config.soft_daily_ceiling,
    )
    hard_daily = _clip(
        config.hard_sigma_multiple * sigma_for_threshold,
        config.hard_daily_floor,
        config.hard_daily_ceiling,
    )
    if (
        run_drawdown >= config.critical_drawdown_threshold
        or check.critical_reconciliation_condition
    ):
        severity = RiskSeverity.CRITICAL_EXIT
    elif (
        daily_loss >= hard_daily
        or run_drawdown >= config.hard_drawdown_threshold
    ):
        severity = RiskSeverity.HARD_REDUCE
    elif (
        daily_loss >= soft_daily
        or run_drawdown >= config.soft_drawdown_threshold
    ):
        severity = RiskSeverity.SOFT_STOP
    else:
        severity = RiskSeverity.NORMAL
    return RiskMetrics(
        current_nav_usd=current_nav,
        daily_loss=daily_loss,
        run_drawdown=run_drawdown,
        daily_sigma=daily_sigma,
        soft_daily_threshold=soft_daily,
        hard_daily_threshold=hard_daily,
        indicated_severity=severity,
    )


def plan_risk_transition(
    *,
    check: RiskCheckInput,
    metrics: RiskMetrics,
    config: RiskEngineConfig,
    provenance: RiskEpisodeProvenance,
    active_episode: RiskEpisode | None,
    existing_episode_events: Iterable[RiskEpisodeEvent] = (),
    is_next_session_strategic_cycle: bool = False,
    consecutive_valid_release_checks: int = 0,
    source_cycle_id: str | None = None,
) -> RiskTransition:
    """Plan immutable episode/event changes without opening a database session."""
    events = tuple(existing_episode_events)
    if active_episode is not None:
        _validate_episode_event_stream(active_episode, events)
    active_severity = (
        _episode_severity(active_episode, events)
        if active_episode is not None
        else RiskSeverity.NORMAL
    )
    indicated = metrics.indicated_severity
    effective = max(active_severity, indicated, key=_severity_rank)
    new_episode: RiskEpisode | None = None
    new_events: list[RiskEpisodeEvent] = []
    episode = active_episode

    if active_episode is None and indicated in {
        RiskSeverity.HARD_REDUCE,
        RiskSeverity.CRITICAL_EXIT,
    }:
        targets = _build_targets(
            check=check,
            metrics=metrics,
            severity=indicated,
            config=config,
            target_generation=1,
        )
        if targets:
            new_episode = _create_episode(
                check=check,
                metrics=metrics,
                severity=indicated,
                targets=targets,
                provenance=provenance,
            )
            episode = new_episode
            new_events.append(
                _episode_event(
                    episode=new_episode,
                    existing_events=(),
                    event_type=RiskEpisodeEventType.ACTIVATE,
                    severity=indicated,
                    targets=targets,
                    check=check,
                    provenance=provenance,
                    source_cycle_id=source_cycle_id,
                )
            )
        else:
            effective = indicated
    elif (
        active_episode is not None
        and active_severity is RiskSeverity.HARD_REDUCE
        and indicated is RiskSeverity.CRITICAL_EXIT
    ):
        targets = _build_targets(
            check=check,
            metrics=metrics,
            severity=RiskSeverity.CRITICAL_EXIT,
            config=config,
            target_generation=_episode_target_generation(
                active_episode,
                events,
            )
            + 1,
        )
        if targets:
            new_events.append(
                _episode_event(
                    episode=active_episode,
                    existing_events=events,
                    event_type=RiskEpisodeEventType.ESCALATE,
                    severity=RiskSeverity.CRITICAL_EXIT,
                    targets=targets,
                    check=check,
                    provenance=provenance,
                    source_cycle_id=source_cycle_id,
                )
            )
        effective = RiskSeverity.CRITICAL_EXIT

    if episode is not None:
        current_targets = _episode_targets(episode, (*events, *new_events))
        residual = residual_targets(current_targets, check.positions)
        required = required_residual_quote_symbols(current_targets, check.positions)
        if _can_release(
            check=check,
            metrics=metrics,
            config=config,
            active_episode=episode,
            is_next_session_strategic_cycle=is_next_session_strategic_cycle,
            consecutive_valid_release_checks=consecutive_valid_release_checks,
        ):
            release_event = _episode_event(
                episode=episode,
                existing_events=(*events, *new_events),
                event_type=RiskEpisodeEventType.RELEASE,
                severity=_episode_severity(episode, (*events, *new_events)),
                targets=(),
                check=check,
                provenance=provenance,
                source_cycle_id=source_cycle_id,
                consecutive_valid_checks=consecutive_valid_release_checks,
            )
            new_events.append(release_event)
            effective = RiskSeverity.NORMAL
            residual = ()
            required = frozenset[str]()
            episode = None
    else:
        residual = ()
        required = frozenset[str]()

    return RiskTransition(
        effective_severity=effective,
        block_new_buys=effective is not RiskSeverity.NORMAL,
        cancel_pending_buys=effective is not RiskSeverity.NORMAL,
        active_episode=episode,
        new_episode=new_episode,
        new_events=tuple(new_events),
        executable_residual_targets=residual,
        required_quote_symbols=required,
        release_allows_automatic_buys=False,
    )


def residual_targets(
    targets: Iterable[RiskTarget],
    positions: Mapping[str, Decimal],
) -> tuple[RiskTarget, ...]:
    """Exclude achieved targets while retaining them in the episode audit record."""
    return tuple(
        target
        for target in targets
        if positions.get(target.symbol, Decimal("0")) > target.target_quantity
    )


def current_episode_targets(
    episode: RiskEpisode,
    events: Iterable[RiskEpisodeEvent],
) -> tuple[RiskTarget, ...]:
    """Return the immutable latest-generation targets for an episode."""

    materialized = tuple(events)
    _validate_episode_event_stream(episode, materialized)
    return _episode_targets(episode, materialized)


def required_residual_quote_symbols(
    targets: Iterable[RiskTarget],
    positions: Mapping[str, Decimal],
) -> frozenset[str]:
    """Require quotes only for still-executable residual reductions."""
    return frozenset(
        target.symbol
        for target in targets
        if positions.get(target.symbol, Decimal("0")) > target.target_quantity
    )


def target_progress_events(
    *,
    episode: RiskEpisode,
    existing_events: Iterable[RiskEpisodeEvent],
    positions: Mapping[str, Decimal],
    check: RiskCheckInput,
    provenance: RiskEpisodeProvenance,
    source_cycle_id: str | None = None,
) -> tuple[RiskEpisodeEvent, ...]:
    """Create deterministic progress/reached events for changed observations."""
    materialized = tuple(existing_events)
    _validate_episode_event_stream(episode, materialized)
    targets = _episode_targets(episode, materialized)
    latest_observed = _latest_observed_quantities(materialized)
    result: list[RiskEpisodeEvent] = []
    for target in targets:
        observed = positions.get(target.symbol, Decimal("0"))
        if latest_observed.get(target.symbol) == observed:
            continue
        residual = max(Decimal("0"), observed - target.target_quantity)
        event_type = (
            RiskEpisodeEventType.TARGET_REACHED
            if residual == 0
            else RiskEpisodeEventType.TARGET_PROGRESS
        )
        result.append(
            _episode_event(
                episode=episode,
                existing_events=(*materialized, *result),
                event_type=event_type,
                severity=_episode_severity(episode, materialized),
                targets=(),
                check=check,
                provenance=provenance,
                source_cycle_id=source_cycle_id,
                target_symbol=target.symbol,
                observed_quantity=observed,
                residual_quantity=residual,
            )
        )
    return tuple(result)


def _build_targets(
    *,
    check: RiskCheckInput,
    metrics: RiskMetrics,
    severity: RiskSeverity,
    config: RiskEngineConfig,
    target_generation: int,
) -> tuple[RiskTarget, ...]:
    positive = {
        symbol: quantity
        for symbol, quantity in check.positions.items()
        if quantity > 0
    }
    if not positive:
        return ()
    if severity is RiskSeverity.CRITICAL_EXIT:
        quantities = {symbol: Decimal("0") for symbol in positive}
    elif check.arm_id in {Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM}:
        quantities = _q1_hard_targets(
            positions=positive,
            quotes=check.quotes,
            nav=metrics.current_nav_usd,
            config=config,
        )
    elif check.arm_id is Q1ArmId.LIVE_MIRROR:
        quantities = _live_mirror_hard_targets(
            positions=positive,
            quotes=check.quotes,
            nav=metrics.current_nav_usd,
            config=config,
        )
    else:
        raise Q1RiskError(f"Deterministic loss overlay is not permitted for {check.arm_id}")
    targets: list[RiskTarget] = []
    for symbol, quantity in sorted(quantities.items()):
        quote = check.quotes.get(symbol)
        if quote is None:
            raise Q1RiskError(f"Trigger quote missing for target symbol {symbol}")
        targets.append(
            RiskTarget(
                symbol=symbol,
                target_quantity=quantity,
                trigger_quote_id=quote.quote_id,
                target_generation=target_generation,
                target_id=stable_id(
                    "q1-risk-target",
                    provenance_free_target_identity(
                        arm_id=check.arm_id,
                        symbol=symbol,
                        target_quantity=quantity,
                        quote_id=quote.quote_id,
                        triggered_at=check.scheduled_at,
                        target_generation=target_generation,
                    ),
                ),
                trigger_quantity=positive[symbol],
                trigger_price=quote.midpoint,
                target_weight=quantity * quote.midpoint / metrics.current_nav_usd,
            )
        )
    return tuple(targets)


def _q1_hard_targets(
    *,
    positions: Mapping[str, Decimal],
    quotes: Mapping[str, RiskQuote],
    nav: Decimal,
    config: RiskEngineConfig,
) -> dict[str, Decimal]:
    eligible = {
        symbol: quantity
        for symbol, quantity in positions.items()
        if symbol in {"QQQ", "SOXX"}
    }
    if not eligible:
        return {}
    values = {
        symbol: quantity * quotes[symbol].midpoint
        for symbol, quantity in eligible.items()
    }
    if "SOXX" in values:
        values["SOXX"] = min(
            values["SOXX"],
            nav * config.q1_hard_soxx_weight_cap,
        )
    gross = sum(values.values(), Decimal("0"))
    gross_cap = nav * config.q1_hard_gross_cap
    if gross > gross_cap:
        scale = gross_cap / gross
        values = {symbol: value * scale for symbol, value in values.items()}
    return {
        symbol: _floor_quantity(
            values[symbol] / quotes[symbol].midpoint,
            config.quantity_precision,
        )
        for symbol in sorted(values)
    }


def _live_mirror_hard_targets(
    *,
    positions: Mapping[str, Decimal],
    quotes: Mapping[str, RiskQuote],
    nav: Decimal,
    config: RiskEngineConfig,
) -> dict[str, Decimal]:
    targets = {
        symbol: Decimal("0")
        for symbol, quantity in positions.items()
        if quantity > 0 and symbol in config.leveraged_symbols
    }
    semiconductors = {
        symbol: quantity
        for symbol, quantity in positions.items()
        if (
            quantity > 0
            and symbol in config.semiconductor_symbols
            and symbol not in config.leveraged_symbols
        )
    }
    semiconductor_value = sum(
        (
            quantity * quotes[symbol].midpoint
            for symbol, quantity in semiconductors.items()
        ),
        Decimal("0"),
    )
    cap = nav * config.live_mirror_semiconductor_weight_cap
    scale = Decimal("1") if semiconductor_value <= cap else cap / semiconductor_value
    for symbol, quantity in semiconductors.items():
        targets[symbol] = _floor_quantity(
            quantity * scale,
            config.quantity_precision,
        )
    return targets


def _create_episode(
    *,
    check: RiskCheckInput,
    metrics: RiskMetrics,
    severity: RiskSeverity,
    targets: tuple[RiskTarget, ...],
    provenance: RiskEpisodeProvenance,
) -> RiskEpisode:
    if not targets:
        raise Q1RiskError("Empty targets cannot activate a typed risk episode")
    target_manifest_hash = canonical_hash(targets)
    identity = {
        "run_id": provenance.run_id,
        "arm_id": check.arm_id,
        "calendar_session_id": check.calendar_session_id,
        "severity": severity,
        "triggered_at": check.scheduled_at,
        "target_manifest_hash": target_manifest_hash,
    }
    episode_id = stable_id("q1-risk-episode", identity)
    episode_hash = canonical_hash(
        {
            **identity,
            "trigger_nav_usd": metrics.current_nav_usd,
            "daily_loss": metrics.daily_loss,
            "run_drawdown": metrics.run_drawdown,
            "config_manifest_hash": provenance.config_manifest_hash,
            "code_version": provenance.code_version,
            "model_version": provenance.model_version,
            "source_manifest_hash": provenance.source_manifest_hash,
        }
    )
    return RiskEpisode(
        risk_episode_id=episode_id,
        run_id=provenance.run_id,
        arm_id=check.arm_id,
        severity=severity,
        calendar_session_id=check.calendar_session_id,
        triggered_at=check.scheduled_at,
        trigger_nav_usd=metrics.current_nav_usd,
        session_open_nav_usd=check.session_open_nav_usd,
        running_peak_nav_usd=check.running_peak_nav_usd,
        daily_loss=metrics.daily_loss,
        run_drawdown=metrics.run_drawdown,
        portfolio_annualized_vol=check.portfolio_annualized_vol,
        soft_daily_threshold=metrics.soft_daily_threshold,
        hard_daily_threshold=metrics.hard_daily_threshold,
        reconciliation_status=check.reconciliation_status,
        targets=targets,
        target_manifest_hash=target_manifest_hash,
        episode_hash=episode_hash,
        created_at=check.decision_created_at,
        config_manifest_hash=provenance.config_manifest_hash,
        code_version=provenance.code_version,
        model_version=provenance.model_version,
        source_manifest_hash=provenance.source_manifest_hash,
    )


def _episode_event(
    *,
    episode: RiskEpisode,
    existing_events: Iterable[RiskEpisodeEvent],
    event_type: RiskEpisodeEventType,
    severity: RiskSeverity,
    targets: tuple[RiskTarget, ...],
    check: RiskCheckInput,
    provenance: RiskEpisodeProvenance,
    source_cycle_id: str | None,
    target_symbol: str | None = None,
    observed_quantity: Decimal | None = None,
    residual_quantity: Decimal | None = None,
    consecutive_valid_checks: int = 0,
) -> RiskEpisodeEvent:
    materialized = tuple(existing_events)
    _validate_episode_event_stream(episode, materialized)
    sequence = len(materialized) + 1
    target_generation = (
        targets[0].target_generation
        if targets
        else _episode_target_generation(episode, materialized)
    )
    if any(target.target_generation != target_generation for target in targets):
        raise Q1RiskError("Risk event targets must use one target generation")
    identity = {
        "risk_episode_id": episode.risk_episode_id,
        "event_type": event_type,
        "event_sequence": sequence,
        "severity": severity,
        "target_generation": target_generation,
        "targets": targets,
        "target_symbol": target_symbol,
        "observed_quantity": observed_quantity,
        "residual_quantity": residual_quantity,
        "occurred_at": check.scheduled_at,
        "source_cycle_id": source_cycle_id,
    }
    event_id = stable_id("q1-risk-episode-event", identity)
    return RiskEpisodeEvent(
        risk_episode_event_id=event_id,
        risk_episode_id=episode.risk_episode_id,
        event_type=event_type,
        event_sequence=sequence,
        severity=severity,
        target_generation=target_generation,
        occurred_at=check.scheduled_at,
        available_at=check.decision_created_at,
        targets=targets,
        target_symbol=target_symbol,
        observed_quantity=observed_quantity,
        residual_quantity=residual_quantity,
        consecutive_valid_checks=consecutive_valid_checks,
        source_cycle_id=source_cycle_id,
        worker_fence_token=provenance.worker_fence_token,
        cycle_attempt_count=provenance.cycle_attempt_count,
        idempotency_key=stable_id("q1-risk-event-idem", event_id),
        event_hash=canonical_hash(
            {
                **identity,
                "config_manifest_hash": provenance.config_manifest_hash,
                "code_version": provenance.code_version,
                "model_version": provenance.model_version,
                "source_manifest_hash": provenance.source_manifest_hash,
            }
        ),
        config_manifest_hash=provenance.config_manifest_hash,
        code_version=provenance.code_version,
        model_version=provenance.model_version,
        source_manifest_hash=provenance.source_manifest_hash,
    )


def _episode_targets(
    episode: RiskEpisode,
    events: Iterable[RiskEpisodeEvent],
) -> tuple[RiskTarget, ...]:
    targets = episode.targets
    for event in sorted(events, key=lambda item: item.event_sequence):
        if event.event_type in {
            RiskEpisodeEventType.ACTIVATE,
            RiskEpisodeEventType.ESCALATE,
        }:
            targets = event.targets
    if not targets:
        raise Q1RiskError("Active risk episode cannot have empty targets")
    return targets


def _episode_target_generation(
    episode: RiskEpisode,
    events: Iterable[RiskEpisodeEvent],
) -> int:
    generation = 1
    for event in sorted(events, key=lambda item: item.event_sequence):
        generation = event.target_generation
    return generation


def _episode_severity(
    episode: RiskEpisode | None,
    events: Iterable[RiskEpisodeEvent],
) -> RiskSeverity:
    if episode is None:
        return RiskSeverity.NORMAL
    severity = episode.severity
    released = False
    for event in sorted(events, key=lambda item: item.event_sequence):
        if event.event_type is RiskEpisodeEventType.ESCALATE:
            severity = max(severity, event.severity, key=_severity_rank)
        elif event.event_type is RiskEpisodeEventType.RELEASE:
            released = True
    return RiskSeverity.NORMAL if released else severity


def _can_release(
    *,
    check: RiskCheckInput,
    metrics: RiskMetrics,
    config: RiskEngineConfig,
    active_episode: RiskEpisode,
    is_next_session_strategic_cycle: bool,
    consecutive_valid_release_checks: int,
) -> bool:
    if check.calendar_session_id == active_episode.calendar_session_id:
        return False
    return (
        is_next_session_strategic_cycle
        and check.reconciliation_ok
        and not check.critical_reconciliation_condition
        and consecutive_valid_release_checks
        >= config.release_consecutive_valid_checks
        and metrics.daily_loss
        < config.release_daily_loss_soft_fraction * metrics.soft_daily_threshold
        and metrics.run_drawdown < config.release_drawdown_threshold
    )


def _validate_episode_event_stream(
    episode: RiskEpisode,
    events: Iterable[RiskEpisodeEvent],
) -> None:
    materialized = tuple(events)
    if any(event.risk_episode_id != episode.risk_episode_id for event in materialized):
        raise Q1RiskError("Risk episode event references another episode")
    ids = [event.risk_episode_event_id for event in materialized]
    keys = [event.idempotency_key for event in materialized]
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise Q1RiskError("Duplicate risk episode event")
    ordered = sorted(materialized, key=lambda item: item.event_sequence)
    if any(
        event.event_sequence != expected
        for expected, event in enumerate(ordered, start=1)
    ):
        raise Q1RiskError("Risk episode event sequence must be contiguous")
    released = False
    severity = episode.severity
    generation = 1
    for event in ordered:
        if released:
            raise Q1RiskError("No risk episode event may follow RELEASE")
        if _severity_rank(event.severity) < _severity_rank(severity):
            raise Q1RiskError("Risk episode cannot silently downgrade")
        if event.event_type is RiskEpisodeEventType.ESCALATE:
            if severity is not RiskSeverity.HARD_REDUCE:
                raise Q1RiskError("Only HARD_REDUCE may escalate")
            if event.severity is not RiskSeverity.CRITICAL_EXIT:
                raise Q1RiskError("Only CRITICAL_EXIT escalation is permitted")
            if event.target_generation != generation + 1:
                raise Q1RiskError("Escalation must advance target generation by one")
            generation = event.target_generation
            severity = event.severity
        elif event.event_type is RiskEpisodeEventType.ACTIVATE:
            if event.event_sequence != 1 or event.target_generation != 1:
                raise Q1RiskError("Activation must create target generation one")
        elif event.target_generation != generation:
            raise Q1RiskError("Only escalation may change target generation")
        elif event.event_type is RiskEpisodeEventType.RELEASE:
            released = True


def _latest_observed_quantities(
    events: Iterable[RiskEpisodeEvent],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for event in sorted(events, key=lambda item: item.event_sequence):
        if event.target_symbol is not None and event.observed_quantity is not None:
            result[event.target_symbol] = event.observed_quantity
    return result


def provenance_free_target_identity(
    *,
    arm_id: Q1ArmId,
    symbol: str,
    target_quantity: Decimal,
    quote_id: str,
    triggered_at: datetime,
    target_generation: int,
) -> dict[str, object]:
    return {
        "arm_id": arm_id,
        "symbol": symbol,
        "target_quantity": target_quantity,
        "quote_id": quote_id,
        "triggered_at": triggered_at,
        "target_generation": target_generation,
    }


def _floor_quantity(value: Decimal, precision: Decimal) -> Decimal:
    return value.quantize(precision, rounding=ROUND_DOWN)


def _clip(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return min(upper, max(lower, value))


def _severity_rank(severity: RiskSeverity) -> int:
    return {
        RiskSeverity.NORMAL: 0,
        RiskSeverity.SOFT_STOP: 1,
        RiskSeverity.HARD_REDUCE: 2,
        RiskSeverity.CRITICAL_EXIT: 3,
    }[severity]
