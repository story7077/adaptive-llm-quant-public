#!/usr/bin/env python3
"""Fail-closed public-release scanner.

The scanner reports only rule identifiers, paths, and line numbers. It never
prints matched content, which prevents a CI log from becoming a second leak.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT_MARKER = "public-release-root.json"
MARKER_SCHEMA = "public-release-root.v1"
MARKER_HISTORY_POLICY = "sanitized-working-tree-new-root"
MAX_TEXT_BYTES = 2_000_000
MAX_DISPLAY_VIOLATIONS = 200

FORBIDDEN_PATH_PARTS = frozenset(
    {
        ".local",
        "__pycache__",
        ".hypothesis",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".pyright",
        ".venv",
        "browser-profile",
        "chrome-profile",
        "user-data-dir",
        "playwright-report",
        "test-results",
        "node_modules",
    }
)
FORBIDDEN_FILE_NAMES = frozenset(
    {
        ".coverage",
        ".gitmodules",
        ".ds_store",
        "thumbs.db",
        "config/paper-account.yaml",
        "config/providers.yaml",
    }
)
FORBIDDEN_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".pyc",
    ".pyo",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".har",
    ".trace.zip",
)
PRIVATE_KEY_NAMES = ("id_rsa", "id_ed25519")
ALLOWED_DOTENV = ".env.example"
ALLOWED_RAW_PATH = "data/raw/.gitignore"
ALLOWED_EMAIL_DOMAINS = frozenset(
    {
        "example.com",
        "example.net",
        "example.org",
        "example.invalid",
        "users.noreply.github.com",
    }
)
EXPECTED_SYNTHETIC_POSITIONS = {
    "GLD": "6",
    "HYG": "19",
    "IWM": "13",
    "QQQ": "7",
    "SMH": "9",
    "SPY": "11",
    "TLT": "17",
}
EXPECTED_SYNTHETIC_CASH = {"KRW": "0", "USD": "100000.00"}

TOKEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PRIVATE_KEY_MATERIAL", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OPENAI_TOKEN", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[\w]{30,})\b")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("STRIPE_LIVE_TOKEN", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}\b")),
    ("ALPACA_KEY_ID", re.compile(r"\bPK[A-Z0-9]{18,40}\b")),
    (
        "JWT_TOKEN",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
)
EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9.!#$%&'*+=?^_`{|}~-]+)"
    r"@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"
)
_WINDOWS_SEPARATOR = "[" + re.escape("/" + chr(92)) + "]"
_WINDOWS_UNC_PREFIX = re.escape(chr(92) * 2)
WINDOWS_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:"
    + _WINDOWS_SEPARATOR
    + "|"
    + _WINDOWS_UNC_PREFIX
    + r"[^\s]+"
    + _WINDOWS_SEPARATOR
    + ")"
)
POSIX_HOME_PATH = re.compile(r"(?i)/(?:home|users)/[^/\s\"'<>]+(?:/|\\b)")
GENERIC_CREDENTIAL_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|refresh[_-]?token|
    password|passwd|cookie|authorization)\b
    \s*(?:=|:)\s*
    (?:
        ["']([^"']+)["']
        |
        ([A-Za-z0-9_./+=-]{12,})
    )
    """
)
ACCOUNT_IDENTIFIER = re.compile(
    r"""(?ix)
    \baccount[_-]?(?:id|number|no)\b
    \s*(?:=|:)\s*
    (?:
        ["']([^"']+)["']
        |
        ([A-Za-z0-9][A-Za-z0-9._-]{5,})(?=\s*(?:\#|$|[,}]))
    )
    """
)
SAFE_VALUE_PREFIXES = (
    "<",
    "${",
    "$",
    "example",
    "synthetic",
    "test",
    "paper-test",
    "unit-test",
    "integration-test",
    "fake",
    "dummy",
    "sample",
    "placeholder",
    "redacted",
    "sensitive",
    "none",
    "null",
    "self.",
    "settings.",
    "active_settings.",
    "os.",
    "row.",
    "account.",
    "binding.",
    "payload.",
    "spec.",
)
SAFE_CODE_VALUES = frozenset({"str", "bytes", "secretstr", "false", "true"})
BINARY_MAGIC = (
    b"\x7fELF",
    b"MZ",
    b"\x89PNG\r\n\x1a\n",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",
    b"PK\x03\x04",
    b"%PDF-",
    b"SQLite format 3\x00",
)
LFS_HEADER = "version https://" + "git-lfs.github.com/spec/v1"
LFS_FILTER_MARKER = "filter" + "=lfs"
LONG_TOKEN_CANDIDATE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{40,128}(?![A-Za-z0-9])")


