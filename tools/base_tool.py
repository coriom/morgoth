"""Base tool contract for all Morgoth tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field


class ToolExecutionResult(BaseModel):
    """Normalized result schema returned by every tool."""

    success: bool
    result: Any = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BaseTool(ABC):
    """Abstract base class for all tools exposed to the LLM."""

    name: str
    description: str
    parameters: dict[str, Any]

    @abstractmethod
    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Execute the tool and return the contract-defined result."""

    def to_ollama_schema(self) -> dict[str, Any]:
        """Return an OpenAI-compatible function calling schema for Ollama."""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def success(self, result: Any, **metadata: Any) -> dict[str, Any]:
        """Build a successful tool response payload."""

        payload = ToolExecutionResult(success=True, result=result, metadata=metadata)
        logger.debug("Tool '{}' succeeded", self.name)
        return payload.model_dump()

    def failure(self, error: str, **metadata: Any) -> dict[str, Any]:
        """Build a failed tool response payload."""

        payload = ToolExecutionResult(success=False, error=error, metadata=metadata)
        logger.warning("Tool '{}' failed: {}", self.name, error)
        return payload.model_dump()
