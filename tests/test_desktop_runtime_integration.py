"""Guarded morgoth_test proof of managed-project startup and profile guard."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import pytest

from core.config import load_config
from core.llm import environment as E, profiles as P
from core.project import current_project, load_projects
from core.runtime import RuntimeHome
from memory.persistent import PersistentMemory
from project_manager import ProjectManager


@pytest.mark.integration
@pytest.mark.asyncio
async def test_managed_projects_initialize_campaign_guard_in_disjoint_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual manager/loader/storage path creates private campaign tables."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    for identifier in ("desktop_a", "desktop_b"):
        manager.create_project(identifier, name=identifier, domain="crypto")
    projects = {p.id: p for p in load_projects(home / "projects")}
    assert projects["desktop_a"].postgres_schema != projects["desktop_b"].postgres_schema
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    for key, value in {
        "POSTGRES_URL": dsn,
        "OLLAMA_BASE_URL": "http://127.0.0.1:65535",
        "OLLAMA_PRIMARY_MODEL": "test-model",
        "OLLAMA_AGENT_MODEL": "test-model",
        "SECRET_KEY": "synthetic-private-test-key",
        "MAX_CONCURRENT_AGENTS": "1",
        "LOG_RETENTION_DAYS": "1",
        "LOG_LEVEL_THOUGHT": "false",
    }.items():
        monkeypatch.setenv(key, value)
    cap = E.Capability("ok", "synthetic")
    monkeypatch.setattr(E, "detect_environment", lambda: _environment(cap))
    from types import MappingProxyType
    from core.llm import tasks
    local = P.LLMProfile("synthetic_local", {task: "ollama:default" for task in tasks.all_tasks()})
    monkeypatch.setattr(P, "PROFILES", MappingProxyType({**P.PROFILES, local.id: local}))
    admin = await asyncpg.connect(dsn, timeout=5)
    memories: list[PersistentMemory] = []
    try:
        for identifier in ("desktop_a", "desktop_b"):
            monkeypatch.setenv("MORGOTH_PROJECT", identifier)
            current_project.cache_clear()
            assert current_project().id == identifier
            memory = PersistentMemory(await load_config())
            await memory.initialize()
            memories.append(memory)
            async with memory._pool.acquire() as connection:
                assert await connection.fetchval("SELECT to_regclass('campaigns') IS NOT NULL")
                assert await connection.fetchval("SHOW search_path") == projects[identifier].postgres_schema
        a, b = projects["desktop_a"], projects["desktop_b"]
        await P.switch_profile(a, "synthetic_local")
        assert P.current_profile(a).id == "synthetic_local"
        assert P.current_profile(b).id == "legacy"
        async with memories[0]._pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO campaigns(subject, ends_at) VALUES('synthetic', NOW()+interval '1 day')"
            )
        async with memories[1]._pool.acquire() as connection:
            assert await connection.fetchval("SELECT count(*) FROM campaigns") == 0
        with pytest.raises(P.ProfileError, match="research-live"):
            await P.switch_profile(a, "legacy")
        await P.switch_profile(b, "synthetic_local")
        assert P.current_profile(b).id == "synthetic_local"
    finally:
        for memory in memories:
            await memory.close()
        for identifier in ("desktop_a", "desktop_b"):
            schema = projects[identifier].postgres_schema
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()


async def _environment(cap: E.Capability) -> E.Environment:
    return E.Environment("synthetic", cap, cap, cap, cap, cap)
