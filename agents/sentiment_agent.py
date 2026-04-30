"""Sentiment agent for news and social signal analysis."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from loguru import logger
from pydantic import Field

from agents.base_agent import AgentStatus, AgentType, BaseAgent
from core.llm_client import ChatMessage


class SentimentAgent(BaseAgent):
    """Agent specialized in converting news and social text into sentiment summaries."""

    llm_client: Any
    current_task: str | None = None
    last_error: str | None = None
    specialization: str = "sentiment"
    preferred_tools: list[str] = Field(default_factory=lambda: ["get_news", "reddit_search"])

    async def run(self, task: str) -> dict[str, Any]:
        """Run a sentiment mission and return a scored narrative payload."""

        self.status = AgentStatus.RUNNING
        self.current_task = task
        self.last_error = None

        response = await self._chat_with_backoff(self._build_prompt(task))
        if response["success"]:
            self.status = AgentStatus.COMPLETED if self.agent_type == AgentType.EPHEMERAL else AgentStatus.IDLE
        else:
            self.status = AgentStatus.FAILED
            self.last_error = response["error"]

        return {
            "agent_id": self.agent_id,
            "specialization": self.specialization,
            "task": task,
            "success": response["success"],
            "message": response["message"],
            "error": response["error"],
        }

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
                "tools": sorted(set(payload["tools"]) | set(self.preferred_tools)),
            }
        )
        return payload

    def _build_prompt(self, task: str) -> str:
        """Build the sentiment instruction sent to the local model."""

        available_tools = sorted(set(self.tools) | set(self.preferred_tools))
        return (
            "You are Morgoth's sentiment agent. Analyze the mission text and any provided source material for "
            "news/social sentiment. Return: overall label, score from -1 to 1, key positive drivers, key negative "
            "drivers, uncertainty, and what fresh data Morgoth should fetch next.\n\n"
            f"Available tool names for Morgoth orchestration: {', '.join(available_tools)}\n"
            f"Mission: {task}"
        )

    async def _chat_with_backoff(self, prompt: str) -> dict[str, str | bool | None]:
        """Call Ollama with retry/backoff and return a non-throwing result."""

        for attempt in range(3):
            try:
                response = await self.llm_client.chat([ChatMessage(role="user", content=prompt)], model=self.model)
                return {"success": True, "message": response.message.content or "", "error": None}
            except httpx.TimeoutException as exc:
                logger.warning("Sentiment agent '{}' timed out on attempt {}", self.agent_id, attempt + 1)
                if attempt == 2:
                    return {
                        "success": False,
                        "message": "Sentiment analysis timed out before completion.",
                        "error": str(exc),
                    }
            except Exception as exc:
                logger.exception("Sentiment agent '{}' failed on attempt {}", self.agent_id, attempt + 1)
                if attempt == 2:
                    return {
                        "success": False,
                        "message": "Sentiment analysis failed before completion.",
                        "error": str(exc),
                    }
            await asyncio.sleep(2**attempt)

        return {"success": False, "message": "Sentiment analysis failed before completion.", "error": "retry_exhausted"}
