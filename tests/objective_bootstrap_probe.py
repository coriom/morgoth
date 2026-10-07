"""Bounded separate-process objective service proof after Desktop PAUSED."""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlparse

import asyncpg

from core.config import load_config
from core.objectives import Objective, ObjectiveCategory, ObjectivesManager
from core.project import current_project
from memory.persistent import PersistentMemory


async def main() -> None:
    """Prove the engine-created table exists before using objective services."""
    dsn = os.environ["POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    project = current_project()
    assert not project.is_legacy and project.domain == "weather"
    admin = await asyncpg.connect(dsn, timeout=5)
    try:
        assert await admin.fetchval(
            "SELECT to_regclass(format('%I.objectives', $1::text)) IS NOT NULL",
            project.postgres_schema,
        )
    finally:
        await admin.close()
    memory = PersistentMemory(await load_config())
    try:
        await memory.initialize()
        manager = ObjectivesManager(await load_config(), memory)
        objective = Objective(title="PAUSED human objective",
                              description="Disposable storage bootstrap probe",
                              category=ObjectiveCategory.RESEARCH, generated_by="human")
        await manager.create_objective(objective)
        matches = await manager.list_objectives()
        assert any(item.objective_id == objective.objective_id for item in matches)
        print(json.dumps({"objective_id": objective.objective_id,
                          "project": project.id, "read_back": True}))
    finally:
        await memory.close()


if __name__ == "__main__":
    asyncio.run(main())
