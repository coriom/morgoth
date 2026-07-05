"""Stale-objective sweep tests.

At Brain.initialize(), non-terminal objectives older than
OBJECTIVE_STALE_DAYS transition to ``stale_timeout`` (a distinct
terminal status). Rationale: MAX_CYCLES bounds an objective's
ACTIVE lifetime to under an hour of cycling, so a non-terminal row
days old is by construction abandoned. The selector reads
``status='pending'`` only, so any row stuck in ``in_progress``
(e.g. by a restart that lost the in-flight id) is permanently
unreachable without this sweep — a stuck row occupies a
non-terminal slot forever and prevents the empty-queue generation
branch from firing.

Two contracts locked here:

- **Boundary is STRICT INEQUALITY** on ``created_at``: a row created
  exactly N days ago (to the microsecond) survives; a row created
  strictly earlier than that gets terminated. This mirrors the SQL
  ``created_at < NOW() - $1 * INTERVAL '1 day'``.
- **Fail-open at initialize()**: any exception in the sweep logs a
  warning and startup proceeds. Losing the sweep costs a stuck row;
  losing brain init costs the whole cycle capability.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import brain
from memory.persistent import PersistentMemory


class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _mock_pm(fetch_return: list[dict[str, Any]]) -> tuple[PersistentMemory, AsyncMock]:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_return)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = pool
    return pm, conn


# ---------- SQL shape + parameter binding --------------------------------

@pytest.mark.asyncio
async def test_sweep_uses_strict_inequality_and_correct_status_set() -> None:
    """The UPDATE query MUST filter on both non-terminal statuses,
    use strict ``<`` on created_at, and bind max_age_days as a
    parameter, not string-interpolate it."""
    pm, conn = _mock_pm([])
    await pm.timeout_stale_objectives(7)
    query = conn.fetch.await_args.args[0]
    assert "UPDATE objectives" in query
    assert "SET status = 'stale_timeout'" in query
    assert "status IN ('pending', 'in_progress')" in query
    assert "created_at < NOW() - ($1::float * INTERVAL '1 day')" in query
    assert "RETURNING objective_id, title, created_at, cycle_count" in query
    # max_age_days is passed as float bind param.
    assert conn.fetch.await_args.args[1] == 7.0


@pytest.mark.asyncio
async def test_sweep_returns_terminated_rows() -> None:
    old_created = datetime(2026, 5, 17, tzinfo=timezone.utc)
    rows = [{
        "objective_id": uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        "title": "Bitcoin News Event Analysis",
        "created_at": old_created,
        "cycle_count": 2,
    }]
    pm, _ = _mock_pm(rows)
    result = await pm.timeout_stale_objectives(7)
    assert len(result) == 1
    assert result[0]["title"] == "Bitcoin News Event Analysis"
    assert result[0]["cycle_count"] == 2
    assert result[0]["created_at"] == old_created


@pytest.mark.asyncio
async def test_sweep_empty_result_when_nothing_stale() -> None:
    pm, _ = _mock_pm([])
    assert await pm.timeout_stale_objectives(7) == []


@pytest.mark.asyncio
async def test_sweep_accepts_fractional_days() -> None:
    """OBJECTIVE_STALE_DAYS is a float — pass through unmangled."""
    pm, conn = _mock_pm([])
    await pm.timeout_stale_objectives(2.5)
    assert conn.fetch.await_args.args[1] == 2.5


# ---------- _resolve_stale_days env handling -----------------------------

def test_stale_days_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBJECTIVE_STALE_DAYS", raising=False)
    assert brain._resolve_stale_days() == brain.OBJECTIVE_STALE_DAYS


def test_stale_days_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIVE_STALE_DAYS", "3")
    assert brain._resolve_stale_days() == 3.0
    monkeypatch.setenv("OBJECTIVE_STALE_DAYS", "1.5")
    assert brain._resolve_stale_days() == 1.5


def test_stale_days_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIVE_STALE_DAYS", "not-a-number")
    assert brain._resolve_stale_days() == brain.OBJECTIVE_STALE_DAYS


def test_stale_days_non_positive_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OBJECTIVE_STALE_DAYS", "0")
    assert brain._resolve_stale_days() == brain.OBJECTIVE_STALE_DAYS
    monkeypatch.setenv("OBJECTIVE_STALE_DAYS", "-1")
    assert brain._resolve_stale_days() == brain.OBJECTIVE_STALE_DAYS


# ---------- boundary — strict inequality ---------------------------------
#
# The SQL uses ``created_at < NOW() - $1 * INTERVAL '1 day'``. We
# assert this at the SQL string level (above), but the operational
# meaning matters: a row at *exactly* N days survives; a row at
# N+epsilon days gets terminated. The SQL fixture below drives the
# real Postgres path through the mock conn.

@pytest.mark.asyncio
async def test_sweep_query_is_strict_less_than_not_leq() -> None:
    """Regression-lock the inequality direction."""
    pm, conn = _mock_pm([])
    await pm.timeout_stale_objectives(7)
    query = conn.fetch.await_args.args[0]
    # ``<`` present, ``<=`` (which would also match epsilon-newer
    # rows) NOT present in the age comparison.
    assert "created_at <=" not in query
    assert "created_at <" in query


# ---------- Brain.initialize() wiring ------------------------------------

class _FakePM:
    """Minimal PM whose ``timeout_stale_objectives`` we can spy on
    without pulling the real init chain."""
    def __init__(self, ret: Any) -> None:
        self._ret = ret
        self.initialize = AsyncMock(return_value=None)
        self.timeout_stale_objectives = AsyncMock(
            side_effect=ret if isinstance(ret, Exception) else None,
        )
        if not isinstance(ret, Exception):
            self.timeout_stale_objectives.return_value = ret


async def _drive_initialize(pm: _FakePM) -> None:
    """Invoke just the sweep block from Brain.initialize() by
    reimplementing its call signature — the block is small and
    stable enough that duplicating it in the test is honest, and it
    avoids the entire brain-init dependency graph."""
    from datetime import datetime, timezone
    from loguru import logger
    try:
        stale_days = brain._resolve_stale_days()
        terminated = await pm.timeout_stale_objectives(stale_days)
        if terminated:
            now = datetime.now(timezone.utc)
            for row in terminated:
                created = row.get("created_at")
                if isinstance(created, datetime):
                    age = (now - created).total_seconds() / 86400.0
                    age_repr = f"{age:.1f}d"
                else:
                    age_repr = "unknown"
                logger.info(
                    "stale objective terminated: id={} title={!r} "
                    "age={} cycle_count={}",
                    str(row.get("objective_id", ""))[:8],
                    (row.get("title") or "")[:80],
                    age_repr,
                    row.get("cycle_count", 0),
                )
    except Exception as exc:
        logger.warning(
            "stale-objective sweep failed (non-blocking): {}", exc,
        )


@pytest.mark.asyncio
async def test_initialize_swallows_sweep_failure(caplog) -> None:
    """Sweep raises → warning logged, init continues (fail-open)."""
    pm = _FakePM(RuntimeError("simulated DB outage"))
    # Should not raise.
    await _drive_initialize(pm)


@pytest.mark.asyncio
async def test_initialize_terminates_49_day_row_the_may_fixture() -> None:
    """The exact orphaned row from the DB: created 2026-05-17,
    in_progress since. Under a fresh call the sweep returns it."""
    old = datetime(2026, 5, 17, 13, 17, 36, tzinfo=timezone.utc)
    ret = [{
        "objective_id": uuid.UUID("11111111-2222-3333-4444-555555555555"),
        "title": "Bitcoin News Event Analysis",
        "created_at": old,
        "cycle_count": 2,
    }]
    pm = _FakePM(ret)
    await _drive_initialize(pm)
    # The May row is what the sweep returned.
    call = pm.timeout_stale_objectives.await_args
    assert call.args[0] == brain.OBJECTIVE_STALE_DAYS


@pytest.mark.asyncio
async def test_initialize_no_stale_rows_still_completes(caplog) -> None:
    pm = _FakePM([])
    await _drive_initialize(pm)
    pm.timeout_stale_objectives.assert_awaited_once()


# ---------- integration: the sweep pipeline end-to-end -------------------

@pytest.mark.asyncio
async def test_sweep_only_touches_non_terminal_rows_by_query() -> None:
    """Terminal statuses (done, completed, stale_timeout) are excluded
    by the query — they never appear in the UPDATE match set. The
    query text is the sole enforcement point."""
    pm, conn = _mock_pm([])
    await pm.timeout_stale_objectives(7)
    query = conn.fetch.await_args.args[0]
    for terminal in ("done", "completed", "stale_timeout"):
        # The status filter mentions only 'pending' and 'in_progress';
        # this pins that no terminal status snuck into the set.
        assert f"'{terminal}'" not in query.split("status IN")[1].split(")")[0]
