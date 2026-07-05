"""Semantic dedup gate for create_objective.

Prevents side-door mid-cycle spawns from re-seeding the on-chain
basin: the working 8B can invoke create_objective while working
another objective (the tool is in CHAT_TOOL_NAMES), which bypasses
the knowledge-grounded generation branch. The gate compares each
new proposal to non-terminal rows via cosine similarity; near-dups
fail with an actionable "work the existing one" message.

Every embedding call is injected — no real MiniLM load — mirroring
the ``embed_fn`` pattern in ``core.contradictions``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import objectives_tool as ot


# ---------- fixtures + helpers ------------------------------------------

class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _mock_pm(existing_rows: list[dict[str, Any]]) -> MagicMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=existing_rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    pm.create_objective = AsyncMock(return_value={
        "objective_id": "new-id-1234abcd", "title": "x", "description": "y",
    })
    return pm


def _fake_embed(mapping: dict[str, list[float]]):
    """Return an ``embed_fn`` that yields pre-baked vectors keyed by
    input text. Any unmapped input gets a fresh unit vector; the goal
    is deterministic control over the cosine outputs."""
    def _fn(texts: list[str]) -> list[list[float]]:
        return [mapping[t] for t in texts]
    return _fn


# ---------- _resolve_dedup_threshold -----------------------------------

def test_threshold_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBJECTIVE_DEDUP_THRESHOLD", raising=False)
    assert ot._resolve_dedup_threshold() == ot.DEFAULT_OBJECTIVE_DEDUP_THRESHOLD


def test_threshold_env_override_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIVE_DEDUP_THRESHOLD", "0.62")
    assert ot._resolve_dedup_threshold() == 0.62


def test_threshold_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIVE_DEDUP_THRESHOLD", "not-a-float")
    assert ot._resolve_dedup_threshold() == ot.DEFAULT_OBJECTIVE_DEDUP_THRESHOLD


def test_threshold_out_of_range_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIVE_DEDUP_THRESHOLD", "1.5")
    assert ot._resolve_dedup_threshold() == ot.DEFAULT_OBJECTIVE_DEDUP_THRESHOLD
    monkeypatch.setenv("OBJECTIVE_DEDUP_THRESHOLD", "0")
    assert ot._resolve_dedup_threshold() == ot.DEFAULT_OBJECTIVE_DEDUP_THRESHOLD


# ---------- _find_semantic_duplicate ------------------------------------

@pytest.mark.asyncio
async def test_empty_queue_returns_none() -> None:
    pm = _mock_pm(existing_rows=[])
    result = await ot._find_semantic_duplicate(
        pm, "any title", "any desc", threshold=0.75,
        embed_fn=_fake_embed({}),
    )
    assert result is None


@pytest.mark.asyncio
async def test_near_duplicate_matched_at_threshold() -> None:
    existing = [
        {"objective_id": "existing-uuid-01", "title": "Old A",
         "description": "old desc a", "status": "pending"},
    ]
    pm = _mock_pm(existing)
    # Same vector both sides → cosine 1.0.
    vec = [1.0, 0.0, 0.0]
    result = await ot._find_semantic_duplicate(
        pm, "New A", "new desc a", threshold=0.75,
        embed_fn=_fake_embed({
            "New A. new desc a": vec,
            "Old A. old desc a": vec,
        }),
    )
    assert result is not None
    assert result["objective_id"] == "existing-uuid-01"


@pytest.mark.asyncio
async def test_dissimilar_returns_none() -> None:
    existing = [
        {"objective_id": "existing-uuid-01", "title": "Old A",
         "description": "old desc a", "status": "pending"},
    ]
    pm = _mock_pm(existing)
    result = await ot._find_semantic_duplicate(
        pm, "New A", "new desc a", threshold=0.75,
        embed_fn=_fake_embed({
            "New A. new desc a": [1.0, 0.0, 0.0],
            "Old A. old desc a": [0.0, 1.0, 0.0],
        }),
    )
    assert result is None


@pytest.mark.asyncio
async def test_terminal_status_rows_ignored_by_query_shape() -> None:
    """The SQL fetch filters to non-terminal statuses. The mock
    conn.fetch is scripted to return an empty list only when the
    param is exactly ``['pending', 'in_progress']`` — anything else
    would have returned the (empty) mocked list too, so we assert
    on the query params directly."""
    pm = _mock_pm(existing_rows=[])
    await ot._find_semantic_duplicate(
        pm, "T", "D", threshold=0.75, embed_fn=_fake_embed({}),
    )
    conn = pm._require_pool.return_value.acquire.return_value._conn
    args = conn.fetch.await_args.args
    assert args[1] == ["pending", "in_progress"]


@pytest.mark.asyncio
async def test_returns_first_match_not_best_match() -> None:
    """Deterministic render on operator failure message: the row
    identity returned is the first candidate at or above threshold,
    even if a later candidate has a higher cosine."""
    existing = [
        {"objective_id": "first-match-uuid", "title": "First",
         "description": "d1", "status": "pending"},
        {"objective_id": "second-match-uuid", "title": "Second",
         "description": "d2", "status": "in_progress"},
    ]
    pm = _mock_pm(existing)
    # Both match; first sits at threshold, second at 1.0.
    v_new  = [1.0, 0.0, 0.0]
    v_first  = [0.8, 0.6, 0.0]  # cosine ≈ 0.8 → passes 0.75
    v_second = [1.0, 0.0, 0.0]  # cosine = 1.0
    result = await ot._find_semantic_duplicate(
        pm, "New", "d_new", threshold=0.75,
        embed_fn=_fake_embed({
            "New. d_new": v_new,
            "First. d1": v_first,
            "Second. d2": v_second,
        }),
    )
    assert result is not None
    assert result["objective_id"] == "first-match-uuid"


# ---------- CreateObjectiveTool.execute end-to-end ----------------------

@pytest.mark.asyncio
async def test_execute_creates_when_no_duplicate() -> None:
    existing = [
        {"objective_id": "existing-uuid-01", "title": "Old",
         "description": "old desc", "status": "pending"},
    ]
    pm = _mock_pm(existing)
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        result = await tool.execute(
            title="Different topic", description="unrelated",
        )
    assert result["success"] is True
    pm.create_objective.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_fails_when_duplicate_names_existing() -> None:
    duplicate = {
        "objective_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "title": "Existing objective title",
        "description": "existing", "status": "in_progress",
    }
    pm = _mock_pm([duplicate])
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=duplicate),
    ):
        tool = ot.CreateObjectiveTool(pm)
        result = await tool.execute(
            title="Near dup of existing", description="whatever",
        )
    assert result["success"] is False
    error = result["error"]
    assert "duplicate of active objective" in error
    assert "Existing objective title" in error
    # Short-id (first 8) appears.
    assert "aaaaaaaa" in error
    assert "work that objective instead of spawning a variant" in error
    # No row was written.
    pm.create_objective.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_creates_after_dedup_gate_raises_fail_open() -> None:
    """A raised embedding call must NOT block creation — the gate is
    defense-in-depth, generation capability is load-bearing."""
    pm = _mock_pm([])
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(side_effect=RuntimeError("MiniLM died")),
    ):
        tool = ot.CreateObjectiveTool(pm)
        result = await tool.execute(
            title="Whatever", description="body text",
        )
    assert result["success"] is True
    pm.create_objective.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_creates_when_queue_is_empty() -> None:
    """No non-terminal rows → dedup gate is a no-op."""
    pm = _mock_pm([])
    tool = ot.CreateObjectiveTool(pm)
    result = await tool.execute(
        title="First ever", description="cold-start body",
    )
    assert result["success"] is True
    pm.create_objective.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_env_override_threshold_reaches_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OBJECTIVE_DEDUP_THRESHOLD is picked up at each call — verifies
    the resolve_dedup_threshold plumbing survives from env to gate."""
    monkeypatch.setenv("OBJECTIVE_DEDUP_THRESHOLD", "0.42")
    pm = _mock_pm([])
    dedup_spy = AsyncMock(return_value=None)
    with patch("tools.objectives_tool._find_semantic_duplicate", dedup_spy):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(title="T", description="D")
    assert dedup_spy.await_count == 1
    # Threshold arg is the 4th positional (pm, title, description, threshold).
    args = dedup_spy.await_args.args
    assert args[3] == pytest.approx(0.42)


