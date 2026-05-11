"""Unit tests for CreateObjectiveTool and UpdateObjectiveTool."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest

from tools.objectives_tool import CreateObjectiveTool, UpdateObjectiveTool


class MockPersistentMemory:
    """Minimal persistent memory double for CreateObjectiveTool tests."""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        """Store the row to return and capture call arguments."""

        self.calls: list[dict[str, Any]] = []
        self._row = row or {
            "objective_id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "title": "Investigate BTC dominance",
            "description": "Analyse BTC market share trend over 30 days.",
            "category": "research",
            "priority": 2,
            "generated_by": "morgoth_autonomous",
            "status": "pending",
            "evidence": [],
            "created_at": datetime(2026, 5, 11, 9, 0, 0, tzinfo=timezone.utc),
            "completed_at": None,
            "user_id": "default",
        }

    async def create_objective(
        self,
        title: str,
        description: str,
        priority: int = 3,
    ) -> dict[str, Any]:
        """Record the call and return the pre-configured row."""

        self.calls.append({"title": title, "description": description, "priority": priority})
        return dict(self._row)


@pytest.mark.asyncio
async def test_create_objective_tool_returns_success() -> None:
    """Tool should return a success payload with the created objective row."""

    memory = MockPersistentMemory()
    tool = CreateObjectiveTool(memory)

    result = await tool.execute(
        title="Investigate BTC dominance",
        description="Analyse BTC market share trend over 30 days.",
        priority=2,
    )

    assert result["success"] is True
    assert result["result"]["title"] == "Investigate BTC dominance"
    assert result["result"]["status"] == "pending"
    assert memory.calls == [
        {"title": "Investigate BTC dominance", "description": "Analyse BTC market share trend over 30 days.", "priority": 2}
    ]


@pytest.mark.asyncio
async def test_create_objective_tool_serializes_uuid_and_datetime() -> None:
    """UUID and datetime fields in the row must be serialized to strings."""

    import json

    memory = MockPersistentMemory()
    tool = CreateObjectiveTool(memory)

    result = await tool.execute(
        title="Test objective",
        description="Testing serialization.",
    )

    assert result["success"] is True
    serialized = json.dumps(result)
    assert "12345678-1234-5678-1234-567812345678" in serialized
    assert "2026-05-11" in serialized


@pytest.mark.asyncio
async def test_create_objective_tool_truncates_long_title() -> None:
    """Titles exceeding 100 characters should be truncated."""

    memory = MockPersistentMemory()
    tool = CreateObjectiveTool(memory)

    long_title = "A" * 150
    await tool.execute(title=long_title, description="desc")

    assert memory.calls[0]["title"] == "A" * 100


@pytest.mark.asyncio
async def test_create_objective_tool_defaults_priority_to_3() -> None:
    """Priority should default to 3 when not provided."""

    memory = MockPersistentMemory()
    tool = CreateObjectiveTool(memory)

    await tool.execute(title="Gap analysis", description="Some gap.")

    assert memory.calls[0]["priority"] == 3


class MockPersistentMemoryWithUpdate(MockPersistentMemory):
    """Extends mock with update_objective support."""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        super().__init__(row)
        self.update_calls: list[dict[str, Any]] = []

    async def update_objective(
        self,
        objective_id: str,
        status: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.update_calls.append({"objective_id": objective_id, "status": status, "evidence": evidence})
        updated = dict(self._row)
        if status is not None:
            updated["status"] = status
        return updated


@pytest.mark.asyncio
async def test_update_objective_tool_marks_done() -> None:
    """Tool should pass status and evidence to persistent memory and return success."""

    memory = MockPersistentMemoryWithUpdate()
    tool = UpdateObjectiveTool(memory)

    result = await tool.execute(
        objective_id="12345678-1234-5678-1234-567812345678",
        status="done",
        evidence_summary="Found BTC dominance at 52%.",
    )

    assert result["success"] is True
    assert result["result"]["status"] == "done"
    assert memory.update_calls[0]["status"] == "done"
    assert memory.update_calls[0]["evidence"] == {"summary": "Found BTC dominance at 52%."}


@pytest.mark.asyncio
async def test_update_objective_tool_no_evidence_summary() -> None:
    """When evidence_summary is absent, evidence passed to persistent memory is None."""

    memory = MockPersistentMemoryWithUpdate()
    tool = UpdateObjectiveTool(memory)

    result = await tool.execute(
        objective_id="12345678-1234-5678-1234-567812345678",
        status="in_progress",
    )

    assert result["success"] is True
    assert memory.update_calls[0]["evidence"] is None


@pytest.mark.asyncio
async def test_update_objective_tool_returns_failure_on_db_error() -> None:
    """A database exception should return a failure payload, not raise."""

    class FailingMemory(MockPersistentMemoryWithUpdate):
        async def update_objective(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("row not found")

    tool = UpdateObjectiveTool(FailingMemory())  # type: ignore[arg-type]
    result = await tool.execute(objective_id="00000000-0000-0000-0000-000000000000")

    assert result["success"] is False
    assert "row not found" in result["error"]


@pytest.mark.asyncio
async def test_create_objective_tool_returns_failure_on_db_error() -> None:
    """A database exception should return a failure payload, not raise."""

    class FailingMemory(MockPersistentMemory):
        async def create_objective(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("connection lost")

    tool = CreateObjectiveTool(FailingMemory())  # type: ignore[arg-type]
    result = await tool.execute(title="x", description="y")

    assert result["success"] is False
    assert "connection lost" in result["error"]
