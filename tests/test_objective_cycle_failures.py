"""Bounded, durable diagnostics for allocated autonomous work attempts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import build_test_brain


OBJECTIVE = {
    "objective_id": "12345678-1234-5678-1234-567812345678",
    "title": "Fixture objective", "description": "Fixture", "status": "pending",
}
SECRET = "fixture-secret-must-not-persist"


def _one_cycle_brain(monkeypatch: pytest.MonkeyPatch):
    brain = build_test_brain(MagicMock(), monkeypatch, config_overrides={
        "max_cycles_per_objective": 3,
    })
    calls = 0

    async def claim(*, limit: int):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [OBJECTIVE]
        raise asyncio.CancelledError

    brain._persistent_memory.claim_next_objective = AsyncMock(side_effect=claim)
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=1)
    brain._persistent_memory.reset_outage_streak = AsyncMock()
    brain._sleep_between_cycles = AsyncMock()
    result = MagicMock()
    result.tool_results = []
    result.message = "fixture"
    brain.process_message = AsyncMock(return_value=result)
    return brain


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [
    "PROMPT_PREPARE", "WORK_INFERENCE", "OUTAGE_GUARD",
    "PAYLOAD_PERSIST", "EPISODIC_PERSIST", "MODEL_FINALIZATION",
])
async def test_failure_stage_attribution_and_secret_exclusion(stage, monkeypatch) -> None:
    """Each throwing boundary records only allocated identity, stage, class."""
    brain = _one_cycle_brain(monkeypatch)
    error = RuntimeError(SECRET)
    if stage == "PROMPT_PREPARE":
        monkeypatch.setattr("core.brain.effective_min_sources", lambda: (_ for _ in ()).throw(error))
    elif stage == "WORK_INFERENCE":
        brain.process_message.side_effect = error
    elif stage == "OUTAGE_GUARD":
        monkeypatch.setattr("core.outage_guard.cycle_is_all_network_outage", lambda _: (_ for _ in ()).throw(error))
    elif stage == "PAYLOAD_PERSIST":
        brain._persistent_memory.update_objective.side_effect = error
    elif stage == "EPISODIC_PERSIST":
        brain._episodic_memory.add_text.side_effect = error
    else:
        brain._persistent_memory.increment_cycle_count.return_value = 3
        brain._persistent_memory.has_durable_cycle_payload = AsyncMock(return_value=True)
        brain._finalize_objective = AsyncMock(side_effect=error)

    with pytest.raises(asyncio.CancelledError):
        await brain.run_autonomous_cycle()

    ledger = brain._persistent_memory.record_objective_cycle_failure
    ledger.assert_awaited_once_with(OBJECTIVE["objective_id"],
                                    3 if stage == "MODEL_FINALIZATION" else 1,
                                    stage, "RuntimeError")
    assert SECRET not in repr(ledger.await_args)


@pytest.mark.asyncio
async def test_zero_evidence_exhaustion_is_failed_after_two_inference_errors(monkeypatch) -> None:
    """The b6 incident cannot be represented as successful completion."""
    brain = _one_cycle_brain(monkeypatch)
    claims = 0

    async def claim(*, limit: int):
        nonlocal claims
        claims += 1
        if claims <= 3:
            return [OBJECTIVE]
        raise asyncio.CancelledError

    brain._persistent_memory.claim_next_objective.side_effect = claim
    brain._persistent_memory.increment_cycle_count.side_effect = [1, 2, 3]
    brain.process_message.side_effect = TimeoutError(SECRET)
    brain._finalize_objective = AsyncMock()
    brain._synthesize_objective = AsyncMock()
    brain._extract_theses = AsyncMock()

    with pytest.raises(asyncio.CancelledError):
        await brain.run_autonomous_cycle()

    records = brain._persistent_memory.record_objective_cycle_failure.await_args_list
    assert [(call.args[1], call.args[2], call.args[3]) for call in records] == [
        (1, "WORK_INFERENCE", "TimeoutError"),
        (2, "WORK_INFERENCE", "TimeoutError"),
    ]
    brain._persistent_memory.update_objective.assert_awaited_once_with(
        OBJECTIVE["objective_id"], status="failed", evidence={
            "type": "work_failure", "reason": "no_durable_cycle_payload", "attempts": 3,
        },
    )
    brain._finalize_objective.assert_not_awaited()
    brain._synthesize_objective.assert_not_awaited()
    brain._extract_theses.assert_not_awaited()
    assert brain._sleep_between_cycles.await_count == 3
    assert SECRET not in repr(records)


@pytest.mark.asyncio
async def test_ledger_unavailable_does_not_kill_recoverable_cycle(monkeypatch) -> None:
    """A diagnostic write failure does not detach the autonomous task."""
    brain = _one_cycle_brain(monkeypatch)
    brain.process_message.side_effect = RuntimeError(SECRET)
    brain._persistent_memory.record_objective_cycle_failure.side_effect = RuntimeError(SECRET)
    with pytest.raises(asyncio.CancelledError):
        await brain.run_autonomous_cycle()
    brain._sleep_between_cycles.assert_awaited_once()
