"""Catch-up backup for intermittent-usage machines.

The scheduled cron ``0 4 * * * scripts/backup_morgoth.sh`` fires at
04:00 local. The operator now shuts the PC down at night — the job
never runs. Latest backup at 5-day age is a real exposure: the newer
tables (metric_series, source_snapshots, campaigns, numeric_fidelity_
events, session_gaps, web_search_cache) don't yet exist in that dump.

This module runs at Brain startup. If the latest backup directory is
older than MORGOTH_BACKUP_MAX_AGE_HOURS (default 24), it spawns
scripts/backup_morgoth.sh in the background. Non-blocking, non-fatal.
The cron schedule stays as the secondary path — this only ensures a
fresh backup exists at least once per uptime session.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger


BACKUP_ROOT = Path.home() / "Morgoth" / "backups"
BACKUP_SCRIPT = Path.home() / "Morgoth" / "morgoth" / "scripts" / "backup_morgoth.sh"
_TS_RE = re.compile(r"^(\d{8})_(\d{6})$")


def backup_max_age_hours() -> int:
    raw = os.environ.get("MORGOTH_BACKUP_MAX_AGE_HOURS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return 24


def _parse_ts_dir(name: str) -> datetime | None:
    """Parse a backup directory name like 20260913_040001 → datetime."""
    m = _TS_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(0), "%Y%m%d_%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def latest_backup_info(root: Path | None = None) -> dict[str, Any] | None:
    """Return {path, timestamp, size_bytes, age_seconds} for the newest
    backup dir, or None when no valid directory exists.

    `root` defaults to the module-level BACKUP_ROOT read at call time
    (not at def time), so monkeypatching bw.BACKUP_ROOT in tests works.
    """
    if root is None:
        root = BACKUP_ROOT
    if not root.exists():
        return None
    newest: tuple[datetime, Path] | None = None
    for child in root.iterdir():
        if not child.is_dir():
            continue
        ts = _parse_ts_dir(child.name)
        if ts is None:
            continue
        if newest is None or ts > newest[0]:
            newest = (ts, child)
    if newest is None:
        return None
    ts, path = newest
    size = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return {
        "path": str(path),
        "timestamp": ts,
        "size_bytes": int(size),
        "age_seconds": max(0, (datetime.now(timezone.utc) - ts).total_seconds()),
    }


async def catch_up_if_stale() -> dict[str, Any] | None:
    """Fire scripts/backup_morgoth.sh in the background if the latest
    backup is older than the age threshold. Never awaits the script;
    never raises. Returns a status dict for logging."""
    if not BACKUP_SCRIPT.exists():
        logger.warning("backup_watchdog: script missing at {}", BACKUP_SCRIPT)
        return None
    info = latest_backup_info()
    max_age = backup_max_age_hours() * 3600
    if info and info["age_seconds"] < max_age:
        return {"action": "skip", "age_seconds": int(info["age_seconds"])}
    age_h = (info["age_seconds"] / 3600.0) if info else float("inf")
    logger.info(
        "backup_watchdog: latest backup is {} old (threshold {}h) — firing",
        f"{age_h:.1f}h" if info else "MISSING",
        backup_max_age_hours(),
    )
    try:
        proc = await _spawn_backup_script()
        # Fire-and-forget: DO NOT await proc.wait(); the script writes
        # its own log line and takes a few seconds.
        logger.info("backup_watchdog: spawned pid={}", proc.pid)
        return {"action": "spawned", "pid": proc.pid,
                "prior_age_seconds": int(info["age_seconds"]) if info else None}
    except Exception as exc:
        logger.warning("backup_watchdog: spawn failed: {}", exc)
        return {"action": "spawn_failed", "error": str(exc)}


async def _spawn_backup_script():
    """Extracted so tests can patch this instead of asyncio globals."""
    return await asyncio.create_subprocess_exec(
        "bash", str(BACKUP_SCRIPT),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def format_size(bytes_: int) -> str:
    for unit, div in (("G", 1024**3), ("M", 1024**2), ("K", 1024)):
        if bytes_ >= div:
            return f"{bytes_ / div:.1f}{unit}"
    return f"{bytes_}B"
