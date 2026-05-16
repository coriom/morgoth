"""Brain status API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/brain", tags=["brain"])


@router.get("/status")
async def get_status(request: Request) -> dict[str, Any]:
    """Return brain runtime status."""

    return await request.app.state.brain.get_status()


@router.get("/logs")
async def get_logs(request: Request, limit: int = 100) -> dict[str, Any]:
    """Return recent brain logs."""

    return {"items": await request.app.state.brain.get_logs(limit=limit)}


@router.get("/tasks")
async def get_tasks(request: Request) -> dict[str, Any]:
    """Return scheduled tasks."""

    return {"items": await request.app.state.brain.get_tasks()}


@router.get("/cycle-feed")
async def get_cycle_feed(request: Request, limit: int = 50) -> list[dict[str, Any]]:
    """Return recent cycle-feed events, newest first (max 200)."""

    return request.app.state.brain.get_cycle_feed(limit=limit)


@router.get("/self-modifications")
async def get_self_modifications(request: Request, limit: int = 100) -> dict[str, Any]:
    """Return recent self-modification entries."""

    rows = await request.app.state.persistent_memory.fetch(
        "SELECT * FROM self_modifications ORDER BY timestamp DESC LIMIT $1",
        limit,
    )
    return {"items": [dict(row) for row in rows]}
