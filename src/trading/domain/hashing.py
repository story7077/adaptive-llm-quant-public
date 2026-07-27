from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented deterministically."""


type CanonicalValue = (
    bool
    | int
    | float
    | str
    | list["CanonicalValue"]
    | dict[str, "CanonicalValue"]
    | None
)


def canonical_data(value: object) -> CanonicalValue:
    if isinstance(value, BaseModel):
        return canonical_data(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalizationError("Naive datetime cannot be canonicalized")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return canonical_data(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return {
            str(key): canonical_data(mapping[key])
            for key in sorted(mapping, key=lambda item: str(item))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        sequence = cast(Sequence[object], value)
        return [canonical_data(item) for item in sequence]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("Non-finite float cannot be canonicalized")
        return float(format(value, ".12g"))
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise CanonicalizationError(f"Unsupported canonical type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical_data(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    digest = canonical_hash({"prefix": prefix, "parts": parts})[:24]
    return f"{prefix}_{digest}"
