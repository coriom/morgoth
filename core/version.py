"""Code-version capture — short git sha, memoized once at startup.

Cached at module import via a module-level singleton. `get_code_version()`
NEVER shells out per call — subprocess runs at most ONCE per process
lifetime. Fallback 'unknown' when git is unavailable (installed under
Docker without .git, on a machine without git, etc.).

Why this exists: the pre/post-grounding split at 8d79962 had to be
reconstructed from a systemd restart timestamp because nothing recorded
which code produced which thesis. With code_version stamped at write
time on every thesis / proposal, future measurements can filter by
commit exactly — e.g. "hit-rate on theses produced under commit X vs Y".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_CACHED: str | None = None


def _probe_git_sha() -> str:
    """Run `git rev-parse --short HEAD` from the repo root. Bounded
    timeout; any failure returns 'unknown'."""
    repo = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo, capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
            if sha:
                return sha
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "unknown"


def get_code_version() -> str:
    """Return the cached short-sha. First call runs one subprocess;
    subsequent calls are constant-time dict reads."""
    global _CACHED
    if _CACHED is None:
        _CACHED = _probe_git_sha()
    return _CACHED


def _reset_cache_for_tests() -> None:
    """Test-only hook — clear the memoized value so a subsequent
    get_code_version() call re-probes. NEVER call from production."""
    global _CACHED
    _CACHED = None
