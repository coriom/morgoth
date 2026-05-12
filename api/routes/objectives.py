"""Objective management API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from core.objectives import Objective, ObjectiveCategory, ObjectiveStatus, ObjectivesManager


router = APIRouter(prefix="/api/objectives", tags=["objectives"])


class ObjectiveCreateRequest(BaseModel):
    """Request body for human-created objectives."""

    title: str
    description: str
    category: ObjectiveCategory
    priority: int = Field(default=2, ge=0, le=3)
    user_id: str = "default"


class ObjectiveUpdateRequest(BaseModel):
    """Request body for objective status updates."""

    status: ObjectiveStatus


def _build_manager(request: Request) -> ObjectivesManager:
    """Create an objectives manager from the current application state."""

    return ObjectivesManager(
        request.app.state.config,
        request.app.state.persistent_memory,
        request.app.state.llm_client,
    )


@router.get("/stats")
async def get_objectives_stats(request: Request) -> dict[str, Any]:
    """Return aggregated statistics about all objectives."""

    return await request.app.state.persistent_memory.get_objectives_stats()


@router.get("")
async def list_objectives(request: Request, status: ObjectiveStatus | None = None, limit: int = 100) -> dict[str, Any]:
    """Return objectives for the Mind page."""

    manager = _build_manager(request)
    items = await manager.list_objectives(status=status, limit=limit)
    return {"items": [item.model_dump(mode="json") for item in items]}


@router.post("")
async def create_objective(payload: ObjectiveCreateRequest, request: Request) -> dict[str, Any]:
    """Create a human-generated objective."""

    manager = _build_manager(request)
    objective = Objective(
        title=payload.title,
        description=payload.description,
        category=payload.category,
        priority=payload.priority,
        generated_by="human",
        user_id=payload.user_id,
    )
    created = await manager.create_objective(objective)
    return created.model_dump(mode="json")


@router.patch("/{objective_id}")
async def update_objective_status(
    objective_id: str,
    payload: ObjectiveUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    """Update one objective's lifecycle status."""

    manager = _build_manager(request)
    updated = await manager.update_status(objective_id, payload.status)
    if not updated:
        raise HTTPException(status_code=404, detail="Objective not found")

    items = await manager.list_objectives(limit=200)
    for item in items:
        if item.objective_id == objective_id:
            return item.model_dump(mode="json")
    raise HTTPException(status_code=404, detail="Objective not found after update")
