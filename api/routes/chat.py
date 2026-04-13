"""Chat API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel


router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    """Request body for chat messages."""

    content: str
    user_id: str = "default"


@router.post("")
async def post_chat(payload: ChatRequest, request: Request) -> dict[str, Any]:
    """Send a message to Morgoth and return the response."""

    brain = request.app.state.brain
    response = await brain.process_message(payload.content, payload.user_id)
    return response.model_dump()


@router.get("/history")
async def get_chat_history(request: Request, limit: int = 20) -> dict[str, Any]:
    """Return recent conversation history from episodic memory."""

    items = await request.app.state.episodic_memory.list_recent("conversations", limit=limit)
    return {"items": [item.model_dump() for item in items]}
