"""Filesystem tools for Morgoth."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from core.config import AppConfig
from tools.base_tool import BaseTool


class ReadFileTool(BaseTool):
    """Read files from the repository."""

    name = "read_file"
    description = "Read the contents of a file within the repository."
    parameters = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self, config: AppConfig) -> None:
        """Initialize the tool with app configuration."""

        self._config = config

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Read a file and return its contents."""

        path = self._config.ensure_path_readable(kwargs["path"])

        def _reader() -> str:
            return path.read_text(encoding="utf-8")

        content = await asyncio.to_thread(_reader)
        return self.success({"path": str(path.relative_to(self._config.root_dir)), "content": content})


class WriteFileTool(BaseTool):
    """Write files only inside the evolvable zone."""

    name = "write_file"
    description = "Write a file only if the path is inside the evolvable zone."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, config: AppConfig) -> None:
        """Initialize the tool with app configuration."""

        self._config = config

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Write file content inside the allowed writable zone."""

        path = self._config.ensure_path_writable(kwargs["path"])
        content = str(kwargs["content"])

        def _writer() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_writer)
        return self.success({"path": str(path.relative_to(self._config.root_dir)), "bytes_written": len(content)})
