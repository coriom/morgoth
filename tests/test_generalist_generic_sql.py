"""Real disposable PostgreSQL proof for two new declarative services."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4
import sys

import asyncpg
import httpx
import pytest

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator
from core.project import ProjectConfig
from memory.persistent import PersistentMemory
from tests.test_generalist_generic_read import case


async def _fresh_process_receipt(dsn: str, schema: str, mission_id: object, key: str) -> bool:
    """Read the committed receipt with a new interpreter and connection."""
    child_code = '''import asyncio, os, sys, asyncpg
async def run():
    conn = await asyncpg.connect(os.environ["MORGOTH_TEST_POSTGRES_URL"])
    try:
        await conn.execute('SET search_path TO "' + sys.argv[1] + '"')
        row = await conn.fetchval("SELECT result->>'status' FROM generalist_execution_receipts WHERE mission_id=$1 AND idempotency_key=$2", __import__('uuid').UUID(sys.argv[2]), sys.argv[3])
        print("RECOVERED" if row == "MISSION_COMPLETE" else "MISSING")
    finally:
        await conn.close()
asyncio.run(run())'''
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", child_code, schema, str(mission_id), key,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": os.environ["HOME"],
             "MORGOTH_TEST_POSTGRES_URL": dsn},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(child.communicate(), timeout=10)
    except TimeoutError:
        child.kill()
        await child.wait()
        return False
    return child.returncode == 0 and output == b"RECOVERED\n"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["collection", "detail"])
async def test_two_unfamiliar_services_acquire_claim_recover_and_reuse(shape, tmp_path, monkeypatch):
    """Same generic code serves both shapes, with one grant per request."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    suffix = uuid4().hex[:12]
    project = ProjectConfig(id=f"fixture_{suffix}", name="Generic SQL",
                            domain="crypto", postgres_schema=f"fixture_{suffix}",
                            chroma_prefix=f"fixture_{suffix}_",
                            vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "runtime")
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    document, _, inputs, policy, payload = case(shape)
    calls = 0
    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)
    driver = DeclarativeHttpReadDriver(policy, inputs, transport=httpx.MockTransport(handler))
    memory = None
    async def reopen():
        fresh = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await fresh.initialize()
        store = ProjectMissionStore(fresh, project)
        await store.initialize_optional()
        broker = ProjectApprovalBroker(store, reviewed_request=policy)
        engine = GeneralistMissionOrchestrator(store, project.id, driver, approval_broker=broker)
        return fresh, store, broker, engine
    try:
        memory, store, broker, engine = await reopen()
        desc = driver.inspect(document, now_tick=1)
        first = await engine.receive("Read an unfamiliar public service", desc.capability_id)
        assert (await engine.assess(first.mission_id, document, payload, now_tick=1)).state is MissionState.PENDING
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(first.mission_id, document, grant=None, now_tick=1)
        grant_id = await broker._issue_after_consent(
            first.mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")
        for field, value, code in (("version", "2.0", "CAPABILITY_VERSION_CHANGED"),
                                   ("scope", "admin.write", "UNREVIEWED_DESTINATION"),
                                   ("host", "other.test", "UNREVIEWED_DESTINATION"),
                                   ("operation", "other_read", "UNREVIEWED_DESTINATION")):
            altered = json.loads(document)
            if field == "version":
                altered["info"]["version"] = value
            elif field == "scope":
                altered["x-morgoth"]["scope"] = value
            elif field == "host":
                altered["servers"][0]["url"] = "https://" + value
            else:
                endpoint = next(iter(altered["paths"].values()))["get"]
                endpoint["operationId"] = value
            with pytest.raises(CapabilityRefusal, match=code):
                await engine.execute(first.mission_id, json.dumps(altered), grant=None,
                                     grant_id=grant_id, now_tick=1)
        assert calls == 0
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(first.mission_id, document, grant=None, grant_id=uuid4(), now_tick=1)
        result = await engine.execute(first.mission_id, document, grant=None,
                                      grant_id=grant_id, now_tick=1)
        assert result["evidence"]["result"] == payload and calls == 1
        assert await _fresh_process_receipt(dsn, project.postgres_schema,
                                            first.mission_id, result["execution_receipt"])
        await memory.close()
        memory = None
        memory, store, broker, engine = await reopen()
        assert await engine.execute(first.mission_id, document, grant=None,
                                    grant_id=grant_id, now_tick=1) == result
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(first.mission_id, document, grant=None,
                                 grant_id=uuid4(), now_tick=1)
        second = await engine.receive("Reuse the same compatible capability", desc.capability_id)
        assert (await engine.assess(second.mission_id, document, payload, now_tick=1)).state is MissionState.READY
        second_grant = await broker._issue_after_consent(
            second.mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")
        second_result = await engine.execute(second.mission_id, document, grant=None,
                                             grant_id=second_grant, now_tick=1)
        assert second_result["status"] == "MISSION_COMPLETE" and calls == 2
        third = await engine.receive("Revocation check", desc.capability_id)
        await engine.assess(third.mission_id, document, payload, now_tick=1)
        revoked = await broker._issue_after_consent(
            third.mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")
        await broker.revoke(revoked)
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(third.mission_id, document, grant=None,
                                 grant_id=revoked, now_tick=1)
        assert calls == 2
    finally:
        if memory is not None:
            await memory.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()
