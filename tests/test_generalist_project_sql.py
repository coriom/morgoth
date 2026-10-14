"""Disposable morgoth_test proof for real Project-local mission recovery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import GeneralistFixtureEngine, MissionState
from agents.generalist_mission_store import MissionStoreError, ProjectMissionStore
from core.project import ProjectConfig
from memory.persistent import PersistentMemory

from tests.test_generalist_capability_persistence import SCOPES, TASK_ROWS, document


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_contract_disposable_sql_claim_receipt_recovery(tmp_path, monkeypatch):
    """Actual morgoth_test tables prove claim-before-I/O and fresh-pool recovery."""
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("disposable morgoth_test URL unavailable")
    import httpx
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    from agents.generalist_orchestrator import (
        GeneralistMissionOrchestrator, InvocationGrant, PublicIssuesDriver,
    )
    from tests.test_generalist_real_capability import DOC, ROWS

    project = _project(tmp_path, f"github_{uuid4().hex[:10]}")
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    memory = None
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)

    driver = PublicIssuesDriver(transport=httpx.MockTransport(handler))

    async def reopen():
        fresh = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await fresh.initialize()
        store = ProjectMissionStore(fresh, project)
        await store.initialize_optional()
        return fresh, store

    try:
        memory, store = await reopen()
        engine = GeneralistMissionOrchestrator(store, project.id, driver)
        desc = driver.inspect(DOC, now_tick=1)
        mission = await engine.receive("Public issues", desc.capability_id)
        assert (await engine.assess(mission.mission_id, DOC, ROWS, now_tick=1)).state is MissionState.PENDING
        await memory.close()
        memory = None
        memory, store = await reopen()
        resumed = GeneralistMissionOrchestrator(store, project.id, driver)
        grant = InvocationGrant(project.id, desc.capability_id, desc.version, desc.digest,
                                desc.scopes, 10, driver.provenance)
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await resumed.execute(mission.mission_id, DOC, grant=None, now_tick=1)
        first = await resumed.execute(mission.mission_id, DOC, grant=grant, now_tick=1)
        assert first["status"] == "MISSION_COMPLETE" and calls == 1
        await memory.close()
        memory = None
        memory, store = await reopen()
        recovered = GeneralistMissionOrchestrator(store, project.id, driver)
        assert await recovered.execute(mission.mission_id, DOC, grant=grant, now_tick=1) == first
        assert calls == 1
        saved, revision = await recovered._load(mission.mission_id)
        assert saved.state is MissionState.COMPLETE
        with pytest.raises(MissionStoreError, match="MISSION_CONCURRENT_CHANGE"):
            await store.commit_execution(mission.mission_id, saved.user_id, "stale",
                                         revision - 1, saved.state.value,
                                         saved.model_dump_json(), '{}')
        assert await store.get_receipt(mission.mission_id, "stale") is None
    finally:
        if memory is not None:
            await memory.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_public_read_demo_cross_process_mocked():
    """The bounded command uses separate interpreters and one mocked GET."""
    if not os.environ.get("MORGOTH_TEST_POSTGRES_URL"):
        pytest.skip("disposable morgoth_test URL unavailable")
    from agents.generalist_public_read_demo import run_demo
    result = await run_demo(mode="mock")
    assert result["validation"] == "MOCKED_PASS"
    assert result["prepared_state"] == "PENDING_HUMAN_APPROVAL"
    assert result["completed"]["state"] == "MISSION_COMPLETE"
    assert result["fresh_process_receipt_recovered"] is True
    assert result["mock_get_count"] == 1 and result["public_get_count"] == 0
    assert result["production_approval"] is False


def _project(root: Path, suffix: str) -> ProjectConfig:
    return ProjectConfig(
        id=f"fixture_{suffix}", name="Disposable fixture", domain="crypto",
        postgres_schema=f"fixture_{suffix}", chroma_prefix=f"fixture_{suffix}_",
        vault_dir=root / suffix / "vault", runtime_dir=root / suffix / "runtime",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_sql_project_isolation_restart_and_atomic_receipts(tmp_path, monkeypatch):
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    if not dsn:
        pytest.skip("disposable morgoth_test URL unavailable")
    suffix = uuid4().hex[:10]
    first = _project(tmp_path, f"a_{suffix}")
    second = _project(tmp_path, f"b_{suffix}")
    import core.project as project_module
    import agents.generalist_mission_store as store_module

    async def open_store(project: ProjectConfig):
        monkeypatch.setattr(project_module, "current_namespace", lambda: project)
        monkeypatch.setattr(store_module, "current_namespace", lambda: project)
        memory = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await memory.initialize()
        store = ProjectMissionStore(memory, project)
        await store.initialize_optional()
        return memory, store

    memory_a = memory_b = None
    try:
        memory_a, store_a = await open_store(first)
        engine_a = GeneralistFixtureEngine(store_a, first.id)
        mission = await engine_a.receive("Organize synthetic projects", "synthetic_tasks")
        pending = await engine_a.assess(mission.mission_id, document(), now_tick=1, records=TASK_ROWS)
        assert pending.state is MissionState.PENDING
        await memory_a.close()
        memory_a = None

        # Fresh connection/pool and engine reconstruct the committed checkpoint.
        memory_a, store_a = await open_store(first)
        resumed = GeneralistFixtureEngine(store_a, first.id)
        saved, revision = await resumed._mission(mission.mission_id)
        assert saved.state is MissionState.PENDING
        with pytest.raises(MissionStoreError, match="MISSION_CONCURRENT_CHANGE"):
            await store_a.commit_execution(mission.mission_id, saved.user_id,
                                           "fixture_report:stale", revision - 1,
                                           saved.state.value, saved.model_dump_json(),
                                           '{"report":"fixture"}')
        assert await store_a.get_receipt(mission.mission_id, "fixture_report:stale") is None
        await resumed.approve_test_fixture(mission.mission_id, explicit_test_authorization=True)
        memory_b, store_b = await open_store(second)
        assert await store_b.get_mission(mission.mission_id) is None
        other = GeneralistFixtureEngine(store_b, second.id)
        own = await other.receive("Organize synthetic projects", "synthetic_tasks")
        assert (await other.assess(own.mission_id, document(), now_tick=1,
                                   records=TASK_ROWS)).state is MissionState.PENDING
        with pytest.raises(CapabilityRefusal, match="HUMAN_APPROVAL_REQUIRED"):
            await other.execute(own.mission_id, document(), TASK_ROWS, scopes=SCOPES, now_tick=1)

        # Restore first namespace and prove the exact-once receipt survives restart.
        monkeypatch.setattr(store_module, "current_namespace", lambda: first)
        outcomes = await asyncio.gather(*[
            resumed.execute(mission.mission_id, document(), TASK_ROWS, scopes=SCOPES, now_tick=1)
            for _ in range(2)
        ])
        assert outcomes == ["weekly: Alpha=open; Beta=blocked"] * 2
        assert await store_a.get_receipt(mission.mission_id, "fixture_report:1") is not None
        await memory_a.close()
        memory_a = None
        memory_a, store_a = await open_store(first)
        reloaded = GeneralistFixtureEngine(store_a, first.id)
        assert await reloaded.execute(mission.mission_id, document(), TASK_ROWS,
                                      scopes=SCOPES, now_tick=2) == outcomes[0]
        assert (await reloaded._mission(mission.mission_id))[0].checkpoint["report_tick_count"] == 1
    finally:
        if memory_a is not None:
            await memory_a.close()
        if memory_b is not None:
            await memory_b.close()
        conn = await asyncpg.connect(dsn)
        try:
            for project in (first, second):
                await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_service_demo_recovers_across_processes(monkeypatch):
    if not os.environ.get("MORGOTH_TEST_POSTGRES_URL"):
        pytest.skip("disposable morgoth_test URL unavailable")
    from agents.generalist_persistent_demo import run_demo
    result = await run_demo()
    assert result["cross_process"] is True
    assert result["production_approval"] is False
    for service in ("task", "inventory"):
        stages = [row for row in result["timeline"] if row["service"] == service]
        assert [row["state"] for row in stages] == [
            "PENDING_HUMAN_APPROVAL", "MISSION_COMPLETE", "APPROVED_CAPABILITY_SELECTED"
        ]
        assert stages[1]["duplicate_suppressed"] is True
        assert stages[1]["periodic_tick"] is True


def test_store_requires_managed_project_and_verified_test_db(tmp_path, monkeypatch):
    project = _project(tmp_path, "a_safe")
    memory = PersistentMemory(SimpleNamespace(postgres_url="unused"))
    store = ProjectMissionStore(memory, project)
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    with pytest.raises(MissionStoreError, match="DISPOSABLE_TEST_DATABASE_REQUIRED"):
        store._pool()
    another = _project(tmp_path, "b_safe")
    monkeypatch.setattr(store_module, "current_namespace", lambda: another)
    with pytest.raises(MissionStoreError, match="PROJECT_NAMESPACE_MISMATCH"):
        store._pool()


@pytest.mark.asyncio
async def test_production_approval_record_rejected_before_storage(tmp_path):
    from agents.generalist_mission_engine import CapabilityRecord, CapabilityState
    project = _project(tmp_path, "a_safe")
    store = ProjectMissionStore(PersistentMemory(SimpleNamespace(postgres_url="unused")), project)
    record = CapabilityRecord(project_id=project.id, capability_id="synthetic_tasks",
                              version="1.0", documentation_identity="local_fixture:synthetic_tasks",
                              documentation_digest="a" * 64, operations=[], required_scopes=[],
                              valid_until_tick=10, state=CapabilityState.APPROVED)
    with pytest.raises(MissionStoreError, match="PRODUCTION_APPROVAL_UNAVAILABLE"):
        await store.save_capability(record.user_id, record.capability_id, record.version,
                                    record.documentation_digest, record.model_dump_json())


@pytest.mark.asyncio
async def test_demo_refuses_non_test_database_before_bootstrap(monkeypatch):
    import agents.generalist_persistent_demo as demo
    touched = []

    class FakeConnection:
        async def fetchval(self, query):
            return "production"

        async def close(self):
            touched.append("closed")

    async def fake_connect(dsn):
        return FakeConnection()

    monkeypatch.setenv("MORGOTH_TEST_POSTGRES_URL", "non_secret_fixture_url")
    monkeypatch.setattr(demo.asyncpg, "connect", fake_connect)
    with pytest.raises(RuntimeError, match="DISPOSABLE_TEST_DATABASE_REQUIRED"):
        await demo._child("prepare", "task", None)
    assert touched == ["closed"]
