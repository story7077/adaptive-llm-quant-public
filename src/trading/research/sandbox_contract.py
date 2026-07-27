from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

from trading.domain.hashing import canonical_hash

DEFAULT_ALLOWED_PREFIXES = (
    "src/trading/features/",
    "src/trading/strategies/",
    "src/trading/calibration/",
    "src/trading/research/",
    "src/trading/experiments/",
    "config/strategies/",
    "config/research/",
    "tests/unit/",
    "tests/property/",
    "tests/research/",
    "docs/research/",
)

DEFAULT_FORBIDDEN_PREFIXES = (
    "src/trading/risk/",
    "src/trading/execution/",
    "src/trading/ledger/",
    "src/trading/security/",
    "src/trading/broker/",
    "migrations/",
    "credentials/",
)

DEFAULT_FORBIDDEN_EXACT = (
    "src/trading/persistence/db.py",
    "src/trading/persistence/models.py",
    ".github/workflows/public-release-security.yml",
)


class CandidatePatchRejected(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CandidatePatchInspection:
    changed_paths: tuple[str, ...]
    patch_hash: str


def inspect_candidate_patch(
    *,
    changed_paths: Iterable[str],
    patch_bytes: bytes,
    champion_owned_paths: Iterable[str] = (),
    allowed_prefixes: tuple[str, ...] = DEFAULT_ALLOWED_PREFIXES,
    forbidden_prefixes: tuple[str, ...] = DEFAULT_FORBIDDEN_PREFIXES,
    forbidden_exact: tuple[str, ...] = DEFAULT_FORBIDDEN_EXACT,
) -> CandidatePatchInspection:
    declared = tuple(sorted({_normalize_relative_path(path) for path in changed_paths}))
    normalized = _paths_from_unified_diff(patch_bytes)
    if not normalized:
        raise CandidatePatchRejected("candidate patch has no changed files")
    if declared != normalized:
        raise CandidatePatchRejected("DECLARED_PATHS_DO_NOT_MATCH_PATCH")
    champion_paths = {_normalize_relative_path(path) for path in champion_owned_paths}
    violations: list[str] = []
    for path in normalized:
        if path in champion_paths:
            violations.append(f"CHAMPION_IN_PLACE_CHANGE:{path}")
            continue
        if path in forbidden_exact or any(
            path == prefix.rstrip("/") or path.startswith(prefix)
            for prefix in forbidden_prefixes
        ):
            violations.append(f"FORBIDDEN_PATH:{path}")
            continue
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            violations.append(f"PATH_NOT_ALLOWLISTED:{path}")
    if violations:
        raise CandidatePatchRejected(";".join(violations))
    return CandidatePatchInspection(
        changed_paths=normalized,
        patch_hash=canonical_hash(
            {
                "changed_paths": normalized,
                "patch_sha256": _bytes_hash(patch_bytes),
            }
        ),
    )


def _normalize_relative_path(raw: str) -> str:
    value = raw.replace("\\", "/").strip()
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or path.is_absolute()
        or ":" in path.parts[0]
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CandidatePatchRejected(f"unsafe patch path: {raw!r}")
    return path.as_posix()


def _bytes_hash(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


_DIFF_HEADER = re.compile(r"^diff --git a/([^\r\n]+) b/([^\r\n]+)$")


def _paths_from_unified_diff(patch_bytes: bytes) -> tuple[str, ...]:
    if not patch_bytes or b"\x00" in patch_bytes:
        raise CandidatePatchRejected("candidate patch is empty or binary")
    try:
        patch = patch_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CandidatePatchRejected("candidate patch is not UTF-8 text") from exc
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise CandidatePatchRejected("binary candidate patches are forbidden")
    paths: set[str] = set()
    for line in patch.splitlines():
        match = _DIFF_HEADER.fullmatch(line)
        if match is None:
            continue
        paths.add(_normalize_relative_path(match.group(1)))
        paths.add(_normalize_relative_path(match.group(2)))
    if not paths:
        raise CandidatePatchRejected("candidate patch has no unified-diff headers")
    return tuple(sorted(paths))