@dataclass(frozen=True, order=True)
class Violation:
    rule: str
    path: str
    line: int = 0

    def display(self) -> str:
        safe_path = _safe_display_path(self.path)
        location = safe_path if self.line <= 0 else f"{safe_path}:{self.line}"
        return f"{self.rule} {location}"


def _normal_path(path: str | Path) -> str:
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return PurePosixPath(value).as_posix()


def _safe_display_path(path: str) -> str:
    sensitive = (
        WINDOWS_ABSOLUTE_PATH.search(path) is not None
        or POSIX_HOME_PATH.search(path) is not None
        or EMAIL_PATTERN.search(path) is not None
        or any(pattern.search(path) is not None for _, pattern in TOKEN_PATTERNS)
        or any(
            _looks_like_high_entropy_token(match.group(0))
            for match in LONG_TOKEN_CANDIDATE.finditer(path)
        )
    )
    if not sensitive:
        return path
    fingerprint = hashlib.sha256(path.encode("utf-8")).hexdigest()[:12]
    return f"[redacted-path:{fingerprint}]"


def _path_violations(path: str) -> list[Violation]:
    normalized = _normal_path(path)
    lowered = normalized.lower()
    parts = tuple(part.lower() for part in PurePosixPath(normalized).parts)
    violations: list[Violation] = []

    if any(part in FORBIDDEN_PATH_PARTS for part in parts):
        violations.append(Violation("FORBIDDEN_LOCAL_PATH", normalized))
    if lowered in FORBIDDEN_FILE_NAMES:
        violations.append(Violation("FORBIDDEN_PRIVATE_FILE", normalized))
    if lowered.endswith(FORBIDDEN_SUFFIXES):
        violations.append(Violation("FORBIDDEN_FILE_TYPE", normalized))
    if any(PurePosixPath(lowered).name.startswith(name) for name in PRIVATE_KEY_NAMES):
        violations.append(Violation("FORBIDDEN_PRIVATE_KEY_FILE", normalized))
    if PurePosixPath(lowered).name.startswith(".env") and lowered != ALLOWED_DOTENV:
        violations.append(Violation("FORBIDDEN_DOTENV", normalized))
    if lowered.startswith("data/raw/") and lowered != ALLOWED_RAW_PATH:
        violations.append(Violation("RAW_PAYLOAD_FILE", normalized))
    if lowered.startswith("runs/") or lowered.startswith("artifacts/"):
        violations.append(Violation("RESEARCH_RUN_ARTIFACT", normalized))
    if "cookie" in PurePosixPath(lowered).name:
        violations.append(Violation("BROWSER_CREDENTIAL_FILE", normalized))
    if "storage-state" in PurePosixPath(lowered).name:
        violations.append(Violation("BROWSER_CREDENTIAL_FILE", normalized))
    return violations


def _looks_binary(data: bytes) -> bool:
    if any(data.startswith(magic) for magic in BINARY_MAGIC):
        return True
    sample = data[:8192]
    return b"\x00" in sample


def _safe_assigned_value(value: str) -> bool:
    normalized = value.strip().strip("\"'").lower()
    if not normalized:
        return True
    return normalized in SAFE_CODE_VALUES or normalized.startswith(SAFE_VALUE_PREFIXES)


def _looks_like_high_entropy_token(value: str) -> bool:
    if re.fullmatch(r"[a-fA-F0-9]{40,128}", value):
        return False
    if not (
        any(character.islower() for character in value)
        and any(character.isupper() for character in value)
        and any(character.isdigit() for character in value)
    ):
        return False
    counts = {character: value.count(character) for character in set(value)}
    entropy = -sum(
        (count / len(value)) * math.log2(count / len(value)) for count in counts.values()
    )
    return entropy >= 4.25


