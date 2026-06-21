"""Tests for the terminal-turn corrective retry in process_message.

When the model ends a turn by narrating a tool intent (e.g. "I will now call
update_objective") without emitting a tool_call, the agentic loop should inject
a single corrective system message and grant one additional round. If the model
still produces no tool_call, the loop exits cleanly and MAX_CYCLES remains the
backstop.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain, _looks_like_unemitted_tool_intent
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
async def test_narrated_tool_intent_triggers_corrective_retry() -> None:
    """Text announcing update_objective with no tool_calls must trigger one corrective retry."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        # round 1: model narrates intent without emitting a tool_call
        _fake_response(content="I will now call update_objective to complete this objective."),
        # round 2 (corrective retry): model emits the actual tool_call
        _fake_response(tool_name="update_objective"),
        # round 3: terminal text-only response, no tool_calls → exit
        _fake_response(content="Done."),
    ])

    brain = _build_brain(llm_client)
    brain._current_objective_id = "obj-123"

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        result = await brain.process_message("work on the objective", user_id="test")

    assert llm_client.chat.call_count == 3, (
        "expected three LLM calls: initial, corrective retry, post-tool"
    )

    second_call_messages = llm_client.chat.call_args_list[1][0][0]
    correctives = [
        m for m in second_call_messages
        if m.role == "system" and "did not emit a tool call" in (m.content or "")
    ]
    assert len(correctives) == 1, "expected exactly one corrective system message"

    brain._tool_router.execute_tool.assert_awaited_once_with(
        "update_objective", {"objective_id": "obj-123", "status": "done"}
    )
    assert result is not None


@pytest.mark.asyncio
async def test_pure_analytical_answer_does_not_trigger_corrective() -> None:
    """A purely analytical terminal answer with no tool-intent vocabulary must exit normally."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        _fake_response(content="The current price of Bitcoin is $64,000."),
    ])

    brain = _build_brain(llm_client)
    brain._current_objective_id = "obj-123"

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        result = await brain.process_message("what is the price?", user_id="test")

    assert llm_client.chat.call_count == 1, (
        "no corrective should fire on a purely analytical answer; "
        f"got {llm_client.chat.call_count} LLM calls"
    )
    brain._tool_router.execute_tool.assert_not_called()
    assert result is not None


@pytest.mark.asyncio
async def test_corrective_retry_exits_cleanly_when_model_still_narrates() -> None:
    """If the model still emits no tool_call on the retry, the loop must exit without exception."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        _fake_response(content="I will now call update_objective to complete this objective."),
        _fake_response(content="I will now call update_objective for sure this time."),
    ])

    brain = _build_brain(llm_client)
    brain._current_objective_id = "obj-123"

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        result = await brain.process_message("work on the objective", user_id="test")

    assert llm_client.chat.call_count == 2, (
        "exactly one corrective retry — no infinite loop on repeated narration"
    )
    brain._tool_router.execute_tool.assert_not_called()
    assert result is not None
    assert "update_objective" in (result.message or "")


def test_detector_matches_update_objective_narration() -> None:
    assert _looks_like_unemitted_tool_intent(
        "I will now call update_objective to complete this objective."
    )


def test_detector_matches_call_phrase_with_known_tool() -> None:
    assert _looks_like_unemitted_tool_intent(
        "Let me call get_crypto_price next."
    )


def test_detector_rejects_pure_analytical_answer() -> None:
    assert not _looks_like_unemitted_tool_intent(
        "The current price of Bitcoin is $64,000."
    )


def test_detector_rejects_empty_and_none() -> None:
    assert not _looks_like_unemitted_tool_intent(None)
    assert not _looks_like_unemitted_tool_intent("")
