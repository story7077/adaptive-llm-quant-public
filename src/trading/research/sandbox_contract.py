from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from trading.domain.hashing import canonical_hash

# V1 is immutable because historical Candidate artifacts were judged against it.
CANDIDATE_PATCH_POLICY_V1 = "candidate_patch_policy_v1"
CANDIDATE_PATCH_POLICY_V2 = "candidate_patch_policy_v2"

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

V2_ALLOWED_PATTERNS = (
    "src/trading/strategies/challengers/**",
    "src/trading/features/challengers/**",
    "src/trading/calibration/challengers/**",
    "src/trading/experiments/challengers/**",
    "config/strategies/challengers/**",
    "tests/candidates/**",
    "docs/research/challengers/**",
)

V2_FORBIDDEN_PATTERNS = (
    "src/trading/research/**",
    "src/trading/persistence/**",
    "src/trading/execution/**",
    "src/trading/risk/**",
    "src/trading/ledger/**",
    "src/trading/security/**",
    "src/trading/broker/**",
    "config/research/**",
    "tests/research/**",
    "migrations/**",
    ".github/**",
)

CANDIDATE_PATCH_POLICY_V2_CONTRACT = {
    "schema_version": "candidate_patch_policy_contract_v1",
    "policy_version": CANDIDATE_PATCH_POLICY_V2,
    "path_match_semantics": "POSIX_GLOB_V1",
    "allowed_paths": V2_ALLOWED_PATTERNS,
    "forbidden_paths": V2_FORBIDDEN_PATTERNS,
    "candidate_implementation_required": True,
    "candidate_test_required": True,
    "relative_paths_only": True,
    "symlinks_forbidden": True,
    "new_files_only": True,
}
CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH = canonical_hash(
    CANDIDATE_PATCH_POLICY_V2_CONTRACT
)


class CandidatePatchPolicyVersion(StrEnum):
    V1 = CANDIDATE_PATCH_POLICY_V1
    V2 = CANDIDATE_PATCH_POLICY_V2


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
    """Validate a historical V1 Candidate patch.

    The defaults and behavior of this function are retained for replaying
    artifacts that predate the recursive-improvement trust boundary.
    """

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


def inspect_recursive_candidate_patch(
    *,
    changed_paths: Iterable[str],
    patch_bytes: bytes,
    champion_owned_paths: Iterable[str] = (),
) -> CandidatePatchInspection:
    """Validate a new recursive-research Candidate against policy V2."""

    declared = tuple(
        sorted({_normalize_relative_path_v2(path) for path in changed_paths})
    )
    normalized = _new_paths_from_unified_diff_v2(patch_bytes)
    if declared != normalized:
        raise CandidatePatchRejected("DECLARED_PATHS_DO_NOT_MATCH_PATCH")
    champion_paths = {_normalize_relative_path(path) for path in champion_owned_paths}
    violations: list[str] = []
    for path in normalized:
        if path in champion_paths:
            violations.append(f"CHAMPION_IN_PLACE_CHANGE:{path}")
            continue
        if _matches_any(path, V2_FORBIDDEN_PATTERNS):
            violations.append(f"FORBIDDEN_PATH:{path}")
            continue
        if not _matches_any(path, V2_ALLOWED_PATTERNS):
            violations.append(f"PATH_NOT_ALLOWLISTED:{path}")
    implementation = tuple(
        path
        for path in normalized
        if path.startswith(
            (
                "src/trading/strategies/challengers/",
                "src/trading/features/challengers/",
                "src/trading/calibration/challengers/",
                "src/trading/experiments/challengers/",
                "config/strategies/challengers/",
            )
        )
    )
    tests = tuple(path for path in normalized if path.startswith("tests/candidates/"))
    if not implementation:
        violations.append("CANDIDATE_IMPLEMENTATION_REQUIRED")
    if not tests:
        violations.append("CANDIDATE_TEST_REQUIRED")
    if violations:
        raise CandidatePatchRejected(";".join(violations))
    return CandidatePatchInspection(
        changed_paths=normalized,
        patch_hash=canonical_hash(
            {
                "changed_paths": normalized,
                "patch_sha256": _bytes_hash(patch_bytes),
                "policy_contract_hash": CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH,
            }
        ),
    )


def candidate_patch_policy_contract_hash(
    policy_version: CandidatePatchPolicyVersion | str,
) -> str:
    version = CandidatePatchPolicyVersion(policy_version)
    if version is CandidatePatchPolicyVersion.V2:
        return CANDIDATE_PATCH_POLICY_V2_CONTRACT_HASH
    return canonical_hash(
        {
            "policy_version": CANDIDATE_PATCH_POLICY_V1,
            "allowed_prefixes": DEFAULT_ALLOWED_PREFIXES,
            "forbidden_prefixes": DEFAULT_FORBIDDEN_PREFIXES,
            "forbidden_exact": DEFAULT_FORBIDDEN_EXACT,
        }
    )


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


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