def _scan_text(path: str, text: str) -> list[Violation]:
    violations: list[Violation] = []
    if text.startswith(LFS_HEADER):
        violations.append(Violation("GIT_LFS_POINTER", path, 1))

    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule, pattern in TOKEN_PATTERNS:
            if pattern.search(line):
                violations.append(Violation(rule, path, line_number))
        if any(
            _looks_like_high_entropy_token(match.group(0))
            for match in LONG_TOKEN_CANDIDATE.finditer(line)
        ):
            violations.append(Violation("HIGH_ENTROPY_TOKEN", path, line_number))

        if WINDOWS_ABSOLUTE_PATH.search(line) or POSIX_HOME_PATH.search(line):
            violations.append(Violation("PRIVATE_ABSOLUTE_PATH", path, line_number))

        for match in EMAIL_PATTERN.finditer(line):
            if (
                path.lower().endswith((".diff", ".patch"))
                and match.start() == 0
                and match.group(1) in {"+", "-"}
                and line[1:2] == "@"
            ):
                # A unified-diff marker followed by a Python decorator is not
                # an email address. Keep scanning all other text on the line;
                # addresses with a non-marker local part remain detectable.
                continue
            prefix = line[: match.start()]
            scheme_position = prefix.rfind("://")
            in_url_authority = scheme_position >= 0 and not re.search(
                r"[\s/]", prefix[scheme_position + 3 :]
            )
            if in_url_authority:
                continue
            domain = match.group(2).lower()
            final_label = domain.rsplit(".", maxsplit=1)[-1]
            if (
                domain not in ALLOWED_EMAIL_DOMAINS
                and len(final_label) >= 2
                and any(character.isalpha() for character in final_label)
            ):
                violations.append(Violation("PERSONAL_EMAIL", path, line_number))

        credential = GENERIC_CREDENTIAL_ASSIGNMENT.search(line)
        if credential is not None:
            value = credential.group(1) or credential.group(2)
            if len(value) >= 12 and not _safe_assigned_value(value):
                violations.append(Violation("CREDENTIAL_ASSIGNMENT", path, line_number))

        account = ACCOUNT_IDENTIFIER.search(line)
        if account is not None:
            value = account.group(1) or account.group(2)
            if not _safe_assigned_value(value):
                violations.append(Violation("NON_SYNTHETIC_ACCOUNT_ID", path, line_number))

        if LFS_FILTER_MARKER in line.replace(" ", "").lower():
            violations.append(Violation("GIT_LFS_CONFIGURATION", path, line_number))
    return violations


def _scan_bytes(path: str, data: bytes) -> list[Violation]:
    violations = _path_violations(path)
    if len(data) > MAX_TEXT_BYTES:
        violations.append(Violation("OVERSIZED_FILE", path))
        return violations
    if _looks_binary(data):
        violations.append(Violation("BINARY_FILE", path))
        return violations
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        violations.append(Violation("NON_UTF8_OR_BINARY", path))
        return violations
    violations.extend(_scan_text(path, text))
    return violations


def _fixture_violations(root: Path) -> list[Violation]:
    path = root / "config" / "paper-account.example.yaml"
    relative = "config/paper-account.example.yaml"
    if not path.is_file():
        return [Violation("MISSING_SYNTHETIC_ACCOUNT_FIXTURE", relative)]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [Violation("INVALID_SYNTHETIC_ACCOUNT_FIXTURE", relative)]

    required_lines = {
        'schema_version: "paper-account.v1"',
        'account_id: "synthetic-paper-account"',
        'source: "SYNTHETIC_EXAMPLE"',
        'amount: "100000.00"',
    }
    stripped_lines = {line.strip() for line in text.splitlines()}
    if not required_lines.issubset(stripped_lines):
        return [Violation("INVALID_SYNTHETIC_ACCOUNT_FIXTURE", relative)]

    position_pairs = re.findall(
        r'(?m)^\s*-\s+symbol:\s*"([^"]+)"\s*\n\s*quantity:\s*"([^"]+)"\s*$',
        text,
    )
    cash_pairs = re.findall(
        r'(?m)^\s*-\s+currency:\s*"([^"]+)"\s*\n\s*amount:\s*"([^"]+)"\s*$',
        text,
    )
    positions = dict(position_pairs)
    cash = dict(cash_pairs)
    if (
        len(position_pairs) != len(EXPECTED_SYNTHETIC_POSITIONS)
        or len(cash_pairs) != len(EXPECTED_SYNTHETIC_CASH)
        or positions != EXPECTED_SYNTHETIC_POSITIONS
        or cash != EXPECTED_SYNTHETIC_CASH
    ):
        return [Violation("NON_SYNTHETIC_ACCOUNT_QUANTITY", relative)]
    return []


