"""Behavioral proofs for the shared, durable objective-finalization path."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.brain import BrainResponse, effective_min_sources
from core.domain import _load
from core.tool_rail import effective_tool_rail
from tests.conftest import build_test_brain


def _objective_brain(monkeypatch, *, forced: bool = False):
    llm = MagicMock()
    brain = build_test_brain(llm, monkeypatch)
    row = {
        "objective_id": "11111111-2222-3333-4444-555555555555",
        "title": "Research",
        "description": "One bounded task",
        "status": "in_progress",
        "evidence": [],
    }
    events: list[str] = []
    sources = ["get_weather_forecast_met"]

    async def claim(*_args, **_kwargs):
        events.append("claim")
        return [dict(row)]

    async def update(*, objective_id, status=None, evidence=None):
        assert objective_id == row["objective_id"]
        if status:
            row["status"] = status
        if evidence:
            row["evidence"].append(evidence)
            events.append("persist:" + str(evidence.get("type", "summary")))
        return dict(row)

    async def model(*_args, **_kwargs):
        events.append("model")
        if not forced:
            sources.append("get_nws_weather_observation")
            await update(objective_id=row["objective_id"], status="done",
                         evidence={"summary": "Model's human-readable conclusion"})
            results = [
                {"tool": "get_nws_weather_observation", "result": {"success": True, "result": {"temperature": 22}}},
                {"tool": "update_objective", "result": {"success": True,
                  "result": {"status": "done", "objective_id": row["objective_id"]}}},
            ]
        else:
            results = []
        return BrainResponse(message="done", tool_results=results, model="fixture")

    async def synthesis(_obj, source_names, findings):
        events.append("synthesis")
        assert len(set(source_names)) == 2
        assert any("temperature" in finding for finding in findings)
        if not forced:
            assert row["evidence"][-1]["type"] == "cycle_payload"
        return "Grounded cross-source synthesis"

    async def extraction(*_args):
        events.append("extraction")
        assert _args[1] == "Grounded cross-source synthesis"
        return []

    async def sleep():
        events.append("sleep")
        raise asyncio.CancelledError()

    brain._persistent_memory.claim_next_objective = AsyncMock(side_effect=claim)
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=5 if forced else 1)
    brain._persistent_memory.update_objective = AsyncMock(side_effect=update)
    brain._persistent_memory.get_objective = AsyncMock(side_effect=lambda _id: dict(row))
    brain._persistent_memory.get_sources_used = AsyncMock(side_effect=lambda _id: list(sources))
    brain.process_message = AsyncMock(side_effect=model)
    brain._synthesize_objective = AsyncMock(side_effect=synthesis)
    brain._extract_theses = AsyncMock(side_effect=extraction)
    brain.detect_contradictions = AsyncMock(return_value=[])
    brain._sleep_between_cycles = AsyncMock(side_effect=sleep)
    return brain, row, events


@pytest.mark.asyncio
async def test_model_done_finalizes_only_after_current_payload(monkeypatch):
    brain, row, events = _objective_brain(monkeypatch)
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert row["status"] == "done"
    assert row["evidence"][0]["summary"] == "Model's human-readable conclusion"
    assert events.index("persist:cycle_payload") < events.index("synthesis")
    assert events.count("synthesis") == events.count("extraction") == 1
    assert events[-1] == "sleep"
    assert events.count("claim") == 1


@pytest.mark.asyncio
async def test_forced_done_uses_same_finalizer_and_sleeps_before_reclaim(monkeypatch):
    brain, row, events = _objective_brain(monkeypatch, forced=True)
    row["evidence"].append({"type": "cycle_payload", "cycle": 4,
                            "tool_results": [{"tool": "get_weather_forecast_met",
                                              "success": True, "result": {"temperature": 20}}]})
    brain._persistent_memory.get_sources_used = AsyncMock(return_value=[
        "get_weather_forecast_met", "get_nws_weather_observation",
    ])
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert row["status"] == "done"
    assert events.count("synthesis") == events.count("extraction") == 1
    assert events[-1] == "sleep"
    assert events.count("claim") == 1
    brain.process_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["synthesis", "extraction"])
async def test_analysis_failure_keeps_model_completion_observable(monkeypatch, stage):
    brain, row, events = _objective_brain(monkeypatch)
    brain._feed_append = MagicMock()
    if stage == "synthesis":
        brain._synthesize_objective = AsyncMock(side_effect=RuntimeError("fixture provider failure"))
    else:
        brain._extract_theses = AsyncMock(side_effect=RuntimeError("fixture provider failure"))
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert row["status"] == "done"
    assert events[-1] == "sleep"
    assert any(call.args[0] == "ERROR" and "failed" in call.args[1]
               for call in brain._feed_append.call_args_list)
    if stage == "synthesis":
        brain._extract_theses.assert_not_called()


@pytest.mark.asyncio
async def test_failed_cycle_payload_write_does_not_synthesize_transient_data(monkeypatch):
    brain, row, events = _objective_brain(monkeypatch)
    original = brain._persistent_memory.update_objective.side_effect

    async def fail_payload(**kwargs):
        if (kwargs.get("evidence") or {}).get("type") == "cycle_payload":
            raise RuntimeError("fixture storage failure")
        return await original(**kwargs)

    brain._persistent_memory.update_objective.side_effect = fail_payload
    brain._feed_append = MagicMock()
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert row["status"] == "done"
    assert not any(entry.get("type") == "cycle_payload" for entry in row["evidence"])
    brain._synthesize_objective.assert_not_called()
    assert any("not durable" in call.args[1] for call in brain._feed_append.call_args_list)


@pytest.mark.asyncio
async def test_unrelated_terminal_status_does_not_trigger_research_finalization(monkeypatch):
    brain, row, events = _objective_brain(monkeypatch)

    async def unrelated_result(*_args, **_kwargs):
        row["status"] = "stale_timeout"
        return BrainResponse(message="stale", tool_results=[], model="fixture")

    brain.process_message = AsyncMock(side_effect=unrelated_result)
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert row["status"] == "stale_timeout"
    assert "synthesis" not in events
    brain._extract_theses.assert_not_called()


@pytest.mark.asyncio
async def test_done_from_other_writer_is_not_mistaken_for_model_completion(monkeypatch):
    brain, row, events = _objective_brain(monkeypatch)

    async def no_done_tool(*_args, **_kwargs):
        row["status"] = "done"
        return BrainResponse(message="no completion tool", tool_results=[], model="fixture")

    brain.process_message = AsyncMock(side_effect=no_done_tool)
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert row["status"] == "done"
    assert "synthesis" not in events


@pytest.mark.asyncio
async def test_successful_empty_queue_creation_still_chains_without_sleep(monkeypatch):
    brain = build_test_brain(MagicMock(), monkeypatch)
    claims = 0

    async def claim(*_args, **_kwargs):
        nonlocal claims
        claims += 1
        if claims == 1:
            return []
        raise asyncio.CancelledError()

    brain._persistent_memory.claim_next_objective = AsyncMock(side_effect=claim)
    brain.process_message = AsyncMock(return_value=BrainResponse(
        message="created", model="fixture", tool_results=[
            {"tool": "create_objective", "result": {"success": True, "result": {"objective_id": "fixture"}}},
        ],
    ))
    brain._sleep_between_cycles = AsyncMock(side_effect=AssertionError("chained creation slept"))
    with patch.object(brain, "_write_log_file", new=AsyncMock()):
        with pytest.raises(asyncio.CancelledError):
            await brain.run_autonomous_cycle()
    assert claims == 2
    brain._sleep_between_cycles.assert_not_called()


def test_effective_source_budget_is_rail_bounded():
    assert effective_min_sources(frozenset()) == 0
    assert effective_min_sources(frozenset({"one"})) == 1
    assert effective_min_sources(frozenset({"forecast", "observation"})) == 2
    assert effective_min_sources(frozenset({"a", "b", "c", "d"})) == 3
    assert effective_min_sources(effective_tool_rail(_load("weather")).sources) == 2
    assert effective_min_sources(effective_tool_rail(_load("crypto")).sources) == 3
