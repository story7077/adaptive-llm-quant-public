from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.domain.contracts import model_payload
from trading.domain.hashing import canonical_hash, stable_id
from trading.domain.paper import (
    PaperAccountSpec,
    PaperBootstrapCompletion,
    PaperBootstrapMark,
    PaperCashSpec,
    PaperPositionSpec,
)
from trading.domain.time import SystemClock, require_aware_utc
from trading.persistence.models import (
    PaperAccountSpecRow,
    PaperBootstrapCompletionRow,
    PaperBootstrapMarkRow,
    PaperCashBalanceRow,
    PaperPositionRow,
    RunRow,
)

NAV_QUANTUM = Decimal("0.0000000001")


class PaperAccountConfigError(ValueError):
    pass


class PaperAccountVersionConflict(RuntimeError):
    pass


class PaperBootstrapError(RuntimeError):
    pass


class PaperBootstrapConflict(PaperBootstrapError):
    pass


@dataclass(frozen=True, slots=True)
class ProvisionedPaperAccount:
    account_spec_id: str
    spec: PaperAccountSpec
    created: bool


@dataclass(frozen=True, slots=True)
class PaperBootstrapResult:
    completion: PaperBootstrapCompletion
    created: bool


def load_paper_account_spec(path: Path) -> PaperAccountSpec:
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise PaperAccountConfigError(
            f"Cannot read paper account config: {path}"
        ) from exc
    if not isinstance(document, dict):
        raise PaperAccountConfigError("Paper account config root must be an object")
    try:
        return PaperAccountSpec.model_validate(document)
    except ValidationError as exc:
        raise PaperAccountConfigError(
            f"Invalid paper account config: {path}"
        ) from exc


