"""Real vault bytes through the authenticated Management read contract."""
from pathlib import Path
import secrets

import httpx
import pytest

from agents.desktop_knowledge_read import PREFIX, KnowledgeUnavailable, install_desktop_knowledge_routes, read_vault
from api.management_app import ManagementSecurity, create_management_app
from core.runtime import RuntimeHome
from project_manager import ProjectManager


@pytest.mark.asyncio
async def test_project_vault_auth_links_search_and_scope(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("alpha", name="Alpha", domain="crypto")
    manager.create_project("beta", name="Beta", domain="crypto")
    a = home / "projects" / "alpha" / "vault"
    b = home / "projects" / "beta" / "vault"
    assert a.is_dir() and b.is_dir()
    (a / "index.md").write_text("# Index\n\n[[topic|Sujet]] et [[absent|Absent]]", encoding="utf-8")
    (a / "topic.md").write_text("# Sujet\n\nUne connaissance persistée.", encoding="utf-8")
    (b / "secret.md").write_text("# Autre Project", encoding="utf-8")
    token = secrets.token_urlsafe(32)
    app = create_management_app(manager, ManagementSecurity(token, 8765))
    install_desktop_knowledge_routes(app, manager)
    base = f"{PREFIX}/projects/alpha/documents"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        assert (await client.get(base)).status_code == 401
        headers = {"X-Morgoth-Management-Token": token}
        assert (await client.get(base, headers={**headers, "Origin": "null"})).status_code == 403
        response = await client.get(base, headers=headers)
        assert response.status_code == 200
        page = response.json()
        assert page["schema_version"] == 1 and page["source"] == "PROJECT_MARKDOWN_VAULT"
        assert len(page["documents"]) == 2
        index, topic = page["documents"]
        assert index["title"] == "Index" and "content" in index and index["content"] is None
        assert index["links"] == [{"label": "Sujet", "target_id": topic["document_id"]}, {"label": "Absent", "target_id": None}]
        detail = (await client.get(base + "/" + index["document_id"], headers=headers)).json()
        assert detail["schema_version"] == 1 and detail["content"].startswith("# Index")
        assert (await client.get(base + "?query=connaissance", headers=headers)).json()["documents"][0]["title"] == "Sujet"
        assert (await client.get(base + "?limit=1", headers=headers)).json()["next_offset"] == 1
        assert (await client.get(base + "?offset=1&limit=1", headers=headers)).json()["documents"][0]["title"] == "Sujet"
        assert (await client.get(f"{PREFIX}/projects/beta/documents/{index['document_id']}", headers=headers)).status_code == 404
        assert (await client.get(f"{PREFIX}/projects/beta/documents", headers=headers)).json()["documents"][0]["title"] == "Autre Project"
        assert (await client.get(f"{PREFIX}/projects/missing/documents", headers=headers)).status_code == 404
        assert (await client.post(base, headers={**headers, "Content-Type": "application/json"})).status_code == 405
        assert (await client.get(base + "?limit=21", headers=headers)).status_code == 422
        (a / "bad.md").symlink_to(b / "secret.md")
        assert (await client.get(base, headers=headers)).status_code == 409
        (a / "bad.md").unlink()
        (a / "huge.md").write_bytes(b"x" * 16_385)
        assert (await client.get(base, headers=headers)).json()["error"]["code"] == "SOURCE_TOO_LARGE"
        (a / "huge.md").unlink()
        (a / "invalid.md").write_bytes(b"\xff")
        assert (await client.get(base, headers=headers)).json()["error"]["code"] == "INCOMPATIBLE_SOURCE"


@pytest.mark.asyncio
async def test_empty_vault_distinct_from_missing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("alpha", name="Alpha", domain="crypto")
    token = secrets.token_urlsafe(32)
    app = create_management_app(manager, ManagementSecurity(token, 8765))
    install_desktop_knowledge_routes(app, manager)
    headers = {"X-Morgoth-Management-Token": token}
    path = f"{PREFIX}/projects/alpha/documents"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        vault = home / "projects" / "alpha" / "vault"
        vault.rmdir()
        assert (await client.get(path, headers=headers)).status_code == 503
        vault.mkdir()
        result = (await client.get(path, headers=headers)).json()
        assert result["documents"] == [] and result["next_offset"] is None


@pytest.mark.asyncio
async def test_nested_vault_links_do_not_traverse_or_infer_relationships(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("alpha", name="Alpha", domain="crypto")
    vault = home / "projects" / "alpha" / "vault"
    (vault / "system" / "tools").mkdir(parents=True)
    (vault / "system" / "tools" / "reader.md").write_text("# Reader", encoding="utf-8")
    (vault / "index.md").write_text("# Index\n[[system/tools/reader|Reader]] [[../outside|Bad]]", encoding="utf-8")
    token = secrets.token_urlsafe(32)
    app = create_management_app(manager, ManagementSecurity(token, 8765))
    install_desktop_knowledge_routes(app, manager)
    headers = {"X-Morgoth-Management-Token": token}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        page = (await client.get(f"{PREFIX}/projects/alpha/documents", headers=headers)).json()
        index, reader = page["documents"]
        assert index["links"] == [{"label": "Reader", "target_id": reader["document_id"]},
                                   {"label": "Bad", "target_id": None}]


def test_project_directory_swap_cannot_redirect_a_held_vault_read(tmp_path: Path) -> None:
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("alpha", name="Alpha", domain="crypto")
    manager.create_project("beta", name="Beta", domain="crypto")
    selected = next(p for p in manager._catalog() if p.id == "alpha")
    own = home / "projects" / "alpha"
    parked = home / "projects" / "alpha_parked"
    own.rename(parked)
    own.symlink_to(home / "projects" / "beta", target_is_directory=True)
    with pytest.raises(KnowledgeUnavailable, match="UNSAFE_SOURCE"):
        read_vault(selected, manager)
