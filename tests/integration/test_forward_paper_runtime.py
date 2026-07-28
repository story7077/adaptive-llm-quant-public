from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading.data.alpaca import parse_stream_message
from trading.data.market_repository import MarketDataRepository
from trading.domain.contracts import MarketQuote
from trading.domain.hashing import canonical_hash, stable_id
from trading.experiments.arms import ArmState
from trading.persistence.models import ArmStateSnapshotRow
from trading.runtime.paper import PaperRuntimeError, PaperRuntimeService
from trading.settings import ConfigBundle

POSITION_SYMBOLS = ("SPY", "QQQ", "IWM", "SMH", "TLT", "HYG", "GLD")


def test_nav_requires_fresh_bundle_and_cycle_scope_is_idempotent(
    sqlite_database,
    config_bundle,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    open_at = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
    available_at = open_at + timedelta(seconds=1)
    repository = MarketDataRepository(factory)
    repository.append(
        quotes=[
            _quote(symbol, event_time=open_at, available_at=available_at)
            for symbol in POSITION_SYMBOLS
        ]
    )
    service = PaperRuntimeService(
        factory,
        config=config_bundle,
        workspace_root=repository_root,
    )
    account_file = repository_root / "config" / "paper-account.example.yaml"
    service.initialize(
        run_id="paper-one",
        account_file=account_file,
        now=open_at - timedelta(minutes=1),
    )
    service.bootstrap_from_fresh_quotes(
        run_id="paper-one",
        session_open_at=open_at,
        account_file=account_file,
        max_quote_age_seconds=15,
        now=open_at + timedelta(seconds=5),
    )

    first = service.record_nav(
        run_id="paper-one",
        as_of=open_at + timedelta(seconds=10),
        snapshot_scope="cycle-nav-1",
        max_quote_age_seconds=15,
    )
    replay = service.record_nav(
        run_id="paper-one",
        as_of=open_at + timedelta(minutes=5),
        snapshot_scope="cycle-nav-1",
        max_quote_age_seconds=15,
    )

    assert len(first) == 8
    assert [item.nav_snapshot_id for item in replay] == [
        item.nav_snapshot_id for item in first
    ]
    with pytest.raises(PaperRuntimeError, match="Stale NAV quote"):
        service.record_nav(
            run_id="paper-one",
            as_of=open_at + timedelta(minutes=1),
            snapshot_scope="cycle-nav-stale",
            max_quote_age_seconds=15,
        )

    service.initialize(
        run_id="paper-two",
        account_file=account_file,
        now=open_at - timedelta(minutes=1),
    )
    service.bootstrap_from_fresh_quotes(
        run_id="paper-two",
        session_open_at=open_at,
        account_file=account_file,
        max_quote_age_seconds=15,
        now=open_at + timedelta(seconds=5),
    )
    second_run = service.record_nav(
        run_id="paper-two",
        as_of=open_at + timedelta(seconds=10),
        snapshot_scope="cycle-nav-1",
        max_quote_age_seconds=15,
    )

    assert {item.nav_snapshot_id for item in first}.isdisjoint(
        item.nav_snapshot_id for item in second_run
    )


def test_nav_excludes_state_snapshots_created_after_nav_time(
    sqlite_database,
    config_bundle,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    run_id = "paper-nav-pit"
    arm_id = "B0-CASH"
    open_at = datetime(2026, 7, 27, 13, 30, tzinfo=UTC)
    repository = MarketDataRepository(factory)
    repository.append(
        quotes=[
            _quote(
                symbol,
                event_time=open_at,
                available_at=open_at + timedelta(seconds=1),
            )
            for symbol in POSITION_SYMBOLS
        ]
    )
    service = PaperRuntimeService(
        factory,
        config=config_bundle,
        workspace_root=repository_root,
    )
    account_file = repository_root / "config" / "paper-account.example.yaml"
    service.initialize(
        run_id=run_id,
        account_file=account_file,
        now=open_at - timedelta(minutes=1),
    )
    completion = service.bootstrap_from_fresh_quotes(
        run_id=run_id,
        session_open_at=open_at,
        account_file=account_file,
        max_quote_age_seconds=15,
        now=open_at + timedelta(seconds=5),
    )
    future_state = ArmState(
        arm_id=arm_id,
        initial_cash_usd=completion.initial_nav_usd,
        cash_usd=Decimal("0"),
        positions={"NVDA": Decimal("1")},
        sequence=1,
    )
    with factory.begin() as session:
        session.add(
            ArmStateSnapshotRow(
                arm_state_snapshot_id=stable_id(
                    "arm-state",
                    run_id,
                    arm_id,
                    future_state.sequence,
                ),
                run_id=run_id,
                arm_id=arm_id,
                sequence=future_state.sequence,
                source_cycle_id=None,
                state_hash=canonical_hash(future_state.as_payload()),
                payload_json=future_state.as_payload(),
                created_at=open_at + timedelta(seconds=11),
            )
        )

    snapshots = service.record_nav(
        run_id=run_id,
        as_of=open_at + timedelta(seconds=10),
        snapshot_scope="cycle-nav-pit",
        max_quote_age_seconds=15,
    )

    selected = next(item for item in snapshots if item.arm_id == arm_id)
    assert selected.nav_usd == completion.initial_nav_usd


def test_existing_run_rejects_config_drift(
    sqlite_database,
    config_bundle,
    repository_root: Path,
) -> None:
    _, _, factory = sqlite_database
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    account_file = repository_root / "config" / "paper-account.example.yaml"
    PaperRuntimeService(
        factory,
        config=config_bundle,
        workspace_root=repository_root,
    ).initialize(run_id="paper-config", account_file=account_file, now=now)

    documents = deepcopy(config_bundle.documents)
    documents["schedules.yaml"]["news_poll_minutes"] = 999
    changed = ConfigBundle(documents=documents, manifest_hash="f" * 64)

    with pytest.raises(PaperRuntimeError, match="different config manifest"):
        PaperRuntimeService(
            factory,
            config=changed,
            workspace_root=repository_root,
        ).initialize(
            run_id="paper-config",
            account_file=account_file,
            now=now,
        )


def _quote(
    symbol: str,
    *,
    event_time: datetime,
    available_at: datetime,
) -> MarketQuote:
    parsed = parse_stream_message(
        {
            "T": "q",
            "S": symbol,
            "bx": "V",
            "bp": "99.90",
            "bs": 10,
            "ax": "V",
            "ap": "100.10",
            "as": 10,
            "c": [],
            "t": event_time.isoformat().replace("+00:00", "Z"),
            "z": "C",
        },
        available_at=available_at,
        raw_object_uri=f"raw://quote/{symbol}",
    )
    assert isinstance(parsed, MarketQuote)
    return parsed
