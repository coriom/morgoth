"""Tool routing for Morgoth."""

from __future__ import annotations

from typing import Any

from loguru import logger

from tools.base_tool import BaseTool


class ToolRouter:
    """Registry and execution router for tools."""

    def __init__(self, persistent_memory: Any = None) -> None:
        """Initialize an empty tool registry.

        ``persistent_memory`` is optional; when provided, execute_tool
        consults the source cache (core.source_cache) BEFORE hitting a
        live tool for in-scope slow-moving sources. Snapshots are
        served from the store annotated with age/staleness. See
        SOURCE_CACHE_CONFIG for the whitelist.
        """
        self._tools: dict[str, BaseTool] = {}
        self._persistent_memory = persistent_memory

    def register(self, tool: BaseTool) -> None:
        """Register a tool by its unique name."""

        self._tools[tool.name] = tool
        logger.debug("Registered tool '{}'", tool.name)

    def has_tool(self, name: str) -> bool:
        """Return True if the tool name is registered."""

        return name in self._tools

    def list_names(self) -> list[str]:
        """Return all registered tool names."""

        return list(self._tools.keys())

    def get_tool(self, name: str) -> BaseTool:
        """Return a tool by name."""

        if name not in self._tools:
            raise KeyError(f"Unknown tool: {name}")
        return self._tools[name]

    def get_schemas(self, allowed_tools: list[str] | None = None) -> list[dict[str, Any]]:
        """Return Ollama schemas for all or a subset of tools."""

        tools = self._tools.values() if allowed_tools is None else [self.get_tool(name) for name in allowed_tools]
        return [tool.to_ollama_schema() for tool in tools]

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        bypass_cache: bool = False,
    ) -> dict[str, Any]:
        """Execute a registered tool and return its structured result.

        Source-cache read path: for in-scope slow-moving sources, the
        newest snapshot from source_snapshots is returned instead of a
        live call. The result envelope carries observed_at + age_seconds
        + stale in ``metadata`` so the model and downstream gates see
        how fresh the value is. ``bypass_cache=True`` forces a live
        call — used by the collector itself to write fresh snapshots.
        """
        logger.info("Executing tool '{}'", name)
        if not bypass_cache and self._persistent_memory is not None:
            from core.source_cache import (
                is_cached_source, cache_enabled, serve_from_cache,
            )
            if is_cached_source(name) and cache_enabled():
                try:
                    cached = await serve_from_cache(self._persistent_memory, name)
                except Exception as exc:
                    logger.warning("source_cache read failed for {}: {}", name, exc)
                    cached = None
                if cached is not None:
                    logger.debug(
                        "served {} from cache age={}s stale={}",
                        name, cached["metadata"]["age_seconds"],
                        cached["metadata"]["stale"],
                    )
                    return cached
        tool = self.get_tool(name)
        return await tool.execute(**arguments)

    async def close(self) -> None:
        """Close registered tools that expose an async ``close`` method."""

        for tool in self._tools.values():
            close_method = getattr(tool, "close", None)
            if close_method is not None:
                await close_method()
