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
