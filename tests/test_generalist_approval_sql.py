"""Actual disposable PostgreSQL authorization, race and receipt proofs."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_orchestrator import GeneralistMissionOrchestrator, InvocationGrant, PublicIssuesDriver
from core.project import ProjectConfig
from memory.persistent import PersistentMemory
from tests.test_generalist_real_capability import DOC, ROWS


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Bind one unique test schema; never the production or default Project."""
    suffix = uuid4().hex[:12]
    project = ProjectConfig(id=f"fixture_{suffix}", name="Approval SQL",
                            domain="crypto", postgres_schema=f"fixture_{suffix}",
                            chroma_prefix=f"fixture_{suffix}_",
                            vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "runtime")
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    return project


async def _setup(project, handler):
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    memory = PersistentMemory(SimpleNamespace(postgres_url=dsn))
    await memory.initialize()
    store = ProjectMissionStore(memory, project)
    await store.initialize_optional()
    driver = PublicIssuesDriver(transport=httpx.MockTransport(handler))
    broker = ProjectApprovalBroker(store)
    engine = GeneralistMissionOrchestrator(store, project.id, driver, approval_broker=broker)
    desc = driver.inspect(DOC, now_tick=1)
    mission = await engine.receive("Read public issues", desc.capability_id)
    await engine.assess(mission.mission_id, DOC, ROWS, now_tick=1)
    return memory, store, broker, engine, desc, mission


async def _drop(project, memory):
    await memory.close()
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
    finally:
        await conn.close()


def _provenance():
    return f"foreground_operator_uid:{os.geteuid()}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_grant_binds_mission_project_scope_and_rejects_forged_dataclass(project):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)
    memory, store, broker, engine, desc, mission = await _setup(project, handler)
    try:
        grant_id = await broker._issue_after_consent(mission.mission_id, desc,
                                                     provenance=_provenance())
        fake = InvocationGrant(project.id, desc.capability_id, desc.version, desc.digest,
                               desc.scopes, 10, "operator_task_public_read")
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(mission.mission_id, DOC, grant=fake, now_tick=1)
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(mission.mission_id, DOC, grant=None,
                                 grant_id=uuid4(), now_tick=1)
        second = await engine.receive("Different mission", desc.capability_id)
        await engine.assess(second.mission_id, DOC, ROWS, now_tick=1)
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(second.mission_id, DOC, grant=None,
                                 grant_id=grant_id, now_tick=1)
        assert calls == 0
        first = await engine.execute(mission.mission_id, DOC, grant=None,
                                     grant_id=grant_id, now_tick=1)
        assert first["status"] == "MISSION_COMPLETE" and calls == 1
        assert await engine.execute(mission.mission_id, DOC, grant=None,
                                    grant_id=grant_id, now_tick=1) == first
        assert calls == 1
    finally:
        await _drop(project, memory)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["revoked", "expired", "budget", "digest", "query",
                                    "project", "host", "method", "scope", "version"])