def _normalize_relative_path_v2(raw: str) -> str:
    value = raw.replace("\\", "/").strip()
    if (
        not value
        or value.startswith("/")
        or "\0" in value
        or ":" in value.split("/", maxsplit=1)[0]
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise CandidatePatchRejected(f"unsafe patch path: {raw!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value:
        raise CandidatePatchRejected(f"unsafe patch path: {raw!r}")
    return value


def _bytes_hash(value: bytes) -> str:
    import hashlib

    return hashlib.sha256(value).hexdigest()


_DIFF_HEADER = re.compile(r"^diff --git a/([^\r\n]+) b/([^\r\n]+)$")
_V2_DIFF_HEADER = re.compile(
    r"^diff --git a/([^ \t\r\n]+) b/([^ \t\r\n]+)$"
)
_V2_NEW_FILE_HUNK = re.compile(
    r"^@@ -0,0 \+[1-9][0-9]*(?:,[1-9][0-9]*)? @@(?: .*)?$"
)
_V2_NEW_FILE_METADATA = re.compile(
    r"^(?:new file mode (?!120000$)[0-7]{6}|"
    r"index 0+\.\.[0-9a-fA-F]+(?: [0-7]{6})?)$"
)


def _paths_from_unified_diff(
    patch_bytes: bytes,
    *,
    strict_paths: bool = False,
) -> tuple[str, ...]:
    if not patch_bytes or b"\x00" in patch_bytes:
        raise CandidatePatchRejected("candidate patch is empty or binary")
    try:
        patch = patch_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CandidatePatchRejected("candidate patch is not UTF-8 text") from exc
    if "GIT binary patch" in patch or "Binary files " in patch:
        raise CandidatePatchRejected("binary candidate patches are forbidden")
    if (
        strict_paths
        and re.search(
            r"^(?:new file mode|old mode|new mode) 120000$",
            patch,
            re.MULTILINE,
        )
    ):
        raise CandidatePatchRejected("symbolic-link candidate patches are forbidden")
    paths: set[str] = set()
    normalize = (
        _normalize_relative_path_v2
        if strict_paths
        else _normalize_relative_path
    )
    for line in patch.splitlines():
        match = _DIFF_HEADER.fullmatch(line)
        if match is None:
            continue
        paths.add(normalize(match.group(1)))
        paths.add(normalize(match.group(2)))
    if not paths:
        raise CandidatePatchRejected("candidate patch has no unified-diff headers")
    return tuple(sorted(paths))


def _new_paths_from_unified_diff_v2(patch_bytes: bytes) -> tuple[str, ...]:
    """Return V2 paths only when every diff section adds one new text file."""

    if not patch_bytes or b"\x00" in patch_bytes:
        raise CandidatePatchRejected("candidate patch is empty or binary")
    try:
        patch = patch_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CandidatePatchRejected("candidate patch is not UTF-8 text") from exc
    if (
        "GIT binary patch" in patch
        or "Binary files " in patch
        or re.search(
            r"^(?:old mode|deleted file mode|rename from|rename to|"
            r"copy from|copy to)\b",
            patch,
            re.MULTILINE,
        )
    ):
        raise CandidatePatchRejected(
            "candidate patch policy V2 permits new files only"
        )
    if re.search(
        r"^(?:new file mode|new mode) 120000$",
        patch,
        re.MULTILINE,
    ):
        raise CandidatePatchRejected("symbolic-link candidate patches are forbidden")

    lines = patch.splitlines()
    section_starts = [
        index for index, line in enumerate(lines) if line.startswith("diff --git ")
    ]
    if not section_starts:
        raise CandidatePatchRejected("candidate patch has no unified-diff headers")
    if any(line for line in lines[: section_starts[0]]):
        raise CandidatePatchRejected("candidate patch has content before its first section")

    paths: list[str] = []
    for position, start in enumerate(section_starts):
        end = (
            section_starts[position + 1]
            if position + 1 < len(section_starts)
            else len(lines)
        )
        header = _V2_DIFF_HEADER.fullmatch(lines[start])
        if header is None:
            raise CandidatePatchRejected("malformed candidate diff section header")
        old_path = _normalize_relative_path_v2(header.group(1))
        new_path = _normalize_relative_path_v2(header.group(2))
        if old_path != new_path:
            raise CandidatePatchRejected(
                "candidate patch policy V2 forbids rename or copy sections"
            )
        if new_path in paths:
            raise CandidatePatchRejected(
                f"candidate patch contains duplicate diff section: {new_path}"
            )

        body = lines[start + 1 : end]
        try:
            from_index = body.index("--- /dev/null")
            to_index = body.index(f"+++ b/{new_path}")
        except ValueError as exc:
            raise CandidatePatchRejected(
                f"candidate patch policy V2 requires a new-file section: {new_path}"
            ) from exc
        if to_index != from_index + 1:
            raise CandidatePatchRejected(
                f"malformed new-file section headers: {new_path}"
            )
        if any(
            not _V2_NEW_FILE_METADATA.fullmatch(line)
            for line in body[:from_index]
        ):
            raise CandidatePatchRejected(
                f"unsupported new-file section metadata: {new_path}"
            )

        hunks = body[to_index + 1 :]
        if not hunks or _V2_NEW_FILE_HUNK.fullmatch(hunks[0]) is None:
            raise CandidatePatchRejected(
                f"candidate new-file section has no valid hunk: {new_path}"
            )
        for line in hunks:
            if line.startswith("@@ "):
                if _V2_NEW_FILE_HUNK.fullmatch(line) is None:
                    raise CandidatePatchRejected(
                        f"candidate new-file hunk is malformed: {new_path}"
                    )
            elif not line.startswith(("+", r"\ No newline at end of file")):
                raise CandidatePatchRejected(
                    f"candidate patch policy V2 forbids existing-file content: {new_path}"
                )
        paths.append(new_path)
    return tuple(sorted(paths))
