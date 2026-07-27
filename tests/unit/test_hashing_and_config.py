from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from trading.domain.hashing import canonical_hash, canonical_json
from trading.runtime.provenance import workspace_code_version
from trading.settings import EXPECTED_ARMS, Settings, load_config_bundle


def test_canonical_hash_ignores_mapping_order() -> None:
    first = {"b": 2, "a": {"y": Decimal("1.20"), "x": 1.0}}
    second = {"a": {"x": 1.0, "y": Decimal("1.20")}, "b": 2}
    assert canonical_hash(first) == canonical_hash(second)
    assert canonical_json(first) == canonical_json(second)


def test_canonical_hash_supports_versioned_session_dates() -> None:
    assert canonical_json({"session_date": date(2026, 7, 27)}) == (
        '{"session_date":"2026-07-27"}'
    )


def test_workspace_code_version_is_line_ending_invariant(tmp_path: Path) -> None:
    paths = (
        tmp_path / "pyproject.toml",
        tmp_path / "alembic.ini",
        tmp_path / "src" / "trading" / "module.py",
        tmp_path / "migrations" / "versions" / "0001_example.py",
        tmp_path / "migrations" / "script.py.mako",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"first\r\nsecond\r\n")

    windows_version = workspace_code_version(tmp_path)
    for path in paths:
        path.write_bytes(b"first\nsecond\n")

    assert workspace_code_version(tmp_path) == windows_version


def test_all_configs_validate(repository_root) -> None:
    bundle = load_config_bundle(repository_root / "config")
    assert tuple(bundle.get("experiments.yaml")["arms"]) == EXPECTED_ARMS
    assert bundle.get("providers.example.yaml")["production_unlock"] is False
    universe = bundle.get("universe.yaml")
    sell_only = set(universe["sell_only_symbols"])
    leveraged = set(universe["leveraged_symbols"])
    entry = set(universe["entry_symbols"])
    assert "SOXL" in sell_only
    assert leveraged >= {"SOXL", "SOXS"}
    assert "SOXS" in entry
    assert sell_only.isdisjoint(entry)


def test_toss_commission_is_versioned(repository_root) -> None:
    bundle = load_config_bundle(repository_root / "config")
    costs = bundle.get("costs.yaml")
    assert costs["version"] == "toss_us_equities_2026_05_14_v1"
    assert costs["commission"]["us_equity_rate"] == 0.001


def test_settings_load_gitignored_dotenv_without_exposing_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "APCA_API_KEY_ID=test-key\nAPCA_API_SECRET_KEY=test-secret\n",
        encoding="utf-8",
    )

    settings = Settings.from_env(tmp_path)

    assert settings.has_alpaca_credentials is True
    assert settings.alpaca_key_id == "test-key"
    assert settings.alpaca_secret_key == "test-secret"
    assert "test-secret" not in repr(settings)

    with pytest.raises(ValueError, match="must exceed"):
        Settings(
            database_url="sqlite://",
            config_dir=tmp_path,
            raw_store=tmp_path / "raw",
            real_broker_enabled=False,
            real_llm_enabled=False,
            production_unlock=False,
            market_heartbeat_seconds=10,
            market_connection_stale_seconds=10,
        )