# ---------- title-fallback path still passes through dedup gate ----------

@pytest.mark.asyncio
async def test_derived_title_still_hits_dedup_gate() -> None:
    """The 8B may omit ``title``; the derived title should still be
    evaluated by the dedup gate — otherwise the side door reopens
    behind the title-fallback logic."""
    pm = _mock_pm([])
    dedup_spy = AsyncMock(return_value=None)
    with patch("tools.objectives_tool._find_semantic_duplicate", dedup_spy):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(description="Investigate BTC futures funding rates")
    assert dedup_spy.await_count == 1
    args = dedup_spy.await_args.args
    # Second positional is `title` — should be the derived string,
    # NOT the empty title the model sent.
    assert args[1] == "Investigate BTC futures funding rates"
    assert args[2] == "Investigate BTC futures funding rates"


# ---------- integration with the real cosine (small dim, hand-crafted) --

@pytest.mark.asyncio
async def test_end_to_end_real_cosine_near_dup_blocked() -> None:
    """Exercise the real ``_cosine`` under a fake embedding so the
    integer arithmetic path is proven wired through."""
    existing = [
        {"objective_id": "old-uuid-000-000", "title": "Investigate mempool depth",
         "description": "look at mempool metrics", "status": "pending"},
    ]
    pm = _mock_pm(existing)
    # Near-parallel vectors → cosine 0.98.
    v_new = [1.0, 0.0, 0.0, 0.0]
    v_old = [0.98, 0.2, 0.0, 0.0]
    with patch(
        "core.contradictions._get_embedding_fn",
        return_value=_fake_embed({
            "Look at mempool depth for BTC. body": v_new,
            "Investigate mempool depth. look at mempool metrics": v_old,
        }),
    ):
        tool = ot.CreateObjectiveTool(pm)
        result = await tool.execute(
            title="Look at mempool depth for BTC", description="body",
        )
    assert result["success"] is False
    assert "old-uuid" in result["error"]  # short-id prefix visible
    pm.create_objective.assert_not_awaited()


@pytest.mark.asyncio
async def test_end_to_end_real_cosine_distinct_creates() -> None:
    existing = [
        {"objective_id": "old-uuid-000-000", "title": "Investigate mempool depth",
         "description": "look at mempool metrics", "status": "pending"},
    ]
    pm = _mock_pm(existing)
    # Orthogonal → cosine 0.
    v_new = [1.0, 0.0, 0.0, 0.0]
    v_old = [0.0, 1.0, 0.0, 0.0]
    with patch(
        "core.contradictions._get_embedding_fn",
        return_value=_fake_embed({
            "Investigate BTC futures funding. body": v_new,
            "Investigate mempool depth. look at mempool metrics": v_old,
        }),
    ):
        tool = ot.CreateObjectiveTool(pm)
        result = await tool.execute(
            title="Investigate BTC futures funding", description="body",
        )
    assert result["success"] is True
    pm.create_objective.assert_awaited_once()
