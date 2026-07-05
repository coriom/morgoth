"""Objective creation tool for Morgoth."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from memory.persistent import PersistentMemory
from tools.base_tool import BaseTool


# How many words of the description form the derived title when the
# LLM omits ``title`` from its ``create_objective`` tool-call args.
# llama3.1:8b was measured to drop the title 2/5 dry-runs — those
# calls used to KeyError and silently waste the cycle slot; now they
# land as writable rows with a derived title.
_TITLE_DERIVATION_WORDS = 8


def derive_title_from_description(description: str) -> str:
    """Deterministic title fallback for missing/empty ``title`` args.

    - First ``_TITLE_DERIVATION_WORDS`` words of the description.
    - Ellipsis appended if the description had more words than that.
    - Empty description → ``"(untitled investigation)"`` sentinel so
      the row remains queryable + human-readable.

    No LLM, no round-trip, no writes; purely a string operation.
    """
    if not isinstance(description, str):
        description = "" if description is None else str(description)
    words = description.split()
    if not words:
        return "(untitled investigation)"
    head = words[: _TITLE_DERIVATION_WORDS]
    ellipsis = "…" if len(words) > _TITLE_DERIVATION_WORDS else ""
    return " ".join(head) + ellipsis


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

        description = str(kwargs.get("description") or "")
        # Missing / empty / whitespace-only title → derive from
        # description. The prior handler KeyError'd here and returned
        # failure, silently wasting the cycle slot; measured 2/5 on
        # the dry-run. Deterministic fallback keeps the row writable.
        raw_title = kwargs.get("title")
        title = str(raw_title).strip() if raw_title is not None else ""
        if not title:
            title = derive_title_from_description(description)
        title = title[:100]
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
