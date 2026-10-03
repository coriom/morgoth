"""Tool routing for Morgoth."""

from __future__ import annotations

from typing import Any

from loguru import logger

from tools.base_tool import BaseTool
from core.tool_rail import EffectiveToolRail, effective_tool_rail


class ToolAccessError(KeyError):
    """A tool is unknown or installed but inactive for this Domain."""

    def __init__(self, name: str, code: str) -> None:
        self.code = code
        super().__init__(f"{code}: {name}")


def _validate_arguments(tool: "BaseTool", arguments: dict[str, Any]) -> str | None:
    """Check `arguments` against `tool.parameters` (JSON-schema dict).

    Returns an error message string when the call is malformed —
    missing required param OR unexpected param name — else None.
    Empty/opaque schemas short-circuit to None (no validation possible).
    Structural: only inspects top-level required + properties.keys.
    """
    schema = getattr(tool, "parameters", None) or {}
    if not isinstance(schema, dict):
        return None
    props = schema.get("properties") or {}
    if not isinstance(props, dict) or not props:
        return None  # opaque / free-form schema — do not validate
    required = schema.get("required") or []
    if not isinstance(required, list):
        required = []
    allowed = set(props.keys())
    missing = [k for k in required if k not in (arguments or {})]
    unknown = [k for k in (arguments or {}) if k not in allowed]
    if not missing and not unknown:
        return None
    parts: list[str] = []
    if missing:
        parts.append(f"missing required: {sorted(missing)}")
    if unknown:
        parts.append(f"unknown param(s): {sorted(unknown)}")
    return (
        "invalid arguments — " + "; ".join(parts)
        + f". Allowed parameters: {sorted(allowed)}."
    )


class ToolRouter:
    """Registry and execution router for tools."""

    def __init__(self, persistent_memory: Any = None,
                 policy: EffectiveToolRail | None = None) -> None:
        """Initialize an empty tool registry.

        ``persistent_memory`` is optional; when provided, execute_tool
        consults the source cache (core.source_cache) BEFORE hitting a
        live tool for in-scope slow-moving sources. Snapshots are
        served from the store annotated with age/staleness. See
        SOURCE_CACHE_CONFIG for the whitelist.
        """
        self._tools: dict[str, BaseTool] = {}
        self._persistent_memory = persistent_memory
        if policy is None:
            from core.domain import current_domain
            policy = effective_tool_rail(current_domain())
        self.policy = policy

    def register(self, tool: BaseTool) -> None:
        """Register a tool by its unique name."""
        if not self.policy.is_allowed(tool.name):
            raise ToolAccessError(tool.name, self.policy.denial_code(tool.name))
        if tool.name in self._tools:
            raise ValueError(f"duplicate active tool: {tool.name}")
        self._tools[tool.name] = tool
        logger.debug("Registered tool '{}'", tool.name)

    def has_tool(self, name: str) -> bool:
        """Return True if the tool name is registered."""

        return self.policy.is_allowed(name) and name in self._tools

    def list_names(self) -> list[str]:
        """Return all registered tool names."""
        return [name for name in self._tools if self.policy.is_allowed(name)]

    def get_tool(self, name: str) -> BaseTool:
        """Return a tool by name."""
        if not self.policy.is_allowed(name):
            raise ToolAccessError(name, self.policy.denial_code(name))
        if name not in self._tools:
            raise ToolAccessError(name, "UNKNOWN_TOOL")
        return self._tools[name]

    def get_schemas(self, allowed_tools: list[str] | None = None) -> list[dict[str, Any]]:
        """Return Ollama schemas for all or a subset of tools."""

        tools = ([tool for name, tool in self._tools.items() if self.policy.is_allowed(name)]
                 if allowed_tools is None else [self.get_tool(name) for name in allowed_tools])
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
        # Remove the double-fetch below — _validate_arguments already
        # got the tool via get_tool. Keep the reference for downstream.
        # (Merged into the pattern that follows.)
        # ARGUMENT VALIDATION at the router boundary — 123 malformed
        # calls in ChromaDB history burned cycles on KeyError. Reject
        # missing-required + unknown-param BEFORE execute; return a
        # structured failure that names what's wrong + the allowed
        # parameter names so the model can retry on the next turn.
        # Never invents defaults — that's the model's error to fix.
        try:
            tool = self.get_tool(name)
        except ToolAccessError as exc:
            return {"success": False, "result": None, "error": exc.code,
                    "metadata": {"tool_error": exc.code}}
        _val_err = _validate_arguments(tool, arguments)
        if _val_err is not None:
            logger.warning("tool-arg reject: {} — {}", name, _val_err)
            return {"success": False, "result": None,
                    "error": _val_err, "metadata": {"validation": True}}
        if not bypass_cache and self._persistent_memory is not None:
            from core.source_cache import (
                is_cached_source, cache_enabled, serve_from_cache,
                serve_web_search, record_web_search_hit, normalize_query,
                query_bypasses_cache,
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
            # Web-search cache — query-keyed, TTL-bounded, recency-bypass.
            elif name == "web_search" and cache_enabled():
                query = str((arguments or {}).get("query", ""))
                try:
                    cached = await serve_web_search(self._persistent_memory, query)
                except Exception as exc:
                    logger.warning("web_search cache read failed: {}", exc)
                    cached = None
                if cached is not None:
                    logger.debug(
                        "served web_search from cache age={}s query={!r}",
                        cached["metadata"]["age_seconds"], query[:60],
                    )
                    return cached
                # MISS or bypass → live call, then persist for next time
                # (only if the query is non-empty and not a recency query).
                result = await tool.execute(**arguments)
                if (isinstance(result, dict) and result.get("success")
                        and query and not query_bypasses_cache(query)):
                    try:
                        await record_web_search_hit(
                            self._persistent_memory, query, result.get("result"),
                        )
                    except Exception as exc:
                        logger.warning("web_search cache write failed: {}", exc)
                return result
        result = await tool.execute(**arguments)
        if (self._persistent_memory is not None and isinstance(result, dict)
                and result.get("success") and name in self.policy.sources):
            from core.domain import current_domain
            domain = current_domain()
            if name in domain.fact_captures:
                try:
                    from datetime import datetime, timezone
                    from core.project import current_project
                    from core.temporal_facts import extract_temporal_facts
                    from core.version import get_code_version
                    facts = extract_temporal_facts(
                        domain, current_project(), name, result.get("result"),
                        datetime.now(timezone.utc), get_code_version(),
                    )
                    await self._persistent_memory.insert_temporal_facts(facts)
                    capture = {"status": "captured", "facts_processed": len(facts)}
                except Exception as exc:
                    # Evidence capture is explicit, but cannot rewrite the
                    # acquisition tool's success/failure semantics.
                    logger.warning("temporal fact capture failed for {}: {}", name, type(exc).__name__)
                    capture = {"status": "failed", "facts_processed": 0}
                metadata = result.get("metadata")
                result["metadata"] = {**(metadata if isinstance(metadata, dict) else {}),
                                      "temporal_fact_capture": capture}
        return result

    async def close(self) -> None:
        """Close registered tools that expose an async ``close`` method."""

        for tool in self._tools.values():
            close_method = getattr(tool, "close", None)
            if close_method is not None:
                await close_method()
