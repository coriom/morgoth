"""Tests for the per-cycle finding formatter.

A cycle that successfully calls a data tool but produces no narrative text
(routinely the case because the autonomous prompt instructs "Tool call only.
No explanation.") must still persist a finding that contains the fetched
payload. Otherwise downstream synthesis sees "(no output)" and fabricates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain, BrainResponse


def _build_brain() -> Brain:
    tool_router = MagicMock()
    tool_router.get_schemas.return_value = []
    tool_router.execute_tool = AsyncMock()
    config = MagicMock()
    config.log_level_thought = False
    persistent_memory = MagicMock()
    persistent_memory.insert_log = AsyncMock()
    episodic_memory = MagicMock()
    episodic_memory.add_text = AsyncMock(return_value="doc-1")
    return Brain(
        config=config,
        llm_client=MagicMock(),
        persistent_memory=persistent_memory,
        episodic_memory=episodic_memory,
        scheduler=MagicMock(),
        tool_router=tool_router,
        agent_manager=MagicMock(),
        notifier=MagicMock(),
        websocket_manager=None,
    )


def test_successful_tool_with_empty_narrative_yields_non_empty_finding() -> None:
    """Regression: the data the cycle just fetched must reach episodic memory.

    Before the fix the cycle wrote '(no output)' here because only
    result.message was persisted; the fetched price payload was discarded.
    """

    brain = _build_brain()
    response = BrainResponse(
        message="",
        tool_results=[{
            "tool": "get_crypto_price",
            "result": {
                "success": True,
                "result": {"symbol": "BITCOIN", "price": 64100.0, "change_24h": 0.5},
                "error": None,
                "metadata": {"source": "coingecko"},
            },
        }],
        model="test",
    )

    finding = brain._format_cycle_finding(response)

    assert finding != "(no output)"
    assert "get_crypto_price" in finding
    assert "64100" in finding


def test_both_empty_falls_back_to_no_output_marker() -> None:
    """If neither narrative nor tools produced anything, keep the existing marker."""

    brain = _build_brain()
    response = BrainResponse(message="", tool_results=[], model="test")

    assert brain._format_cycle_finding(response) == "(no output)"


def test_failed_tool_call_records_failure_with_error() -> None:
    """A failed data tool must still be visible in the finding (not silently dropped)."""

    brain = _build_brain()
    response = BrainResponse(
        message="",
        tool_results=[{
            "tool": "fred_series_observations",
            "result": {
                "success": False,
                "result": None,
                "error": "FRED_API_KEY is required for FRED API access",
                "metadata": {},
            },
        }],
        model="test",
    )

    finding = brain._format_cycle_finding(response)

    assert "fred_series_observations" in finding
    assert "FAILED" in finding
    assert "FRED_API_KEY" in finding


def test_narrative_and_tools_both_present_combines_both() -> None:
    """When the model does narrate AND a tool ran, keep both in the finding."""

    brain = _build_brain()
    response = BrainResponse(
        message="Bitcoin shows mild bullish momentum.",
        tool_results=[{
            "tool": "get_crypto_price",
            "result": {
                "success": True,
                "result": {"price": 64100.0},
                "error": None,
                "metadata": {},
            },
        }],
        model="test",
    )

    finding = brain._format_cycle_finding(response)

    assert "Bitcoin shows mild bullish momentum." in finding
    assert "64100" in finding
    # Tool results lead so they survive the synthesis prompt's 300-char trim
    assert finding.index("64100") < finding.index("Bitcoin shows mild bullish momentum.")
