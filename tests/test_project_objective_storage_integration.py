"""Objective schema bootstrap proofs confined to disposable morgoth_test schemas."""

from __future__ import annotations

import os
import json
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg
import pytest

from core.config import load_config
from core.objectives import Objective, ObjectiveCategory, ObjectivesManager
from core.project import current_project, load_projects
from core.runtime import RuntimeHome
from memory.persistent import PersistentMemory
from project_manager import ProjectManager
from scripts import init_db


REQUIRED_COLUMNS = {
    "objective_id", "title", "description", "category", "priority",
    "generated_by", "status", "evidence", "created_at", "completed_at",
    "user_id", "cycle_count", "sources_used",
    "consecutive_network_outage_cycles", "campaign_id", "updated_at",
}


def _select_project(monkeypatch: pytest.MonkeyPatch, home: Path, project_id: str, dsn: str) -> None:
    """Select one disposable Project and the dedicated test database."""
    for key, value in {
        "MORGOTH_HOME": str(home), "MORGOTH_PROJECT": project_id,
        "POSTGRES_URL": dsn, "OLLAMA_BASE_URL": "http://127.0.0.1:65535",
        "OLLAMA_PRIMARY_MODEL": "test-primary", "OLLAMA_AGENT_MODEL": "test-agent",
        "SECRET_KEY": "synthetic-disposable-key", "MAX_CONCURRENT_AGENTS": "1",
        "LOG_RETENTION_DAYS": "1", "LOG_LEVEL_THOUGHT": "false",
    }.items():
        monkeypatch.setenv(key, value)
    current_project.cache_clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_managed_objectives_bootstrap_and_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime initialization alone creates a usable isolated objective table."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    project_id = f"objective_{uuid4().hex[:12]}"
    ProjectManager(RuntimeHome(home, home / "projects")).create_project(
        project_id, name="Objective test", domain="weather",
    )
    project = next(p for p in load_projects(home / "projects") if p.id == project_id)
    admin = await asyncpg.connect(dsn, timeout=5)
    memory: PersistentMemory | None = None
    try:
        assert not await admin.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)",
            project.postgres_schema,
        )
        _select_project(monkeypatch, home, project_id, dsn)
        memory = PersistentMemory(await load_config())
        await memory.initialize()
        assert current_project().id == project_id
        columns = {row["column_name"] for row in await admin.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=$1 AND table_name='objectives'", project.postgres_schema,
        )}
        assert REQUIRED_COLUMNS <= columns
        async with memory._pool.acquire() as conn:
            assert await conn.fetchval("SHOW search_path") == project.postgres_schema
            assert await conn.fetchval("SELECT to_regclass('objectives') IS NOT NULL")
            assert await conn.fetchval("SELECT count(*) FROM objectives") == 0
        manager = ObjectivesManager(await load_config(), memory)
        objective = Objective(title="Weather snapshot", description="Human test objective",
                              category=ObjectiveCategory.RESEARCH, generated_by="human")
        await manager.create_objective(objective)
        listed = await manager.list_objectives()
        assert [item.objective_id for item in listed] == [objective.objective_id]
        claimed = await memory.claim_next_objective()
        assert [str(item["objective_id"]) for item in claimed] == [objective.objective_id]
        assert await memory.increment_cycle_count(objective.objective_id) == 1
        await memory.update_objective(objective.objective_id, evidence={"summary": "synthetic"})
        row = await memory.fetchrow("SELECT * FROM objectives WHERE objective_id=$1",
                                    UUID(objective.objective_id))
        assert row["status"] == "in_progress" and row["cycle_count"] == 1
        assert json.loads(row["sources_used"]) == []
        assert json.loads(row["evidence"])[-1]["summary"] == "synthetic"
        assert row["campaign_id"] is None
        await init_db.main()
        await init_db.main()
        assert await memory.fetchrow("SELECT title FROM objectives WHERE objective_id=$1",
                                     UUID(objective.objective_id))
    finally:
        if memory is not None:
            await memory.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_historical_objectives_migrate_without_row_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old base table survives idempotent runtime migrations."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    project_id = f"historical_{uuid4().hex[:12]}"
    ProjectManager(RuntimeHome(home, home / "projects")).create_project(
        project_id, name="Historical test", domain="crypto",
    )
    project = next(p for p in load_projects(home / "projects") if p.id == project_id)
    admin = await asyncpg.connect(dsn, timeout=5)
    memory: PersistentMemory | None = None
    objective_id = uuid4()
    try:
        await admin.execute(f'CREATE SCHEMA "{project.postgres_schema}"')
        await admin.execute(f'''CREATE TABLE "{project.postgres_schema}".objectives (
            objective_id UUID PRIMARY KEY, title VARCHAR(500) NOT NULL,
            description TEXT NOT NULL, category VARCHAR(100) NOT NULL,
            priority INTEGER NOT NULL DEFAULT 2,
            generated_by VARCHAR(100) NOT NULL DEFAULT 'morgoth',
            status VARCHAR(50) NOT NULL DEFAULT 'pending',
            evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMPTZ,
            user_id VARCHAR(100) NOT NULL DEFAULT 'default'
        )''')
        await admin.execute(f'INSERT INTO "{project.postgres_schema}".objectives '
                            '(objective_id,title,description,category) VALUES ($1,$2,$3,$4)',
                            objective_id, "historical row", "preserve me", "research")
        _select_project(monkeypatch, home, project_id, dsn)
        memory = PersistentMemory(await load_config())
        await memory.initialize()
        first = await memory.fetchrow("SELECT * FROM objectives WHERE objective_id=$1", objective_id)
        assert first["title"] == "historical row" and first["description"] == "preserve me"
        assert first["cycle_count"] == 0 and json.loads(first["sources_used"]) == []
        assert first["campaign_id"] is None
        await memory.close()
        memory = PersistentMemory(await load_config())
        await memory.initialize()
        second = await memory.fetchrow("SELECT * FROM objectives WHERE objective_id=$1", objective_id)
        assert dict(second) == dict(first)
    finally:
        if memory is not None:
            await memory.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_incompatible_objective_relation_fails_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime cannot report initialized when objective migrations cannot run."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    project_id = f"invalid_{uuid4().hex[:12]}"
    ProjectManager(RuntimeHome(home, home / "projects")).create_project(
        project_id, name="Invalid schema test", domain="weather",
    )
    project = next(p for p in load_projects(home / "projects") if p.id == project_id)
    admin = await asyncpg.connect(dsn, timeout=5)
    memory: PersistentMemory | None = None
    try:
        await admin.execute(f'CREATE SCHEMA "{project.postgres_schema}"')
        await admin.execute(f'CREATE VIEW "{project.postgres_schema}".objectives AS '
                            'SELECT gen_random_uuid() AS objective_id')
        _select_project(monkeypatch, home, project_id, dsn)
        memory = PersistentMemory(await load_config())
        with pytest.raises(asyncpg.PostgresError):
            await memory.initialize()
    finally:
        if memory is not None:
            await memory.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()
