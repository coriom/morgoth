"""Agent lifecycle manager for Morgoth."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from agents.base_agent import AgentStatus, AgentType, BaseAgent
from agents.macro_agent import MacroAgent
from agents.research_agent import ResearchAgent
from agents.sentiment_agent import SentimentAgent
from core.config import AppConfig
from core.llm_client import ChatMessage, OllamaLLMClient
from memory.persistent import PersistentMemory


SPECIALIST_AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "research": ResearchAgent,
    "sentiment": SentimentAgent,
    "macro": MacroAgent,
}


class ManagedAgent(BaseAgent):
    """Concrete agent managed by the phase 1 runtime."""

    llm_client: Any
    current_task: str | None = None
    last_error: str | None = None
    specialization: str = "general"

    async def run(self, task: str) -> dict[str, Any]:
        """Run a single LLM call for the assigned task."""

        self.status = AgentStatus.RUNNING
        self.current_task = task
        self.last_error = None
        try:
            response = await self.llm_client.chat(
                [ChatMessage(role="user", content=task)],
                model=self.model,
            )
            self.status = AgentStatus.COMPLETED if self.agent_type == AgentType.EPHEMERAL else AgentStatus.IDLE
            return {
                "agent_id": self.agent_id,
                "specialization": self.specialization,
                "message": response.message.content,
                "tool_calls": [item.model_dump() for item in response.message.tool_calls],
            }
        except Exception as exc:
            self.status = AgentStatus.FAILED
            self.last_error = str(exc)
            logger.exception("General agent '{}' failed", self.agent_id)
            raise

    async def pause(self) -> None:
        """Pause the agent."""

        self.status = AgentStatus.PAUSED

    async def stop(self) -> None:
        """Stop the agent."""

        self.status = AgentStatus.COMPLETED
        self.current_task = None

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable representation for API and UI consumers."""

        payload = super().to_dict()
        payload.update(
            {
                "current_task": self.current_task,
                "last_error": self.last_error,
                "specialization": self.specialization,
            }
        )
        return payload


class AgentManager:
    """Lifecycle manager for all active agents."""

    def __init__(self, config: AppConfig, llm_client: OllamaLLMClient, persistent_memory: PersistentMemory) -> None:
        """Initialize the manager with runtime dependencies."""

        self._config = config
        self._llm_client = llm_client
        self._persistent_memory = persistent_memory
        self._agents: dict[str, BaseAgent] = {}
        self._tasks: dict[str, asyncio.Task[dict[str, Any]]] = {}

    async def create(
        self,
        name: str,
        task: str,
        agent_type: str = "ephemeral",
        model: str | None = None,
        tools: list[str] | None = None,
        user_id: str = "default",
    ) -> dict[str, Any]:
        """Create an agent and start its first task."""

        if len(self._agents) >= self._config.max_concurrent_agents:
            raise RuntimeError("Maximum concurrent agents reached")

        enum_type = AgentType(agent_type)
        agent_class = self._select_agent_class(name=name, task=task, tools=tools or [])
        agent = agent_class(
            name=name,
            agent_type=enum_type,
            model=model or self._config.choose_model_for_task("agent_subtask"),
            tools=tools or [],
            user_id=user_id,
            llm_client=self._llm_client,
        )
        if hasattr(agent, "current_task"):
            agent.current_task = task
        self._agents[agent.agent_id] = agent
        await self._persistent_memory.save_agent(agent.to_dict() | {"stopped_at": None})
        self._tasks[agent.agent_id] = asyncio.create_task(self._run_agent(agent, task))
        logger.info("Created agent '{}' ({})", agent.name, agent.agent_id)
        return agent.to_dict()

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return all active agents as dictionaries."""

        return [agent.to_dict() for agent in self._agents.values()]

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        """Return one agent by id if it exists."""

        agent = self._agents.get(agent_id)
        return agent.to_dict() if agent else None

    async def pause(self, agent_id: str) -> bool:
        """Pause an existing agent."""

        agent = self._require_agent(agent_id)
        await agent.pause()
        await self._persistent_memory.save_agent(agent.to_dict() | {"stopped_at": None})
        return True

    async def stop(self, agent_id: str) -> bool:
        """Stop and remove an agent."""

        agent = self._require_agent(agent_id)
        await agent.stop()
        stopped_at = datetime.now(timezone.utc)
        await self._persistent_memory.save_agent(agent.to_dict() | {"stopped_at": stopped_at})
        task = self._tasks.pop(agent_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._agents.pop(agent_id, None)
        logger.info("Stopped agent '{}'", agent_id)
        return True

    async def _run_agent(self, agent: BaseAgent, task: str) -> dict[str, Any]:
        """Run an agent task and persist the final state."""

        try:
            result = await agent.run(task)
            await self._persistent_memory.save_agent(agent.to_dict() | {"stopped_at": None})
            if agent.agent_type == AgentType.EPHEMERAL:
                self._agents.pop(agent.agent_id, None)
                self._tasks.pop(agent.agent_id, None)
            return result
        except Exception:
            agent.status = AgentStatus.FAILED
            await self._persistent_memory.save_agent(agent.to_dict() | {"stopped_at": None})
            logger.exception("Agent '{}' failed", agent.agent_id)
            raise

    def _select_agent_class(self, *, name: str, task: str, tools: list[str]) -> type[BaseAgent]:
        """Return the registered agent implementation for the requested mission."""

        explicit = " ".join([name, *tools]).lower()
        if "research" in explicit or "synthesis" in explicit:
            return SPECIALIST_AGENT_REGISTRY["research"]
        if "sentiment" in explicit or "reddit" in explicit or "news" in explicit:
            return SPECIALIST_AGENT_REGISTRY["sentiment"]
        if "macro" in explicit or "fred" in explicit or "economic" in explicit:
            return SPECIALIST_AGENT_REGISTRY["macro"]

        haystack = " ".join([name, task, *tools]).lower()
        if "sentiment" in haystack or "reddit" in haystack or "news" in haystack:
            return SPECIALIST_AGENT_REGISTRY["sentiment"]
        if "macro" in haystack or "fred" in haystack or "economic" in haystack:
            return SPECIALIST_AGENT_REGISTRY["macro"]
        if "research" in haystack or "synthesis" in haystack:
            return SPECIALIST_AGENT_REGISTRY["research"]
        return ManagedAgent

    def _require_agent(self, agent_id: str) -> BaseAgent:
        """Return an existing agent or raise an error."""

        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        return agent
