"""Tool routing for Morgoth."""

from __future__ import annotations

from typing import Any

from loguru import logger

from tools.base_tool import BaseTool


class ToolRouter:
    """Registry and execution router for tools."""

    def __init__(self) -> None:
        """Initialize an empty tool registry."""

        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register a tool by its unique name."""

        self._tools[tool.name] = tool
        logger.debug("Registered tool '{}'", tool.name)

    def get_tool(self, name: str) -> BaseTool:
        """Return a tool by name."""

        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def get_schemas(self, allowed_tools: list[str] | None = None) -> list[dict[str, Any]]:
        """Return Ollama schemas for all or a subset of tools."""

        tools = self._tools.values() if allowed_tools is None else [self.get_tool(name) for name in allowed_tools]
        return [tool.to_ollama_schema() for tool in tools]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute a registered tool and return its structured result."""

        logger.info("Executing tool '{}'", name)
        tool = self.get_tool(name)
        return await tool.execute(**arguments)

    async def close(self) -> None:
        """Close registered tools that expose an async ``close`` method."""

        for tool in self._tools.values():
            close_method = getattr(tool, "close", None)
            if close_method is not None:
                await close_method()
