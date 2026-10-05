"""Actual research-app ASGI boundary with inert route collaborators."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from api import desktop_authority, server, token
from api.routes import objectives, proposals, runtime
from api.ws.handler import WebSocketManager
from core.project import load_projects
from core.runtime import RuntimeHome
from project_manager import ProjectManager
from scripts import research_engine


PORT = 8743
SECRET = "synthetic-private-project-token"
HEADERS = {"X-Morgoth-Token": SECRET}


@pytest.fixture
def route_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Use the mounted production app and replace only side-effecting state."""
    app = server.app
    monkeypatch.setattr(app.state, "desktop_runtime", True)
    monkeypatch.setattr(app.state, "desktop_expected_port", PORT)
    monkeypatch.setattr(desktop_authority, "read_ui_token", lambda: SECRET)
    monkeypatch.setattr(proposals, "ensure_ui_token", lambda: SECRET)
    brain = MagicMock()
    brain.get_status = AsyncMock(return_value={"synthetic": True})
    brain.process_message = AsyncMock(return_value=SimpleNamespace(model_dump=lambda: {"content": "synthetic"}))
    brain.initialized = True
    brain.awakening_ready = True
    brain.research_state = "PAUSED"
    brain.autonomous_task_alive = False
    monkeypatch.setattr(app.state, "brain", brain, raising=False)
    manager = MagicMock()
    manager.list_objectives = AsyncMock(return_value=[])
    manager.create_objective = AsyncMock(side_effect=lambda item: item)
    monkeypatch.setattr(objectives, "_build_manager", lambda _request: manager)
    router = MagicMock()
    router.policy.sources = set()
    router.policy.chat = set()
    router._tools = {}
    monkeypatch.setattr(app.state, "tool_router", router, raising=False)
    store = MagicMock()
    store.list_recent = AsyncMock(return_value=[])
    store.resolve_id = AsyncMock(return_value="synthetic-proposal-id")
    store.get = AsyncMock(return_value=None)
    store.update_status = AsyncMock()
    monkeypatch.setattr(proposals.P, "ProposalStore", lambda _memory: store)
    monkeypatch.setattr(app.state, "persistent_memory", MagicMock(), raising=False)
    monkeypatch.setattr(runtime, "current_project", lambda: SimpleNamespace(id="synthetic", domain="crypto"))
    monkeypatch.setattr(runtime, "_profiles", AsyncMock(return_value=runtime.ProfilesResponse(
        current="claude", recommended="claude", profiles=[runtime.ProfileView(
            id="claude", status="READY", reason="synthetic", providers={},
        )],
    )))
    monkeypatch.setattr(app.state, "code_sha", "a" * 40, raising=False)
    monkeypatch.setattr(app.state, "websocket_manager", WebSocketManager(), raising=False)
    return {"app": app, "brain": brain, "manager": manager, "store": store}


def _request_cases() -> tuple[tuple[str, str, dict | None], ...]:
    return (
        ("GET", "/api/runtime/v1/status", None),
        ("GET", "/api/brain/status", None),
        ("GET", "/api/tools", None),
        ("GET", "/api/objectives", None),
        ("POST", "/api/chat", {"content": "synthetic"}),
        ("POST", "/api/objectives", {"title": "Synthetic", "description": "Fixture", "category": "research"}),
        ("GET", "/api/proposals", None),
        ("POST", "/api/proposals/synthetic/reject", {"reason": "synthetic"}),
        ("GET", "/docs", None),
        ("GET", "/redoc", None),
        ("GET", "/openapi.json", None),
    )


@pytest.mark.asyncio
async def test_every_mounted_http_surface_requires_desktop_token(route_fixture: dict[str, object]) -> None:
    app = route_fixture["app"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=f"http://127.0.0.1:{PORT}") as client:
        for method, path, body in _request_cases():
            for kwargs in ({}, {"headers": {"X-Morgoth-Token": "wrong"}},
                           {"params": {"token": SECRET}}, {"headers": {"Cookie": f"token={SECRET}"}}):
                response = await client.request(method, path, json=body, **kwargs)
                assert response.status_code == 401, (method, path, response.status_code)
                assert SECRET not in response.text
            response = await client.request(method, path, json=body, headers=HEADERS)
            assert response.status_code in {200, 404}, (method, path, response.status_code)
    route_fixture["brain"].process_message.assert_awaited_once()
    route_fixture["manager"].create_objective.assert_awaited_once()
    route_fixture["store"].update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_desktop_host_origin_header_shape_and_cors(route_fixture: dict[str, object]) -> None:
    app = route_fixture["app"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=f"http://127.0.0.1:{PORT}") as client:
        for origin in ("null", "http://localhost:3000", ""):
            response = await client.get("/api/brain/status", headers={**HEADERS, "Origin": origin})
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
        for host in ("localhost:8743", "evil.example:8743", "127.0.0.1:8000"):
            response = await client.get("/api/brain/status", headers={**HEADERS, "Host": host})
            assert response.status_code == 403
        duplicated = await client.get("/api/brain/status", headers=[
            ("Host", f"127.0.0.1:{PORT}"), ("X-Morgoth-Token", SECRET),
            ("X-Morgoth-Token", SECRET),
        ])
        assert duplicated.status_code == 401
        missing_port = await client.get("/api/brain/status", headers=HEADERS)
        assert missing_port.status_code == 200
        preflight = await client.options("/api/chat", headers={"Origin": "http://localhost:3000"})
        assert preflight.status_code == 403
        assert "access-control-allow-origin" not in preflight.headers
    assert route_fixture["brain"].get_status.await_count == 1


