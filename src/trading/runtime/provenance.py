from __future__ import annotations

import hashlib
from pathlib import Path

from trading.domain.hashing import canonical_hash


def _portable_text_hash(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def workspace_code_version(repo_root: Path) -> str:
    candidates = [
        repo_root / "pyproject.toml",
        repo_root / "alembic.ini",
        *sorted((repo_root / "src" / "trading").rglob("*.py")),
        *sorted((repo_root / "migrations").rglob("*.py")),
        *sorted((repo_root / "migrations").rglob("*.mako")),
    ]
    manifest: dict[str, str] = {}
    for path in candidates:
        if not path.is_file():
            raise FileNotFoundError(f"Code manifest input does not exist: {path}")
        relative = path.relative_to(repo_root).as_posix()
        manifest[relative] = _portable_text_hash(path)
    return f"workspace:{canonical_hash(manifest)}"
