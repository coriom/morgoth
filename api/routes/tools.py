"""Tools inventory endpoint.

GET /api/tools returns the currently-registered tool inventory as
``[{name, is_data_source, is_chat_tool}]``. Used by the self-modify
apply pipeline's health check: after applying a new tool, restart, and
poll this endpoint to confirm the new tool's ``name`` appears before
declaring apply success. Also useful for humans debugging discovery.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools(request: Request) -> list[dict[str, Any]]:
    """Return the registered tool inventory in deterministic name order."""
    router_obj = request.app.state.tool_router
    tools: list[dict[str, Any]] = []
    for tool in router_obj._tools.values():  # noqa: SLF001 — inventory read
        tools.append(
            {
                "name": tool.name,
                "is_data_source": bool(getattr(tool, "is_data_source", False)),
                "is_chat_tool": bool(getattr(tool, "is_chat_tool", True)),
            }
        )
    tools.sort(key=lambda t: t["name"])
    return tools
