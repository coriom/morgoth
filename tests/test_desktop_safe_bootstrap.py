"""Safe managed startup and Linux Project lease contracts."""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.brain import Brain, BrainInitializationMode
from core.project import current_project, load_projects
from core.project_engine_lease import ProjectEngineLease, ProjectEngineLeaseError
from core.runtime import RuntimeHome
from project_manager import ProjectManager
from scripts import research_engine


def _projects(tmp_path: Path):
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    for name, domain in (("crypto_lab", "crypto"), ("weather_lab", "weather")):
        manager.create_project(name, name=name, domain=domain)
    return home, {p.id: p for p in load_projects(home / "projects")}


def _brain() -> Brain:
    config = MagicMock(max_cycles_per_objective=5, ollama_primary_model="primary", ollama_agent_model="agent")
    llm = MagicMock()
    llm.list_models = AsyncMock(return_value=[SimpleNamespace(name="primary"), SimpleNamespace(name="agent")])
    llm.chat = AsyncMock(side_effect=AssertionError("inference during PAUSED"))
    llm.close = AsyncMock()
    memory = MagicMock()
    memory.initialize = AsyncMock()
    memory.timeout_stale_objectives = AsyncMock(return_value=[])
    memory.reclaim_orphan_objectives = AsyncMock(return_value=[])
    memory.close = AsyncMock()
    episodic = MagicMock()
    episodic.initialize = AsyncMock()
    scheduler = MagicMock()
    scheduler.initialize = AsyncMock()
    scheduler.list_tasks = AsyncMock(return_value=[])
    scheduler.schedule = AsyncMock()
    router = MagicMock()
    router.execute_tool = AsyncMock(side_effect=AssertionError("research tool during PAUSED"))
    router.close = AsyncMock()
    notifier = MagicMock()
    notifier.send = AsyncMock(side_effect=AssertionError("notification during PAUSED"))
    notifier.close = AsyncMock()
    brain = Brain(config, llm, memory, episodic, scheduler, router, MagicMock(), notifier)
    brain._test_parts = (llm, memory, scheduler, router, notifier)
    return brain


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["crypto", "weather"])
async def test_empty_workspace_desktop_paused_is_structural(tmp_path: Path, monkeypatch, domain: str) -> None:
    home, projects = _projects(tmp_path)
    project = next(p for p in projects.values() if p.domain == domain and not p.is_legacy)
    assert not any(project.workspace_root.iterdir())
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    monkeypatch.setenv("MORGOTH_PROJECT", project.id)
    current_project.cache_clear()
    brain = _brain()
    brain.awaken = AsyncMock(side_effect=AssertionError("legacy awakening during PAUSED"))
    brain.run_autonomous_cycle = AsyncMock(side_effect=AssertionError("autonomy during PAUSED"))
    llm, memory, scheduler, router, notifier = brain._test_parts
    result = await brain.initialize(mode=BrainInitializationMode.DESKTOP_PAUSED)
    assert result["status"] == "READY"
    assert brain.initialized and brain.awakening_ready and brain.research_state == "PAUSED"
    assert not brain.autonomous_task_alive
    llm.list_models.assert_awaited_once()
    llm.chat.assert_not_awaited()
    memory.timeout_stale_objectives.assert_not_awaited()
    memory.reclaim_orphan_objectives.assert_not_awaited()
    scheduler.list_tasks.assert_not_awaited()
    scheduler.schedule.assert_not_awaited()
    router.execute_tool.assert_not_awaited()
    notifier.send.assert_not_awaited()
    brain.awaken.assert_not_awaited()
    await brain.shutdown()


