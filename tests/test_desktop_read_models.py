"""Read-only Desktop projections; real SQL uses only disposable morgoth_test."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
from uuid import uuid4

import asyncpg
import httpx
import pytest

from agents.desktop_read_models import DesktopReadStore, MissionPage, MissionSummary, ReadUnavailable
from agents.desktop_read_routes import PREFIX, install_desktop_read_routes
from agents.generalist_mission_engine import CapabilityRecord, CapabilityState, MissionRecord, MissionState
from agents.generalist_mission_store import _DDL
from api.management_app import ManagementSecurity, create_management_app
from core.runtime import RuntimeHome
from project_manager import ProjectManager


def _manager(root: Path) -> ProjectManager:
    home = root / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("project_a", name="A", domain="crypto")
    manager.create_project("project_b", name="B", domain="crypto")
    return manager


@pytest.mark.asyncio
async def test_optional_routes_keep_v1_offline_and_authenticate(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    token = secrets.token_urlsafe(32)
    app = create_management_app(manager, ManagementSecurity(token, 8765))
    assert not any(route.path.startswith(PREFIX) for route in app.routes)
    class Stub:
        calls = 0
        async def missions(self, project, *, limit=20, offset=0):
            self.calls += 1
            return MissionPage(project_id=project.id, missions=[], next_offset=None)
        async def mission(self, project, mission_id):
            self.calls += 1
            return None
        async def capabilities(self, project, *, limit=20, offset=0):
            self.calls += 1
            raise ReadUnavailable("READ_MODEL_UNAVAILABLE")
    reader = Stub()
    install_desktop_read_routes(app, manager, reader)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://127.0.0.1:8765") as client:
        path = f"{PREFIX}/projects/project_a/missions"
        assert (await client.get(path)).status_code == 401
        headers = {"X-Morgoth-Management-Token": token}
        assert (await client.get(path, headers={**headers, "Origin": "null"})).status_code == 403
        ok = await client.get(path, headers=headers)
        assert ok.status_code == 200 and ok.json()["source"] == "DISPOSABLE_POSTGRESQL"
        assert reader.calls == 1
        assert (await client.get(f"{PREFIX}/projects/missing/missions", headers=headers)).status_code == 404
        assert (await client.get(f"{PREFIX}/projects/../missions", headers=headers)).status_code != 200
        assert (await client.get(path + "?limit=11", headers=headers)).status_code == 422
        assert (await client.get(f"{PREFIX}/projects/project_a/capabilities", headers=headers)).status_code == 503
        assert (await client.post(path, headers={**headers, "Content-Type": "application/json"})).status_code == 405
        assert (await client.get(path + "/" + str(uuid4()), headers=headers)).status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disposable_sql_project_scope_order_and_redaction(tmp_path: Path) -> None:
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    socket = os.environ.get("MORGOTH_TEST_PG_SOCKET")
    if not dsn or not socket:
        pytest.skip("owned disposable morgoth_test cluster unavailable")
    manager = _manager(tmp_path)
    a, b = (next(p for p in manager._catalog() if p.id == name) for name in ("project_a", "project_b"))
    conn = await asyncpg.connect(dsn)
    assert await conn.fetchval("SELECT current_database()") == "morgoth_test"
    mission_a = MissionRecord(project_id=a.id, objective="Synthetic historical task", state=MissionState.PENDING,
                              state_history=["MISSION_RECEIVED", "PENDING_HUMAN_APPROVAL"])
    mission_b = MissionRecord(project_id=b.id, objective="Foreign mission")
    cap = CapabilityRecord(project_id=a.id, capability_id="synthetic_read", version="1",
                           documentation_identity="fixture:local", documentation_digest="a" * 64,
                           operations=["read"], required_scopes=[], valid_until_tick=10,
                           state=CapabilityState.PENDING_APPROVAL, validation_state="PASSED")
    try:
        for project in (a, b):
            await conn.execute(f'CREATE SCHEMA "{project.postgres_schema}"')
            await conn.execute(f'SET search_path TO "{project.postgres_schema}"')
            for ddl in _DDL:
                await conn.execute(ddl)
        for project, item in ((a, mission_a), (b, mission_b)):
            await conn.execute(f'''INSERT INTO "{project.postgres_schema}".generalist_missions
                (mission_id,project_id,user_id,state,record) VALUES($1,$2,$3,$4,$5::jsonb)''',
                item.mission_id, project.id, item.user_id, item.state.value, item.model_dump_json())
        await conn.execute(f'''INSERT INTO "{a.postgres_schema}".generalist_capabilities
            (project_id,user_id,capability_id,version,document_digest,record)
            VALUES($1,$2,$3,$4,$5,$6::jsonb)''',
            a.id, cap.user_id, cap.capability_id, cap.version, cap.documentation_digest, cap.model_dump_json())
        reader = DesktopReadStore(Path(socket))
        page = await reader.missions(a, limit=1)
        assert [m.mission_id for m in page.missions] == [mission_a.mission_id]
        assert page.missions[0].state == "PENDING_HUMAN_APPROVAL"
        assert page.missions[0].evidence_kind == "HISTORICAL_CHECKPOINT"
        assert page.missions[0].receipt_count == 0
        assert page.next_offset is None
        assert await reader.mission(a, mission_b.mission_id) is None
        assert (await reader.mission(b, mission_b.mission_id)).project_id == b.id
        caps = await reader.capabilities(a)
        assert len(caps.capabilities) == 1
        assert caps.capabilities[0].approval_kind == "NONE_RECORDED"
        assert caps.capabilities[0].source_identity == "fixture:local"
        assert "invocation_contract" not in caps.model_dump_json()
        with pytest.raises(ReadUnavailable, match="INVALID_PAGE"):
            await reader.missions(a, limit=11)
        # A fresh connection observes committed records, without initializing or migrating tables.
        fresh = DesktopReadStore(Path(socket))
        assert (await fresh.mission(a, mission_a.mission_id)).mission_id == mission_a.mission_id
        token = secrets.token_urlsafe(32)
        app = create_management_app(manager, ManagementSecurity(token, 8765))
        install_desktop_read_routes(app, manager, fresh)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://127.0.0.1:8765") as client:
            foreign = f"{PREFIX}/projects/{a.id}/missions/{mission_b.mission_id}"
            assert (await client.get(foreign)).status_code == 401
            response = await client.get(foreign, headers={"X-Morgoth-Management-Token": token})
            assert response.status_code == 404
            owned = await client.get(f"{PREFIX}/projects/{a.id}/missions/{mission_a.mission_id}",
                                     headers={"X-Morgoth-Management-Token": token})
            assert owned.status_code == 200 and owned.json()["mission_id"] == str(mission_a.mission_id)
            assert "checkpoint" not in owned.text and "user_id" not in owned.text
    finally:
        for project in (a, b):
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await conn.close()
