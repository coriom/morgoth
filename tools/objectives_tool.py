"""Objective creation tool for Morgoth."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from memory.persistent import PersistentMemory
from tools.base_tool import BaseTool


class CreateObjectiveTool(BaseTool):
    """Create a persistent objective in the PostgreSQL objectives table."""

    name = "create_objective"
    description = (
        "Create a persistent objective in Morgoth's goal queue. "
        "Use this when identifying a knowledge gap or task to pursue. "
        "Objectives have lifecycle: pending → in_progress → done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title, max 100 chars"},
            "description": {"type": "string", "description": "What to accomplish"},
            "priority": {"type": "integer", "description": "1=highest, 5=lowest", "default": 3},
        },
        "required": ["title", "description"],
    }

    def __init__(self, persistent_memory: PersistentMemory) -> None:
        """Initialize with persistent memory for PostgreSQL access."""

        self._persistent_memory = persistent_memory

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Insert a new objective row and return the created record."""

        title = str(kwargs["title"])[:100]
        description = str(kwargs["description"])
        priority = int(kwargs.get("priority", 3))

        try:
            row = await self._persistent_memory.create_objective(
                title=title,
                description=description,
                priority=priority,
            )
            return self.success(self._serialize(row))
        except Exception as exc:
            return self.failure(str(exc))

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        """Convert asyncpg non-JSON-serializable types before returning to the LLM."""

        result: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, uuid.UUID):
                result[key] = str(value)
            elif isinstance(value, datetime):
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result


class UpdateObjectiveTool(BaseTool):
    """Update an existing objective's status or add evidence."""

    name = "update_objective"
    description = (
        "Update an existing objective's status or add evidence. "
        "Use to mark progress: in_progress when starting work, "
        "done when complete. Add evidence describing what was found."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
            "evidence_summary": {
                "type": "string",
                "description": "What was found or accomplished",
            },
        },
        "required": ["objective_id"],
    }

    def __init__(self, persistent_memory: PersistentMemory) -> None:
        """Initialize with persistent memory for PostgreSQL access."""

        self._persistent_memory = persistent_memory

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Update the objective row and return the updated record."""

        objective_id = str(kwargs["objective_id"])
        status = kwargs.get("status")
        evidence_summary = kwargs.get("evidence_summary")
        evidence = {"summary": evidence_summary} if evidence_summary else None

        try:
            row = await self._persistent_memory.update_objective(
                objective_id=objective_id,
                status=status,
                evidence=evidence,
            )
            return self.success(self._serialize(row))
        except Exception as exc:
            return self.failure(str(exc))

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        """Convert asyncpg non-JSON-serializable types before returning to the LLM."""

        result: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, uuid.UUID):
                result[key] = str(value)
            elif isinstance(value, datetime):
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result
