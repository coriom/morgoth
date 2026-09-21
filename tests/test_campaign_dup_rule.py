"""Campaign dup guard's retry rule (2026-09-21 replaces the 90-s
retry-once window). New contract:

  · A retry is ACCEPTED only when the drift/dup check itself passes.
  · A per-instance consecutive-reject counter increments on every
    reject; a clean pass resets it.
  · After CAMPAIGN_DUP_MAX_REJECTS (default 3) consecutive rejects
    on the SAME tool instance, force-accept + log — the loop MUST
    NOT block on a stubborn model.

Reproduction case: BTC-dominance campaign had 3 IDENTICAL objective
titles created at t, t+23min, t+47min despite the dup guard. Each
duplicate arrived on a fresh cycle, well past the old 90-s window,
so the window's "second-attempt=accept" clause admitted them one at
a time. New rule catches every one until the deadlock cap fires."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.objectives_tool import CAMPAIGN_DUP_MAX_REJECTS, CreateObjectiveTool


@pytest.fixture(autouse=True)
def _no_semantic_dedup(monkeypatch):
    from tools import objectives_tool as ot
    monkeypatch.setattr(ot, "_find_semantic_duplicate",
                         AsyncMock(return_value=None))


def _tool_with_prior(subject: str, prior_titles: list[str]):
    pm = MagicMock()
    pm.get_active_campaign = AsyncMock(return_value={
        "campaign_id": "cid-1", "subject": subject,
    })
    pm.list_campaign_objectives = AsyncMock(return_value=[
        {"title": t} for t in prior_titles
    ])
    pm.create_objective = AsyncMock(return_value={
        "objective_id": "o-new", "title": "T", "description": "D",
    })
    pm.attach_objective_to_campaign = AsyncMock()
    return CreateObjectiveTool(pm), pm


@pytest.mark.asyncio
class TestIdenticalTitleRejected:
    async def test_identical_retry_now_rejected(self):
        # The exact production failure mode: same title arriving in a
        # new cycle (long after any 90-s window). MUST reject.
        subject = "BTC dominance"
        dup = "Impact of Major Economic News Events on BTC…"
        tool, pm = _tool_with_prior(subject, [dup])
        # First attempt with identical title.
        r1 = await tool.execute(title=dup, description="d1")
        assert r1["success"] is False
        assert "campaign guard" in r1["error"]
        pm.create_objective.assert_not_called()
        # Second attempt with the SAME identical title — the old 90-s
        # window would have accepted this. New rule REJECTS.
        r2 = await tool.execute(title=dup, description="d2")
        assert r2["success"] is False
        assert "campaign guard" in r2["error"]
        pm.create_objective.assert_not_called()


@pytest.mark.asyncio
class TestDeadlockCap:
    async def test_after_N_consecutive_rejects_force_accepts(self):
        # The loop must never block. After CAMPAIGN_DUP_MAX_REJECTS
        # consecutive rejects on the same tool instance, the next
        # call force-accepts and logs. Default is 3 → third call
        # is the force-accept.
        subject = "BTC dominance"
        dup = "Duplicate title BTC dominance"
        tool, pm = _tool_with_prior(subject, [dup])
        # Rejects 1..N-1
        for i in range(CAMPAIGN_DUP_MAX_REJECTS - 1):
            r = await tool.execute(title=dup, description=f"d{i}")
            assert r["success"] is False, f"attempt {i+1} should reject"
            expected_marker = f"({i+1}/{CAMPAIGN_DUP_MAX_REJECTS})"
            assert expected_marker in r["error"]
        # The N-th attempt is the deadlock guard: force-accept.
        pm.create_objective.assert_not_called()
        r = await tool.execute(title=dup, description="dfinal")
        assert r["success"] is True
        pm.create_objective.assert_awaited_once()

    async def test_clean_pass_resets_counter(self):
        # Two rejects then a clean pass — the counter resets, so the
        # next duplicate cycle starts fresh (rejects again at 1/3
        # rather than force-accepting at 3/3).
        subject = "BTC dominance"
        dup = "Duplicate title BTC dominance"
        tool, pm = _tool_with_prior(subject, [dup])
        for _ in range(CAMPAIGN_DUP_MAX_REJECTS - 1):
            await tool.execute(title=dup, description="d")
        # Clean pass on a genuinely fresh angle.
        pm.list_campaign_objectives = AsyncMock(return_value=[
            {"title": "BTC dominance short-term"},
        ])
        r = await tool.execute(title="BTC dominance macro drivers",
                                 description="fresh")
        assert r["success"] is True
        # Counter reset → next duplicate rejects again at 1/N, not
        # force-accepts at N/N.
        pm.list_campaign_objectives = AsyncMock(return_value=[
            {"title": dup},
        ])
        r = await tool.execute(title=dup, description="d")
        assert r["success"] is False
        assert f"(1/{CAMPAIGN_DUP_MAX_REJECTS})" in r["error"]


class TestGrepLockNoBypass:
    def test_no_time_window_retry_remains(self):
        # Structural fence: the OLD _last_drift_reject_ts + `> 90.0`
        # comparison MUST be gone from the execute-path code (not just
        # comments). If either comes back accidentally the loop can
        # once again accept identical retries.
        import inspect
        from tools.objectives_tool import CreateObjectiveTool as _COT
        src = inspect.getsource(_COT.execute)
        assert "_last_drift_reject_ts" not in src
        assert "> 90.0" not in src
        # Correct machinery present.
        assert "_consecutive_drift_rejects" in src
        assert "CAMPAIGN_DUP_MAX_REJECTS" in src

    def test_llm_writes_go_through_one_create_path(self):
        # Grep-lock: the LLM's tool loop writes objectives via
        # CreateObjectiveTool.execute. ObjectiveManager.create_objective
        # in core/objectives.py is HTTP-API-only. Ensure brain.py does
        # not shadow the tool path.
        import inspect
        from core import brain
        src = inspect.getsource(brain)
        assert "CreateObjectiveTool" not in src or src.count("CreateObjectiveTool") <= 2


@pytest.mark.asyncio
class TestLegitimateAnglesStillPass:
    async def test_matching_title_still_creates(self):
        # Regression: legitimate campaign objectives still create.
        subject = "BTC dominance"
        tool, pm = _tool_with_prior(subject, [])
        r = await tool.execute(title="BTC dominance macro drivers",
                                 description="fresh")
        assert r["success"] is True
        pm.attach_objective_to_campaign.assert_awaited_once()