def _gitignore_violations(root: Path) -> list[Violation]:
    path = root / ".gitignore"
    if not path.is_file():
        return [Violation("MISSING_GITIGNORE", ".gitignore")]
    required = {
        ".env",
        ".env.*",
        "!.env.example",
        ".local/",
        "config/paper-account.yaml",
        "data/raw/**",
        "browser-profile/",
        "chrome-profile/",
        "user-data-dir/",
        "*.pem",
        "*.key",
    }
    try:
        lines = {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except (OSError, UnicodeDecodeError):
        return [Violation("INVALID_GITIGNORE", ".gitignore")]
    missing = required - lines
    return [Violation("GITIGNORE_RULE_MISSING", ".gitignore") for _ in sorted(missing)]


def scan_worktree(root: Path) -> list[Violation]:
    """Scan the public release set without following links.

    In an initialized repository this is the index plus non-ignored untracked
    files. Ignored local runtime material is not part of a public release, but
    the same path is rejected if it is force-added or appears in history.
    """

    root = root.resolve()
    violations: list[Violation] = []
    if not root.is_dir():
        return [Violation("INVALID_REPOSITORY_ROOT", _normal_path(root))]

    if (root / ".git").exists():
        try:
            output = _run_git(
                root,
                ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                binary=True,
            )
        except RuntimeError:
            return [Violation("WORKTREE_INDEX_UNREADABLE", ".git")]
        if not isinstance(output, bytes):
            return [Violation("WORKTREE_INDEX_UNREADABLE", ".git")]
        for raw_path in sorted(set(output.split(b"\x00"))):
            if not raw_path:
                continue
            try:
                relative = raw_path.decode("utf-8")
            except UnicodeDecodeError:
                violations.append(Violation("NON_UTF8_PATH", ".git/index"))
                continue
            file_path = root / Path(relative)
            normalized = _normal_path(relative)
            if file_path.is_symlink():
                violations.append(Violation("SYMLINK", normalized))
                continue
            try:
                data = file_path.read_bytes()
            except OSError:
                violations.append(Violation("UNREADABLE_FILE", normalized))
                continue
            violations.extend(_scan_bytes(normalized, data))
        violations.extend(_fixture_violations(root))
        violations.extend(_gitignore_violations(root))
        return sorted(set(violations))

    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directory_names[:] = sorted(name for name in directory_names if name != ".git")

        retained_directories: list[str] = []
        for name in directory_names:
            directory = current_path / name
            relative = _normal_path(directory.relative_to(root))
            if directory.is_symlink():
                violations.append(Violation("SYMLINK", relative))
            else:
                path_violations = _path_violations(relative)
                violations.extend(path_violations)
                if not path_violations:
                    retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in sorted(file_names):
            file_path = current_path / name
            relative = _normal_path(file_path.relative_to(root))
            if file_path.is_symlink():
                violations.append(Violation("SYMLINK", relative))
                continue
            try:
                data = file_path.read_bytes()
            except OSError:
                violations.append(Violation("UNREADABLE_FILE", relative))
                continue
            violations.extend(_scan_bytes(relative, data))

    violations.extend(_fixture_violations(root))
    violations.extend(_gitignore_violations(root))
    return sorted(set(violations))


def _run_git(root: Path, arguments: Sequence[str], *, binary: bool = False) -> bytes | str:
    command = ["git", "-c", "core.quotepath=false", "-C", str(root), *arguments]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )
    if completed.returncode != 0:
        raise RuntimeError("git command failed")
    return completed.stdout


def _marker_is_valid(data: bytes, expected_repository: str | None) -> bool:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != MARKER_SCHEMA:
        return False
    if payload.get("history_policy") != MARKER_HISTORY_POLICY:
        return False
    if payload.get("private_history_imported") is not False:
        return False
    repository_name = payload.get("repository_name")
    if not isinstance(repository_name, str) or not repository_name:
        return False
    if expected_repository is not None:
        expected_name = expected_repository.rsplit("/", maxsplit=1)[-1]
        if repository_name != expected_name:
            return False
    return True


