"""Focused regression tests for agent serialization and Ollama payload handling."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agents.base_agent import AgentStatus, AgentType, BaseAgent
from core.brain import Brain
from core.llm_client import ChatMessage, ChatResponse
from tools.agent_control import CreateAgentTool
from tools.code_executor import ExecutePythonTool


class SerializableTestAgent(BaseAgent):
    """Concrete test agent with a non-serializable runtime dependency."""

    llm_client: object

    async def run(self, task: str) -> dict[str, Any]:
        """Satisfy the abstract interface for tests."""

        return {"task": task}

    async def pause(self) -> None:
        """Satisfy the abstract interface for tests."""

    async def stop(self) -> None:
        """Satisfy the abstract interface for tests."""


class RecordingLLMClient:
    """LLM double that fails once with tools, then succeeds."""

    def __init__(self) -> None:
        """Initialize captured chat calls."""

        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        options: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Record calls and simulate an Ollama 400 for the first tool-enabled request."""

        self.calls.append(
            {
                "messages": [message.to_ollama_dict() for message in messages],
                "tools": tools,
            }
        )
        if tools:
            request = httpx.Request("POST", "http://localhost:11434/api/chat")
            response = httpx.Response(400, request=request, text='{"error":"bad request"}')
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

        return ChatResponse(
            model="test-model",
            message=ChatMessage(role="assistant", content="fallback response"),
            done=True,
        )


class DummyPersistentMemory:
    """Minimal persistent memory double for Brain tests."""

    def __init__(self) -> None:
        """Initialize captured log writes."""

        self.logs: list[dict[str, Any]] = []

    async def insert_log(self, payload: dict[str, Any]) -> None:
        """Capture persisted logs."""

        self.logs.append(payload)


class DummyEpisodicMemory:
    """Minimal episodic memory double for Brain tests."""

    async def add_text(
        self,
        collection_name: str,
        content: str,
        *,
        category: str,
        agent_id: str,
        user_id: str = "default",
    ) -> str:
        """Pretend the memory write succeeded."""

        return "doc-123"


class DummyScheduler:
    """Placeholder scheduler for Brain tests."""


class DummyNotifier:
    """Placeholder notifier for Brain tests."""


class DummyToolRouter:
    """Tool router double with one intentionally noisy schema."""

    def get_schemas(self) -> list[dict[str, Any]]:
        """Return a schema with fields that should already be sanitized."""

        return [
            CreateAgentTool.parameters,  # type: ignore[attr-defined]
        ]

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """No-op execution path for this test."""

        return {"success": True, "result": {"name": name, "arguments": arguments}, "error": None, "metadata": {}}

    async def close(self) -> None:
        """Satisfy the Brain shutdown interface."""


@pytest.mark.asyncio
async def test_agent_to_dict_excludes_runtime_dependencies() -> None:
    """Agent serialization should not leak runtime-only objects into API payloads."""

    agent = SerializableTestAgent(
        name="tester",
        agent_type=AgentType.EPHEMERAL,
        status=AgentStatus.IDLE,
        model="llama3.1:8b",
        tools=["read_file"],
        user_id="default",
        llm_client=object(),
    )

    payload = agent.to_dict()

    assert payload["name"] == "tester"
    assert payload["agent_type"] == "ephemeral"
    assert "llm_client" not in payload


def test_chat_message_to_ollama_dict_omits_empty_runtime_fields() -> None:
    """User messages sent to Ollama should stay in the minimal accepted shape."""

    message = ChatMessage(role="user", content="hello")

    assert message.to_ollama_dict() == {"role": "user", "content": "hello"}


def test_tool_schema_is_sanitized_for_ollama(app_config) -> None:
    """Tool schemas should not include unsupported JSON Schema keywords."""

    schema = ExecutePythonTool(app_config).to_ollama_schema()
    parameters = schema["function"]["parameters"]
    serialized = str(parameters)

    assert "default" not in serialized
    assert "minimum" not in serialized
    assert "maximum" not in serialized


@pytest.mark.asyncio
async def test_brain_retries_without_tools_after_ollama_400(app_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Brain chat should fall back to a plain message payload if Ollama rejects tools."""

    llm_client = RecordingLLMClient()
    brain = Brain(
        app_config,
        llm_client,  # type: ignore[arg-type]
        DummyPersistentMemory(),  # type: ignore[arg-type]
        DummyEpisodicMemory(),  # type: ignore[arg-type]
        DummyScheduler(),  # type: ignore[arg-type]
        DummyToolRouter(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        DummyNotifier(),  # type: ignore[arg-type]
    )

    async def _noop_write_log_file(entry: Any) -> None:
        return None

    monkeypatch.setattr(brain, "_write_log_file", _noop_write_log_file)

    response = await brain.process_message("hello")

    assert response.message == "fallback response"
    assert len(llm_client.calls) == 2
    assert llm_client.calls[0]["tools"] is not None
    assert llm_client.calls[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert llm_client.calls[1]["tools"] is None
