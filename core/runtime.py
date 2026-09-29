"""Installation paths and future desktop home boundary; no I/O at import."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RuntimeHome:
    """Application home is separate from the immutable engine installation."""

    application_home: Path
    projects_root: Path
    engine_root: Path = ENGINE_ROOT


def runtime_home() -> RuntimeHome:
    """Resolve the project catalog root without relocating legacy state."""
    raw = os.environ.get("MORGOTH_HOME")
    if raw is not None and not raw.strip():
        raise ValueError("MORGOTH_HOME must not be empty")
    home = Path(raw).expanduser() if raw else Path.home() / "Morgoth"
    if not home.is_absolute():
        raise ValueError("MORGOTH_HOME must be absolute")
    home = home.resolve()
    return RuntimeHome(home, home / "projects")
