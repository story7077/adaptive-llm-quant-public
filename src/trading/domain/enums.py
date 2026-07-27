from __future__ import annotations

from enum import IntEnum, StrEnum


class Horizon(StrEnum):
    H4 = "H4"
    H5D = "H5D"


class ExposureKind(StrEnum):
    ACTIVE_DELTA = "ACTIVE_DELTA"


class ForecastStatus(StrEnum):
    ACTIVE = "ACTIVE"
    NO_SIGNAL = "NO_SIGNAL"
    QUARANTINED = "QUARANTINED"
    EXPIRED = "EXPIRED"


class ConditionType(StrEnum):
    TIME_REACHED = "TIME_REACHED"
    SOURCE_RETRACTION = "SOURCE_RETRACTION"
    OFFICIAL_CONFIRMATION = "OFFICIAL_CONFIRMATION"
    MARKET_FEATURE_THRESHOLD = "MARKET_FEATURE_THRESHOLD"
    EVENT_STATUS_CHANGED = "EVENT_STATUS_CHANGED"
    PATCH_ATTRIBUTED_PNL = "PATCH_ATTRIBUTED_PNL"
    DATA_STALENESS = "DATA_STALENESS"


class ComparisonOperator(StrEnum):
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"
    GTE = "GTE"
    GT = "GT"


class EventDirection(IntEnum):
    NEGATIVE = -1
    NEUTRAL = 0
    POSITIVE = 1


class OrdinalBucket(IntEnum):
    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    EXTREME = 4


class PolicyAction(StrEnum):
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    REDUCE_RISK_BUDGET = "REDUCE_RISK_BUDGET"
    APPLY_STRATEGY_TILT = "APPLY_STRATEGY_TILT"
    RESTORE_DEFAULT = "RESTORE_DEFAULT"


class PolicyTargetKind(StrEnum):
    STRATEGY = "STRATEGY"
    FACTOR = "FACTOR"
    PORTFOLIO = "PORTFOLIO"
    SYMBOL = "SYMBOL"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class MarketConnectionState(StrEnum):
    STOPPED = "STOPPED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"


class MarketDataSourceKind(StrEnum):
    REST_BACKFILL = "REST_BACKFILL"
    REST_LATEST = "REST_LATEST"
    STREAM_BAR = "STREAM_BAR"
    STREAM_UPDATE = "STREAM_UPDATE"
    STREAM_QUOTE = "STREAM_QUOTE"
    STREAM_TRADE = "STREAM_TRADE"


class MarketTradeEventKind(StrEnum):
    TRADE = "TRADE"
    CORRECTION = "CORRECTION"
    CANCEL_ERROR = "CANCEL_ERROR"


class RunMode(StrEnum):
    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    CANARY = "CANARY"
    LIVE = "LIVE"


class ReplayMode(StrEnum):
    FULL = "FULL"
    DECISION = "DECISION"
    EXECUTION = "EXECUTION"
    ARM = "ARM"
