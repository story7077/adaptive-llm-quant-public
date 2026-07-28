from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any, cast

from trading.domain.q1 import Q1ArmId

ALPACA_PAPER_CONFIG_FILE = "alpaca-paper.yaml"


class AlpacaPaperConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AlpacaPaperCanaryConfig:
    version: str
    execution_lane: str
    provider: str
    rest_base_url: str
    source_arm: Q1ArmId
    allowed_symbols: tuple[str, ...]
    require_clean_account_on_first_bind: bool
    require_dedicated_account: bool
    reject_foreign_open_orders: bool
    reject_foreign_positions: bool
    order_type: str
    time_in_force: str
    extended_hours: bool
    whole_shares_only: bool
    minimum_order_notional_usd: Decimal
    limit_offset_bps: Decimal
    price_increment_usd: Decimal
    client_order_id_prefix: str
    maximum_client_order_id_length: int
    maximum_open_orders_per_symbol: int
    no_risk_increase_after_et: time
    request_timeout_seconds: Decimal
    reconciliation_lookup_attempts: int
    reconciliation_lookup_interval_seconds: Decimal
    maximum_consecutive_failures: int
    maximum_quote_age_seconds: int
    maximum_multi_symbol_quote_skew_seconds: int
    required_stream_status: str
    account_snapshot_interval_seconds: int
    order_history_limit: int
    fill_activity_page_size: int
    unknown_outcome_state: str


def parse_alpaca_paper_config(
    document: dict[str, Any],
) -> AlpacaPaperCanaryConfig:
    if document.get("version") != "alpaca_paper_canary_v1":
        raise AlpacaPaperConfigError(
            "Alpaca Paper config version must be alpaca_paper_canary_v1"
        )
    if document.get("execution_lane") != "ALPACA_PAPER_CANARY":
        raise AlpacaPaperConfigError(
            "Alpaca Paper execution lane must be ALPACA_PAPER_CANARY"
        )
    if document.get("provider") != "ALPACA":
        raise AlpacaPaperConfigError("Alpaca Paper provider must be ALPACA")
    if document.get("rest_base_url") != "https://paper-api.alpaca.markets":
        raise AlpacaPaperConfigError(
            "Alpaca Paper rest_base_url must use the exact paper host"
        )
    if document.get("real_order_routing") is not False:
        raise AlpacaPaperConfigError(
            "Alpaca Paper canary must keep real_order_routing=false"
        )

    activation = _section(document, "activation")
    orders = _section(document, "orders")
    transport = _section(document, "transport")
    market_data = _section(document, "market_data")
    reconciliation = _section(document, "reconciliation")
    source_arm = Q1ArmId(_string(document, "source_arm"))
    if source_arm not in {Q1ArmId.Q1_DET, Q1ArmId.Q1_LLM}:
        raise AlpacaPaperConfigError(
            "Alpaca Paper source arm must be Q1-DET or Q1-LLM"
        )
    symbols = tuple(
        str(value).strip().upper()
        for value in _list(document, "allowed_symbols")
    )
    if symbols != ("QQQ", "SOXX"):
        raise AlpacaPaperConfigError(
            "Alpaca Paper allowed symbols must be exactly QQQ and SOXX"
        )
    if _string(activation, "environment_gate") != (
        "TRADING_Q1_ALPACA_PAPER_ENABLED"
    ):
        raise AlpacaPaperConfigError(
            "Alpaca Paper activation gate name is immutable"
        )
    for key in (
        "require_clean_account_on_first_bind",
        "require_dedicated_account",
        "reject_foreign_open_orders",
        "reject_foreign_positions",
    ):
        if not _boolean(activation, key):
            raise AlpacaPaperConfigError(
                f"Alpaca Paper safety control {key} must remain enabled"
            )
    if _string(orders, "order_type") != "limit":
        raise AlpacaPaperConfigError(
            "Alpaca Paper canary requires limit orders"
        )
    if _string(orders, "time_in_force") != "day":
        raise AlpacaPaperConfigError(
            "Alpaca Paper canary requires day time-in-force"
        )
    if _boolean(orders, "extended_hours"):
        raise AlpacaPaperConfigError(
            "Alpaca Paper canary must remain regular-session only"
        )
    if not _boolean(orders, "whole_shares_only"):
        raise AlpacaPaperConfigError(
            "Alpaca Paper canary v1 requires whole-share orders"
        )
    prefix = _string(orders, "client_order_id_prefix")
    maximum_client_order_id_length = _positive_integer(
        orders,
        "maximum_client_order_id_length",
    )
    if prefix != "q1p" or maximum_client_order_id_length > 128:
        raise AlpacaPaperConfigError(
            "Alpaca Paper client-order identity contract is invalid"
        )
    if _positive_integer(orders, "maximum_open_orders_per_symbol") != 1:
        raise AlpacaPaperConfigError(
            "Alpaca Paper permits at most one open order per symbol"
        )
    required_stream_status = _string(
        market_data,
        "required_stream_status",
    )
    if required_stream_status != "CONNECTED":
        raise AlpacaPaperConfigError(
            "Alpaca Paper canary requires a CONNECTED market stream"
        )

    return AlpacaPaperCanaryConfig(
        version=_string(document, "version"),
        execution_lane=_string(document, "execution_lane"),
        provider=_string(document, "provider"),
        rest_base_url=_string(document, "rest_base_url"),
        source_arm=source_arm,
        allowed_symbols=symbols,
        require_clean_account_on_first_bind=_boolean(
            activation,
            "require_clean_account_on_first_bind",
        ),
        require_dedicated_account=_boolean(
            activation,
            "require_dedicated_account",
        ),
        reject_foreign_open_orders=_boolean(
            activation,
            "reject_foreign_open_orders",
        ),
        reject_foreign_positions=_boolean(
            activation,
            "reject_foreign_positions",
        ),
        order_type=_string(orders, "order_type"),
        time_in_force=_string(orders, "time_in_force"),
        extended_hours=_boolean(orders, "extended_hours"),
        whole_shares_only=_boolean(orders, "whole_shares_only"),
        minimum_order_notional_usd=_positive_decimal(
            orders,
            "minimum_order_notional_usd",
        ),
        limit_offset_bps=_non_negative_decimal(
            orders,
            "limit_offset_bps",
        ),
        price_increment_usd=_positive_decimal(
            orders,
            "price_increment_usd",
        ),
        client_order_id_prefix=prefix,
        maximum_client_order_id_length=maximum_client_order_id_length,
        maximum_open_orders_per_symbol=_positive_integer(
            orders,
            "maximum_open_orders_per_symbol",
        ),
        no_risk_increase_after_et=time.fromisoformat(
            _string(orders, "no_risk_increase_after_et")
        ),
        request_timeout_seconds=_positive_decimal(
            transport,
            "request_timeout_seconds",
        ),
        reconciliation_lookup_attempts=_positive_integer(
            transport,
            "reconciliation_lookup_attempts",
        ),
        reconciliation_lookup_interval_seconds=_positive_decimal(
            transport,
            "reconciliation_lookup_interval_seconds",
        ),
        maximum_consecutive_failures=_positive_integer(
            transport,
            "maximum_consecutive_failures",
        ),
        maximum_quote_age_seconds=_positive_integer(
            market_data,
            "maximum_quote_age_seconds",
        ),
        maximum_multi_symbol_quote_skew_seconds=_positive_integer(
            market_data,
            "maximum_multi_symbol_quote_skew_seconds",
        ),
        required_stream_status=required_stream_status,
        account_snapshot_interval_seconds=_positive_integer(
            reconciliation,
            "account_snapshot_interval_seconds",
        ),
        order_history_limit=_positive_integer(
            reconciliation,
            "order_history_limit",
        ),
        fill_activity_page_size=_positive_integer(
            reconciliation,
            "fill_activity_page_size",
        ),
        unknown_outcome_state=_string(
            reconciliation,
            "unknown_outcome_state",
        ),
    )


