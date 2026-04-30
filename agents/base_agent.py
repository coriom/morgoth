"""Base agent contract for Morgoth."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class AgentType(Enum):
    """Supported agent lifecycles."""

    EPHEMERAL = "ephemeral"
    PERSISTENT = "persistent"


class AgentStatus(Enum):
    """Supported agent runtime states."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class BaseAgent(BaseModel, ABC):
    """Abstract base agent contract."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    agent_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    agent_type: AgentType
    status: AgentStatus = AgentStatus.IDLE
    model: str
    tools: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    user_id: str = "default"

    @abstractmethod
    async def run(self, task: str) -> dict[str, Any]:
        """Run the agent on a task."""

    @abstractmethod
    async def pause(self) -> None:
        """Pause the agent if possible."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the agent."""

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation for API and logging."""

        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "agent_type": self.agent_type.value,
            "status": self.status.value,
            "model": self.model,
            "tools": list(self.tools),
            "created_at": self.created_at.isoformat(),
            "user_id": self.user_id,
        }
