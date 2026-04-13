"""Agent management API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field


router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentCreateRequest(BaseModel):
    """Request body to create an agent."""

    name: str
    task: str
    agent_type: str = "ephemeral"
    tools: list[str] = Field(default_factory=list)
    user_id: str = "default"


@router.get("")
async def list_agents(request: Request) -> dict[str, Any]:
    """List all active agents."""

    items = await request.app.state.agent_manager.list_agents()
    return {"items": items}


@router.post("")
async def create_agent(payload: AgentCreateRequest, request: Request) -> dict[str, Any]:
    """Create a new agent."""

    item = await request.app.state.agent_manager.create(
        name=payload.name,
        task=payload.task,
        agent_type=payload.agent_type,
        tools=payload.tools,
        user_id=payload.user_id,
    )
    return item


@router.get("/{agent_id}")
async def get_agent(agent_id: str, request: Request) -> dict[str, Any]:
    """Get one agent by id."""

    item = await request.app.state.agent_manager.get_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return item


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, request: Request) -> dict[str, Any]:
    """Stop and remove an agent."""

    await request.app.state.agent_manager.stop(agent_id)
    return {"success": True}
