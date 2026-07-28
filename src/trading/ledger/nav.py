from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from trading.domain.contracts import NavSnapshot
from trading.domain.hashing import canonical_hash, stable_id

CENT = Decimal("0.01")


def calculate_nav(
    *,
    arm_id: str,
    as_of: datetime,
    cash_usd: Decimal,
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
) -> NavSnapshot:
    missing = sorted(set(positions) - set(prices))
    if missing:
        raise ValueError(f"Missing NAV prices for: {missing}")
    market_value = sum(
        (quantity * prices[symbol] for symbol, quantity in positions.items()),
        Decimal("0"),
    ).quantize(CENT, rounding=ROUND_HALF_EVEN)
    rounded_cash = cash_usd.quantize(CENT, rounding=ROUND_HALF_EVEN)
    nav = rounded_cash + market_value
    price_hash = canonical_hash({symbol: str(price) for symbol, price in prices.items()})
    return NavSnapshot(
        nav_snapshot_id=stable_id("nav", arm_id, as_of, price_hash),
        arm_id=arm_id,
        as_of=as_of,
        cash_usd=rounded_cash,
        positions_market_value_usd=market_value,
        nav_usd=nav,
        price_manifest_hash=price_hash,
        created_at=as_of,
    )