@pytest.mark.asyncio
async def test_missing_token_file_or_trusted_port_fails_closed(
    route_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = route_fixture["app"]
    monkeypatch.setattr(desktop_authority, "read_ui_token", lambda: None)
    monkeypatch.setattr(proposals, "ensure_ui_token", lambda: pytest.fail("request created token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url=f"http://127.0.0.1:{PORT}") as client:
        assert (await client.get("/api/brain/status", headers=HEADERS)).status_code == 401
        monkeypatch.setattr(desktop_authority, "read_ui_token", lambda: SECRET)
        monkeypatch.setattr(app.state, "desktop_expected_port", None)
        assert (await client.get("/api/brain/status", headers=HEADERS)).status_code == 403
    route_fixture["brain"].get_status.assert_not_awaited()


async def _websocket_exchange(app: object, *, query: bytes = b"") -> list[dict]:
    sent: list[dict] = []
    received = iter(({"type": "websocket.connect"}, {"type": "websocket.disconnect", "code": 1000}))

    async def receive() -> dict:
        return next(received)

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "websocket", "path": "/ws/chat", "raw_path": b"/ws/chat",
             "root_path": "", "scheme": "ws", "query_string": query,
             "headers": [(b"host", f"127.0.0.1:{PORT}".encode())],
             "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", PORT),
             "subprotocols": [], "asgi": {"version": "3.0", "spec_version": "2.3"}}
    await asyncio.wait_for(app(scope, receive, send), timeout=3)
    return sent


@pytest.mark.asyncio
async def test_desktop_websocket_rejected_before_handler_legacy_still_accepts(
    route_fixture: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = route_fixture["app"]
    for query in (b"", f"token={SECRET}".encode()):
        assert await _websocket_exchange(app, query=query) == [{"type": "websocket.close", "code": 1008}]
    assert app.state.websocket_manager._connections == []
    monkeypatch.setattr(app.state, "desktop_runtime", False)
    sent = await _websocket_exchange(app)
    assert sent == [{"type": "websocket.accept", "subprotocol": None, "headers": []}]
    assert app.state.websocket_manager._connections == []


@pytest.mark.asyncio
async def test_legacy_routes_and_cors_remain_unchanged(route_fixture: dict[str, object],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    app = route_fixture["app"]
    monkeypatch.setattr(app.state, "desktop_runtime", False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://localhost:8000") as client:
        response = await client.get("/api/brain/status", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
        assert (await client.post("/api/chat", json={"content": "synthetic"})).status_code == 200
        assert (await client.post("/api/proposals/synthetic/reject", json={"reason": "fixture"})).status_code == 401
        assert (await client.get("/openapi.json")).status_code == 200


def test_token_path_and_startup_order_are_existing_contract(tmp_path: Path) -> None:
    manager = ProjectManager(RuntimeHome(tmp_path / "home", tmp_path / "home/projects"))
    manager.create_project("desktop_fixture", name="Desktop fixture", domain="crypto")
    project = next(p for p in load_projects(tmp_path / "home/projects") if p.id == "desktop_fixture")
    assert project.ui_token_dir == project.runtime_dir / "auth"
    path = project.ui_token_dir / "ui_token"
    assert token.read_ui_token(path) is None
    first = token.ensure_ui_token(path)
    assert path.is_file() and token.read_ui_token(path) == first
    assert token.ensure_ui_token(path) == first
    source = inspect.getsource(server.lifespan)
    assert source.index("await brain_service.initialize") < source.index("ensure_ui_token()") < source.index("yield")
    assert "ensure_ui_token" not in inspect.getsource(desktop_authority.DesktopAuthorityMiddleware)


def test_launcher_sets_expected_port_before_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ProjectManager(RuntimeHome(tmp_path / "home", tmp_path / "home/projects"))
    manager.create_project("desktop_fixture", name="Desktop fixture", domain="crypto")
    for name in research_engine.REQUIRED_ENV:
        monkeypatch.setenv(name, "synthetic")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://invalid@127.0.0.1:1/morgoth_test")
    from core.project import current_project
    current_project.cache_clear()
    import uvicorn
    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda _app, **options: calls.append(options))
    assert research_engine.main(["--home", str(tmp_path / "home"), "--project", "desktop_fixture",
                                 "--port", str(PORT)]) == 0
    assert server.app.state.desktop_runtime is True
    assert server.app.state.desktop_expected_port == PORT
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["proxy_headers"] is False
    server.app.state.desktop_runtime = False
    server.app.state.desktop_expected_port = None
    current_project.cache_clear()


def test_fresh_process_binds_only_selected_project_token(tmp_path: Path) -> None:
    manager = ProjectManager(RuntimeHome(tmp_path / "home", tmp_path / "home/projects"))
    manager.create_project("desktop_fixture", name="Desktop fixture", domain="crypto")
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "fake-home"),
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "LANG": "C.UTF-8"}
    environment.update({name: "synthetic" for name in research_engine.REQUIRED_ENV})
    environment["POSTGRES_URL"] = "postgresql://invalid@127.0.0.1:1/morgoth_test"
    code = (
        "import sys; from pathlib import Path; "
        "from scripts.research_engine import configure; "
        "configure(Path(sys.argv[1]), 'desktop_fixture', 8743); "
        "from api.token import TOKEN_PATH, ensure_ui_token; "
        "from core.project import current_project; "
        "assert TOKEN_PATH == current_project().runtime_dir / 'auth' / 'ui_token'; "
        "ensure_ui_token(); assert TOKEN_PATH.is_file(); "
        "print('SELECTED_TOKEN_OK')"
    )
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "home")],
                            cwd=Path(__file__).resolve().parents[1], env=environment,
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr[-300:]
    assert result.stdout.strip() == "SELECTED_TOKEN_OK"
    assert not (tmp_path / "fake-home/.morgoth/ui_token").exists()
