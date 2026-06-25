"""Tests for cross-source synthesis on objective completion.

Synthesis runs once when forced-completion fires, takes the distinct sources
used and per-objective findings, and emits a cross-source analysis stored as
a separate evidence entry. It must inherit the transient-retry resilience of
the rest of the cycle and must never block completion on failure.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain
from core.llm_client import ChatMessage, ChatResponse
from memory.episodic import EpisodicMetadata, QueryMatch


def _fake_response(content: str = "") -> ChatResponse:
    return ChatResponse(
        model="test",
        message=ChatMessage(role="assistant", content=content or None, tool_calls=[]),
        done=True,
    )


def _fake_match(content: str, objective_id: str = "obj-1") -> QueryMatch:
    return QueryMatch(
        document_id=str(uuid4()),
        content=content,
        metadata=EpisodicMetadata(
            timestamp="2026-06-21T18:00:00+00:00",
            agent_id="morgoth_autonomous",
            user_id="morgoth_autonomous",
            category="objective_action",
            objective_id=objective_id,
        ),
        distance=0.1,
    )


def _build_brain(llm_client: MagicMock) -> Brain:
    tool_router = MagicMock()
    tool_router.get_schemas.return_value = []
    tool_router.has_tool.return_value = True
    tool_router.list_names.return_value = []
    tool_router.execute_tool = AsyncMock(
        return_value={"success": True, "result": {}, "error": None, "metadata": {}}
    )

    config = MagicMock()
    config.log_level_thought = False
    config.max_cycles_per_objective = 5
    config.autonomous_cycle_minutes = 0

    episodic_memory = MagicMock()
    episodic_memory.add_text = AsyncMock(return_value="doc-1")
    episodic_memory.query = AsyncMock(return_value=[])

    persistent_memory = MagicMock()
    persistent_memory.insert_log = AsyncMock()
    persistent_memory.add_source_used = AsyncMock(return_value=[])
    persistent_memory.get_objectives = AsyncMock(return_value=[])
    persistent_memory.increment_cycle_count = AsyncMock(return_value=0)
    persistent_memory.get_sources_used = AsyncMock(return_value=[])
    persistent_memory.update_objective = AsyncMock(return_value={})

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


def _make_short_sleep():
    """Return an async sleep that runs the cycle body once then cancels."""

    sleep_calls = [0]

    async def short_sleep(*_args, **_kwargs):
        sleep_calls[0] += 1
        if sleep_calls[0] >= 2:
            raise asyncio.CancelledError()

    return short_sleep


@pytest.mark.asyncio
async def test_synthesis_entry_added_on_forced_completion() -> None:
    """T1: forced-completion with >=2 sources stores a synthesis evidence entry."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=_fake_response(
        content="Cross-source: news sentiment aligns with the price surge while reddit lags."
    ))
    brain = _build_brain(llm_client)
    obj_row = {
        "objective_id": "obj-1",
        "title": "BTC analysis",
        "description": "Investigate BTC surge",
        "status": "pending",
    }
    brain._persistent_memory.get_objectives = AsyncMock(return_value=[obj_row])
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=5)
    brain._persistent_memory.get_sources_used = AsyncMock(
        return_value=["web_search", "get_news", "get_crypto_price"]
    )
    brain._episodic_memory.query = AsyncMock(return_value=[
        _fake_match("BTC current price 64k"),
        _fake_match("News: positive macro signals"),
    ])

    with (
        patch("asyncio.sleep", new=_make_short_sleep()),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        await brain.run_autonomous_cycle()

    update_calls = brain._persistent_memory.update_objective.call_args_list
    summary = [c.kwargs for c in update_calls if c.kwargs.get("evidence", {}).get("auto_completed")]
    synth = [c.kwargs for c in update_calls if c.kwargs.get("evidence", {}).get("type") == "synthesis"]

    assert len(summary) == 1, "auto-completion summary entry must still be written"
    assert summary[0]["status"] == "done", "status=done must be set on the summary update"

    assert len(synth) == 1, "exactly one synthesis evidence entry expected"
    assert "Cross-source:" in synth[0]["evidence"]["content"]
    assert synth[0]["evidence"]["sources"] == ["get_crypto_price", "get_news", "web_search"]
    assert llm_client.chat.call_count == 1, "synthesis runs once per objective, not per cycle"


@pytest.mark.asyncio
async def test_synthesis_failure_does_not_block_completion() -> None:
    """T2: synthesis Ollama failure still completes the objective and records a fallback."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    brain = _build_brain(llm_client)
    obj_row = {
        "objective_id": "obj-1",
        "title": "X",
        "description": "Y",
        "status": "pending",
    }
    brain._persistent_memory.get_objectives = AsyncMock(return_value=[obj_row])
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=5)
    brain._persistent_memory.get_sources_used = AsyncMock(
        return_value=["web_search", "get_news"]
    )
    brain._episodic_memory.query = AsyncMock(return_value=[
        _fake_match("finding A"),
    ])

    with (
        patch("asyncio.sleep", new=_make_short_sleep()),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        await brain.run_autonomous_cycle()

    update_calls = brain._persistent_memory.update_objective.call_args_list
    summary = [c.kwargs for c in update_calls if c.kwargs.get("evidence", {}).get("auto_completed")]
    synth = [c.kwargs for c in update_calls if c.kwargs.get("evidence", {}).get("type") == "synthesis"]

    assert len(summary) == 1, "completion must still happen when synthesis fails"
    assert summary[0]["status"] == "done"
    assert len(synth) == 1, "a fallback synthesis entry must still be recorded"
    assert "synthesis failed" in synth[0]["evidence"]["content"]


@pytest.mark.asyncio
async def test_synthesis_skipped_when_under_two_sources() -> None:
    """T3: with <2 distinct sources, synthesis is skipped and no entry is added."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=_fake_response(content="should not be called"))
    brain = _build_brain(llm_client)

    result = await brain._synthesize_objective(
        obj={"objective_id": "obj-1", "title": "X", "description": "Y"},
        sources_used=["web_search"],
        findings=["some finding"],
    )

    assert result is None, "single-source objectives skip synthesis"
    llm_client.chat.assert_not_called()


@pytest.mark.asyncio
async def test_synthesis_inherits_transient_retry() -> None:
    """T4: a transient timeout in the synthesis chat is retried once, not fatal."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        httpx.ReadTimeout("first timeout"),
        _fake_response(content="Synthesis OK after retry"),
    ])
    brain = _build_brain(llm_client)

    result = await brain._synthesize_objective(
        obj={"objective_id": "obj-1", "title": "X", "description": "Y"},
        sources_used=["web_search", "get_news"],
        findings=["finding A", "finding B"],
    )

    assert llm_client.chat.call_count == 2, (
        "synthesis must inherit _chat_with_transient_retry: first timeout, then success"
    )
    assert result == "Synthesis OK after retry"
