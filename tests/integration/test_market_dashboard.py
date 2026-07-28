from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from trading.dashboard.service import MarketDashboardService
from trading.ui.app import create_app


def test_dashboard_projects_market_portfolio_and_execution(seeded_demo) -> None:
    settings, _, factory, _, _ = seeded_demo
    snapshot = MarketDashboardService(factory).snapshot(
        run_id="demo_run",
        arm_id="B3-RISK",
        symbol="QQQ",
    )

    assert snapshot["source"]["mode"] == "SYNTHETIC"
    assert snapshot["source"]["freshness"] == "FIXTURE_NOT_LIVE"
    assert snapshot["source"]["candle_quality"] == "OPEN_CLOSE_SOURCE_HIGH_LOW_DERIVED"
    assert snapshot["filters"]["selected_arm"] == "B3-RISK"
    assert snapshot["filters"]["selected_symbol"] == "QQQ"
    assert snapshot["reconciliation"]["status"] == "MATCHED"
    assert Decimal(snapshot["reconciliation"]["difference_usd"]) == 0

    candles = snapshot["market"]["candles"]
    assert len(candles) == 10
    assert all(candle["high_low_derived"] for candle in candles)
    assert all(
        Decimal(candle["high"]) >= max(Decimal(candle["open"]), Decimal(candle["close"]))
        for candle in candles
    )
    assert all(
        Decimal(candle["low"]) <= min(Decimal(candle["open"]), Decimal(candle["close"]))
        for candle in candles
    )

    assert {position["symbol"] for position in snapshot["positions"]} == {
        "GLD",
        "QQQ",
        "SOXX",
    }
    assert snapshot["portfolio"]["position_count"] == 3
    assert len(snapshot["orders"]) == 3
    assert all(order["status"] == "FILLED" for order in snapshot["orders"])
    assert len(snapshot["fills"]) == 3
    assert snapshot["paper_execution"]["real_order_routing"] is False

    with TestClient(create_app(settings=settings, session_factory=factory)) as client:
        response = client.get(
            "/api/trading/dashboard",
            params={"run_id": "demo_run", "arm_id": "B3-RISK", "symbol": "SOXX"},
        )
        assert response.status_code == 200
        assert response.json()["market"]["selected_symbol"] == "SOXX"


def test_dashboard_exposes_partial_fill_and_cash_arm(seeded_demo) -> None:
    _, _, factory, _, _ = seeded_demo
    service = MarketDashboardService(factory)

    full = service.snapshot(run_id="demo_run", arm_id="B3-FULL", symbol="SOXX")
    soxx_order = next(order for order in full["orders"] if order["symbol"] == "SOXX")
    assert soxx_order["status"] == "PARTIAL"
    assert Decimal(soxx_order["filled_quantity"]) < Decimal(soxx_order["requested_quantity"])

    cash = service.snapshot(run_id="demo_run", arm_id="B0-CASH", symbol="QQQ")
    assert cash["positions"] == []
    assert cash["orders"] == []
    assert cash["fills"] == []
    assert Decimal(cash["portfolio"]["cash_usd"]) == Decimal("100000.00")
    assert Decimal(cash["portfolio"]["total_pnl_usd"]) == 0


def test_dashboard_endpoint_reports_unknown_selection(seeded_demo) -> None:
    settings, _, factory, _, _ = seeded_demo
    with TestClient(create_app(settings=settings, session_factory=factory)) as client:
        missing_run = client.get(
            "/api/trading/dashboard",
            params={"run_id": "does-not-exist"},
        )
        assert missing_run.status_code == 404

        missing_arm = client.get(
            "/api/trading/dashboard",
            params={"run_id": "demo_run", "arm_id": "does-not-exist"},
        )
        assert missing_arm.status_code == 400

        missing_symbol = client.get(
            "/api/trading/dashboard",
            params={"run_id": "demo_run", "symbol": "SOXL"},
        )
        assert missing_symbol.status_code == 400