def scan_git_history(root: Path, expected_repository: str | None = None) -> list[Violation]:
    """Scan all reachable blobs and enforce a marked, single clean root."""

    root = root.resolve()
    violations: list[Violation] = []
    try:
        inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    except RuntimeError:
        return [Violation("GIT_REPOSITORY_REQUIRED", ".git")]
    if not isinstance(inside, str) or inside.strip() != "true":
        return [Violation("GIT_REPOSITORY_REQUIRED", ".git")]

    try:
        top_level = _run_git(root, ["rev-parse", "--show-toplevel"])
        roots_output = _run_git(root, ["rev-list", "--max-parents=0", "--all"])
    except RuntimeError:
        return [Violation("GIT_HISTORY_UNREADABLE", ".git")]
    if not isinstance(top_level, str) or Path(top_level.strip()).resolve() != root:
        violations.append(Violation("NESTED_OR_FOREIGN_GIT_ROOT", ".git"))
    roots = roots_output.splitlines() if isinstance(roots_output, str) else []
    if len(roots) != 1:
        violations.append(Violation("CLEAN_ROOT_COUNT", ".git"))
        return sorted(set(violations))

    root_commit = roots[0]
    try:
        marker_data = _run_git(root, ["show", f"{root_commit}:{ROOT_MARKER}"], binary=True)
    except RuntimeError:
        marker_data = b""
    if not isinstance(marker_data, bytes) or not _marker_is_valid(
        marker_data, expected_repository
    ):
        violations.append(Violation("CLEAN_ROOT_MARKER", ROOT_MARKER))

    try:
        commits_output = _run_git(root, ["rev-list", "--all"])
    except RuntimeError:
        return sorted(set([*violations, Violation("GIT_HISTORY_UNREADABLE", ".git")]))
    commits = commits_output.splitlines() if isinstance(commits_output, str) else []
    scanned_entries: set[tuple[str, str, str]] = set()
    for commit in commits:
        try:
            commit_object = _run_git(root, ["cat-file", "commit", commit], binary=True)
        except RuntimeError:
            violations.append(Violation("GIT_HISTORY_UNREADABLE", ".git"))
            continue
        if isinstance(commit_object, bytes):
            try:
                commit_text = commit_object.decode("utf-8")
            except UnicodeDecodeError:
                violations.append(Violation("NON_UTF8_GIT_COMMIT", f".git/commits/{commit}"))
            else:
                violations.extend(_scan_text(f".git/commits/{commit}", commit_text))
        try:
            tree_output = _run_git(root, ["ls-tree", "-r", "-z", commit], binary=True)
        except RuntimeError:
            violations.append(Violation("GIT_HISTORY_UNREADABLE", ".git"))
            continue
        if not isinstance(tree_output, bytes):
            continue
        for raw_entry in tree_output.split(b"\x00"):
            if not raw_entry:
                continue
            try:
                metadata, raw_path = raw_entry.split(b"\t", maxsplit=1)
                mode, object_type, object_id = metadata.decode("ascii").split()
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                violations.append(Violation("INVALID_GIT_TREE_ENTRY", ".git"))
                continue
            entry_key = (mode, object_id, path)
            if entry_key in scanned_entries:
                continue
            scanned_entries.add(entry_key)
            normalized = _normal_path(path)
            if mode == "120000":
                violations.append(Violation("GIT_HISTORY_SYMLINK", normalized))
                continue
            if mode == "160000" or object_type == "commit":
                violations.append(Violation("GIT_HISTORY_SUBMODULE", normalized))
                continue
            if object_type != "blob":
                continue
            try:
                blob = _run_git(root, ["cat-file", "blob", object_id], binary=True)
            except RuntimeError:
                violations.append(Violation("GIT_HISTORY_UNREADABLE", normalized))
                continue
            if isinstance(blob, bytes):
                violations.extend(_scan_bytes(normalized, blob))
    return sorted(set(violations))


def scan_repository(
    root: Path,
    *,
    expected_repository: str | None = None,
    require_git: bool = True,
) -> list[Violation]:
    violations = scan_worktree(root)
    marker_path = root.resolve() / ROOT_MARKER
    try:
        current_marker = marker_path.read_bytes()
    except OSError:
        violations.append(Violation("CURRENT_ROOT_MARKER", ROOT_MARKER))
    else:
        if not _marker_is_valid(current_marker, expected_repository):
            violations.append(Violation("CURRENT_ROOT_MARKER", ROOT_MARKER))
    if require_git:
        violations.extend(scan_git_history(root, expected_repository))
    return sorted(set(violations))


def format_report(violations: Iterable[Violation]) -> str:
    ordered = sorted(set(violations))
    if not ordered:
        return "public-release scan: PASS"
    lines = [f"public-release scan: FAIL ({len(ordered)} violation(s))"]
    displayed = ordered[:MAX_DISPLAY_VIOLATIONS]
    lines.extend(f"- {violation.display()}" for violation in displayed)
    omitted = len(ordered) - len(displayed)
    if omitted:
        lines.append(f"- REPORT_TRUNCATED {omitted} additional violation(s)")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan a repository before public release.")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--expected-repository",
        help="Expected owner/repository or repository name bound by the root marker.",
    )
    parser.add_argument(
        "--worktree-only",
        action="store_true",
        help="Skip Git-history checks for local pre-initialization diagnostics only.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        violations = scan_repository(
            arguments.root,
            expected_repository=arguments.expected_repository,
            require_git=not arguments.worktree_only,
        )
    except Exception:
        print("public-release scan: ERROR (fail-closed)", file=sys.stderr)
        return 2
    report = format_report(violations)
    print(report)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
