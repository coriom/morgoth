"""Safe file integration for self-modification outputs."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig, PermissionDeniedError
from self_modify.code_tester import CodeTester, TestRunRequest, TestRunResult
from self_modify.diff_logger import DiffLogger


class FileUpdateRequest(BaseModel):
    """Requested file update payload."""

    target_path: str
    content: str
    reason: str
    test_paths: list[str] = Field(default_factory=list)
    approved_by: str = "morgoth"
    user_id: str = "default"


class FileUpdateResult(BaseModel):
    """Result of a safe file integration."""

    target_path: str
    backup_path: str | None = None
    tests_run: bool = False
    test_result: TestRunResult | None = None
    diff_logged: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SafeUpdater:
    """Apply validated file updates with rollback on failure."""

    def __init__(self, config: AppConfig, tester: CodeTester, diff_logger: DiffLogger) -> None:
        """Store runtime dependencies."""

        self._config = config
        self._tester = tester
        self._diff_logger = diff_logger

    async def integrate(self, request: FileUpdateRequest) -> FileUpdateResult:
        """Write a file atomically, run optional tests, and rollback on failure."""

        self._ensure_self_modify_enabled()
        target_path = self._config.ensure_path_writable(request.target_path)
        relative_path = str(target_path.relative_to(self._config.root_dir))
        previous_content = await self._read_optional_text(target_path)
        backup_path = await self._backup_existing_file(target_path, previous_content)
        self._validate_content(target_path, request.content)

        try:
            await self._write_atomic(target_path, request.content)
            test_result: TestRunResult | None = None
            if request.test_paths:
                test_result = await self._tester.run_pytest(TestRunRequest(paths=request.test_paths))
                if not test_result.success:
                    raise RuntimeError("Validation tests failed")

            await self._diff_logger.snapshot_and_log(
                relative_path,
                reason=request.reason,
                test_result=test_result.model_dump(mode="json") if test_result else None,
                approved_by=request.approved_by,
                user_id=request.user_id,
            )
            logger.info("Integrated file update for '{}'", relative_path)
            return FileUpdateResult(
                target_path=relative_path,
                backup_path=str(backup_path.relative_to(self._config.root_dir)) if backup_path else None,
                tests_run=bool(request.test_paths),
                test_result=test_result,
                diff_logged=True,
            )
        except Exception:
            logger.exception("Safe update failed for '{}'; restoring previous version", relative_path)
            await self._restore(target_path, previous_content)
            raise

    def _ensure_self_modify_enabled(self) -> None:
        """Fail fast when self-modification is disabled."""

        if not self._config.permissions.permissions.can_self_modify:
            raise PermissionDeniedError("Self-modification is disabled by permissions")

    def _validate_content(self, target_path: Path, content: str) -> None:
        """Validate Python syntax before writing Python modules."""

        if target_path.suffix != ".py":
            return
        compile(content, str(target_path), "exec")

    async def _read_optional_text(self, path: Path) -> str | None:
        """Read existing file content if the file already exists."""

        if not path.exists():
            return None
        return await asyncio.to_thread(path.read_text, "utf-8")

    async def _backup_existing_file(self, target_path: Path, previous_content: str | None) -> Path | None:
        """Persist a backup copy before mutating an existing file."""

        if previous_content is None:
            return None
        backup_dir = self._config.data_dir / "self_modify_backups"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup_path = backup_dir / f"{timestamp}_{target_path.name}.bak"

        def _writer() -> None:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path.write_text(previous_content, encoding="utf-8")

        await asyncio.to_thread(_writer)
        return backup_path

    async def _write_atomic(self, target_path: Path, content: str) -> None:
        """Write content atomically using a temporary sibling file."""

        temp_path = target_path.with_name(f".{target_path.name}.tmp")

        def _writer() -> None:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(content, encoding="utf-8")
            os.replace(temp_path, target_path)

        await asyncio.to_thread(_writer)

    async def _restore(self, target_path: Path, previous_content: str | None) -> None:
        """Restore the previous file state after a failed integration."""

        if previous_content is None:
            await asyncio.to_thread(target_path.unlink, missing_ok=True)
            return
        await asyncio.to_thread(target_path.write_text, previous_content, "utf-8")