@pytest.mark.asyncio
async def test_desktop_not_ready_remains_inspectable_without_research() -> None:
    brain = _brain()
    brain._llm_client.list_models = AsyncMock(side_effect=TimeoutError)
    result = await brain.initialize(mode=BrainInitializationMode.DESKTOP_PAUSED)
    assert result["status"] == "MISSING_DEPENDENCIES"
    assert brain.research_state == "NOT_READY" and brain.initialized
    with pytest.raises(RuntimeError, match="not ready"):
        await brain.start_autonomous_research()
    await brain.shutdown()


@pytest.mark.asyncio
async def test_desktop_start_prepares_recurring_once_before_loop(monkeypatch) -> None:
    brain = _brain()
    await brain.initialize(mode=BrainInitializationMode.DESKTOP_PAUSED)
    calls = []
    async def recover():
        calls.append("recover")
    async def recurring():
        calls.append("recurring")
    async def loop():
        calls.append("loop")
        await asyncio.Event().wait()
    monkeypatch.setattr(brain, "_recover_objectives_at_start", recover)
    monkeypatch.setattr(brain, "ensure_recurring_tasks", recurring)
    monkeypatch.setattr(brain, "run_autonomous_cycle", loop)
    assert await brain.start_autonomous_research()
    assert not await brain.start_autonomous_research()
    await asyncio.sleep(0)
    assert calls == ["recover", "recurring", "loop"]
    await brain.shutdown()


@pytest.mark.asyncio
async def test_legacy_still_awakes_recovers_and_schedules() -> None:
    brain = _brain()
    brain.awaken = AsyncMock(return_value={"status": "READY"})
    brain.run_autonomous_cycle = AsyncMock(side_effect=lambda: asyncio.Event().wait())
    await brain.initialize()
    _, memory, _, _, _ = brain._test_parts
    brain.awaken.assert_awaited_once()
    memory.timeout_stale_objectives.assert_awaited_once()
    memory.reclaim_orphan_objectives.assert_awaited_once()
    assert brain.autonomous_task_alive
    await brain.shutdown()


@pytest.mark.asyncio
async def test_legacy_full_awakening_still_runs_tools_warmup_and_recurring(monkeypatch) -> None:
    brain = _brain()
    llm, _, scheduler, router, _ = brain._test_parts
    llm.health_check = AsyncMock(return_value=True)
    llm.ensure_models_available = AsyncMock(return_value={"primary": True, "agent": True})
    llm.chat = AsyncMock(return_value=SimpleNamespace(model="primary"))
    router.execute_tool = AsyncMock(return_value={"success": True})
    brain.log = AsyncMock()
    from core import brain as brain_module
    monkeypatch.setattr(brain_module, "current_domain", lambda: SimpleNamespace(
        test_bootstrap_tool_defaults={},
        bootstrap_recurring_task={"description": "synthetic recurring", "cron": "0 * * * *"},
    ))
    result = await brain.awaken()
    assert result["status"] == "READY"
    assert llm.chat.await_count == 2
    assert {"web_search", "execute_python", "read_file", "write_file", "get_news", "notify", "remember", "recall"} <= {
        call.args[0] for call in router.execute_tool.await_args_list
    }
    scheduler.schedule.assert_awaited_once()
    await brain.shutdown()


def test_launcher_acquires_lease_before_server_import() -> None:
    source = inspect.getsource(research_engine.main)
    assert source.index("with ProjectEngineLease(current_project())") < source.index("from api.server import app")
    assert source.index("with ProjectEngineLease(current_project())") < source.index("uvicorn.run(")


