from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from trading.settings import ConfigBundle

ALPACA_BASIC_STREAM_SUBSCRIPTION_LIMIT = 30
PREFERRED_LIVE_QUOTE_SYMBOLS = ("SOXS", "SPY", "QQQ", "SOXX", "SMH", "XLK")


@dataclass(frozen=True, slots=True)
class IexStreamSubscriptionPlan:
    trades: tuple[str, ...]
    quotes: tuple[str, ...]
    bars: tuple[str, ...]
    updated_bars: tuple[str, ...]

    @property
    def subscription_count(self) -> int:
        return sum(
            len(items)
            for items in (
                self.trades,
                self.quotes,
                self.bars,
                self.updated_bars,
            )
        )


def market_data_symbols(config: ConfigBundle) -> tuple[str, ...]:
    universe = config.get("universe.yaml")
    candidates = list(universe.get("symbols", ()))
    symbols: list[str] = []
    for item in candidates:
        symbol = str(item).strip().upper()
        if symbol and symbol != "USD_CASH" and symbol not in symbols:
            symbols.append(symbol)
    if not symbols:
        raise ValueError("Market-data universe is empty")
    if len(symbols) > ALPACA_BASIC_STREAM_SUBSCRIPTION_LIMIT:
        raise ValueError("Alpaca Basic IEX supports at most 30 bar subscriptions")
    return tuple(symbols)


def basic_iex_stream_plan(
    config: ConfigBundle,
    *,
    required_quote_symbols: tuple[str, ...] = (),
) -> IexStreamSubscriptionPlan:
    symbols = market_data_symbols(config)
    symbol_set = set(symbols)
    required = _ordered_symbols(required_quote_symbols)
    missing = [symbol for symbol in required if symbol not in symbol_set]
    if missing:
        raise ValueError(f"Required quote symbols missing from universe: {missing}")

    quote_capacity = ALPACA_BASIC_STREAM_SUBSCRIPTION_LIMIT - len(symbols)
    if len(required) > quote_capacity:
        raise ValueError(
            "Paper positions exceed the remaining Alpaca Basic quote capacity"
        )
    quote_priority = _ordered_symbols(
        (*required, *PREFERRED_LIVE_QUOTE_SYMBOLS, *symbols)
    )
    quotes = tuple(
        symbol for symbol in quote_priority if symbol in symbol_set
    )[:quote_capacity]
    plan = IexStreamSubscriptionPlan(
        trades=(),
        quotes=quotes,
        bars=symbols,
        updated_bars=(),
    )
    if plan.subscription_count > ALPACA_BASIC_STREAM_SUBSCRIPTION_LIMIT:
        raise ValueError("Alpaca Basic stream plan exceeds 30 subscriptions")
    return plan


def sell_only_symbols(config: ConfigBundle) -> frozenset[str]:
    universe = config.get("universe.yaml")
    return _symbol_set(universe.get("sell_only_symbols", ()))


def entry_symbols(config: ConfigBundle) -> frozenset[str]:
    universe = config.get("universe.yaml")
    configured = _symbol_set(universe.get("entry_symbols", ()))
    streamed = frozenset(market_data_symbols(config))
    if not configured.issubset(streamed):
        missing = sorted(configured - streamed)
        raise ValueError(f"Entry symbols missing from market-data universe: {missing}")
    if configured & sell_only_symbols(config):
        overlap = sorted(configured & sell_only_symbols(config))
        raise ValueError(f"Symbols cannot be both entry-enabled and sell-only: {overlap}")
    return configured


def leveraged_symbols(config: ConfigBundle) -> frozenset[str]:
    universe = config.get("universe.yaml")
    configured = _symbol_set(universe.get("leveraged_symbols", ()))
    streamed = frozenset(market_data_symbols(config))
    if not configured.issubset(streamed):
        missing = sorted(configured - streamed)
        raise ValueError(f"Leveraged symbols missing from market-data universe: {missing}")
    return configured


def _symbol_set(items: object) -> frozenset[str]:
    if not isinstance(items, (list, tuple)):
        raise ValueError("Symbol collection must be an array")
    values = cast(list[object] | tuple[object, ...], items)
    return frozenset(
        str(item).strip().upper() for item in values if str(item).strip()
    )


def _ordered_symbols(items: tuple[str, ...]) -> tuple[str, ...]:
    symbols: list[str] = []
    for item in items:
        symbol = str(item).strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)
