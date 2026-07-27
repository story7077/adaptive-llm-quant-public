from __future__ import annotations

from trading.data.universe import basic_iex_stream_plan


def test_basic_iex_plan_prioritizes_paper_positions_within_30(config_bundle) -> None:
    required = ("NVDA", "SGOV", "SOXL", "TSM", "KLAC", "LRCX", "MU")

    plan = basic_iex_stream_plan(
        config_bundle,
        required_quote_symbols=required,
    )

    assert plan.subscription_count == 30
    assert set(required).issubset(plan.quotes)
    assert plan.bars == (
        "SPY",
        "QQQ",
        "IWM",
        "SOXX",
        "SMH",
        "XLK",
        "TLT",
        "HYG",
        "GLD",
        "SGOV",
        "NVDA",
        "TSM",
        "KLAC",
        "LRCX",
        "MU",
        "SOXL",
        "SOXS",
    )
    assert plan.trades == ()
    assert plan.updated_bars == ()
