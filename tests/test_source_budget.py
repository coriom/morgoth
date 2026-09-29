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


async def _build_prompt(
    monkeypatch, sources_used: list[str],
) -> str:
    """Drive run_autonomous_cycle exactly far enough to capture the
    prompt sent to the LLM.

    Uses the shared conftest factory so the cycle is silenced and
    the loop exits after ONE iteration via claim_then_cancel."""
    import asyncio
    from tests.conftest import build_test_brain, claim_then_cancel

    objective_id = "11111111-2222-3333-4444-555555555555"
    objective_row = {
        "objective_id": objective_id,
        "title": "Test objective",
        "description": "investigate",
        "status": "pending",
    }

    captured: dict[str, Any] = {}

    async def _capture_and_raise(*args, **kwargs):
        # First positional arg is the messages list.
        captured["messages"] = args[0] if args else kwargs.get("messages")
        # Raise so the cycle body's try/except catches it and falls
        # through to the sleep — the second claim call will then raise
        # CancelledError to end the loop.
        raise RuntimeError("captured")

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=_capture_and_raise)
    brain = build_test_brain(llm_client, monkeypatch)
    brain._persistent_memory.get_objectives = AsyncMock(return_value=[objective_row])
    brain._persistent_memory.claim_next_objective = AsyncMock(
        side_effect=claim_then_cancel(objective_row)
    )
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=1)
    brain._persistent_memory.get_sources_used = AsyncMock(return_value=sources_used)

    with (
        patch.object(brain, "_recall_relevant_context", new=AsyncMock(return_value=None)),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()

    assert "messages" in captured, "LLM chat was never called"
    user_msgs = [m for m in captured["messages"] if m.role == "user"]
    assert user_msgs, "no user message captured"
    return user_msgs[-1].content


async def test_prompt_renders_min_distinct_sources_constant(monkeypatch) -> None:
    """Prompt must show count/MIN_DISTINCT_SOURCES, not a hardcoded literal."""

    prompt = await _build_prompt(monkeypatch, sources_used=[])

    expected_marker = f"(0/{MIN_DISTINCT_SOURCES} minimum)"
    assert expected_marker in prompt, (
        f"prompt must contain {expected_marker!r}; got: {prompt!r}"
    )


async def test_prompt_demands_different_source_when_below_minimum(monkeypatch) -> None:
    """When count < MIN_DISTINCT_SOURCES the prompt must enforce gathering a new source."""

    sources_below = ["get_crypto_price"]
    assert len(set(sources_below)) < MIN_DISTINCT_SOURCES

    prompt = await _build_prompt(monkeypatch, sources_used=sources_below)

    assert "MUST gather from a DIFFERENT source not yet used" in prompt
    assert "Minimum sources met" not in prompt


async def test_prompt_switches_to_minimum_met_when_threshold_reached(monkeypatch) -> None:
    """At or above MIN_DISTINCT_SOURCES the prompt switches to the completion-friendly wording."""

    sources_full = ["get_crypto_price", "web_search", "get_news"]
    assert len(set(sources_full)) >= MIN_DISTINCT_SOURCES

    prompt = await _build_prompt(monkeypatch, sources_used=sources_full)

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
