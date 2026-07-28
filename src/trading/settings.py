from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml
from dotenv import load_dotenv

from trading.domain.algorithm import (
    LEGACY_FORWARD_ALGORITHM_VERSION,
    SUPPORTED_PAPER_ALGORITHM_VERSIONS,
)
from trading.domain.alpaca_paper import (
    ALPACA_PAPER_CONFIG_FILE,
    AlpacaPaperCanaryConfig,
    parse_alpaca_paper_config,
)
from trading.domain.hashing import canonical_hash
from trading.llm.q1_overlay import validate_q1_overlay_config

REQUIRED_CONFIG_FILES = (
    "universe.yaml",
    "strategies.yaml",
    "portfolio.yaml",
    "risk.yaml",
    "experiments.yaml",
    "schedules.yaml",
    "providers.example.yaml",
    "costs.yaml",
    "forward-paper.yaml",
)
Q1_CONFIG_FILE = "q1-math-core.yaml"

EXPECTED_ARMS = (
    "B0-CASH",
    "B0-QQQ",
    "B0-VOL",
    "B1",
    "B2",
    "B3-RISK",
    "B3-FULL",
)


class ConfigurationError(ValueError):
    """Raised when immutable economic configuration is invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    config_dir: Path
    raw_store: Path
    real_broker_enabled: bool
    real_llm_enabled: bool
    production_unlock: bool
    commander_dir: Path | None = None
    market_data_enabled: bool = True
    alpaca_key_id: str | None = field(default=None, repr=False)
    alpaca_secret_key: str | None = field(default=None, repr=False)
    alpaca_data_url: str = "https://data.alpaca.markets"
    alpaca_stream_url: str = "wss://stream.data.alpaca.markets/v2/iex"
    market_quote_stale_seconds: int = 15
    market_bar_stale_seconds: int = 120
    market_poll_after_ms: int = 5000
    market_heartbeat_seconds: int = 10
    market_connection_stale_seconds: int = 35
    alpaca_trading_url: str = "https://paper-api.alpaca.markets"
    paper_runtime_enabled: bool = False
    paper_run_id: str = "paper_20260728_v4"
    paper_algorithm_version: str = LEGACY_FORWARD_ALGORITHM_VERSION
    paper_poll_seconds: int = 5
    paper_account_file: Path | None = None
    webgpt_enabled: bool = False
    q1_alpaca_paper_enabled: bool = False

    def __post_init__(self) -> None:
        if self.real_broker_enabled:
            raise ConfigurationError(
                "Real broker routing is not implemented; paper execution is the only "
                "permitted mode"
            )
        if self.market_connection_stale_seconds <= self.market_heartbeat_seconds:
            raise ConfigurationError(
                "market_connection_stale_seconds must exceed market_heartbeat_seconds"
            )
        if self.paper_poll_seconds <= 0:
            raise ConfigurationError("paper_poll_seconds must be positive")
        if self.paper_algorithm_version not in SUPPORTED_PAPER_ALGORITHM_VERSIONS:
            raise ConfigurationError(
                "paper_algorithm_version must be one of "
                f"{SUPPORTED_PAPER_ALGORITHM_VERSIONS}"
            )
        if self.q1_alpaca_paper_enabled:
            if (
                self.alpaca_trading_url.rstrip("/")
                != "https://paper-api.alpaca.markets"
            ):
                raise ConfigurationError(
                    "Q1 Alpaca Paper requires the exact paper-api host"
                )
            if not self.has_alpaca_credentials:
                raise ConfigurationError(
                    "Q1 Alpaca Paper requires Paper API credentials"
                )

    @classmethod
    def from_env(cls, repo_root: Path | None = None) -> Settings:
        root = repo_root or Path.cwd()
        load_dotenv(root / ".env", override=False)
        return cls(
            database_url=os.getenv(
                "TRADING_DATABASE_URL",
                "postgresql+psycopg://postgres@127.0.0.1:55432/trading_phase0",
            ),
            config_dir=_resolve(root, os.getenv("TRADING_CONFIG_DIR", "config")),
            raw_store=_resolve(root, os.getenv("TRADING_RAW_STORE", "data/raw")),
            real_broker_enabled=_bool_env("TRADING_REAL_BROKER_ENABLED", False),
            real_llm_enabled=_bool_env("TRADING_REAL_LLM_ENABLED", False),
            production_unlock=_bool_env("TRADING_PRODUCTION_UNLOCK", False),
            commander_dir=_resolve(
                root,
                os.getenv("TRADING_COMMANDER_DIR", str(root.parent / "stock-commander")),
            ),
            market_data_enabled=_bool_env("TRADING_MARKET_DATA_ENABLED", True),
            alpaca_key_id=os.getenv("APCA_API_KEY_ID") or None,
            alpaca_secret_key=os.getenv("APCA_API_SECRET_KEY") or None,
            alpaca_data_url=os.getenv(
                "TRADING_ALPACA_DATA_URL",
                "https://data.alpaca.markets",
            ).rstrip("/"),
            alpaca_stream_url=os.getenv(
                "TRADING_ALPACA_STREAM_URL",
                "wss://stream.data.alpaca.markets/v2/iex",
            ),
            market_quote_stale_seconds=_positive_int_env(
                "TRADING_MARKET_QUOTE_STALE_SECONDS",
                15,
            ),
            market_bar_stale_seconds=_positive_int_env(
                "TRADING_MARKET_BAR_STALE_SECONDS",
                120,
            ),
            market_poll_after_ms=_positive_int_env(
                "TRADING_MARKET_POLL_AFTER_MS",
                5000,
            ),
            market_heartbeat_seconds=_positive_int_env(
                "TRADING_MARKET_HEARTBEAT_SECONDS",
                10,
            ),
            market_connection_stale_seconds=_positive_int_env(
                "TRADING_MARKET_CONNECTION_STALE_SECONDS",
                35,
            ),
            alpaca_trading_url=os.getenv(
                "TRADING_ALPACA_TRADING_URL",
                "https://paper-api.alpaca.markets",
            ).rstrip("/"),
            paper_runtime_enabled=_bool_env("TRADING_PAPER_RUNTIME_ENABLED", False),
            paper_run_id=os.getenv(
                "TRADING_PAPER_RUN_ID",
                "paper_20260728_v4",
            ).strip(),
            paper_algorithm_version=os.getenv(
                "TRADING_PAPER_ALGORITHM_VERSION",
                LEGACY_FORWARD_ALGORITHM_VERSION,
            ).strip(),
            paper_poll_seconds=_positive_int_env("TRADING_PAPER_POLL_SECONDS", 5),
            paper_account_file=_resolve(
                root,
                os.getenv(
                    "TRADING_PAPER_ACCOUNT_FILE",
                    "config/paper-account.example.yaml",
                ),
            ),
            webgpt_enabled=_bool_env("TRADING_WEBGPT_ENABLED", False),
            q1_alpaca_paper_enabled=_bool_env(
                "TRADING_Q1_ALPACA_PAPER_ENABLED",
                False,
            ),
        )

    @property
    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret_key)


@dataclass(frozen=True, slots=True)
class ConfigBundle:
    documents: dict[str, dict[str, Any]]
    manifest_hash: str

    def get(self, name: str) -> dict[str, Any]:
        try:
            return self.documents[name]
        except KeyError as exc:
            raise ConfigurationError(f"Missing loaded config: {name}") from exc


@dataclass(frozen=True, slots=True)
class Q1ConfigBundle:
    document: dict[str, Any]
    cost_document: dict[str, Any]
    manifest_hash: str

    def get(self, name: str) -> dict[str, Any]:
        if name == Q1_CONFIG_FILE:
            return self.document
        if name == "costs.yaml":
            return self.cost_document
        raise ConfigurationError(f"Q1 config does not expose {name!r}")


@dataclass(frozen=True, slots=True)
class AlpacaPaperConfigBundle:
    document: dict[str, Any]
    config: AlpacaPaperCanaryConfig
    manifest_hash: str


def load_config_bundle(config_dir: Path) -> ConfigBundle:
    documents: dict[str, dict[str, Any]] = {}
    for filename in REQUIRED_CONFIG_FILES:
        path = config_dir / filename
        if not path.is_file():
            raise ConfigurationError(f"Missing config file: {path}")
        with path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if not isinstance(loaded, dict):
            raise ConfigurationError(f"Config root must be an object: {path}")
        document = cast(dict[str, Any], loaded)
        if not isinstance(document.get("version"), str):
            raise ConfigurationError(f"Config must have a string version: {path}")
        documents[filename] = document

    arms = tuple(documents["experiments.yaml"].get("arms", ()))
    if arms != EXPECTED_ARMS:
        raise ConfigurationError(f"Shadow arms must be exactly {EXPECTED_ARMS}, got {arms}")

    providers = documents["providers.example.yaml"]
    if providers.get("production_unlock") is not False:
        raise ConfigurationError("Phase 0 production unlock must be false")
    for name in ("news", "llm", "broker"):
        section = providers.get(name)
        if not isinstance(section, dict):
            raise ConfigurationError(
                f"Phase 0 provider {name!r} must be disabled"
            )
        provider_section = cast(dict[str, Any], section)
        if provider_section.get("enabled") is not False:
            raise ConfigurationError(f"Phase 0 provider {name!r} must be disabled")

    universe = documents["universe.yaml"]
    symbols = {
        str(item).strip().upper()
        for item in universe.get("symbols", ())
        if str(item).strip()
    }
    sell_only = {
        str(item).strip().upper()
        for item in universe.get("sell_only_symbols", ())
        if str(item).strip()
    }
    entries = {
        str(item).strip().upper()
        for item in universe.get("entry_symbols", ())
        if str(item).strip()
    }
    if not sell_only.issubset(symbols) or not entries.issubset(symbols):
        raise ConfigurationError("Execution classifications must be in the market universe")
    if sell_only & entries:
        raise ConfigurationError("A symbol cannot be both entry-enabled and sell-only")
    if "SOXL" not in sell_only:
        raise ConfigurationError("The pre-existing SOXL position must start sell-only")

    return ConfigBundle(documents=documents, manifest_hash=canonical_hash(documents))


def load_q1_config_bundle(config_dir: Path) -> Q1ConfigBundle:
    legacy = load_config_bundle(config_dir)
    path = config_dir / Q1_CONFIG_FILE
    if not path.is_file():
        raise ConfigurationError(f"Missing Q1 config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"Q1 config root must be an object: {path}")
    document = cast(dict[str, Any], loaded)
    if document.get("version") != "q1_math_core_v1":
        raise ConfigurationError("Q1 config version must be q1_math_core_v1")
    if document.get("algorithm_version") != "q1_math_core_v1":
        raise ConfigurationError(
            "Q1 algorithm_version must be q1_math_core_v1"
        )
    if document.get("real_order_routing") is not False:
        raise ConfigurationError("Q1 real_order_routing must remain false")
    active_universe = document.get("active_universe")
    if not isinstance(active_universe, dict):
        raise ConfigurationError("Q1 active_universe must be an object")
    universe_config = cast(dict[str, Any], active_universe)
    if universe_config.get("strategy_symbols") != [
        "QQQ",
        "SOXX",
        "USD_CASH",
    ]:
        raise ConfigurationError(
            "Q1 active_universe must be exactly QQQ, SOXX, USD_CASH"
        )
    if universe_config.get("risky_symbols") != ["QQQ", "SOXX"]:
        raise ConfigurationError("Q1 risky_symbols must be exactly QQQ, SOXX")
    if universe_config.get("cash_symbol") != "USD_CASH":
        raise ConfigurationError("Q1 cash_symbol must be USD_CASH")
    if "SOXS" not in universe_config.get("disabled_symbols", []):
        raise ConfigurationError("Q1 must explicitly disable SOXS")
    try:
        validate_q1_overlay_config(document)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    costs = legacy.get("costs.yaml")
    expected_cost_version = document.get("cost_model_version")
    if expected_cost_version != costs.get("version"):
        raise ConfigurationError(
            "Q1 cost_model_version must match costs.yaml"
        )
    return Q1ConfigBundle(
        document=document,
        cost_document=costs,
        manifest_hash=canonical_hash(
            {
                Q1_CONFIG_FILE: document,
                "costs.yaml": costs,
            }
        ),
    )


def load_alpaca_paper_config_bundle(
    config_dir: Path,
) -> AlpacaPaperConfigBundle:
    path = config_dir / ALPACA_PAPER_CONFIG_FILE
    if not path.is_file():
        raise ConfigurationError(f"Missing Alpaca Paper config file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ConfigurationError(
            f"Alpaca Paper config root must be an object: {path}"
        )
    document = cast(dict[str, Any], loaded)
    try:
        parsed = parse_alpaca_paper_config(document)
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    return AlpacaPaperConfigBundle(
        document=document,
        config=parsed,
        manifest_hash=canonical_hash(
            {ALPACA_PAPER_CONFIG_FILE: document}
        ),
    )


def _resolve(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean value")


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value
