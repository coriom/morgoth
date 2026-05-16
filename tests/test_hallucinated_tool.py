"""Test that hallucinated (unregistered) tool calls are rejected gracefully."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain
from core.llm_client import ChatMessage, ChatResponse, OllamaFunction, OllamaToolCall


def _fake_response(tool_name: str | None = None, content: str = "") -> ChatResponse:
    tool_calls = []
    if tool_name:
        tool_calls = [OllamaToolCall(id="call-1", function=OllamaFunction(name=tool_name, arguments={"url": "https://example.com"}))]
    return ChatResponse(
        model="test",
        message=ChatMessage(role="assistant", content=content or None, tool_calls=tool_calls),
        done=True,
    )


@pytest.mark.asyncio
async def test_hallucinated_tool_rejected_and_corrected() -> None:
    """Unregistered tool: not executed, correction injected, no exception."""

    tool_router = MagicMock()
    tool_router.get_schemas.return_value = []
    tool_router.has_tool.return_value = False          # "get_api_data" not registered
    tool_router.list_names.return_value = ["web_search", "update_objective"]
    tool_router.execute_tool = AsyncMock()             # must NOT be called

    config = MagicMock()
    config.log_level_thought = False

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        _fake_response(tool_name="get_api_data"),      # round 1: hallucinated call
        _fake_response(content="I'll use a real tool"),  # round 2: no tool calls → exit loop
    ])

    episodic_memory = MagicMock()
    episodic_memory.add_text = AsyncMock(return_value="doc-1")

    persistent_memory = MagicMock()
    persistent_memory.insert_log = AsyncMock()

    brain = Brain(
        config=config,
        llm_client=llm_client,
        persistent_memory=persistent_memory,
        episodic_memory=episodic_memory,
        scheduler=MagicMock(),
        tool_router=tool_router,
        agent_manager=MagicMock(),
        notifier=MagicMock(),
        websocket_manager=None,
    )

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        result = await brain.process_message("research bitcoin on-chain", user_id="test")

    # (a) execute_tool was never called
    tool_router.execute_tool.assert_not_called()

    # (b) second LLM call received a corrective tool-role message containing "does not exist"
    second_call_messages = llm_client.chat.call_args_list[1][0][0]
    corrections = [
        m for m in second_call_messages
        if m.role == "tool" and "does not exist" in (m.content or "")
    ]
    assert len(corrections) == 1, "expected exactly one correction message"
    assert "get_api_data" in corrections[0].content

    # (c) no exception raised (implicit — we reached this line)
    assert result is not None
