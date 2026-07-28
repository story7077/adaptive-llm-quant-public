from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from trading.domain.time import require_aware_utc


class ImmutableRawStore:
    """Content-addressed storage for provider responses and stream frames."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def persist(
        self,
        *,
        provider: str,
        feed: str,
        channel: str,
        received_at: datetime,
        content: str | bytes,
    ) -> str:
        instant = require_aware_utc(received_at, "received_at")
        encoded = content.encode("utf-8") if isinstance(content, str) else content
        digest = hashlib.sha256(encoded).hexdigest()
        relative = (
            Path(provider)
            / feed
            / instant.strftime("%Y")
            / instant.strftime("%m")
            / instant.strftime("%d")
            / channel
            / f"{digest}.json"
        )
        target = self._root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("xb") as handle:
                handle.write(encoded)
        except FileExistsError:
            pass
        return f"raw://{relative.as_posix()}"
