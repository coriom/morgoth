"""Task scheduler backed by PostgreSQL."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from memory.persistent import PersistentMemory


class TaskPriority(Enum):
    """Queue priority for tasks."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    BACKGROUND = 3


class TaskType(Enum):
    """Supported task scheduling types."""

    ONE_SHOT = "one_shot"
    RECURRING = "recurring"
    TRIGGERED = "triggered"


class Task(BaseModel):
    """Scheduled work item."""

    task_id: str = Field(default_factory=lambda: str(uuid4()))
    type: TaskType
    priority: TaskPriority
    description: str
    agent_id: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    scheduled_at: datetime | None = None
    recurrence_cron: str | None = None
    status: str = "pending"
    result: dict[str, Any] | None = None
    user_id: str = "default"

    def to_record(self) -> dict[str, Any]:
        """Serialize the task for PostgreSQL writes."""

        return {
            "task_id": self.task_id,
            "type": self.type.value,
            "priority": self.priority.value,
            "description": self.description,
            "agent_id": self.agent_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "scheduled_at": self.scheduled_at,
            "recurrence_cron": self.recurrence_cron,
            "status": self.status,
            "result": self.result,
            "user_id": self.user_id,
        }


class Scheduler:
    """Async priority queue that persists tasks to PostgreSQL."""

    def __init__(self, persistent_memory: PersistentMemory) -> None:
        """Initialize the scheduler."""

        self._persistent_memory = persistent_memory
        self._queue: asyncio.PriorityQueue[tuple[int, str, Task]] = asyncio.PriorityQueue()

    async def initialize(self) -> None:
        """Load pending tasks from PostgreSQL into the in-memory queue."""

        rows = await self._persistent_memory.list_tasks(status="pending")
        for row in rows:
            task = Task(
                task_id=str(row["task_id"]),
                type=TaskType(row["type"]),
                priority=TaskPriority(row["priority"]),
                description=row["description"],
                agent_id=str(row["agent_id"]) if row["agent_id"] else None,
                created_by=row["created_by"],
                created_at=row["created_at"],
                scheduled_at=row["scheduled_at"],
                recurrence_cron=row["recurrence_cron"],
                status=row["status"],
                result=row["result"],
                user_id=row["user_id"],
            )
            await self._queue.put((task.priority.value, task.created_at.isoformat(), task))

    async def schedule(self, task: Task) -> Task:
        """Persist and enqueue a task."""

        await self._persistent_memory.save_task(task.to_record())
        await self._queue.put((task.priority.value, task.created_at.isoformat(), task))
        return task

    async def get_next_task(self) -> Task | None:
        """Return the next task if one is immediately available."""

        try:
            _, _, task = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        return task

    async def mark_complete(self, task: Task, result: dict[str, Any] | None = None) -> Task:
        """Mark a task as completed and persist its result."""

        task.status = "completed"
        task.result = result
        await self._persistent_memory.save_task(task.to_record())
        return task

    async def list_tasks(self) -> list[dict[str, Any]]:
        """Return all known tasks from PostgreSQL."""

        return await self._persistent_memory.list_tasks()
