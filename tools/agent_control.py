"""Agent management tools."""

from __future__ import annotations

from typing import Any, Protocol

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


class AgentCreator(Protocol):
    """Protocol describing the subset of agent manager functionality needed by the tool."""

    async def create(self, name: str, task: str, agent_type: str, tools: list[str], user_id: str) -> dict[str, Any]:
        """Create a new agent and optionally start it."""


class CreateAgentTool(BaseTool):
    """Create a new agent through the agent manager."""

    name = "create_agent"
    description = "Create a new Morgoth agent for a subtask."
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "task": {"type": "string"},
            "agent_type": {"type": "string", "enum": ["ephemeral", "persistent"], "default": "ephemeral"},
            "tools": {"type": "array", "items": {"type": "string"}},
            "user_id": {"type": "string", "default": "default"},
        },
        "required": ["name", "task"],
    }

    def __init__(self, config: AppConfig, agent_manager: AgentCreator) -> None:
        """Initialize the tool with config and agent manager."""

        self._config = config
        self._agent_manager = agent_manager

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Create a new agent if the permission model allows it."""

        agent_type = str(kwargs.get("agent_type", "ephemeral"))
        if agent_type == "persistent" and not self._config.permissions.permissions.can_create_persistent_agents:
            raise PermissionDeniedError("Persistent agent creation is disabled by permissions")
        if agent_type == "ephemeral" and not self._config.permissions.permissions.can_create_ephemeral_agents:
            raise PermissionDeniedError("Ephemeral agent creation is disabled by permissions")

        result = await self._agent_manager.create(
            name=str(kwargs["name"]),
            task=str(kwargs["task"]),
            agent_type=agent_type,
            tools=list(kwargs.get("tools", [])),
            user_id=str(kwargs.get("user_id", "default")),
        )
        return self.success(result, agent_type=agent_type)
