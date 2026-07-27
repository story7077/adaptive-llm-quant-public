from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.domain.contracts import (
    Fill,
    LedgerEntry,
    LedgerPosting,
    LedgerTransaction,
)
from trading.domain.enums import OrderSide
from trading.domain.hashing import stable_id

BALANCE_TOLERANCE = Decimal("0.000001")


def capital_entry(arm_id: str, amount: Decimal, effective_at: datetime) -> LedgerEntry:
    transaction_id = stable_id("ltx", arm_id, "INITIAL_CAPITAL", str(amount))
    transaction = LedgerTransaction(
        ledger_transaction_id=transaction_id,
        arm_id=arm_id,
        transaction_type="INITIAL_CAPITAL",
        source_id=stable_id("capital", arm_id),
        effective_at=effective_at,
        created_at=effective_at,
    )
    postings = [
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "cash"),
            ledger_transaction_id=transaction_id,
            account_code="CASH",
            asset_code="USD_CASH",
            quantity_delta=amount,
            usd_value_delta=amount,
            metadata={},
        ),
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "equity"),
            ledger_transaction_id=transaction_id,
            account_code="EQUITY:CONTRIBUTED_CAPITAL",
            asset_code="USD_CASH",
            quantity_delta=Decimal("0"),
            usd_value_delta=-amount,
            metadata={},
        ),
    ]
    return LedgerEntry(transaction=transaction, postings=postings)


def portfolio_opening_entry(
    *,
    arm_id: str,
    source_id: str,
    cash_usd: Decimal,
    positions: dict[str, Decimal],
    prices: dict[str, Decimal],
    effective_at: datetime,
) -> LedgerEntry:
    missing = sorted(set(positions) - set(prices))
    if missing:
        raise ValueError(f"Missing opening prices for: {missing}")
    if cash_usd < 0 or any(quantity <= 0 for quantity in positions.values()):
        raise ValueError("Opening paper holdings must be non-negative")
    transaction_id = stable_id("ltx", source_id, arm_id, "PAPER_OPENING_BALANCE")
    transaction = LedgerTransaction(
        ledger_transaction_id=transaction_id,
        arm_id=arm_id,
        transaction_type="PAPER_OPENING_BALANCE",
        source_id=source_id,
        effective_at=effective_at,
        created_at=effective_at,
    )
    postings: list[LedgerPosting] = [
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "cash"),
            ledger_transaction_id=transaction_id,
            account_code="CASH",
            asset_code="USD_CASH",
            quantity_delta=cash_usd,
            usd_value_delta=cash_usd,
            metadata={"source_id": source_id},
        )
    ]
    total_value = cash_usd
    for symbol in sorted(positions):
        value = positions[symbol] * prices[symbol]
        total_value += value
        postings.append(
            LedgerPosting(
                posting_id=stable_id("post", transaction_id, "asset", symbol),
                ledger_transaction_id=transaction_id,
                account_code="ASSET:SECURITY",
                asset_code=symbol,
                quantity_delta=positions[symbol],
                usd_value_delta=value,
                metadata={
                    "source_id": source_id,
                    "opening_mark": format(prices[symbol], "f"),
                },
            )
        )
    postings.append(
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "equity"),
            ledger_transaction_id=transaction_id,
            account_code="EQUITY:OPENING_BALANCE",
            asset_code="USD_CASH",
            quantity_delta=Decimal("0"),
            usd_value_delta=-total_value,
            metadata={"source_id": source_id},
        )
    )
    entry = LedgerEntry(transaction=transaction, postings=postings)
    validate_entries([entry])
    return entry


def fill_entry(fill: Fill) -> LedgerEntry:
    transaction_id = stable_id("ltx", fill.arm_id, fill.fill_id)
    transaction = LedgerTransaction(
        ledger_transaction_id=transaction_id,
        arm_id=fill.arm_id,
        transaction_type="FILL",
        source_id=fill.fill_id,
        effective_at=fill.effective_at,
        created_at=fill.created_at,
    )
    notional = fill.quantity * fill.price
    if fill.side is OrderSide.BUY:
        asset_quantity = fill.quantity
        asset_value = notional
        cash_quantity = -(notional + fill.commission_usd)
        cash_value = cash_quantity
    else:
        asset_quantity = -fill.quantity
        asset_value = -notional
        cash_quantity = notional - fill.commission_usd
        cash_value = cash_quantity
    postings = [
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "asset"),
            ledger_transaction_id=transaction_id,
            account_code="ASSET:SECURITY",
            asset_code=fill.symbol,
            quantity_delta=asset_quantity,
            usd_value_delta=asset_value,
            metadata={"fill_id": fill.fill_id},
        ),
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "cash"),
            ledger_transaction_id=transaction_id,
            account_code="CASH",
            asset_code="USD_CASH",
            quantity_delta=cash_quantity,
            usd_value_delta=cash_value,
            metadata={"fill_id": fill.fill_id},
        ),
        LedgerPosting(
            posting_id=stable_id("post", transaction_id, "fee"),
            ledger_transaction_id=transaction_id,
            account_code="EXPENSE:COMMISSION",
            asset_code="USD_CASH",
            quantity_delta=Decimal("0"),
            usd_value_delta=fill.commission_usd,
            metadata={"fill_id": fill.fill_id},
        ),
    ]
    return LedgerEntry(transaction=transaction, postings=postings)


def validate_entries(entries: list[LedgerEntry]) -> None:
    seen_transactions: set[str] = set()
    seen_postings: set[str] = set()
    for entry in entries:
        transaction_id = entry.transaction.ledger_transaction_id
        if transaction_id in seen_transactions:
            raise ValueError(f"Duplicate ledger transaction: {transaction_id}")
        seen_transactions.add(transaction_id)
        balance = sum((posting.usd_value_delta for posting in entry.postings), Decimal("0"))
        if abs(balance) > BALANCE_TOLERANCE:
            raise ValueError(f"Unbalanced ledger transaction {transaction_id}: {balance}")
        for posting in entry.postings:
            if posting.posting_id in seen_postings:
                raise ValueError(f"Duplicate ledger posting: {posting.posting_id}")
            seen_postings.add(posting.posting_id)


def rebuild_holdings(entries: list[LedgerEntry]) -> tuple[Decimal, dict[str, Decimal]]:
    validate_entries(entries)
    cash = Decimal("0")
    positions: dict[str, Decimal] = {}
    for entry in sorted(
        entries,
        key=lambda item: (
            item.transaction.effective_at,
            item.transaction.ledger_transaction_id,
        ),
    ):
        for posting in entry.postings:
            if posting.account_code == "CASH":
                cash += posting.quantity_delta
            elif posting.account_code == "ASSET:SECURITY":
                positions[posting.asset_code] = (
                    positions.get(posting.asset_code, Decimal("0")) + posting.quantity_delta
                )
    return cash, positions
