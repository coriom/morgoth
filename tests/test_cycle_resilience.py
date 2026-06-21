"""Tests for cycle-level resilience against transient Ollama failures.

A per-cycle Ollama timeout used to propagate out of process_message and burn
a MAX_CYCLES slot (increment_cycle_count runs before the chat). The fix is a
bounded single retry inside the same cycle for transient httpx errors.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain
from core.llm_client import ChatMessage, ChatResponse, OllamaFunction, OllamaToolCall


def _fake_response(tool_name: str | None = None, content: str = "") -> ChatResponse:
    tool_calls = []
    if tool_name:
        tool_calls = [
            OllamaToolCall(
                id="call-1",
                function=OllamaFunction(
                    name=tool_name,
                    arguments={"objective_id": "obj-123", "status": "done"},
                ),
            )
        ]
    return ChatResponse(
        model="test",
        message=ChatMessage(
            role="assistant", content=content or None, tool_calls=tool_calls
        ),
        done=True,
    )


def _build_brain(llm_client: MagicMock) -> Brain:
    tool_router = MagicMock()
    tool_router.get_schemas.return_value = []
    tool_router.has_tool.return_value = True
    tool_router.list_names.return_value = ["update_objective"]
    tool_router.execute_tool = AsyncMock(
        return_value={"success": True, "result": {}, "error": None, "metadata": {}}
    )

    config = MagicMock()
    config.log_level_thought = False

    episodic_memory = MagicMock()
    episodic_memory.add_text = AsyncMock(return_value="doc-1")

    persistent_memory = MagicMock()
    persistent_memory.insert_log = AsyncMock()
    persistent_memory.add_source_used = AsyncMock(return_value=[])

    return Brain(
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


@pytest.mark.asyncio
async def test_transient_ollama_timeout_is_caught_and_retried() -> None:
    """T1: a single transient httpx.ReadTimeout must be retried and the cycle completes."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        httpx.ReadTimeout("timed out"),
        _fake_response(content="Recovered after retry."),
    ])

    brain = _build_brain(llm_client)
    brain._current_objective_id = "obj-123"

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        result = await brain.process_message("work on the objective", user_id="test")

    assert llm_client.chat.call_count == 2, (
        "expected one retry after the transient timeout; "
        f"got {llm_client.chat.call_count} chat calls"
    )
    assert result is not None
    assert "Recovered after retry." in (result.message or "")


@pytest.mark.asyncio
async def test_transient_retry_is_bounded_to_exactly_one_attempt() -> None:
    """T2: two consecutive transient errors -> chat called exactly twice, then the error surfaces.

    Budget protection asserted by call_count == 2: one initial attempt plus
    exactly one retry. Without the retry guard, a single timeout would have
    burned the cycle; with an unbounded retry, the loop could hang forever.
    """

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        httpx.ReadTimeout("first timeout"),
        httpx.ReadTimeout("second timeout"),
    ])

    brain = _build_brain(llm_client)
    brain._current_objective_id = "obj-123"

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
        pytest.raises(httpx.ReadTimeout),
    ):
        await brain.process_message("work on the objective", user_id="test")

    assert llm_client.chat.call_count == 2, (
        "expected exactly one retry (total 2 chat calls); "
        f"got {llm_client.chat.call_count}"
    )


@pytest.mark.asyncio
async def test_non_transient_error_is_not_retried_and_surfaces() -> None:
    """T3: a genuine programmer error (ValueError) must NOT be retried or swallowed.

    Resilience must never mask real bugs. Only httpx transient errors are
    retried; anything else surfaces on the first attempt.
    """

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=ValueError("real bug, not transient"))

    brain = _build_brain(llm_client)
    brain._current_objective_id = "obj-123"

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
        pytest.raises(ValueError, match="real bug, not transient"),
    ):
        await brain.process_message("work on the objective", user_id="test")

    assert llm_client.chat.call_count == 1, (
        "non-transient errors must not be retried; "
        f"got {llm_client.chat.call_count} chat calls"
    )
