"""Shared storage validation for Project and deprecated Domain namespaces."""
from __future__ import annotations

import re
from pathlib import Path

COLLECTIONS = ("conversations", "research", "decisions", "market_patterns", "code_archive")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}\Z")
_PREFIX = re.compile(r"[a-z][a-z0-9_]{0,30}_\Z")


def validate_identifier(value: str) -> str:
    """Reject unsafe or PostgreSQL-truncated identifiers before SQL."""
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("invalid storage identifier; use lowercase ASCII letters, digits, underscores (max 63)")
    if value.startswith("pg_") or value == "information_schema":
        raise ValueError("reserved storage identifier")
    return value


def validate_namespace(schema: str, prefix: str, *, legacy: bool = False) -> None:
    """Only the legacy adapter may use public or an empty Chroma prefix."""
    validate_identifier(schema)
    if not isinstance(prefix, str) or (prefix and not _PREFIX.fullmatch(prefix)):
        raise ValueError("invalid Chroma prefix; expected lowercase ASCII ending in underscore (max 32)")
    if not legacy and (schema == "public" or not prefix):
        raise ValueError("new projects require explicit non-public schema and nonempty Chroma prefix")


def storage_path(value: str | Path) -> Path:
    """Canonical absolute path, including existing symlink aliases."""
    if not isinstance(value, (str, Path)) or not str(value).strip() or "$" in str(value):
        raise ValueError("storage path must be explicit and nonempty; environment expansion is forbidden")
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise ValueError("storage path must be absolute and cannot be filesystem root")
    path = path.resolve()
    if path == Path(path.anchor):
        raise ValueError("storage path cannot resolve to filesystem root")
    return path


def overlap(a: Path, b: Path) -> bool:
    """Equal paths or either ancestor constitute the same writable namespace."""
    return a == b or a in b.parents or b in a.parents


def collection_names(prefix: str) -> tuple[str, ...]:
    """All managed physical names; logical collection names stay fixed."""
    return tuple(prefix + name for name in COLLECTIONS)
