"""Offline contract and authority tests for the independent management app."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import httpx
import pytest

from core.project import current_project, load_projects
from core.runtime import RuntimeHome
from api.management_app import API_PREFIX, ManagementSecurity, create_management_app
from project_manager import ProjectManager, ProjectManagerError
from scripts import management_api as launcher


@pytest.fixture
def setup(tmp_path: Path) -> tuple[ProjectManager, str, object, Path]:
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    token = secrets.token_urlsafe(32)
    return manager, token, create_management_app(manager, ManagementSecurity(token, 8765)), home


async def _request(app: object, token: str | None, method: str, path: str,
                   **kwargs: object) -> httpx.Response:
    headers = dict(kwargs.pop("headers", {}) or {})
    if token is not None:
        headers["X-Morgoth-Management-Token"] = token
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8765") as client:
            return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_full_round_trip_and_same_domain_isolation(setup: tuple) -> None:
    manager, token, app, home = setup
    prior_selection = current_project.cache_info().currsize
    empty = await _request(app, token, "GET", f"{API_PREFIX}/projects")
    assert empty.status_code == 200
    assert not home.exists()
    assert [p["id"] for p in empty.json()["projects"]] == ["default"]
    domains = await _request(app, token, "GET", f"{API_PREFIX}/domains")
    assert domains.status_code == 200
    assert {d["id"] for d in domains.json()["domains"] if d["configuration_valid"]} >= {"crypto", "weather"}
    projects = (("research_a", "crypto"), ("research_b", "crypto"), ("weather_lab", "weather"))
    for project_id, domain in projects:
        created = await _request(app, token, "POST", f"{API_PREFIX}/projects",
                                 json={"id": project_id, "name": project_id.upper(), "domain": domain})
        assert created.status_code == 201, created.text
        result = created.json()
        assert result["created"] and result["durability_confirmed"]
        assert not result["engine_started"] and not result["storage_initialized"]
        assert not result["project"]["runtime_checked"]
        for suffix in ("", "/validation"):
            found = await _request(app, token, "GET", f"{API_PREFIX}/projects/{project_id}{suffix}")
            assert found.status_code == 200
        assert not (home / "projects" / project_id / "runtime" / "llm_profile.json").exists()
    actual = {p.id: p for p in load_projects(home / "projects")}
    assert actual["research_a"].domain == actual["research_b"].domain == "crypto"
    assert len({actual[p].postgres_schema for p, _ in projects}) == 3
    assert len({actual[p].chroma_prefix for p, _ in projects}) == 3
    assert len({str(actual[p].vault_dir) for p, _ in projects}) == 3
    assert current_project.cache_info().currsize == prior_selection
    dup = await _request(app, token, "POST", f"{API_PREFIX}/projects",
                         json={"id": "research_a", "name": "Overwrite", "domain": "weather"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "ALREADY_EXISTS"
    assert manager.get_project("research_a")["domain"] == "crypto"


@pytest.mark.asyncio
async def test_auth_host_origin_and_schema_are_guarded(setup: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, token, app, home = setup
    monkeypatch.setattr(manager, "list_projects", lambda: pytest.fail("catalog read before authentication"))
    for supplied in (None, "wrong"):
        response = await _request(app, supplied, "GET", f"{API_PREFIX}/projects")
        assert response.status_code == 401
    assert (await _request(app, None, "GET", f"{API_PREFIX}/status?token={token}")).status_code == 401
    assert (await _request(app, None, "GET", f"{API_PREFIX}/status",
                           headers={"Cookie": f"management_token={token}"})).status_code == 401
    assert (await _request(app, None, "POST", f"{API_PREFIX}/projects",
                           json={"id": "unauth", "name": "A", "domain": "crypto"})).status_code == 401
    for origin in ("null", "http://127.0.0.1:8765", ""):
        assert (await _request(app, token, "GET", f"{API_PREFIX}/status",
                               headers={"Origin": origin})).status_code == 403
    assert (await _request(app, token, "GET", f"{API_PREFIX}/status",
                           headers={"Host": "evil.example"})).status_code == 400
    for path in ("/openapi.json", "/docs", "/redoc"):
        assert (await _request(app, None, "GET", path)).status_code == 401
        assert (await _request(app, token, "GET", path)).status_code == 404
    assert not home.exists()


@pytest.mark.asyncio
async def test_strict_body_unknowns_and_safe_errors(setup: tuple) -> None:
    _manager, token, app, home = setup
    path = f"{API_PREFIX}/projects"
    for payload in (
        {"id": "bad", "name": "A", "domain": "crypto", "workspace_root": "/tmp/evil"},
        {"id": "bad", "name": "A", "domain": "crypto", "profile": "codex"},
        {"id": "bad", "name": "A", "domain": "crypto", "postgres_schema": "public"},
    ):
        response = await _request(app, token, "POST", path, json=payload)
        assert response.status_code == 422 and response.json()["error"]["code"] == "INVALID_REQUEST"
    assert (await _request(app, token, "POST", path, content=b"{}",
                           headers={"Content-Type": "text/plain"})).status_code == 415
    assert (await _request(app, token, "POST", path, content=b"{" ,
                           headers={"Content-Type": "application/json"})).status_code == 422
    assert (await _request(app, token, "POST", path, content=b" " * 8193,
                           headers={"Content-Type": "application/json"})).status_code == 413
    assert (await _request(app, token, "GET", f"{API_PREFIX}/status",
                           content=b"unexpected")).status_code == 400
    async def stream() -> object:
        yield b" " * 4096
        yield b" " * 4097
    assert (await _request(app, token, "POST", path, content=stream(),
                           headers={"Content-Type": "application/json"})).status_code == 413
    assert (await _request(app, token, "POST", path,
                           json={"id": "bad", "name": "A", "domain": "missing"})).status_code == 404
    assert (await _request(app, token, "GET", f"{path}/missing")).status_code == 404
    assert not home.exists()


@pytest.mark.asyncio
async def test_invalid_installed_pack_is_explicit(setup: tuple, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    import core.domain as domain_module
    import api.management_app as api_module
    _manager, token, app, _home = setup
    root = tmp_path / "packs"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "domain.yaml").write_text("rail: [not-a-mapping]\n")
    monkeypatch.setattr(domain_module, "_DOMAINS_ROOT", root)
    monkeypatch.setattr(api_module, "_DOMAINS_ROOT", root)
    response = await _request(app, token, "GET", f"{API_PREFIX}/domains")
    assert response.status_code == 200
    assert response.json()["domains"] == [{"id": "broken", "tagline": None,
                                            "configuration_valid": False,
                                            "diagnostic": "INVALID_DOMAIN_PACK"}]
    (root / "broken" / "domain.yaml").write_text("rail: [unterminated\n")
    malformed = await _request(app, token, "GET", f"{API_PREFIX}/domains")
    assert malformed.status_code == 200
    assert malformed.json()["domains"][0]["diagnostic"] == "INVALID_DOMAIN_PACK"


@pytest.mark.asyncio
async def test_publication_failure_is_not_discoverable(setup: tuple,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    manager, token, app, home = setup
    def fail(_fd: int) -> None:
        raise OSError("synthetic private failure")
    monkeypatch.setattr(ProjectManager, "_publish_manifest", staticmethod(fail))
    response = await _request(app, token, "POST", f"{API_PREFIX}/projects",
                              json={"id": "unpublished", "name": "U", "domain": "crypto"})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "PUBLICATION_FAILED"
    assert "synthetic private failure" not in response.text
    assert not (home / "projects" / "unpublished" / "project.yaml").exists()
    assert [p["id"] for p in manager.list_projects()["projects"]] == ["default"]


@pytest.mark.asyncio
async def test_concurrent_conflict_and_retry(setup: tuple) -> None:
    manager, token, app, _home = setup
    body = {"id": "parallel", "name": "Parallel", "domain": "crypto"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://127.0.0.1:8765",
                                    headers={"X-Morgoth-Management-Token": token}) as client:
            a, b = await asyncio.gather(client.post(f"{API_PREFIX}/projects", json=body),
                                        client.post(f"{API_PREFIX}/projects", json=body))
    assert sorted((a.status_code, b.status_code)) == [201, 409]
    assert manager.get_project("parallel")["name"] == "Parallel"
    retry = await _request(app, token, "POST", f"{API_PREFIX}/projects", json=body)
    assert retry.status_code == 409


@pytest.mark.asyncio
async def test_manager_errors_and_false_durability_remain_distinct(setup: tuple,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    manager, token, app, _home = setup
    original = manager.create_project
    mappings = {"INVALID_REQUEST": 422, "UNKNOWN_DOMAIN": 404, "ALREADY_EXISTS": 409,
                "INVALID_CATALOG": 409, "NAMESPACE_CONFLICT": 409, "PATH_CONFLICT": 409,
                "PUBLICATION_FAILED": 500}
    for code, status in mappings.items():
        def fail(*_args: object, **_kwargs: object) -> None:
            raise ProjectManagerError(code, "safe diagnostic")
        monkeypatch.setattr(manager, "create_project", fail)
        response = await _request(app, token, "POST", f"{API_PREFIX}/projects",
                                  json={"id": "x", "name": "X", "domain": "crypto"})
        assert response.status_code == status and response.json()["error"]["code"] == code
    monkeypatch.setattr(manager, "create_project", original)
    def published(*args: object, **kwargs: object) -> dict:
        result = original(*args, **kwargs)
        result["durability_confirmed"] = False
        return result
    monkeypatch.setattr(manager, "create_project", published)
    created = await _request(app, token, "POST", f"{API_PREFIX}/projects",
                             json={"id": "durability", "name": "D", "domain": "crypto"})
    assert created.status_code == 201 and created.json()["durability_confirmed"] is False
    assert manager.get_project("durability")["id"] == "durability"


def test_openapi_contract_and_no_engine_routes(setup: tuple) -> None:
    _manager, token, app, _home = setup
    schema = app.openapi()
    assert token not in json.dumps(schema)
    assert schema["security"] == [{"ManagementToken": []}]
    expected = {"management_status_v1", "management_list_domains_v1",
                "management_list_projects_v1", "management_get_project_v1",
                "management_validate_project_v1", "management_create_project_v1"}
    assert {operation["operationId"] for path in schema["paths"].values()
            for operation in path.values()} == expected
    assert set(schema["paths"]) == {f"{API_PREFIX}/status", f"{API_PREFIX}/domains",
                                    f"{API_PREFIX}/projects", f"{API_PREFIX}/projects/{{project_id}}",
                                    f"{API_PREFIX}/projects/{{project_id}}/validation"}
    assert schema["components"]["schemas"]["CreateProjectRequest"]["additionalProperties"] is False
    from api.management_app import __file__ as module_path
    artifact = Path(module_path).parent.parent / "docs" / "management_api_v1.openapi.json"
    assert artifact.read_text() == json.dumps(schema, sort_keys=True, indent=2) + "\n"


def test_private_token_loader_and_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = secrets.token_urlsafe(32)
    path = tmp_path / "token"
    path.write_text(token + "\n")
    path.chmod(0o600)
    assert launcher.read_private_token(path) == token
    calls = []
    monkeypatch.setattr(launcher.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("UVICORN_HOST", "0.0.0.0")
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    assert launcher.main(["--home", str(tmp_path / "home"), "--token-file", str(path), "--port", "8765"]) == 0
    assert "UVICORN_HOST" not in os.environ and "FORWARDED_ALLOW_IPS" not in os.environ
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8765, "workers": 1, "reload": False,
                           "proxy_headers": False, "forwarded_allow_ips": "", "access_log": False,
                           "server_header": False, "date_header": False}
    assert not (tmp_path / "home").exists()
    path.chmod(0o644)
    with pytest.raises(ValueError):
        launcher.read_private_token(path)
    path.chmod(0o600)
    symlink = tmp_path / "linked"
    symlink.symlink_to(path)
    with pytest.raises(ValueError):
        launcher.read_private_token(symlink)
    path.write_text("")
    with pytest.raises(ValueError):
        launcher.read_private_token(path)
    with pytest.raises(ValueError):
        ManagementSecurity("short", 8765)


@pytest.mark.asyncio
async def test_unexpected_error_is_redacted(setup: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, token, app, _home = setup
    def fail() -> None:
        raise RuntimeError("synthetic credential-looking marker")
    monkeypatch.setattr(manager, "list_projects", fail)
    response = await _request(app, token, "GET", f"{API_PREFIX}/projects")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "synthetic credential-looking marker" not in response.text


@pytest.mark.asyncio
async def test_http_and_cli_share_manager_data(setup: tuple, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.projects_cli import main as cli_main
    _manager, token, app, home = setup
    created = await _request(app, token, "POST", f"{API_PREFIX}/projects",
                             json={"id": "cli_agree", "name": "Agreement", "domain": "crypto"})
    assert created.status_code == 201
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    assert cli_main(["show", "cli_agree", "--json"]) == 0
    cli_view = json.loads(capsys.readouterr().out)
    http_view = (await _request(app, token, "GET", f"{API_PREFIX}/projects/cli_agree")).json()
    assert cli_view == http_view


def test_factory_never_imports_engine_server(setup: tuple) -> None:
    from api.management_app import __file__ as module_path
    root = Path(module_path).parent.parent
    result = subprocess.run(
        [sys.executable, "-B", "-c",
         "import sys, api.management_app; assert 'api.server' not in sys.modules; "
         "assert 'memory.persistent' not in sys.modules; "
         "assert 'core.brain' not in sys.modules"],
        cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root), "PYTHONPATH": str(root)},
        capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