async def test_revoked_expired_exhausted_or_changed_grant_never_sends_request(project, denial):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)
    memory, store, broker, engine, desc, mission = await _setup(project, handler)
    try:
        grant_id = await broker._issue_after_consent(mission.mission_id, desc,
                                                     provenance=_provenance())
        if denial == "revoked":
            await broker.revoke(grant_id)
        else:
            field = {"expired": "expires_at=NOW() - INTERVAL '1 second'",
                     "budget": "used_invocations=1",
                     "digest": "document_digest='forged'",
                     "query": "query_constraints='{}'::jsonb",
                     "project": "project_id='fixture_other'",
                     "host": "host='other.invalid'",
                     "method": "method='POST'",
                     "scope": "scopes=ARRAY['admin']::text[]",
                     "version": "version='2.0'"}[denial]
            async with store._pool().acquire() as conn:
                await conn.execute(f"UPDATE generalist_approval_grants SET {field} WHERE grant_id=$1",
                                   grant_id)
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(mission.mission_id, DOC, grant=None,
                                 grant_id=grant_id, now_tick=1)
        saved, _ = await engine._load(mission.mission_id)
        assert saved.state is MissionState.PENDING and calls == 0
    finally:
        await _drop(project, memory)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_parallel_claims_consume_one_grant_and_one_get(project):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)
    memory, store, broker, engine, desc, mission = await _setup(project, handler)
    try:
        grant_id = await broker._issue_after_consent(mission.mission_id, desc,
                                                     provenance=_provenance())
        outcomes = await asyncio.gather(*[
            engine.execute(mission.mission_id, DOC, grant=None,
                           grant_id=grant_id, now_tick=1) for _ in range(2)
        ], return_exceptions=True)
        assert sum(isinstance(item, dict) for item in outcomes) == 1
        assert sum(isinstance(item, CapabilityRefusal) for item in outcomes) == 1
        assert calls == 1
        async with store._pool().acquire() as conn:
            assert await conn.fetchval("SELECT used_invocations FROM generalist_approval_grants WHERE grant_id=$1",
                                       grant_id) == 1
    finally:
        await _drop(project, memory)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claimed_but_unconfirmed_request_is_not_replayed(project):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)
    memory, store, broker, engine, desc, mission = await _setup(project, handler)
    try:
        grant_id = await broker._issue_after_consent(mission.mission_id, desc,
                                                     provenance=_provenance())
        loaded, revision = await engine._load(mission.mission_id)
        loaded.state = MissionState.ATTEMPTED
        loaded.resume_eligible = False
        await broker.claim(grant_id, loaded, revision, desc, loaded.model_dump_json())
        with pytest.raises(CapabilityRefusal, match="MISSION_STATE_INVALID"):
            await engine.execute(mission.mission_id, DOC, grant=None,
                                 grant_id=grant_id, now_tick=1)
        assert calls == 0
    finally:
        await _drop(project, memory)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capability_revocation_or_stale_revision_rolls_back_grant_claim(project):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)
    memory, store, broker, engine, desc, mission = await _setup(project, handler)
    try:
        grant_id = await broker._issue_after_consent(mission.mission_id, desc,
                                                     provenance=_provenance())
        loaded, revision = await engine._load(mission.mission_id)
        loaded.state = MissionState.ATTEMPTED
        # A stale concurrent writer must not spend the grant.
        with pytest.raises(CapabilityRefusal, match="MISSION_CONCURRENT_CHANGE"):
            await broker.claim(grant_id, loaded, revision - 1, desc,
                               loaded.model_dump_json())
        async with store._pool().acquire() as conn:
            assert await conn.fetchval("SELECT used_invocations FROM generalist_approval_grants WHERE grant_id=$1",
                                       grant_id) == 0
            await conn.execute(
                """UPDATE generalist_capabilities SET record=jsonb_set(record,'{state}',
                   '"REVOKED"'::jsonb) WHERE project_id=$1 AND capability_id=$2""",
                project.id, desc.capability_id)
        with pytest.raises(CapabilityRefusal, match="CAPABILITY_REVOKED_OR_STALE"):
            await engine.execute(mission.mission_id, DOC, grant=None,
                                 grant_id=grant_id, now_tick=1)
        assert calls == 0
    finally:
        await _drop(project, memory)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_response_without_receipt_remains_attempted_and_never_replays(project, monkeypatch):
    calls = 0
    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=ROWS)
    memory, store, broker, engine, desc, mission = await _setup(project, handler)
    try:
        grant_id = await broker._issue_after_consent(mission.mission_id, desc,
                                                     provenance=_provenance())
        async def broken_commit(*_args, **_kwargs):
            raise CapabilityRefusal("RECEIPT_PERSIST_FAILED")
        monkeypatch.setattr(store, "commit_execution", broken_commit)
        with pytest.raises(CapabilityRefusal, match="RECEIPT_PERSIST_FAILED"):
            await engine.execute(mission.mission_id, DOC, grant=None,
                                 grant_id=grant_id, now_tick=1)
        saved, _ = await engine._load(mission.mission_id)
        assert saved.state is MissionState.ATTEMPTED
        with pytest.raises(CapabilityRefusal, match="MISSION_STATE_INVALID"):
            await engine.execute(mission.mission_id, DOC, grant=None,
                                 grant_id=grant_id, now_tick=1)
        assert calls == 1
    finally:
        await _drop(project, memory)
