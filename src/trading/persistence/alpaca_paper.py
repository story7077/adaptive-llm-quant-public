from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from trading.domain.hashing import canonical_hash
from trading.domain.time import require_aware_utc
from trading.persistence.models import (
    PaperBrokerBindingRow,
    PaperBrokerCommandRow,
    PaperBrokerEventRow,
    PortfolioDecisionRow,
)

ALPACA_PAPER_EXECUTION_LANE = "ALPACA_PAPER_CANARY"
ALPACA_PROVIDER = "ALPACA"
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"
ALLOWED_SOURCE_ARMS = frozenset({"Q1-DET", "Q1-LLM"})
ALLOWED_SYMBOLS = frozenset({"QQQ", "SOXX"})
ALLOWED_SIDES = frozenset({"BUY", "SELL"})
ALLOWED_COMMAND_TYPES = frozenset({"SUBMIT", "CANCEL"})
ALLOWED_EVENT_TYPES = frozenset(
    {
        "ACCOUNT_SNAPSHOT",
        "POSITIONS_SNAPSHOT",
        "ORDER_SNAPSHOT",
        "FILL_ACTIVITY",
        "SUBMIT_RECONCILED",
        "CANCEL_REQUEST_ACCEPTED",
        "RECONCILIATION_READY",
        "RECONCILIATION_BLOCKED",
        "RECONCILIATION_FAILED",
        "ORDER_ACKNOWLEDGED",
        "ORDER_STATUS",
        "CANCEL_ACCEPTED",
        "FILL",
    }
)


class AlpacaPaperPersistenceConflict(RuntimeError):
    """Raised when one immutable broker identity has conflicting content."""