def _section(
    document: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise AlpacaPaperConfigError(f"{key} must be an object")
    return cast(dict[str, Any], value)


def _list(
    document: dict[str, Any],
    key: str,
) -> list[object]:
    value = document.get(key)
    if not isinstance(value, list):
        raise AlpacaPaperConfigError(f"{key} must be a list")
    return cast(list[object], value)


def _string(
    document: dict[str, Any],
    key: str,
) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AlpacaPaperConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _boolean(
    document: dict[str, Any],
    key: str,
) -> bool:
    value = document.get(key)
    if not isinstance(value, bool):
        raise AlpacaPaperConfigError(f"{key} must be a boolean")
    return value


def _positive_integer(
    document: dict[str, Any],
    key: str,
) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AlpacaPaperConfigError(f"{key} must be a positive integer")
    return value


def _positive_decimal(
    document: dict[str, Any],
    key: str,
) -> Decimal:
    value = _decimal(document, key)
    if value <= 0:
        raise AlpacaPaperConfigError(f"{key} must be positive")
    return value


def _non_negative_decimal(
    document: dict[str, Any],
    key: str,
) -> Decimal:
    value = _decimal(document, key)
    if value < 0:
        raise AlpacaPaperConfigError(f"{key} must be non-negative")
    return value


def _decimal(
    document: dict[str, Any],
    key: str,
) -> Decimal:
    try:
        value = Decimal(str(document[key]))
    except (ArithmeticError, KeyError) as exc:
        raise AlpacaPaperConfigError(f"{key} must be numeric") from exc
    if not value.is_finite():
        raise AlpacaPaperConfigError(f"{key} must be finite")
    return value
