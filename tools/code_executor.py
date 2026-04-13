"""Sandboxed Python execution tool."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


class ExecutePythonTool(BaseTool):
    """Execute Python code in an isolated subprocess with a timeout."""

    name = "execute_python"
    description = "Execute Python code in an isolated subprocess with a 30-second timeout."
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30, "default": 30},
        },
        "required": ["code"],
    }

    def __init__(self, config: AppConfig) -> None:
        """Initialize the tool with app configuration."""

        self._config = config

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Execute Python source code in a temp directory."""

        if not self._config.permissions.permissions.can_execute_code:
            raise PermissionDeniedError("Code execution is disabled by permissions")

        code = str(kwargs["code"])
        timeout_seconds = int(kwargs.get("timeout_seconds", 30))
        stdout, stderr, returncode = await self._run_code(code, timeout_seconds)
        return self.success(
            {"stdout": stdout, "stderr": stderr, "returncode": returncode},
            timeout_seconds=timeout_seconds,
        )

    async def _run_code(self, code: str, timeout_seconds: int) -> tuple[str, str, int]:
        """Run Python code in a temporary file and capture outputs."""

        with TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "main.py"
            await asyncio.to_thread(script_path.write_text, code, "utf-8")

            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(script_path),
                cwd=temp_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PYTHONPATH": "", "PATH": os.getenv("PATH", "")},
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.communicate()
                raise TimeoutError("Python execution exceeded timeout")

        return stdout_bytes.decode("utf-8"), stderr_bytes.decode("utf-8"), process.returncode or 0
