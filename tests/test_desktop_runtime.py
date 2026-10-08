"""Hermetic proof of paused research lifecycle, launcher and authenticated API."""
from __future__ import annotations

import asyncio
import builtins
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
import httpx
import pytest

from api.routes import proposals, runtime
from core.brain import Brain
from core.llm import profiles
from core.llm.environment import Capability, Environment
from core.project import current_project
from core.runtime import RuntimeHome
from project_manager import ProjectManager
from scripts import research_engine


def fake_brain() -> Brain:
    """Build the real lifecycle object with inert collaborators."""
    config = MagicMock(max_cycles_per_objective=5)
    memory = MagicMock()
    memory.initialize = AsyncMock()
    memory.timeout_stale_objectives = AsyncMock(return_value=[])
    memory.reclaim_orphan_objectives = AsyncMock(return_value=[])
    memory.close = AsyncMock()
    llm = MagicMock()
    llm.close = AsyncMock()
    episodic = MagicMock()
    episodic.initialize = AsyncMock()
    scheduler = MagicMock()
    scheduler.initialize = AsyncMock()
    tool_router = MagicMock()
    tool_router.close = AsyncMock()
    notifier = MagicMock()
    notifier.close = AsyncMock()
    brain = Brain(config, llm, memory, episodic, scheduler, tool_router,
                  MagicMock(), notifier)
    brain.awaken = AsyncMock(return_value={"status": "READY"})
    return brain


@pytest.mark.asyncio
async def test_paused_then_single_owned_task_and_shutdown() -> None:
    brain = fake_brain()
    gate = asyncio.Event()
    async def wait_for_gate() -> None:
        await gate.wait()
    brain.run_autonomous_cycle = wait_for_gate
    await brain.initialize(start_autonomous=False)
    assert brain.research_state == "PAUSED"
    assert brain._autonomous_task is None
    starts = await asyncio.gather(brain.start_autonomous_research(),
                                  brain.start_autonomous_research())
    assert sorted(starts) == [False, True]
    assert brain.research_state == "RUNNING"
    task = brain._autonomous_task
    await brain.shutdown()
    assert brain.research_state == "STOPPED"
    assert task is not None and task.done() and task.cancelled()


@pytest.mark.asyncio
async def test_legacy_schedules_once_even_when_awaken_degraded() -> None:
    brain = fake_brain()
    brain.awaken = AsyncMock(return_value={"status": "MISSING_DEPENDENCIES"})
    async def wait_forever() -> None:
        await asyncio.Event().wait()
    brain.run_autonomous_cycle = wait_forever
    await brain.initialize()
    task = brain._autonomous_task
    assert task is not None and brain.autonomous_task_alive
    assert brain.research_state == "RUNNING" and not brain.awakening_ready
    await brain.initialize()
    assert brain._autonomous_task is task
    await brain.shutdown()


@pytest.mark.asyncio
async def test_paused_not_ready_cannot_start() -> None:
    brain = fake_brain()
    brain.awaken = AsyncMock(return_value={"status": "MISSING_DEPENDENCIES"})
    await brain.initialize(start_autonomous=False)
    assert brain.research_state == "NOT_READY"
    assert brain._autonomous_task is None
    with pytest.raises(RuntimeError, match="not ready"):
        await brain.start_autonomous_research()
    await brain.shutdown()


@pytest.mark.asyncio
async def test_unexpected_task_exit_is_visible_and_cannot_restart() -> None:
    brain = fake_brain()
    brain.run_autonomous_cycle = AsyncMock(return_value=None)
    await brain.initialize(start_autonomous=False)
    assert await brain.start_autonomous_research()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert brain.research_state == "FAILED"
    assert not brain.autonomous_task_alive
    with pytest.raises(RuntimeError, match="terminated"):
        await brain.start_autonomous_research()
    await brain.shutdown()


def _project(tmp_path: Path):
    manager = ProjectManager(RuntimeHome(tmp_path / "home", tmp_path / "home/projects"))
    manager.create_project("research_a", name="Research A", domain="crypto")
    return next(p for p in manager._catalog() if p.id == "research_a")


def _synthetic_env() -> Environment:
    cap = Capability("ok", "synthetic non-inference check")
    return Environment("synthetic", cap, cap, cap, cap, cap)


