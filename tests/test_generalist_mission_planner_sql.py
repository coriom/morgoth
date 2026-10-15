"""Disposable morgoth_test proof of mission planning, recovery, grant and reuse."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_planner import (
    FixtureProposalSource, MissionCapabilityPlanner, load_fixture_catalog,
)
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator
from core.project import ProjectConfig
from memory.persistent import PersistentMemory

CATALOG = Path(__file__).parent / "fixtures" / "generalist_discovery_catalog_v1.json"
MISSION = "Find public bibliographic information for a book and summarize its title and publication metadata"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_planning_survives_restart_then_mocked_exact_grant_executes_once(tmp_path, monkeypatch):
    """Actual SQL CAS and receipt; the HTTP response is fixture-only."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    suffix = uuid4().hex[:12]
    project = ProjectConfig(
        id=f"fixture_{suffix}", name="Discovery SQL", domain="crypto",
        postgres_schema=f"fixture_{suffix}", chroma_prefix=f"fixture_{suffix}_",
        vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "runtime")
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    catalog = load_fixture_catalog(CATALOG)
    requests = []
    memory = None

    async def reopen():
        fresh = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await fresh.initialize()
        store = ProjectMissionStore(fresh, project)
        await store.initialize_optional()
        return fresh, store

    try:
        memory, store = await reopen()
        planner = MissionCapabilityPlanner(store, project.id, catalog, FixtureProposalSource())
        mission = await planner.receive(MISSION)
        pending = await planner.advance(mission.mission_id, now_tick=1)
        assert pending.state is MissionState.PENDING
        assert pending.checkpoint["catalog_id"] == "books"
        await memory.close()
        memory = None

        memory, store = await reopen()
        restarted = MissionCapabilityPlanner(store, project.id, catalog, FixtureProposalSource())
        assert (await restarted.advance(mission.mission_id, now_tick=1)).state is MissionState.PENDING
        entry = catalog.get("books")
        selected = catalog.load(entry)

        def handler(request):
            requests.append(str(request.url))
            return httpx.Response(200, json=entry.isolated_sample)

        driver = DeclarativeHttpReadDriver(
            entry.reviewed_request, entry.inputs, transport=httpx.MockTransport(handler),
            documentation=selected.evidence)
        broker = ProjectApprovalBroker(store, reviewed_request=entry.reviewed_request)
        engine = GeneralistMissionOrchestrator(store, project.id, driver, approval_broker=broker)
        desc = driver.inspect(selected.document, now_tick=1)
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(mission.mission_id, selected.document, grant=None, now_tick=1)
        assert not requests
        grant = await broker._issue_after_consent(
            mission.mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")
        result = await engine.execute(
            mission.mission_id, selected.document, grant=None, grant_id=grant, now_tick=1)
        assert result["status"] == "MISSION_COMPLETE"
        assert len(requests) == 1
        assert await engine.execute(
            mission.mission_id, selected.document, grant=None, grant_id=grant,
            now_tick=1) == result
        assert len(requests) == 1
        key = result["execution_receipt"]
        await memory.close()
        memory = None

        code = """import asyncio,os,sys,uuid,asyncpg
async def main():
 c=await asyncpg.connect(os.environ['MORGOTH_TEST_POSTGRES_URL'])
 try:
  await c.execute('SET search_path TO "'+sys.argv[1]+'"')
  m=await c.fetchval('SELECT state FROM generalist_missions WHERE mission_id=$1',uuid.UUID(sys.argv[2]))
  r=await c.fetchval("SELECT result->>'status' FROM generalist_execution_receipts WHERE mission_id=$1 AND idempotency_key=$2",uuid.UUID(sys.argv[2]),sys.argv[3])
  print('RECOVERED' if m==r=='MISSION_COMPLETE' else 'MISSING')
 finally: await c.close()
asyncio.run(main())"""
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, project.postgres_schema, str(mission.mission_id), key,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                 "MORGOTH_TEST_POSTGRES_URL": dsn},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        stdout, _ = await asyncio.wait_for(child.communicate(), timeout=10)
        assert child.returncode == 0 and stdout == b"RECOVERED\n"

        memory, store = await reopen()
        reused = MissionCapabilityPlanner(store, project.id, catalog, FixtureProposalSource())
        second = await reused.receive(MISSION + " again")
        ready = await reused.advance(second.mission_id, now_tick=1)
        assert ready.state is MissionState.READY
        assert "reuse_exact_capability" in ready.completed_steps
    finally:
        if memory is not None:
            await memory.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()
