"""Project-isolated objective failure ledger on disposable morgoth_test schemas."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from core.config import load_config
from core.objectives import Objective, ObjectiveCategory, ObjectivesManager
from core.project import current_project, load_projects
from core.runtime import RuntimeHome
from memory.persistent import PersistentMemory
from project_manager import ProjectManager


@pytest.mark.integration
@pytest.mark.asyncio
async def test_failure_ledger_and_zero_payload_guard_are_project_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime DDL, safe fields and no public/neighbor fallback use real SQL."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    ids = [f"failure_{uuid4().hex[:12]}" for _ in range(2)]
    for project_id in ids:
        manager.create_project(project_id, name="Failure fixture", domain="weather")
    projects = {p.id: p for p in load_projects(home / "projects") if p.id in ids}
    admin = await asyncpg.connect(dsn, timeout=5)
    memories: list[PersistentMemory] = []
    try:
        for project_id in ids:
            monkeypatch.setenv("MORGOTH_HOME", str(home))
            monkeypatch.setenv("MORGOTH_PROJECT", project_id)
            for key, value in {
                "POSTGRES_URL": dsn,
                "OLLAMA_BASE_URL": "http://127.0.0.1:65535",
                "OLLAMA_PRIMARY_MODEL": "fixture", "OLLAMA_AGENT_MODEL": "fixture",
                "SECRET_KEY": "disposable-fixture", "MAX_CONCURRENT_AGENTS": "1",
                "LOG_RETENTION_DAYS": "1", "LOG_LEVEL_THOUGHT": "false",
            }.items():
                monkeypatch.setenv(key, value)
            current_project.cache_clear()
            memory = PersistentMemory(await load_config())
            await memory.initialize()
            memories.append(memory)
            async with memory._pool.acquire() as conn:
                assert await conn.fetchval("SHOW search_path") == projects[project_id].postgres_schema
                assert await conn.fetchval("SELECT to_regclass('objective_cycle_failures') IS NOT NULL")
        first, second = memories
        monkeypatch.setenv("MORGOTH_PROJECT", ids[0])
        current_project.cache_clear()
        objective = await ObjectivesManager(first._config, first).create_objective(
            Objective(title="Fixture", description="One work attempt",
                      category=ObjectiveCategory.RESEARCH, generated_by="human")
        )
        oid = objective.objective_id
        assert not await first.has_durable_cycle_payload(oid)
        assert await first.increment_cycle_count(oid) == 1
        await first.record_objective_cycle_failure(oid, 1, "WORK_INFERENCE", "ReadTimeout")
        rows = await first.list_objective_cycle_failures(oid)
        assert len(rows) == 1
        assert (rows[0]["cycle"], rows[0]["stage"], rows[0]["error_class"]) == (
            1, "WORK_INFERENCE", "ReadTimeout",
        )
        assert await second.list_objective_cycle_failures(oid) == []
        assert "secret" not in str(rows).lower()
        with pytest.raises(ValueError):
            await first.record_objective_cycle_failure(oid, 1, "WORK_INFERENCE", "ReadTimeout: secret")
        with pytest.raises(ValueError):
            await first.record_objective_cycle_failure(oid, 1, "untrusted", "ReadTimeout")
        with pytest.raises(ValueError):
            await first.list_objective_cycle_failures(oid, limit=21)
        failed = await first.update_objective(oid, status="failed", evidence={
            "type": "work_failure", "reason": "no_durable_cycle_payload", "attempts": 1,
        })
        assert failed["status"] == "failed" and failed["completed_at"] is not None
        await first.update_objective(oid, evidence={
            "type": "cycle_payload", "cycle": 1, "tool_results": [],
        })
        assert await first.has_durable_cycle_payload(oid)
        assert not await second.has_durable_cycle_payload(oid)
    finally:
        for memory in memories:
            await memory.close()
        for project in projects.values():
            await admin.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()
