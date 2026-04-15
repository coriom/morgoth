"""Git-backed change tracking for self-modification events."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig
from memory.persistent import PersistentMemory


class DiffLogEntry(BaseModel):
    """Persisted metadata for one self-modification event."""

    file_path: str
    diff: str
    reason: str
    test_result: dict[str, Any] | None = None
    approved_by: str = "morgoth"
    user_id: str = "default"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DiffLogger:
    """Capture git diffs and persist them to PostgreSQL."""

    def __init__(self, config: AppConfig, persistent_memory: PersistentMemory) -> None:
        """Store runtime dependencies."""

        self._config = config
        self._persistent_memory = persistent_memory

    async def capture_diff(self, relative_path: str) -> str:
        """Return the current git diff for a path, including untracked files."""

        path = self._config.ensure_path_readable(relative_path)
        repo_relative = str(path.relative_to(self._config.root_dir))
        tracked_diff = await self._run_git("diff", "--", repo_relative, check=False)
        if tracked_diff.strip():
            return tracked_diff

        status = await self._run_git("status", "--short", "--", repo_relative, check=False)
        if status.strip().startswith("??"):
            return await self._run_git("diff", "--no-index", "--", "/dev/null", repo_relative, check=False)
        return tracked_diff

    async def log_change(self, entry: DiffLogEntry) -> None:
        """Persist a self-modification entry."""

        query = """
        INSERT INTO self_modifications (file_path, diff, reason, test_result, approved_by, user_id)
        VALUES ($1, $2, $3, $4::jsonb, $5, $6)
        """
        await self._persistent_memory.execute(
            query,
            entry.file_path,
            entry.diff,
            entry.reason,
            json.dumps(entry.test_result),
            entry.approved_by,
            entry.user_id,
        )
        logger.info("Logged self-modification diff for '{}'", entry.file_path)

    async def snapshot_and_log(
        self,
        relative_path: str,
        *,
        reason: str,
        test_result: dict[str, Any] | None = None,
        approved_by: str = "morgoth",
        user_id: str = "default",
    ) -> DiffLogEntry:
        """Capture the current diff for a path and persist it."""

        diff = await self.capture_diff(relative_path)
        entry = DiffLogEntry(
            file_path=relative_path,
            diff=diff,
            reason=reason,
            test_result=test_result,
            approved_by=approved_by,
            user_id=user_id,
        )
        await self.log_change(entry)
        return entry

    async def _run_git(self, *args: str, check: bool) -> str:
        """Execute a git command in the repository root."""

        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self._config.root_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if check and process.returncode != 0:
            raise RuntimeError(stderr.strip() or f"git {' '.join(args)} failed")
        if process.returncode != 0 and stderr.strip():
            logger.debug("git {} returned {}: {}", " ".join(args), process.returncode, stderr.strip())
        return stdout
