"""Actual disposable PostgreSQL checkpoint proof for opt-in discovery."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from agents.generalist_discovery import DiscoveryMissionPlanner, OfflineDiscovery
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_planner import MissionRequirements, load_fixture_catalog
from agents.generalist_mission_store import ProjectMissionStore
from core.project import ProjectConfig
from memory.persistent import PersistentMemory
from test_generalist_discovery import FixtureIntent, intent
from test_generalist_discovery_heldout import FixtureChoice


CATALOG = Path(__file__).parent / "fixtures" / "generalist_v8_heldout_catalog.json"
MISSION = "Find a novel's public catalog title and other available details"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_discovery_checkpoint_and_pending_approval_survive_pool_restart(tmp_path, monkeypatch):
    """Use only a disposable Project schema in the guarded *_test database."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    suffix = uuid4().hex[:12]
    project = ProjectConfig(id=f"fixture_{suffix}", name="Discovery V8 SQL", domain="crypto",
                            postgres_schema=f"fixture_{suffix}", chroma_prefix=f"fixture_{suffix}_",
                            vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "runtime")
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    catalog = load_fixture_catalog(CATALOG)
    source = FixtureIntent(intent(terms=("book", "bibliographic", "catalog")))
    choice = FixtureChoice("books")
    memory = None

    async def reopen():
        fresh = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await fresh.initialize()
        store = ProjectMissionStore(fresh, project)
        await store.initialize_optional()
        return fresh, store

    def planner(store):
        return DiscoveryMissionPlanner(
            store, project.id, OfflineDiscovery(catalog), source,
            MissionRequirements(MISSION, "detail", ("title",)), choice)

    try:
        memory, store = await reopen()
        mission = await planner(store).receive(MISSION)
        searched = await planner(store).advance(mission.mission_id, now_tick=1)
        assert searched.checkpoint["discovery_phase"] == "searched"
        await memory.close()
        memory = None

        memory, store = await reopen()
        chosen = await planner(store).advance(mission.mission_id, now_tick=1)
        assert chosen.checkpoint["chosen_id"] == "books"
        await memory.close()
        memory = None

        memory, store = await reopen()
        selected = await planner(store).advance(mission.mission_id, now_tick=1)
        assert selected.checkpoint["catalog_id"] == "books"
        await memory.close()
        memory = None

        memory, store = await reopen()
        pending = await planner(store).advance(mission.mission_id, now_tick=1)
        assert pending.state is MissionState.PENDING
        assert pending.checkpoint["catalog_id"] == "books"
        assert source.calls == choice.calls == 1
        await memory.close()
        memory = None

        memory, store = await reopen()
        recovered = await planner(store).advance(mission.mission_id, now_tick=1)
        assert recovered.state is MissionState.PENDING
        assert recovered.checkpoint["digest"] == pending.checkpoint["digest"]
        assert source.calls == choice.calls == 1
    finally:
        if memory is not None:
            await memory.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()
