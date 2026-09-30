"""Tools inventory endpoint.

GET /api/tools lists only active Domain tools. GET /api/tools/catalog lists
installed implementations, including inactive ones, for apply verification.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools(request: Request) -> list[dict[str, Any]]:
    """Return the registered tool inventory in deterministic name order.

    Source/chat flags are the effective rail's policy, never class claims.
    """
    router_obj = request.app.state.tool_router
    source_tools = router_obj.policy.sources
    tools: list[dict[str, Any]] = []
    for tool in router_obj._tools.values():  # noqa: SLF001 — inventory read
        if not router_obj.policy.is_allowed(tool.name):
            continue
        tools.append(
            {
                "name": tool.name,
                "is_data_source": tool.name in source_tools,
                "is_chat_tool": tool.name in router_obj.policy.chat,
            }
        )
    tools.sort(key=lambda t: t["name"])
    return tools


@router.get("/catalog")
async def installed_tools(request: Request) -> list[dict[str, Any]]:
    """Read-only installed catalog; apply verifies installation here, not activation."""
    from core.tool_rail import installed_catalog
    active = set(request.app.state.tool_router.list_names())
    return [{"name": name, "active": name in active}
            for name in sorted(installed_catalog())]