@pytest.mark.asyncio
async def test_managed_historical_entrypoint_cannot_bypass_lease(tmp_path: Path, monkeypatch) -> None:
    import main
    home, projects = _projects(tmp_path)
    project = projects["crypto_lab"]
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    monkeypatch.setenv("MORGOTH_PROJECT", project.id)
    current_project.cache_clear()
    served = AsyncMock()
    monkeypatch.setattr(main, "_serve", served)
    with ProjectEngineLease(project):
        with pytest.raises(SystemExit) as caught:
            await main.main()
        assert caught.value.code == 3
        served.assert_not_awaited()
    await main.main()
    served.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_managed_asgi_lifespan_requires_process_lease(tmp_path: Path, monkeypatch) -> None:
    from api import server
    from fastapi import FastAPI
    home, projects = _projects(tmp_path)
    project = projects["crypto_lab"]
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    monkeypatch.setenv("MORGOTH_PROJECT", project.id)
    current_project.cache_clear()
    app = FastAPI()
    app.state.desktop_runtime = True
    monkeypatch.setattr(server, "load_config", AsyncMock(side_effect=AssertionError("past lease guard")))
    with pytest.raises(ProjectEngineLeaseError, match="LEASE_UNAVAILABLE"):
        async with server.lifespan(app):
            pass
    with ProjectEngineLease(project) as lease:
        app.state.project_engine_lease = lease
        with pytest.raises(AssertionError, match="past lease guard"):
            async with server.lifespan(app):
                pass
    assert not lease.held_for(project)


def test_project_engine_lease_exclusive_reusable_and_distinct(tmp_path: Path) -> None:
    _, projects = _projects(tmp_path)
    a, b = projects["crypto_lab"], projects["weather_lab"]
    with ProjectEngineLease(a):
        with pytest.raises(ProjectEngineLeaseError, match="PROJECT_ENGINE_ALREADY_ACTIVE"):
            ProjectEngineLease(a).acquire()
        with ProjectEngineLease(b):
            assert (b.runtime_dir / ".research-engine.lock").is_file()
    with ProjectEngineLease(a):
        pass
    assert (a.runtime_dir / ".research-engine.lock").stat().st_mode & 0o077 == 0


def test_project_engine_lease_rejects_unsafe_files(tmp_path: Path) -> None:
    _, projects = _projects(tmp_path)
    project = projects["crypto_lab"]
    path = project.runtime_dir / ".research-engine.lock"
    path.symlink_to(tmp_path / "target")
    with pytest.raises(ProjectEngineLeaseError, match="LEASE_UNAVAILABLE"):
        ProjectEngineLease(project).acquire()
    path.unlink()
    path.mkdir()
    with pytest.raises(ProjectEngineLeaseError, match="LEASE_UNAVAILABLE"):
        ProjectEngineLease(project).acquire()
    path.rmdir()
    path.touch(mode=0o644)
    path.chmod(0o644)
    with pytest.raises(ProjectEngineLeaseError, match="LEASE_UNAVAILABLE"):
        ProjectEngineLease(project).acquire()
    path.unlink()
    project.runtime_dir.chmod(0o755)
    with pytest.raises(ProjectEngineLeaseError, match="LEASE_UNAVAILABLE"):
        ProjectEngineLease(project).acquire()


def test_kernel_releases_lease_when_holder_exits(tmp_path: Path) -> None:
    home, projects = _projects(tmp_path)
    child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "HOME": str(tmp_path), "MORGOTH_HOME": str(home),
                 "MORGOTH_PROJECT": "crypto_lab", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    child = subprocess.Popen(
        [sys.executable, "-B", "-c",
         "import sys; from core.project import current_project; from core.project_engine_lease import ProjectEngineLease; "
         "lease=ProjectEngineLease(current_project()).acquire(); print('LOCKED', flush=True); sys.stdin.readline()"],
        env=child_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, cwd=Path(__file__).resolve().parents[1],
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "LOCKED"
        with pytest.raises(ProjectEngineLeaseError, match="PROJECT_ENGINE_ALREADY_ACTIVE"):
            ProjectEngineLease(projects["crypto_lab"]).acquire()
        with ProjectEngineLease(projects["weather_lab"]):
            pass
    finally:
        child.terminate()
        child.wait(timeout=5)
        for stream in (child.stdin, child.stdout):
            if stream is not None:
                stream.close()
    with ProjectEngineLease(projects["crypto_lab"]):
        pass
