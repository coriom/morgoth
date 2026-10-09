"""Fresh and migrated llm_calls error codes on disposable morgoth_test schemas."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from core.config import load_config
from core.llm.logs import log_call
from core.project import current_project, load_projects
from core.runtime import RuntimeHome
from memory.persistent import PersistentMemory
from project_manager import ProjectManager
from self_modify.reflect_llm import ReflectLLMError


@pytest.mark.integration
@pytest.mark.asyncio
async def test_llm_calls_fresh_and_existing_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    ids = [f"claude_log_{uuid4().hex[:12]}" for _ in range(2)]
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    for project_id in ids:
        manager.create_project(project_id, name="Claude log fixture", domain="weather")
    projects = {p.id: p for p in load_projects(home / "projects") if p.id in ids}
    admin = await asyncpg.connect(dsn, timeout=5)
    memories: list[PersistentMemory] = []
    try:
        # Existing installation shape: a row without error_code survives migration.
        old_schema = projects[ids[1]].postgres_schema
        await admin.execute(f'CREATE SCHEMA "{old_schema}"')
        await admin.execute(
            f'CREATE TABLE "{old_schema}".llm_calls ('
            'id UUID PRIMARY KEY DEFAULT gen_random_uuid(), task TEXT NOT NULL, '
            'provider TEXT NOT NULL, model TEXT NOT NULL, prompt_bytes INT NOT NULL DEFAULT 0, '
            'response_bytes INT NOT NULL DEFAULT 0, latency_ms INT NOT NULL DEFAULT 0, '
            'outcome TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())'
        )
        await admin.execute(
            f'INSERT INTO "{old_schema}".llm_calls (task,provider,model,outcome) '
            "VALUES ('legacy','claude-cli','default','ok')"
        )
        for project_id in ids:
            for key, value in {
                "MORGOTH_HOME": str(home), "MORGOTH_PROJECT": project_id,
                "POSTGRES_URL": dsn, "OLLAMA_BASE_URL": "http://127.0.0.1:65535",
                "OLLAMA_PRIMARY_MODEL": "fixture", "OLLAMA_AGENT_MODEL": "fixture",
                "SECRET_KEY": "disposable-fixture", "MAX_CONCURRENT_AGENTS": "1",
                "LOG_RETENTION_DAYS": "1", "LOG_LEVEL_THOUGHT": "false",
            }.items():
                monkeypatch.setenv(key, value)
            current_project.cache_clear()
            memory = PersistentMemory(await load_config())
            await memory.initialize()
            memories.append(memory)

            async def fail():
                raise ReflectLLMError("safe", safe_code="CLAUDE_CLI_TIMEOUT")

            with pytest.raises(ReflectLLMError):
                await log_call(memory, "synthesis", "claude-cli", "default", fail)
            async with memory._pool.acquire() as conn:
                assert await conn.fetchval("SHOW search_path") == projects[project_id].postgres_schema
                row = await conn.fetchrow(
                    "SELECT outcome,error_code,response_bytes FROM llm_calls "
                    "WHERE task='synthesis'"
                )
                assert tuple(row) == ("error:ReflectLLMError", "CLAUDE_CLI_TIMEOUT", 0)
                if project_id == ids[1]:
                    assert await conn.fetchval("SELECT error_code FROM llm_calls WHERE task='legacy'") is None
                else:
                    assert await conn.fetchval("SELECT count(*) FROM llm_calls WHERE task='legacy'") == 0
    finally:
        for memory in memories:
            await memory.close()
        for project in projects.values():
            await admin.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()
