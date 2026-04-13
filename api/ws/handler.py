"""WebSocket connection manager."""

from __future__ import annotations

from typing import Any

from fastapi import WebSocket
from loguru import logger
from pydantic import BaseModel, Field


class OutboundWebSocketMessage(BaseModel):
    """Outbound WebSocket message contract."""

    type: str
    timestamp: str
    agent_id: str | None = None
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class InboundWebSocketMessage(BaseModel):
    """Inbound WebSocket message contract."""

    type: str
    content: str
    user_id: str


class WebSocketManager:
    """Manage active UI websocket connections."""

    def __init__(self) -> None:
        """Initialize the manager."""

        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        """Accept and register a websocket connection."""

        await websocket.accept()
        self._connections.append(websocket)
        logger.info("WebSocket connected; active={}", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        """Remove a websocket connection if present."""

        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("WebSocket disconnected; active={}", len(self._connections))

    async def broadcast(self, message: OutboundWebSocketMessage) -> None:
        """Broadcast a message to all connected clients."""

        stale: list[WebSocket] = []
        payload = message.model_dump()
        for websocket in self._connections:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        for websocket in stale:
            await self.disconnect(websocket)