class AlpacaPaperRepository:
    """Append and project immutable Alpaca Paper canary records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_binding(
        self,
        *,
        binding_id: str,
        run_id: str,
        source_arm_id: str,
        account_id_hash: str,
        initial_equity_usd: Decimal,
        initial_cash_usd: Decimal,
        config_manifest_hash: str,
        code_version: str,
        binding_hash: str,
        payload_json: dict[str, Any],
        created_at: datetime,
        execution_lane: str = ALPACA_PAPER_EXECUTION_LANE,
        provider: str = ALPACA_PROVIDER,
        base_url: str = ALPACA_PAPER_BASE_URL,
    ) -> PaperBrokerBindingRow:
        instant = require_aware_utc(created_at)
        _reject_sensitive_payload(payload_json)
        self._validate_binding_values(
            binding_id=binding_id,
            run_id=run_id,
            source_arm_id=source_arm_id,
            account_id_hash=account_id_hash,
            initial_equity_usd=initial_equity_usd,
            initial_cash_usd=initial_cash_usd,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            binding_hash=binding_hash,
            execution_lane=execution_lane,
            provider=provider,
            base_url=base_url,
        )
        by_run = self.binding_for_run(run_id)
        by_id = self.binding_by_id(binding_id)
        existing = by_id or by_run
        if existing is not None:
            if not _binding_matches(
                existing,
                binding_id=binding_id,
                run_id=run_id,
                execution_lane=execution_lane,
                source_arm_id=source_arm_id,
                provider=provider,
                account_id_hash=account_id_hash,
                base_url=base_url,
                initial_equity_usd=initial_equity_usd,
                initial_cash_usd=initial_cash_usd,
                config_manifest_hash=config_manifest_hash,
                code_version=code_version,
                binding_hash=binding_hash,
                payload_json=payload_json,
                created_at=instant,
            ):
                raise AlpacaPaperPersistenceConflict(
                    "Alpaca Paper run binding has different immutable content"
                )
            return existing
        hash_match = self._session.scalar(
            select(PaperBrokerBindingRow).where(
                PaperBrokerBindingRow.binding_hash == binding_hash
            )
        )
        if hash_match is not None:
            raise AlpacaPaperPersistenceConflict(
                "Alpaca Paper binding hash belongs to another binding"
            )
        row = PaperBrokerBindingRow(
            binding_id=binding_id,
            run_id=run_id,
            execution_lane=execution_lane,
            source_arm_id=source_arm_id,
            provider=provider,
            account_id_hash=account_id_hash,
            base_url=base_url,
            initial_equity_usd=initial_equity_usd,
            initial_cash_usd=initial_cash_usd,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            binding_hash=binding_hash,
            payload_json=payload_json,
            created_at=instant,
        )
        self._session.add(row)
        return row

    def binding_for_run(self, run_id: str) -> PaperBrokerBindingRow | None:
        return self._session.scalar(
            select(PaperBrokerBindingRow).where(
                PaperBrokerBindingRow.run_id == run_id
            )
        )

    def binding_by_id(
        self,
        binding_id: str,
    ) -> PaperBrokerBindingRow | None:
        return self._session.get(PaperBrokerBindingRow, binding_id)

    def append_command(
        self,
        *,
        command_id: str,
        binding_id: str,
        run_id: str,
        source_decision_id: str,
        command_type: str,
        client_order_id: str | None,
        broker_order_id: str | None,
        symbol: str,
        side: str,
        quantity: Decimal,
        limit_price: Decimal | None,
        reason: str,
        idempotency_key: str,
        config_manifest_hash: str,
        code_version: str,
        source_manifest_hash: str,
        command_hash: str,
        payload_json: dict[str, Any],
        created_at: datetime,
    ) -> PaperBrokerCommandRow:
        instant = require_aware_utc(created_at)
        _reject_sensitive_payload(payload_json)
        self._validate_command_values(
            command_id=command_id,
            binding_id=binding_id,
            run_id=run_id,
            source_decision_id=source_decision_id,
            command_type=command_type,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            reason=reason,
            idempotency_key=idempotency_key,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
            command_hash=command_hash,
        )
        binding = self._required_binding(binding_id)
        if binding.run_id != run_id:
            raise AlpacaPaperPersistenceConflict(
                "Broker command run does not match its binding"
            )
        if (
            binding.config_manifest_hash != config_manifest_hash
            or binding.code_version != code_version
        ):
            raise AlpacaPaperPersistenceConflict(
                "Broker command versions do not match its binding"
            )
        decision = self._session.get(
            PortfolioDecisionRow,
            source_decision_id,
        )
        if decision is None:
            raise AlpacaPaperPersistenceConflict(
                "Broker command references an unknown portfolio decision"
            )
        if (
            decision.run_id != run_id
            or decision.arm_id != binding.source_arm_id
        ):
            raise AlpacaPaperPersistenceConflict(
                "Broker command decision is outside its bound run and arm"
            )

        existing = self.command_by_id(command_id)
        if existing is None:
            existing = self._session.scalar(
                select(PaperBrokerCommandRow).where(
                    PaperBrokerCommandRow.idempotency_key
                    == idempotency_key
                )
            )
        if existing is not None:
            if not _command_matches(
                existing,
                command_id=command_id,
                binding_id=binding_id,
                run_id=run_id,
                source_decision_id=source_decision_id,
                command_type=command_type,
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                reason=reason,
                idempotency_key=idempotency_key,
                config_manifest_hash=config_manifest_hash,
                code_version=code_version,
                source_manifest_hash=source_manifest_hash,
                command_hash=command_hash,
                payload_json=payload_json,
                created_at=instant,
            ):
                raise AlpacaPaperPersistenceConflict(
                    "Alpaca Paper command idempotency conflict"
                )
            return existing
        row = PaperBrokerCommandRow(
            command_id=command_id,
            binding_id=binding_id,
            run_id=run_id,
            source_decision_id=source_decision_id,
            command_type=command_type,
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            reason=reason,
            idempotency_key=idempotency_key,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
            command_hash=command_hash,
            payload_json=payload_json,
            created_at=instant,
        )
        self._session.add(row)
        return row

    def command_by_id(
        self,
        command_id: str,
    ) -> PaperBrokerCommandRow | None:
        return self._session.get(PaperBrokerCommandRow, command_id)

    def commands(
        self,
        *,
        binding_id: str,
        command_type: str | None = None,
        created_after: datetime | None = None,
    ) -> tuple[PaperBrokerCommandRow, ...]:
        statement: Select[tuple[PaperBrokerCommandRow]] = select(
            PaperBrokerCommandRow
        ).where(PaperBrokerCommandRow.binding_id == binding_id)
        if command_type is not None:
            if command_type not in ALLOWED_COMMAND_TYPES:
                raise ValueError(
                    f"Unsupported Alpaca Paper command type {command_type!r}"
                )
            statement = statement.where(
                PaperBrokerCommandRow.command_type == command_type
            )
        if created_after is not None:
            statement = statement.where(
                PaperBrokerCommandRow.created_at
                > require_aware_utc(created_after)
            )
        return tuple(
            self._session.scalars(
                statement.order_by(
                    PaperBrokerCommandRow.created_at,
                    PaperBrokerCommandRow.command_id,
                )
            )
        )

    def append_event(
        self,
        *,
        event_id: str,
        binding_id: str,
        run_id: str,
        command_id: str | None,
        event_type: str,
        status: str,
        broker_order_id: str | None,
        client_order_id: str | None,
        provider_event_id: str | None,
        symbol: str | None,
        side: str | None,
        quantity: Decimal | None,
        filled_quantity: Decimal | None,
        fill_price: Decimal | None,
        occurred_at: datetime,
        available_at: datetime,
        provider_request_id: str | None,
        idempotency_key: str,
        config_manifest_hash: str,
        code_version: str,
        source_manifest_hash: str,
        event_hash: str,
        payload_json: dict[str, Any],
        created_at: datetime,
    ) -> PaperBrokerEventRow:
        occurrence = require_aware_utc(occurred_at)
        availability = require_aware_utc(available_at)
        creation = require_aware_utc(created_at)
        _reject_sensitive_payload(payload_json)
        self._validate_event_values(
            event_id=event_id,
            binding_id=binding_id,
            run_id=run_id,
            event_type=event_type,
            status=status,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            provider_event_id=provider_event_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            occurred_at=occurrence,
            available_at=availability,
            created_at=creation,
            idempotency_key=idempotency_key,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
            event_hash=event_hash,
            command_id=command_id,
        )
        binding = self._required_binding(binding_id)
        if binding.run_id != run_id:
            raise AlpacaPaperPersistenceConflict(
                "Broker event run does not match its binding"
            )
        if (
            binding.config_manifest_hash != config_manifest_hash
            or binding.code_version != code_version
        ):
            raise AlpacaPaperPersistenceConflict(
                "Broker event versions do not match its binding"
            )
        command = (
            None
            if command_id is None
            else self._required_command(command_id)
        )
        if command is not None:
            if command.binding_id != binding_id or command.run_id != run_id:
                raise AlpacaPaperPersistenceConflict(
                    "Broker event command is outside its binding"
                )
            if (
                command.config_manifest_hash != config_manifest_hash
                or command.code_version != code_version
            ):
                raise AlpacaPaperPersistenceConflict(
                    "Broker event versions do not match its command"
                )
            if (
                client_order_id is not None
                and command.client_order_id is not None
                and client_order_id != command.client_order_id
            ):
                raise AlpacaPaperPersistenceConflict(
                    "Broker event client order ID conflicts with its command"
                )
            if (
                broker_order_id is not None
                and command.broker_order_id is not None
                and broker_order_id != command.broker_order_id
            ):
                raise AlpacaPaperPersistenceConflict(
                    "Broker event order ID conflicts with its command"
                )
            if symbol is not None and symbol != command.symbol:
                raise AlpacaPaperPersistenceConflict(
                    "Broker event symbol conflicts with its command"
                )
            if side is not None and side != command.side:
                raise AlpacaPaperPersistenceConflict(
                    "Broker event side conflicts with its command"
                )

        existing = self._session.get(PaperBrokerEventRow, event_id)
        if existing is None:
            existing = self._session.scalar(
                select(PaperBrokerEventRow).where(
                    PaperBrokerEventRow.binding_id == binding_id,
                    PaperBrokerEventRow.idempotency_key
                    == idempotency_key,
                )
            )
        if existing is None and provider_event_id is not None:
            existing = self._session.scalar(
                select(PaperBrokerEventRow).where(
                    PaperBrokerEventRow.binding_id == binding_id,
                    PaperBrokerEventRow.provider_event_id
                    == provider_event_id,
                )
            )
        if existing is not None:
            if not _event_matches(
                existing,
                event_id=event_id,
                binding_id=binding_id,
                run_id=run_id,
                command_id=command_id,
                event_type=event_type,
                status=status,
                broker_order_id=broker_order_id,
                client_order_id=client_order_id,
                provider_event_id=provider_event_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                filled_quantity=filled_quantity,
                fill_price=fill_price,
                occurred_at=occurrence,
                available_at=availability,
                provider_request_id=provider_request_id,
                idempotency_key=idempotency_key,
                config_manifest_hash=config_manifest_hash,
                code_version=code_version,
                source_manifest_hash=source_manifest_hash,
                event_hash=event_hash,
                payload_json=payload_json,
                created_at=creation,
            ):
                raise AlpacaPaperPersistenceConflict(
                    "Alpaca Paper event idempotency conflict"
                )
            return existing
        row = PaperBrokerEventRow(
            event_id=event_id,
            binding_id=binding_id,
            run_id=run_id,
            command_id=command_id,
            event_type=event_type,
            status=status,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            provider_event_id=provider_event_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            occurred_at=occurrence,
            available_at=availability,
            provider_request_id=provider_request_id,
            idempotency_key=idempotency_key,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
            event_hash=event_hash,
            payload_json=payload_json,
            created_at=creation,
        )
        self._session.add(row)
        return row

    def events(
        self,
        *,
        binding_id: str,
        command_id: str | None = None,
        as_of: datetime | None = None,
    ) -> tuple[PaperBrokerEventRow, ...]:
        statement: Select[tuple[PaperBrokerEventRow]] = select(
            PaperBrokerEventRow
        ).where(PaperBrokerEventRow.binding_id == binding_id)
        if command_id is not None:
            statement = statement.where(
                PaperBrokerEventRow.command_id == command_id
            )
        if as_of is not None:
            statement = statement.where(
                PaperBrokerEventRow.available_at
                <= require_aware_utc(as_of)
            )
        return tuple(
            self._session.scalars(
                statement.order_by(
                    PaperBrokerEventRow.available_at,
                    PaperBrokerEventRow.created_at,
                    PaperBrokerEventRow.event_id,
                )
            )
        )

    def latest_status(
        self,
        *,
        binding_id: str,
        command_id: str | None = None,
        client_order_id: str | None = None,
        broker_order_id: str | None = None,
        as_of: datetime | None = None,
    ) -> PaperBrokerEventRow | None:
        selectors = tuple(
            value
            for value in (
                command_id,
                client_order_id,
                broker_order_id,
            )
            if value is not None
        )
        if len(selectors) > 1:
            raise ValueError(
                "Select latest broker status by at most one order identity"
            )
        statement: Select[tuple[PaperBrokerEventRow]] = select(
            PaperBrokerEventRow
        ).where(PaperBrokerEventRow.binding_id == binding_id)
        if command_id is not None:
            statement = statement.where(
                PaperBrokerEventRow.command_id == command_id
            )
        elif client_order_id is not None:
            statement = statement.where(
                PaperBrokerEventRow.client_order_id == client_order_id
            )
        elif broker_order_id is not None:
            statement = statement.where(
                PaperBrokerEventRow.broker_order_id == broker_order_id
            )
        if as_of is not None:
            statement = statement.where(
                PaperBrokerEventRow.available_at
                <= require_aware_utc(as_of)
            )
        return self._session.scalar(
            statement.order_by(
                PaperBrokerEventRow.available_at.desc(),
                PaperBrokerEventRow.created_at.desc(),
                PaperBrokerEventRow.event_id.desc(),
            ).limit(1)
        )

    def _required_binding(
        self,
        binding_id: str,
    ) -> PaperBrokerBindingRow:
        binding = self.binding_by_id(binding_id)
        if binding is None:
            raise AlpacaPaperPersistenceConflict(
                f"Unknown Alpaca Paper binding {binding_id!r}"
            )
        return binding

    def _required_command(
        self,
        command_id: str,
    ) -> PaperBrokerCommandRow:
        command = self.command_by_id(command_id)
        if command is None:
            raise AlpacaPaperPersistenceConflict(
                f"Unknown Alpaca Paper command {command_id!r}"
            )
        return command

    @staticmethod
    def _validate_binding_values(
        *,
        binding_id: str,
        run_id: str,
        source_arm_id: str,
        account_id_hash: str,
        initial_equity_usd: Decimal,
        initial_cash_usd: Decimal,
        config_manifest_hash: str,
        code_version: str,
        binding_hash: str,
        execution_lane: str,
        provider: str,
        base_url: str,
    ) -> None:
        _require_strings(
            binding_id=binding_id,
            run_id=run_id,
            account_id_hash=account_id_hash,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            binding_hash=binding_hash,
        )
        if execution_lane != ALPACA_PAPER_EXECUTION_LANE:
            raise ValueError("Only ALPACA_PAPER_CANARY bindings are allowed")
        if provider != ALPACA_PROVIDER:
            raise ValueError("Only the ALPACA paper provider is allowed")
        if base_url != ALPACA_PAPER_BASE_URL:
            raise ValueError("Alpaca Paper binding must use the paper host")
        if source_arm_id not in ALLOWED_SOURCE_ARMS:
            raise ValueError("Alpaca Paper source arm must be Q1-DET or Q1-LLM")
        if initial_equity_usd <= 0 or initial_cash_usd < 0:
            raise ValueError("Alpaca Paper opening balances are invalid")

    @staticmethod
    def _validate_command_values(
        *,
        command_id: str,
        binding_id: str,
        run_id: str,
        source_decision_id: str,
        command_type: str,
        client_order_id: str | None,
        broker_order_id: str | None,
        symbol: str,
        side: str,
        quantity: Decimal,
        limit_price: Decimal | None,
        reason: str,
        idempotency_key: str,
        config_manifest_hash: str,
        code_version: str,
        source_manifest_hash: str,
        command_hash: str,
    ) -> None:
        _require_strings(
            command_id=command_id,
            binding_id=binding_id,
            run_id=run_id,
            source_decision_id=source_decision_id,
            reason=reason,
            idempotency_key=idempotency_key,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
            command_hash=command_hash,
        )
        if command_type not in ALLOWED_COMMAND_TYPES:
            raise ValueError(
                f"Unsupported Alpaca Paper command type {command_type!r}"
            )
        if symbol not in ALLOWED_SYMBOLS or side not in ALLOWED_SIDES:
            raise ValueError("Alpaca Paper command is outside its ETF lane")
        if quantity <= 0:
            raise ValueError("Alpaca Paper command quantity must be positive")
        if limit_price is not None and limit_price <= 0:
            raise ValueError("Alpaca Paper limit price must be positive")
        if command_type == "SUBMIT":
            if (
                client_order_id is None
                or broker_order_id is not None
                or limit_price is None
            ):
                raise ValueError(
                    "SUBMIT requires client ID and limit price only"
                )
            if len(client_order_id) > 128:
                raise ValueError("Alpaca Paper client order ID is too long")
        elif broker_order_id is None:
            raise ValueError("CANCEL requires a broker order ID")

    @staticmethod
    def _validate_event_values(
        *,
        event_id: str,
        binding_id: str,
        run_id: str,
        command_id: str | None,
        event_type: str,
        status: str,
        broker_order_id: str | None,
        client_order_id: str | None,
        provider_event_id: str | None,
        symbol: str | None,
        side: str | None,
        quantity: Decimal | None,
        filled_quantity: Decimal | None,
        fill_price: Decimal | None,
        occurred_at: datetime,
        available_at: datetime,
        created_at: datetime,
        idempotency_key: str,
        config_manifest_hash: str,
        code_version: str,
        source_manifest_hash: str,
        event_hash: str,
    ) -> None:
        _require_strings(
            event_id=event_id,
            binding_id=binding_id,
            run_id=run_id,
            event_type=event_type,
            status=status,
            idempotency_key=idempotency_key,
            config_manifest_hash=config_manifest_hash,
            code_version=code_version,
            source_manifest_hash=source_manifest_hash,
            event_hash=event_hash,
        )
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(
                f"Unsupported Alpaca Paper event type {event_type!r}"
            )
        if symbol is not None and symbol not in ALLOWED_SYMBOLS:
            raise ValueError("Alpaca Paper event symbol is outside its ETF lane")
        if side is not None and side not in ALLOWED_SIDES:
            raise ValueError("Alpaca Paper event side is invalid")
        if quantity is not None and quantity <= 0:
            raise ValueError("Alpaca Paper event quantity must be positive")
        if filled_quantity is not None and filled_quantity < 0:
            raise ValueError(
                "Alpaca Paper filled quantity must be non-negative"
            )
        if fill_price is not None and fill_price <= 0:
            raise ValueError("Alpaca Paper fill price must be positive")
        if available_at < occurred_at or created_at < available_at:
            raise ValueError("Alpaca Paper event timestamps are inconsistent")
        if client_order_id is not None and len(client_order_id) > 128:
            raise ValueError("Alpaca Paper client order ID is too long")
        if event_type in {"FILL", "FILL_ACTIVITY"} and any(
            value is None
            for value in (
                command_id,
                provider_event_id,
                broker_order_id,
                symbol,
                side,
                quantity,
                filled_quantity,
                fill_price,
            )
        ):
            raise ValueError("Alpaca Paper FILL event is incomplete")


def _binding_matches(
    row: PaperBrokerBindingRow,
    **expected: object,
) -> bool:
    return _row_matches(row, expected)


def _command_matches(
    row: PaperBrokerCommandRow,
    **expected: object,
) -> bool:
    return _row_matches(row, expected)


def _event_matches(
    row: PaperBrokerEventRow,
    **expected: object,
) -> bool:
    return _row_matches(row, expected)


def _row_matches(row: object, expected: dict[str, object]) -> bool:
    for name, value in expected.items():
        actual = getattr(row, name)
        if name == "payload_json":
            if canonical_hash(actual) != canonical_hash(value):
                return False
        elif isinstance(value, datetime):
            if _aware(actual) != value:
                return False
        elif actual != value:
            return False
    return True


def _require_strings(**values: str) -> None:
    empty = sorted(name for name, value in values.items() if not value)
    if empty:
        raise ValueError(
            f"Alpaca Paper persistence requires non-empty {empty}"
        )


_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "api_key_id",
        "key_id",
        "secret",
        "secret_key",
        "credentials",
        "credential",
        "authorization",
        "auth_header",
        "auth_headers",
        "headers",
        "password",
        "access_token",
        "refresh_token",
        "bearer_token",
    }
)


def _reject_sensitive_payload(value: object) -> None:
    if isinstance(value, dict):
        mapping = cast(dict[object, object], value)
        for raw_key, item in mapping.items():
            normalized = str(raw_key).strip().lower().replace("-", "_")
            if (
                normalized in _SENSITIVE_PAYLOAD_KEYS
                or normalized.endswith("_secret")
                or normalized.endswith("_secret_key")
                or normalized.endswith("_api_key")
                or normalized.endswith("_access_token")
                or normalized.endswith("_refresh_token")
                or normalized.endswith("_authorization")
                or normalized.endswith("_auth_headers")
            ):
                raise ValueError(
                    "Alpaca Paper persistence rejects credential-bearing payloads"
                )
            _reject_sensitive_payload(item)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for item in cast(Sequence[object], value):
            _reject_sensitive_payload(item)


def _aware(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise AlpacaPaperPersistenceConflict(
            "Stored Alpaca Paper timestamp is invalid"
        )
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
