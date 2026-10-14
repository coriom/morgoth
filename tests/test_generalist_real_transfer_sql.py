"""Disposable PostgreSQL proof for offline Open Library acquisition and reuse."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_store import ProjectMissionStore
from agents.generalist_operator_read_acceptance import _reviewed_inputs
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator
from core.project import ProjectConfig
from memory.persistent import PersistentMemory


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_service_proposal_grant_receipt_fresh_process_and_reuse(tmp_path, monkeypatch):
    """No network: real SQL enforces exact grant and durable receipt semantics."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    suffix = uuid4().hex[:12]
    project = ProjectConfig(id=f"fixture_{suffix}", name="Real transfer SQL", domain="crypto",
                            postgres_schema=f"fixture_{suffix}",
                            chroma_prefix=f"fixture_{suffix}_",
                            vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "runtime")
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    document, reviewed, inputs, sample, source = _reviewed_inputs()
    requests: list[str] = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=sample)
    driver = DeclarativeHttpReadDriver(reviewed, inputs, transport=httpx.MockTransport(handler),
                                       documentation=source)
    memory = None

    async def reopen():
        fresh = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await fresh.initialize()
        store = ProjectMissionStore(fresh, project)
        await store.initialize_optional()
        broker = ProjectApprovalBroker(store, reviewed_request=reviewed)
        engine = GeneralistMissionOrchestrator(store, project.id, driver, approval_broker=broker)
        return fresh, store, broker, engine

    try:
        memory, store, broker, engine = await reopen()
        desc = driver.inspect(document, now_tick=1)
        mission = await engine.receive("Read one public work", desc.capability_id)
        assert (await engine.assess(mission.mission_id, document, sample,
                                    now_tick=1)).state is MissionState.PENDING
        with pytest.raises(CapabilityRefusal, match="PERMISSION_REQUIRED"):
            await engine.execute(mission.mission_id, document, grant=None, now_tick=1)
        assert not requests
        grant = await broker._issue_after_consent(
            mission.mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")
        modified = json.loads(document)
        modified["paths"]["/works/{work_id}.json"]["get"]["operationId"] = "other_operation"
        with pytest.raises(CapabilityRefusal, match="UNREVIEWED_DESTINATION"):
            await engine.execute(mission.mission_id, json.dumps(modified), grant=None,
                                 grant_id=grant, now_tick=1)
        assert not requests
        result = await engine.execute(mission.mission_id, document, grant=None,
                                      grant_id=grant, now_tick=1)
        assert result["evidence"]["result"] == sample
        assert requests == ["https://openlibrary.org/works/OL45804W.json"]
        key = result["execution_receipt"]
        await memory.close()
        memory = None
        # New interpreter, new connection, same disposable SQL receipt.
        # The recovery command expects current_namespace from a Project manifest.
        project_dir = tmp_path / "projects" / project.id
        project_dir.mkdir(parents=True)
        (project_dir / "project.yaml").write_text(json.dumps({
            "id": project.id, "name": project.name, "domain": "crypto",
            "postgres_schema": project.postgres_schema,
            "chroma_prefix": project.chroma_prefix,
            "vault_dir": str(project.vault_dir), "runtime_dir": str(project.runtime_dir),
        }))
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "agents.generalist_operator_read_acceptance", "--recover",
            str(mission.mission_id), key,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                 "MORGOTH_TEST_POSTGRES_URL": dsn,
                 "MORGOTH_HOME": str(tmp_path), "MORGOTH_PROJECT": project.id},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(child.communicate(), timeout=15)
        assert child.returncode == 0
        assert json.loads(output)["sha256"] == result["evidence"]["response_sha256"]
        memory, store, broker, engine = await reopen()
        assert await engine.execute(mission.mission_id, document, grant=None,
                                    grant_id=grant, now_tick=1) == result
        assert len(requests) == 1
        second = await engine.receive("Reuse compatible work reader", desc.capability_id)
        assert (await engine.assess(second.mission_id, document, sample,
                                    now_tick=1)).state is MissionState.READY
        second_grant = await broker._issue_after_consent(
            second.mission_id, desc, provenance=f"foreground_operator_uid:{os.geteuid()}")
        await engine.execute(second.mission_id, document, grant=None,
                             grant_id=second_grant, now_tick=1)
        assert len(requests) == 2
    finally:
        if memory is not None:
            await memory.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()
