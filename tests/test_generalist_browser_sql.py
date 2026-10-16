"""Actual disposable PostgreSQL proof for browser attempt, receipt and recovery."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from agents.generalist_browser_lab import (
    BrowserRefusal, OllamaBrowserActionSource, run_isolated_policy,
)
from agents.generalist_browser_mission import BrowserMissionCoordinator
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_store import ProjectMissionStore
from core.project import ProjectConfig
from memory.persistent import PersistentMemory


ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_browser_claim_receipt_and_fresh_pool_recovery(tmp_path: Path, monkeypatch):
    """A mocked model-action stream persists only confirmed rendered UI evidence."""
    if not BROWSER.joinpath("chrome-headless-shell").is_file():
        pytest.skip("reviewed headless Chromium unavailable")
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    suffix = uuid4().hex[:12]
    project = ProjectConfig(id=f"fixture_{suffix}", name="Browser V9 SQL", domain="crypto",
                            postgres_schema=f"fixture_{suffix}", chroma_prefix=f"fixture_{suffix}_",
                            vault_dir=tmp_path / "vault", runtime_dir=tmp_path / "runtime")
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    monkeypatch.setattr(project_module, "current_namespace", lambda: project)
    monkeypatch.setattr(store_module, "current_namespace", lambda: project)
    memory = None

    async def open_store():
        fresh = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        await fresh.initialize()
        store = ProjectMissionStore(fresh, project)
        await store.initialize_optional()
        return fresh, store

    try:
        memory, store = await open_store()
        coordinator = BrowserMissionCoordinator(store, project.id)
        mission = await coordinator.receive("Find book BK-17", "browser_catalog")
        calls = 0
        model_calls = 0

        class ScriptedClient:
            """Response fixture exercises Ollama protocol, not semantic reasoning."""

            async def chat(self, messages, **_kwargs):
                nonlocal model_calls
                observation = json.loads(messages[1].content)["observation"]
                name = ("Book catalog", "Work code", "Find work", "Open BK-17")[model_calls]
                model_calls += 1
                matches = [item for item in observation["actions"] if item["name"] == name]
                assert len(matches) == 1
                content = json.dumps({"revision": observation["revision"],
                                      "choice_id": matches[0]["id"],
                                      "text": "BK-17" if name == "Work code" else ""})
                return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]),
                                       done=True, done_reason="stop")

        async def browser_run():
            nonlocal calls
            calls += 1
            source = OllamaBrowserActionSource(ScriptedClient(), "fixture-model")
            result = await run_isolated_policy(
                checkout=ROOT, browser_dir=BROWSER, fixture="catalog",
                objective="Find the title for book BK-17 in the public catalog",
                target="BK-17", expected="The Atlas of Rivers", source=source)
            assert source.calls == source.responses == 4
            return result

        pending, procedure = await coordinator.validate_fixture(
            mission.mission_id, capability_id="browser_catalog", run=browser_run)
        assert pending.state is MissionState.PENDING
        assert calls == 1 and model_calls == 4 and len(procedure.steps) == 4
        receipt = await store.get_receipt(mission.mission_id, "browser_fixture_validation")
        assert receipt is not None and '"ui_state_observed": true' in receipt.lower()
        child = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "tests" / "browser_process_probe.py"),
            project.id, str(mission.mission_id),
            cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await asyncio.wait_for(child.communicate(), timeout=15)
        assert child.returncode == 0 and len(output) < 512
        assert json.loads(output) == {"RECOVERED": True, "PROCEDURE_PRESENT": True,
                                      "RECEIPT_PRESENT": True, "APPROVAL_PENDING": True}
        await memory.close()
        memory = None

        memory, store = await open_store()
        recovered, loaded = await BrowserMissionCoordinator(store, project.id).recover(mission.mission_id)
        assert recovered.state is MissionState.PENDING
        assert loaded == procedure
        assert calls == 1 and model_calls == 4
        with pytest.raises(BrowserRefusal, match="MISSION_ALREADY_CLAIMED"):
            await BrowserMissionCoordinator(store, project.id).validate_fixture(
                mission.mission_id, capability_id="browser_catalog", run=browser_run)
        assert calls == 1

        interrupted = await BrowserMissionCoordinator(store, project.id).receive(
            "Find another book", "browser_catalog")

        async def crash():
            raise BrowserRefusal("WORKER_INTERRUPTED")

        with pytest.raises(BrowserRefusal, match="WORKER_INTERRUPTED"):
            await BrowserMissionCoordinator(store, project.id).validate_fixture(
                interrupted.mission_id, capability_id="browser_catalog", run=crash)
        await memory.close()
        memory = None
        memory, store = await open_store()
        blocked, no_procedure = await BrowserMissionCoordinator(store, project.id).recover(
            interrupted.mission_id)
        assert blocked.state is MissionState.ATTEMPTED and no_procedure is None
    finally:
        if memory is not None:
            await memory.close()
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        finally:
            await conn.close()