@pytest.mark.asyncio
async def test_authenticated_runtime_blocked_then_claude_then_one_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    brain = fake_brain()
    async def wait_forever() -> None:
        await asyncio.Event().wait()
    brain.run_autonomous_cycle = wait_forever
    await brain.initialize(start_autonomous=False)
    app = FastAPI()
    app.include_router(runtime.router)
    app.state.desktop_runtime = True
    app.state.runtime_control_lock = asyncio.Lock()
    app.state.brain = brain
    app.state.code_sha = "a" * 40
    monkeypatch.setattr(proposals, "ensure_ui_token", lambda: "synthetic-private-token")
    monkeypatch.setattr(runtime, "current_project", lambda: project)
    monkeypatch.setattr(runtime, "detect_environment", AsyncMock(return_value=_synthetic_env()))
    selected = "legacy"
    monkeypatch.setattr(runtime, "current_profile", lambda _project: profiles.PROFILES[selected])
    switch_entered = asyncio.Event()
    release_switch = asyncio.Event()

    async def switch(_project, target):
        nonlocal selected
        if target == "codex":
            raise profiles.ProfileError("BLOCKED: Codex workload qualification has not passed")
        if target == "claude":
            switch_entered.set()
            await release_switch.wait()
        selected = target
        return profiles.PROFILES[target]

    monkeypatch.setattr(runtime, "switch_profile", switch)
    headers = {"X-Morgoth-Token": "synthetic-private-token"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost") as client:
        path = "/api/runtime/v1"
        for invalid in ({}, {"X-Morgoth-Token": "wrong"}):
            response = await asyncio.wait_for(client.get(f"{path}/profiles", headers=invalid), 3)
            assert response.status_code == 401
        status = await client.get(f"{path}/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["research_state"] == "PAUSED"
        assert status.json()["project"] == project.id
        assert status.json()["domain"] == project.domain
        brain._ready = False
        assert (await client.post(f"{path}/profile", json={"profile": "claude"}, headers=headers)).status_code == 409
        assert selected == "legacy"
        brain._ready = True
        catalog = (await client.get(f"{path}/profiles", headers=headers)).json()
        assert next(p for p in catalog["profiles"] if p["id"] == "codex")["status"] == "BLOCKED"
        assert (await client.post(f"{path}/start", headers=headers)).status_code == 409
        assert (await client.post(f"{path}/profile", json={"profile": "codex"}, headers=headers)).status_code == 409
        assert selected == "legacy"
        assert (await client.post(f"{path}/profile", json={"profile": "claude", "force": True}, headers=headers)).status_code == 422
        changing = asyncio.create_task(client.post(f"{path}/profile", json={"profile": "claude"}, headers=headers))
        await asyncio.wait_for(switch_entered.wait(), 3)
        starting_a = asyncio.create_task(client.post(f"{path}/start", headers=headers))
        starting_b = asyncio.create_task(client.post(f"{path}/start", headers=headers))
        await asyncio.sleep(0)
        assert not starting_a.done() and not starting_b.done()
        probes_before_start = runtime.detect_environment.await_count
        release_switch.set()
        assert (await changing).status_code == 200
        first, second = await asyncio.gather(starting_a, starting_b)
        assert sorted((first.status_code, second.status_code)) == [200, 409]
        assert runtime.detect_environment.await_count == probes_before_start + 2
        assert brain.research_state == "RUNNING"
        assert (await client.post(f"{path}/profile", json={"profile": "legacy"}, headers=headers)).status_code == 409
    assert "synthetic-private-token" not in first.text
    assert runtime.detect_environment.await_count >= 1
    await brain.shutdown()




def _required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in research_engine.REQUIRED_ENV:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://invalid@127.0.0.1:1/morgoth_test")


def test_launcher_selects_project_before_server_import_and_rejects_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _project(tmp_path)
    _required_env(monkeypatch)
    monkeypatch.setenv("MORGOTH_DOMAIN", "weather")
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")
    current_project.cache_clear()
    research_engine.configure(tmp_path / "home", project.id, 8743)
    assert current_project().id == project.id
    assert os.environ.get("MORGOTH_DOMAIN") is None
    assert os.environ.get("UVICORN_HOST") is None
    with pytest.raises(ValueError, match="managed Project"):
        research_engine.configure(tmp_path / "home", "default", 8743)
    with pytest.raises(Exception):
        research_engine.configure(tmp_path / "home", "unknown", 8743)


def test_unknown_project_stops_before_server_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _required_env(monkeypatch)
    (tmp_path / "home").mkdir()
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"api.server", "api.token"}:
            pytest.fail("engine/token imported before Project validation")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    current_project.cache_clear()
    assert research_engine.main(["--home", str(tmp_path / "home"),
                                 "--project", "unknown", "--port", "8743"]) == 2


def test_launcher_binding_and_missing_names_only(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    project = _project(tmp_path)
    _required_env(monkeypatch)
    monkeypatch.delenv("SECRET_KEY")
    current_project.cache_clear()
    assert research_engine.main(["--home", str(tmp_path / "home"),
                                 "--project", project.id, "--port", "8743"]) == 2
    failure = capsys.readouterr().err
    assert "SECRET_KEY" in failure
    assert "POSTGRES_URL" not in failure
    monkeypatch.setenv("SECRET_KEY", "synthetic-private-secret")
    import uvicorn
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert research_engine.main(["--home", str(tmp_path / "home"),
                                 "--project", project.id, "--port", "8743"]) == 0
    app, kwargs = calls[0]
    assert app.state.desktop_runtime
    assert kwargs["host"] == "127.0.0.1" and kwargs["port"] == 8743
    assert kwargs["workers"] == 1 and not kwargs["proxy_headers"]
    app.state.desktop_runtime = False


@pytest.mark.asyncio
async def test_managed_config_does_not_read_checkout_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.config as config
    project = _project(tmp_path)
    _required_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("MAX_CONCURRENT_AGENTS", "1")
    monkeypatch.setenv("LOG_RETENTION_DAYS", "1")
    monkeypatch.setenv("LOG_LEVEL_THOUGHT", "false")
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MORGOTH_PROJECT", project.id)
    current_project.cache_clear()
    monkeypatch.setattr(config, "_load_environment", lambda: pytest.fail("managed .env read"))
    loaded = await config.load_config()
    assert loaded.data_dir == project.runtime_dir


@pytest.mark.asyncio
async def test_historical_main_port_and_immediate_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import main
    import uvicorn
    seen = {}
    monkeypatch.setattr(main, "_wire_log_rotation", lambda: None)
    monkeypatch.setattr(uvicorn, "Config", lambda _app, **kwargs: seen.update(kwargs))
    monkeypatch.setattr(uvicorn, "Server", lambda _cfg: SimpleNamespace(serve=AsyncMock()))
    await main.main()
    assert seen["host"] == "0.0.0.0" and seen["port"] == 8000
