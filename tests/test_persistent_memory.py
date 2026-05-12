"""Regression tests for PostgreSQL task row normalization."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory.persistent import PersistentMemory


pytestmark = pytest.mark.asyncio


async def test_increment_cycle_count_returns_new_count(app_config) -> None:
    """increment_cycle_count should return the updated cycle_count from PostgreSQL."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value={"cycle_count": 3})

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    result = await persistent_memory.increment_cycle_count("12345678-1234-5678-1234-567812345678")

    assert result == 3
    mock_conn.fetchrow.assert_called_once()
    call_args = mock_conn.fetchrow.call_args
    assert "cycle_count" in call_args[0][0]


async def test_increment_cycle_count_returns_zero_when_row_missing(app_config) -> None:
    """increment_cycle_count should return 0 when no row matches the objective_id."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    result = await persistent_memory.increment_cycle_count("00000000-0000-0000-0000-000000000000")

    assert result == 0


class _AsyncCtxManager:
    """Minimal async context manager wrapping a mock connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


async def test_get_objectives_stats_aggregates_correctly(app_config) -> None:
    """get_objectives_stats should aggregate status counts, completions, and cycles from mocked rows."""

    persistent_memory = PersistentMemory(app_config)

    group_rows = [
        {"status": "done", "total": 3, "avg_cycles": 2.0, "auto_done": 2, "last_24h": 1},
        {"status": "pending", "total": 2, "avg_cycles": 0.0, "auto_done": 0, "last_24h": 2},
    ]
    title_rows = [{"title": "Alpha"}, {"title": "Beta"}, {"title": "Gamma"}]

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[group_rows, title_rows])

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    stats = await persistent_memory.get_objectives_stats()

    assert stats["total"] == 5
    assert stats["by_status"] == {"done": 3, "pending": 2}
    assert stats["auto_completed"] == 2
    assert stats["manually_completed"] == 1  # done(3) - auto_done(2)
    assert stats["topics"] == ["Alpha", "Beta", "Gamma"]
    assert abs(stats["avg_cycles_per_objective"] - 1.2) < 1e-9  # (3*2.0 + 2*0.0) / 5
    assert abs(stats["objectives_per_hour"] - 3 / 24) < 1e-9


async def test_get_objectives_stats_uses_array_containment_for_auto_completed(app_config) -> None:
    """get_objectives_stats must filter auto_completed using JSONB array containment (@>), not ->>."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetch = AsyncMock(side_effect=[[], []])

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    await persistent_memory.get_objectives_stats()

    first_call_sql: str = mock_conn.fetch.call_args_list[0][0][0]
    assert "@>" in first_call_sql, (
        "SQL must use JSONB array containment (@>) for auto_completed, "
        f"not the scalar ->> operator. Got: {first_call_sql}"
    )


async def test_list_tasks_parses_string_null_result(app_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task rows with a string ``null`` result should normalize to ``None``."""

    persistent_memory = PersistentMemory(app_config)

    async def _fake_fetch(query: str, *args: object) -> list[dict[str, object]]:
        return [
            {
                "task_id": "task-123",
                "type": "one_shot",
                "priority": 2,
                "description": "demo task",
                "agent_id": None,
                "created_by": "human",
                "created_at": "2026-04-13T00:00:00+00:00",
                "scheduled_at": None,
                "recurrence_cron": None,
                "status": "pending",
                "result": "null",
                "user_id": "default",
            }
        ]

    monkeypatch.setattr(persistent_memory, "fetch", _fake_fetch)

    tasks = await persistent_memory.list_tasks()

    assert tasks[0]["result"] is None


async def test_list_tasks_parses_json_result(app_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task rows with JSON text should deserialize into dictionaries."""

    persistent_memory = PersistentMemory(app_config)

    async def _fake_fetch(query: str, *args: object) -> list[dict[str, object]]:
        return [
            {
                "task_id": "task-456",
                "type": "one_shot",
                "priority": 1,
                "description": "demo task",
                "agent_id": None,
                "created_by": "human",
                "created_at": "2026-04-13T00:00:00+00:00",
                "scheduled_at": None,
                "recurrence_cron": None,
                "status": "completed",
                "result": "{\"status\": \"ok\"}",
                "user_id": "default",
            }
        ]

    monkeypatch.setattr(persistent_memory, "fetch", _fake_fetch)

    tasks = await persistent_memory.list_tasks()

    assert tasks[0]["result"] == {"status": "ok"}
