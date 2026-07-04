"""Tests for the MIN_DISTINCT_SOURCES / MAX_CYCLES_PER_OBJECTIVE budget.

The per-cycle prompt must derive the source minimum from the named constant
MIN_DISTINCT_SOURCES (not a literal 3), and the rail must be arithmetically
satisfiable: MIN_DISTINCT_SOURCES < MAX_CYCLES_PER_OBJECTIVE so the model has
at least one slack cycle to call update_objective after reaching the minimum.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import brain as brain_module
from core.brain import MIN_DISTINCT_SOURCES, Brain
from core.config import AppConfig


pytestmark = pytest.mark.asyncio


class _AsyncCtxManager:
    """Minimal async context manager wrapping a mock connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _build_brain(app_config: AppConfig) -> tuple[Brain, MagicMock, MagicMock]:
    """Construct a Brain with mocked dependencies; return (brain, llm_client, persistent_memory)."""

    tool_router = MagicMock()
    tool_router.get_schemas.return_value = []
    tool_router.has_tool.return_value = True
    tool_router.list_names.return_value = []
    tool_router.execute_tool = AsyncMock(
        return_value={"success": True, "result": {}, "error": None, "metadata": {}}
    )

    llm_client = MagicMock()
    persistent_memory = MagicMock()
    persistent_memory.insert_log = AsyncMock()

    episodic_memory = MagicMock()
    episodic_memory.add_text = AsyncMock(return_value="doc-1")
    episodic_memory.query = AsyncMock(return_value=[])

    brain = Brain(
        config=app_config,
        llm_client=llm_client,
        persistent_memory=persistent_memory,
        episodic_memory=episodic_memory,
        scheduler=MagicMock(),
        tool_router=tool_router,
        agent_manager=MagicMock(),
        notifier=MagicMock(),
        websocket_manager=None,
    )
    return brain, llm_client, persistent_memory


async def _build_prompt(
    brain: Brain,
    sources_used: list[str],
) -> str:
    """Drive run_autonomous_cycle exactly far enough to capture the prompt sent to the LLM.

    The cycle queues one pending objective, returns the given sources_used from
    persistent memory, runs process_message once, and stops. We intercept the
    LLM chat call to capture the prompt content without continuing execution.
    """

    objective_id = "11111111-2222-3333-4444-555555555555"
    objective_row = {
        "objective_id": objective_id,
        "title": "Test objective",
        "description": "investigate",
        "status": "pending",
    }

    pm = brain._persistent_memory
    pm.get_objectives = AsyncMock(return_value=[objective_row])
    pm.increment_cycle_count = AsyncMock(return_value=1)
    pm.get_sources_used = AsyncMock(return_value=sources_used)
    pm.update_objective = AsyncMock(return_value={})

    captured: dict[str, Any] = {}

    async def _capture_and_raise(*args, **kwargs):
        # First positional arg is the messages list
        captured["messages"] = args[0] if args else kwargs.get("messages")
        # Raise to break out of the cycle's try-block cleanly
        raise RuntimeError("captured")

    brain._llm_client.chat = AsyncMock(side_effect=_capture_and_raise)

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
        patch("core.brain.asyncio.sleep", new=AsyncMock()),
    ):
        # Cancel the loop after one iteration by making sleep raise CancelledError on second call
        import asyncio

        call_count = {"n": 0}

        async def _sleep_then_cancel(_seconds):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise asyncio.CancelledError()

        with patch("core.brain.asyncio.sleep", new=_sleep_then_cancel):
            await brain.run_autonomous_cycle()

    assert "messages" in captured, "LLM chat was never called"
    user_msgs = [m for m in captured["messages"] if m.role == "user"]
    assert user_msgs, "no user message captured"
    return user_msgs[-1].content


async def test_prompt_renders_min_distinct_sources_constant(app_config) -> None:
    """Prompt must show count/MIN_DISTINCT_SOURCES, not a hardcoded literal."""

    prompt = await _build_prompt(_build_brain(app_config)[0], sources_used=[])

    expected_marker = f"(0/{MIN_DISTINCT_SOURCES} minimum)"
    assert expected_marker in prompt, (
        f"prompt must contain {expected_marker!r}; got: {prompt!r}"
    )


async def test_prompt_demands_different_source_when_below_minimum(app_config) -> None:
    """When count < MIN_DISTINCT_SOURCES the prompt must enforce gathering a new source."""

    sources_below = ["get_crypto_price"]
    assert len(set(sources_below)) < MIN_DISTINCT_SOURCES

    prompt = await _build_prompt(_build_brain(app_config)[0], sources_used=sources_below)

    assert "MUST gather from a DIFFERENT source not yet used" in prompt
    assert "Minimum sources met" not in prompt


async def test_prompt_switches_to_minimum_met_when_threshold_reached(app_config) -> None:
    """At or above MIN_DISTINCT_SOURCES the prompt switches to the completion-friendly wording."""

    sources_full = ["get_crypto_price", "web_search", "get_news"]
    assert len(set(sources_full)) >= MIN_DISTINCT_SOURCES

    prompt = await _build_prompt(_build_brain(app_config)[0], sources_used=sources_full)

    assert "Minimum sources met" in prompt
    assert "MUST gather from a DIFFERENT source" not in prompt


async def test_rail_is_satisfiable_under_default_config() -> None:
    """MIN_DISTINCT_SOURCES must be strictly less than the default MAX_CYCLES_PER_OBJECTIVE."""

    from core.config import AppConfig as _AppConfig

    default_max = _AppConfig.model_fields["max_cycles_per_objective"].default
    assert MIN_DISTINCT_SOURCES < default_max, (
        f"rail is unsatisfiable: MIN_DISTINCT_SOURCES={MIN_DISTINCT_SOURCES} "
        f"vs default MAX_CYCLES_PER_OBJECTIVE={default_max}"
    )
