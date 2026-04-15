"""Isolated pytest runner for self-modification validation."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from time import perf_counter

from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig, PermissionDeniedError


class TestRunRequest(BaseModel):
    """Pytest execution request."""

    __test__ = False

    paths: list[str] = Field(default_factory=list)
    extra_args: list[str] = Field(default_factory=list)
    timeout_seconds: int = 180


class TestRunResult(BaseModel):
    """Pytest execution result."""

    __test__ = False

    success: bool
    command: list[str]
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float
    timed_out: bool = False
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CodeTester:
    """Run pytest in a subprocess with bounded scope and timeout."""

    def __init__(self, config: AppConfig) -> None:
        """Store runtime configuration."""

        self._config = config

    async def run_pytest(self, request: TestRunRequest) -> TestRunResult:
        """Execute pytest against repo-local targets."""

        if not self._config.permissions.permissions.can_execute_code:
            raise PermissionDeniedError("Code execution is disabled by permissions")

        target_paths = self._normalize_targets(request.paths)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *request.extra_args,
            *target_paths,
        ]
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        started = perf_counter()
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self._config.root_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds,
            )
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning("Pytest timed out after {}s", request.timeout_seconds)
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            stdout_bytes, stderr_bytes = await process.communicate()

        duration = perf_counter() - started
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        result = TestRunResult(
            success=(process.returncode == 0 and not timed_out),
            command=command,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            timed_out=timed_out,
        )
        if result.success:
            logger.info("Pytest completed successfully in {:.2f}s", duration)
        else:
            logger.warning("Pytest failed in {:.2f}s with return code {}", duration, process.returncode)
        return result

    def _normalize_targets(self, paths: list[str]) -> list[str]:
        """Resolve and sanitize pytest targets."""

        if not paths:
            return ["tests"]
        normalized: list[str] = []
        for raw_path in paths:
            resolved = self._config.ensure_path_readable(raw_path)
            normalized.append(str(resolved.relative_to(self._config.root_dir)))
        return normalized