class PaperBootstrapRepository:
    """Append-only persistence operations for the paper account's T0 state."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def account_for_version(
        self,
        *,
        account_id: str,
        version: int,
    ) -> PaperAccountSpecRow | None:
        return self._session.scalar(
            select(PaperAccountSpecRow).where(
                PaperAccountSpecRow.account_id == account_id,
                PaperAccountSpecRow.version == version,
            )
        )

    def account(self, account_spec_id: str) -> PaperAccountSpecRow | None:
        return self._session.get(PaperAccountSpecRow, account_spec_id)

    def cash(self, account_spec_id: str) -> list[PaperCashBalanceRow]:
        return list(
            self._session.scalars(
                select(PaperCashBalanceRow)
                .where(PaperCashBalanceRow.account_spec_id == account_spec_id)
                .order_by(PaperCashBalanceRow.currency)
            )
        )

    def positions(self, account_spec_id: str) -> list[PaperPositionRow]:
        return list(
            self._session.scalars(
                select(PaperPositionRow)
                .where(PaperPositionRow.account_spec_id == account_spec_id)
                .order_by(PaperPositionRow.symbol)
            )
        )

    def append_account(
        self,
        *,
        account_spec_id: str,
        spec: PaperAccountSpec,
        config_hash: str,
        created_at: datetime,
    ) -> None:
        self._session.add(
            PaperAccountSpecRow(
                account_spec_id=account_spec_id,
                account_id=spec.account_id,
                version=spec.version,
                schema_version=spec.schema_version,
                base_currency=spec.base_currency,
                source=spec.source,
                config_hash=config_hash,
                created_at=created_at,
                payload_json=model_payload(spec),
            )
        )
        for item in spec.cash:
            self._session.add(
                PaperCashBalanceRow(
                    cash_balance_id=stable_id(
                        "paper-cash",
                        account_spec_id,
                        item.currency,
                    ),
                    account_spec_id=account_spec_id,
                    currency=item.currency,
                    amount=item.amount,
                    tradable=item.tradable,
                    exclusion_reason=item.exclusion_reason,
                    created_at=created_at,
                    payload_json=model_payload(item),
                )
            )
        for item in spec.positions:
            self._session.add(
                PaperPositionRow(
                    paper_position_id=stable_id(
                        "paper-position",
                        account_spec_id,
                        item.symbol,
                    ),
                    account_spec_id=account_spec_id,
                    symbol=item.symbol,
                    quantity=item.quantity,
                    currency=item.currency,
                    created_at=created_at,
                    payload_json=model_payload(item),
                )
            )

    def completion_for_run(
        self,
        run_id: str,
    ) -> PaperBootstrapCompletionRow | None:
        return self._session.scalar(
            select(PaperBootstrapCompletionRow).where(
                PaperBootstrapCompletionRow.run_id == run_id
            )
        )

    def append_completion(
        self,
        *,
        marks: list[PaperBootstrapMark],
        completion: PaperBootstrapCompletion,
    ) -> None:
        for mark in marks:
            self._session.add(
                PaperBootstrapMarkRow(
                    bootstrap_mark_id=mark.bootstrap_mark_id,
                    run_id=mark.run_id,
                    account_spec_id=mark.account_spec_id,
                    symbol=mark.symbol,
                    price=mark.price,
                    currency=mark.currency,
                    marked_at=mark.marked_at,
                    source_kind=mark.source_kind,
                    source_record_id=mark.source_record_id,
                    payload_hash=mark.payload_hash,
                    created_at=mark.created_at,
                    payload_json=model_payload(mark),
                )
            )
        self._session.add(
            PaperBootstrapCompletionRow(
                bootstrap_completion_id=completion.bootstrap_completion_id,
                run_id=completion.run_id,
                account_spec_id=completion.account_spec_id,
                common_mark_at=completion.common_mark_at,
                initial_nav_usd=completion.initial_nav_usd,
                input_manifest_hash=completion.input_manifest_hash,
                completed_at=completion.completed_at,
                payload_json=model_payload(completion),
            )
        )


class PaperBootstrapService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._clock = SystemClock()

    def provision_from_file(
        self,
        path: Path,
        *,
        now: datetime | None = None,
    ) -> ProvisionedPaperAccount:
        return self.provision(load_paper_account_spec(path), now=now)

    def provision(
        self,
        spec: PaperAccountSpec,
        *,
        now: datetime | None = None,
    ) -> ProvisionedPaperAccount:
        created_at = self._now(now)
        config_hash = canonical_hash(spec)
        account_spec_id = stable_id(
            "paper-account",
            spec.account_id,
            spec.version,
            config_hash,
        )
        with self._session_factory.begin() as session:
            repository = PaperBootstrapRepository(session)
            existing = repository.account_for_version(
                account_id=spec.account_id,
                version=spec.version,
            )
            if existing is not None:
                if existing.config_hash != config_hash:
                    raise PaperAccountVersionConflict(
                        f"Paper account {spec.account_id!r} version "
                        f"{spec.version} already has different content"
                    )
                self._validate_existing_account(repository, existing, spec)
                return ProvisionedPaperAccount(
                    account_spec_id=existing.account_spec_id,
                    spec=spec,
                    created=False,
                )
            repository.append_account(
                account_spec_id=account_spec_id,
                spec=spec,
                config_hash=config_hash,
                created_at=created_at,
            )
            session.flush()
        return ProvisionedPaperAccount(
            account_spec_id=account_spec_id,
            spec=spec,
            created=True,
        )

    def complete(
        self,
        *,
        run_id: str,
        account_spec_id: str,
        prices: Mapping[str, Decimal],
        common_mark_at: datetime,
        source_kind: str,
        source_record_ids: Mapping[str, str | None] | None = None,
        now: datetime | None = None,
    ) -> PaperBootstrapResult:
        marked_at = require_aware_utc(common_mark_at, "common_mark_at")
        completed_at = self._now(now)
        if completed_at < marked_at:
            raise PaperBootstrapError("Bootstrap cannot complete before its common mark")

        with self._session_factory.begin() as session:
            repository = PaperBootstrapRepository(session)
            account_row = repository.account(account_spec_id)
            if account_row is None:
                raise PaperBootstrapError(
                    f"Unknown paper account spec: {account_spec_id}"
                )
            run = session.get(RunRow, run_id)
            if run is None:
                raise PaperBootstrapError(f"Unknown run_id: {run_id}")
            if run.mode != "PAPER":
                raise PaperBootstrapError("Paper bootstrap requires a PAPER run")

            spec = PaperAccountSpec.model_validate(account_row.payload_json)
            symbols = {item.symbol for item in spec.positions}
            supplied_symbols = set(prices)
            if supplied_symbols != symbols:
                missing = sorted(symbols - supplied_symbols)
                extra = sorted(supplied_symbols - symbols)
                raise PaperBootstrapError(
                    f"Bootstrap marks must exactly cover positions; "
                    f"missing={missing}, extra={extra}"
                )
            source_ids = dict(source_record_ids or {})
            unknown_sources = sorted(set(source_ids) - symbols)
            if unknown_sources:
                raise PaperBootstrapError(
                    f"Source IDs contain unknown symbols: {unknown_sources}"
                )

            marks = self._build_marks(
                run_id=run_id,
                account_spec_id=account_spec_id,
                spec=spec,
                prices=prices,
                marked_at=marked_at,
                source_kind=source_kind,
                source_record_ids=source_ids,
                created_at=completed_at,
            )
            initial_nav = self._initial_nav(spec, prices)
            manifest = {
                "schema_version": "paper-bootstrap-manifest.v1",
                "run_id": run_id,
                "account_spec_id": account_spec_id,
                "account_config_hash": account_row.config_hash,
                "common_mark_at": marked_at,
                "tradable_base_cash": next(
                    item.amount
                    for item in spec.cash
                    if item.currency == spec.base_currency and item.tradable
                ),
                "marks": [
                    {
                        "bootstrap_mark_id": mark.bootstrap_mark_id,
                        "symbol": mark.symbol,
                        "price": mark.price,
                        "currency": mark.currency,
                        "marked_at": mark.marked_at,
                        "source_kind": mark.source_kind,
                        "source_record_id": mark.source_record_id,
                        "payload_hash": mark.payload_hash,
                    }
                    for mark in marks
                ],
            }
            input_manifest_hash = canonical_hash(manifest)
            completion = PaperBootstrapCompletion(
                bootstrap_completion_id=stable_id(
                    "paper-bootstrap-completion",
                    run_id,
                    input_manifest_hash,
                ),
                run_id=run_id,
                account_spec_id=account_spec_id,
                common_mark_at=marked_at,
                initial_nav_usd=initial_nav,
                input_manifest_hash=input_manifest_hash,
                mark_ids=[mark.bootstrap_mark_id for mark in marks],
                completed_at=completed_at,
            )

            existing = repository.completion_for_run(run_id)
            if existing is not None:
                if (
                    existing.account_spec_id != account_spec_id
                    or existing.input_manifest_hash != input_manifest_hash
                ):
                    raise PaperBootstrapConflict(
                        f"Run {run_id!r} already has a different bootstrap"
                    )
                return PaperBootstrapResult(
                    completion=PaperBootstrapCompletion.model_validate(
                        existing.payload_json
                    ),
                    created=False,
                )

            repository.append_completion(marks=marks, completion=completion)
            session.flush()
        return PaperBootstrapResult(completion=completion, created=True)

    @staticmethod
    def _build_marks(
        *,
        run_id: str,
        account_spec_id: str,
        spec: PaperAccountSpec,
        prices: Mapping[str, Decimal],
        marked_at: datetime,
        source_kind: str,
        source_record_ids: Mapping[str, str | None],
        created_at: datetime,
    ) -> list[PaperBootstrapMark]:
        marks: list[PaperBootstrapMark] = []
        positions = {item.symbol: item for item in spec.positions}
        for symbol in sorted(positions):
            position = positions[symbol]
            mark_payload = {
                "run_id": run_id,
                "account_spec_id": account_spec_id,
                "symbol": symbol,
                "price": prices[symbol],
                "currency": position.currency,
                "marked_at": marked_at,
                "source_kind": source_kind,
                "source_record_id": source_record_ids.get(symbol),
            }
            payload_hash = canonical_hash(mark_payload)
            marks.append(
                PaperBootstrapMark(
                    bootstrap_mark_id=stable_id(
                        "paper-bootstrap-mark",
                        run_id,
                        symbol,
                        payload_hash,
                    ),
                    payload_hash=payload_hash,
                    created_at=created_at,
                    run_id=run_id,
                    account_spec_id=account_spec_id,
                    symbol=symbol,
                    price=prices[symbol],
                    currency=position.currency,
                    marked_at=marked_at,
                    source_kind=source_kind,
                    source_record_id=source_record_ids.get(symbol),
                )
            )
        return marks

    @staticmethod
    def _initial_nav(
        spec: PaperAccountSpec,
        prices: Mapping[str, Decimal],
    ) -> Decimal:
        tradable_cash = sum(
            (
                item.amount
                for item in spec.cash
                if item.currency == spec.base_currency and item.tradable
            ),
            Decimal("0"),
        )
        positions = sum(
            (item.quantity * prices[item.symbol] for item in spec.positions),
            Decimal("0"),
        )
        return (tradable_cash + positions).quantize(
            NAV_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )

    @staticmethod
    def _validate_existing_account(
        repository: PaperBootstrapRepository,
        row: PaperAccountSpecRow,
        spec: PaperAccountSpec,
    ) -> None:
        stored_spec = PaperAccountSpec.model_validate(row.payload_json)
        if canonical_hash(stored_spec) != canonical_hash(spec):
            raise PaperAccountVersionConflict(
                f"Paper account row {row.account_spec_id!r} payload is inconsistent"
            )
        stored_cash = {
            item.currency: canonical_hash(item)
            for item in (
                PaperCashSpec.model_validate(child.payload_json)
                for child in repository.cash(row.account_spec_id)
            )
        }
        expected_cash = {
            item.currency: canonical_hash(item)
            for item in spec.cash
        }
        stored_positions = {
            item.symbol: canonical_hash(item)
            for item in (
                PaperPositionSpec.model_validate(child.payload_json)
                for child in repository.positions(row.account_spec_id)
            )
        }
        expected_positions = {
            item.symbol: canonical_hash(item)
            for item in spec.positions
        }
        if stored_cash != expected_cash or stored_positions != expected_positions:
            raise PaperAccountVersionConflict(
                f"Paper account row {row.account_spec_id!r} children are inconsistent"
            )

    def _now(self, value: datetime | None) -> datetime:
        return self._clock.now() if value is None else require_aware_utc(value)
