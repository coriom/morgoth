"""Regression tests for PostgreSQL task row normalization."""

from __future__ import annotations

import pytest

from memory.persistent import PersistentMemory


pytestmark = pytest.mark.asyncio


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
