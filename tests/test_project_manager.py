"""Offline Project management contracts, using only disposable homes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
import yaml

from core import project as project_runtime
from core.llm.profiles import current_profile, state_path
from core.runtime import ENGINE_ROOT, RuntimeHome
from project_manager import ProjectManager, ProjectManagerError


ROOT = Path(__file__).resolve().parents[1]


def _manager(tmp_path: Path) -> ProjectManager:
    home = tmp_path / "app"
    return ProjectManager(RuntimeHome(home, home / "projects"))


def _env(home: Path, project: str | None = None) -> dict[str, str]:
    values = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
              "PYTHONPATH": str(ROOT), "MORGOTH_HOME": str(home)}
    if project:
        values["MORGOTH_PROJECT"] = project
    return values


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.projects_cli", *args],
        cwd=home.parent, env=_env(home), capture_output=True, text=True,
        timeout=30, check=False,
    )


def test_empty_catalog_is_read_only_and_legacy_compatible(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    home = manager.home.application_home
    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline management reached runtime, database or model")
    monkeypatch.setattr(project_runtime, "current_project", forbidden)
    monkeypatch.setattr("asyncpg.connect", forbidden)
    monkeypatch.setattr("core.config.load_config", forbidden)
    monkeypatch.setattr("core.llm.profiles.current_profile", forbidden)
    result = manager.list_projects()
    assert [item["id"] for item in result["projects"]] == ["default"]
    assert result["projects"][0]["postgres_schema"] == "public"
    assert result["projects"][0]["runtime_checked"] is False
    assert not home.exists()
    with pytest.raises(ProjectManagerError) as error:
        manager.get_project("absent")
    assert error.value.code == "UNKNOWN_PROJECT"
    assert not home.exists()


def test_round_trip_loader_profiles_and_immutable_process_selection(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    home = manager.home.application_home
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    monkeypatch.delenv("MORGOTH_PROJECT", raising=False)
    selected = project_runtime.current_project()
    assert selected.id == "default"
    a = manager.create_project("research_a", name="Shared name", domain="crypto")["project"]
    b = manager.create_project("research_b", name="Shared name", domain="crypto")["project"]
    weather = manager.create_project("weather_lab", name="Weather Lab", domain="weather")["project"]
    assert a["postgres_schema"] != b["postgres_schema"]
    assert a["chroma_prefix"] != b["chroma_prefix"]
    assert a["vault_dir"] != b["vault_dir"]
    assert a["runtime_dir"] != b["runtime_dir"]
    assert weather["domain"] == "weather"
    assert [entry["id"] for entry in manager.list_projects()["projects"]] == [
        "default", "research_a", "research_b", "weather_lab"]
    for identifier in ("research_a", "research_b", "weather_lab"):
        assert manager.get_project(identifier) == manager.validate_project(identifier)["project"]
    loaded = {p.id: p for p in project_runtime.load_projects(home / "projects")}
    assert set(loaded) == {"default", "research_a", "research_b", "weather_lab"}
    assert all(current_profile(loaded[identifier]).id == "legacy"
               for identifier in ("research_a", "research_b", "weather_lab"))
    assert all(not state_path(loaded[identifier]).exists()
               for identifier in ("research_a", "research_b", "weather_lab"))
    assert project_runtime.current_project() is selected
    for identifier in ("research_a", "research_b", "weather_lab"):
        directory = home / "projects" / identifier
        assert stat.S_IMODE((directory / "project.yaml").stat().st_mode) & 0o077 == 0
        assert all(stat.S_IMODE((directory / child).stat().st_mode) & 0o077 == 0
                   for child in ("runtime", "vault", "workspace"))


def test_existing_catalog_reads_do_not_write_or_initialize(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    manager.create_project("alpha", name="Alpha", domain="crypto")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("read command attempted mutation or runtime startup")
    monkeypatch.setattr("project_manager.os.mkdir", forbidden)
    monkeypatch.setattr("project_manager.os.link", forbidden)
    monkeypatch.setattr("asyncpg.connect", forbidden)
    monkeypatch.setattr("core.config.load_config", forbidden)
    assert len(manager.list_projects()["projects"]) == 2
    assert manager.get_project("alpha")["id"] == "alpha"
    assert manager.validate_project("alpha")["configuration_valid"] is True


@pytest.mark.parametrize("identifier", ["default", "../escape", "x/y", "UPPER", "pg_unsafe",
                                         "a" * 64, "public;drop", ""])
def test_invalid_identifier_has_no_write(tmp_path, identifier):
    manager = _manager(tmp_path)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project(identifier, name="Valid", domain="crypto")
    assert error.value.code == "INVALID_REQUEST"
    assert not manager.home.application_home.exists()


@pytest.mark.parametrize("name", ["", "  ", "\n", "a" * 129])
def test_invalid_name_has_no_write(tmp_path, name):
    manager = _manager(tmp_path)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("safe_id", name=name, domain="crypto")
    assert error.value.code == "INVALID_REQUEST"
    assert not manager.home.application_home.exists()


def test_unknown_domain_and_name_independent_namespace(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("research", name="Research", domain="not_installed")
    assert error.value.code == "UNKNOWN_DOMAIN"
    assert not manager.home.application_home.exists()
    first = manager.create_project("research", name="First", domain="crypto")["project"]
    other_home = tmp_path / "other"
    second = ProjectManager(RuntimeHome(other_home, other_home / "projects")).create_project(
        "research", name="Renamed", domain="weather")["project"]
    assert first["postgres_schema"] == second["postgres_schema"]
    assert first["chroma_prefix"] == second["chroma_prefix"]
    assert first["vault_dir"] != second["vault_dir"]


def test_duplicate_never_overwrites(tmp_path):
    manager = _manager(tmp_path)
    manager.create_project("alpha", name="First", domain="crypto")
    manifest = manager.home.projects_root / "alpha" / "project.yaml"
    before = manifest.read_bytes()
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("alpha", name="Second", domain="weather")
    assert error.value.code == "ALREADY_EXISTS"
    assert manifest.read_bytes() == before


def test_concurrent_same_and_different_ids(tmp_path):
    manager = _manager(tmp_path)
    def attempt(identifier: str) -> str:
        try:
            manager.create_project(identifier, name=identifier, domain="crypto")
            return "CREATED"
        except ProjectManagerError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=4) as executor:
        same = list(executor.map(attempt, ["shared", "shared"]))
        different = list(executor.map(attempt, ["separate_a", "separate_b"]))
    assert sorted(same) == ["ALREADY_EXISTS", "CREATED"]
    assert different == ["CREATED", "CREATED"]
    projects = manager.list_projects()["projects"]
    assert len({p["postgres_schema"] for p in projects}) == len(projects)
    assert len({p["chroma_prefix"] for p in projects}) == len(projects)


def test_fresh_process_creators_share_lock_and_never_overwrite(tmp_path):
    home = tmp_path / "new_home"
    command = [sys.executable, "-m", "scripts.projects_cli", "create", "shared",
               "--name", "Shared", "--domain", "crypto", "--json"]
    children = [subprocess.Popen(command, cwd=tmp_path, env=_env(home),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for _ in range(2)]
    results = [child.communicate(timeout=45) for child in children]
    assert sorted(child.returncode for child in children) == [0, 3]
    success = next(out for child, (out, _) in zip(children, results) if child.returncode == 0)
    conflict = next(err for child, (_, err) in zip(children, results) if child.returncode == 3)
    assert json.loads(success)["project"]["id"] == "shared"
    assert json.loads(conflict)["error"]["code"] == "ALREADY_EXISTS"
    assert len(_manager(tmp_path).list_projects()["projects"]) == 1  # different explicit home
    assert len(ProjectManager(RuntimeHome(home, home / "projects")).list_projects()["projects"]) == 2


def test_symlinks_and_existing_namespace_collision_fail_closed(tmp_path):
    manager = _manager(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    home = manager.home.application_home
    home.mkdir()
    (home / "projects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("alpha", name="Alpha", domain="crypto")
    assert error.value.code == "PATH_CONFLICT"
    assert list(outside.iterdir()) == []
    (home / "projects").unlink()
    (home / "projects").mkdir()
    (home / "projects" / "alpha").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ProjectManagerError) as error:
        manager.list_projects()
    assert error.value.code == "PATH_CONFLICT"
    (home / "projects" / "alpha").unlink()
    manifest_target = outside / "external.yaml"
    manifest_target.write_text("synthetic external file")
    alias = home / "projects" / "alias"
    alias.mkdir()
    (alias / "project.yaml").symlink_to(manifest_target)
    with pytest.raises(ProjectManagerError) as error:
        manager.list_projects()
    assert error.value.code == "PATH_CONFLICT"
    (alias / "project.yaml").unlink()
    alias.rmdir()
    candidate_id = "candidate"
    candidate = manager.create_project(candidate_id, name="Candidate", domain="crypto")["project"]
    # A valid neighbouring manifest can occupy a future candidate's namespace.
    import hashlib
    future = "future"
    digest = hashlib.sha256(future.encode()).hexdigest()
    neighbor_dir = home / "projects" / "neighbor"
    neighbor_dir.mkdir()
    raw = yaml.safe_load((home / "projects" / candidate_id / "project.yaml").read_text())
    raw.update(id="neighbor", name="Neighbor", postgres_schema=f"p_{digest[:60]}",
               chroma_prefix=f"p_{digest[:28]}_",
               vault_dir=str(neighbor_dir / "vault"),
               runtime_dir=str(neighbor_dir / "runtime"),
               workspace_root=str(neighbor_dir / "workspace"))
    (neighbor_dir / "project.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project(future, name="Future", domain="weather")
    assert error.value.code == "NAMESPACE_CONFLICT"
    assert not (home / "projects" / future).exists()
    assert candidate["id"] == candidate_id


def test_reserved_installation_and_symlinked_home_rejected(tmp_path):
    with pytest.raises(ProjectManagerError) as error:
        ProjectManager(RuntimeHome(ENGINE_ROOT, ENGINE_ROOT / "projects"))
    assert error.value.code == "PATH_CONFLICT"
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    manager = ProjectManager(RuntimeHome(alias, alias / "projects"))
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("safe", name="Safe", domain="crypto")
    assert error.value.code == "PATH_CONFLICT"
    assert list(target.iterdir()) == []


def test_lock_symlink_cannot_redirect_manager_write(tmp_path):
    manager = _manager(tmp_path)
    root = manager.home.projects_root
    root.mkdir(parents=True)
    outside = tmp_path / "outside_lock"
    outside.write_text("synthetic original")
    (root / ".project-manager.lock").symlink_to(outside)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("alpha", name="Alpha", domain="crypto")
    assert error.value.code == "PATH_CONFLICT"
    assert outside.read_text() == "synthetic original"
    assert not (root / "alpha").exists()


def test_malformed_neighbor_never_disappears(tmp_path):
    manager = _manager(tmp_path)
    path = manager.home.projects_root / "broken" / "project.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("id: broken\nname: Broken\ndomain: crypto\n")
    with pytest.raises(ProjectManagerError) as error:
        manager.list_projects()
    assert error.value.code == "INVALID_CATALOG"
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("valid", name="Valid", domain="crypto")
    assert error.value.code == "INVALID_CATALOG"
    assert not (manager.home.projects_root / "valid").exists()


def test_no_partial_visibility_and_failure_cleanup(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    original = manager._publish_manifest
    def inspect_then_publish(project_fd: int) -> None:
        assert [p["id"] for p in manager.list_projects()["projects"]] == ["default"]
        original(project_fd)
    monkeypatch.setattr(manager, "_publish_manifest", inspect_then_publish)
    manager.create_project("visible", name="Visible", domain="crypto")
    def fail_before_publish(_project_fd: int) -> None:
        assert [p["id"] for p in manager.list_projects()["projects"]] == ["default", "visible"]
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(manager, "_publish_manifest", fail_before_publish)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("failed", name="Failed", domain="crypto")
    assert error.value.code == "PUBLICATION_FAILED"
    assert not (manager.home.projects_root / "failed").exists()
    assert [p["id"] for p in manager.list_projects()["projects"]] == ["default", "visible"]


def test_failure_cleanup_never_deletes_foreign_content(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    def leave_foreign_content(project_fd: int) -> None:
        with os.fdopen(os.open("workspace/foreign", os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                               0o600, dir_fd=project_fd), "w") as stream:
            stream.write("synthetic user file")
        raise OSError("synthetic publication failure")
    monkeypatch.setattr(manager, "_publish_manifest", leave_foreign_content)
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("foreign", name="Foreign", domain="crypto")
    assert error.value.code == "PUBLICATION_FAILED"
    project_dir = manager.home.projects_root / "foreign"
    assert (project_dir / "workspace" / "foreign").read_text() == "synthetic user file"
    assert not (project_dir / "project.yaml").exists()
    assert [p["id"] for p in manager.list_projects()["projects"]] == ["default"]


def test_post_publication_durability_failure_reports_created(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    original = manager._publish_manifest
    def published_then_fsync_unavailable(project_fd: int) -> None:
        original(project_fd)
        def fail_fsync(_fd: int) -> None:
            raise OSError("synthetic fsync failure")
        monkeypatch.setattr("project_manager.os.fsync", fail_fsync)
    monkeypatch.setattr(manager, "_publish_manifest", published_then_fsync_unavailable)
    result = manager.create_project("alpha", name="Alpha", domain="crypto")
    assert result["created"] and result["durability_confirmed"] is False
    assert manager.get_project("alpha")["id"] == "alpha"
    with pytest.raises(ProjectManagerError) as error:
        manager.create_project("alpha", name="Again", domain="crypto")
    assert error.value.code == "ALREADY_EXISTS"


def test_cli_json_and_fresh_process_runtime_resolution(tmp_path):
    home = tmp_path / "app"
    listed = _cli(home, "list", "--json")
    assert listed.returncode == 0 and not listed.stderr and not home.exists()
    assert [p["id"] for p in json.loads(listed.stdout)["projects"]] == ["default"]
    for identifier, domain in (("research_a", "crypto"), ("research_b", "crypto"),
                               ("weather_lab", "weather")):
        created = _cli(home, "create", identifier, "--name", identifier,
                       "--domain", domain, "--json")
        assert created.returncode == 0 and not created.stderr
        payload = json.loads(created.stdout)
        assert payload["project"]["id"] == identifier
        assert payload["engine_started"] is False and payload["storage_initialized"] is False
    duplicate = _cli(home, "create", "research_a", "--name", "Again",
                     "--domain", "crypto", "--json")
    assert duplicate.returncode == 3 and not duplicate.stdout
    assert json.loads(duplicate.stderr)["error"]["code"] == "ALREADY_EXISTS"
    projects = json.loads(_cli(home, "list", "--json").stdout)["projects"]
    assert len(projects) == 4
    for identifier in ("research_a", "research_b", "weather_lab"):
        shown = _cli(home, "show", identifier, "--json")
        validated = _cli(home, "validate", identifier, "--json")
        assert shown.returncode == validated.returncode == 0
        assert json.loads(shown.stdout) == json.loads(validated.stdout)["project"]
    bad = _cli(home, "create", "../bad", "--name", "Bad", "--domain", "crypto", "--json")
    assert bad.returncode == 2 and not bad.stdout
    assert json.loads(bad.stderr)["error"]["code"] == "INVALID_REQUEST"
    assert json.loads(_cli(home, "list", "--json").stdout)["projects"] == projects
    found = []
    for identifier in ("research_a", "research_b"):
        child = subprocess.run(
            [sys.executable, "-c",
             "import json; from core.project import current_project, current_namespace; "
             "p=current_project(); n=current_namespace(); "
             "print(json.dumps({'id':p.id,'domain':p.domain,'schema':n.postgres_schema,"
             "'prefix':n.chroma_prefix,'vault':str(n.vault_dir),'runtime':str(n.runtime_dir)}))"],
            cwd=tmp_path, env=_env(home, identifier), capture_output=True, text=True,
            timeout=30, check=False)
        assert child.returncode == 0, child.stderr
        found.append(json.loads(child.stdout))
    assert [entry["id"] for entry in found] == ["research_a", "research_b"]
    assert {entry["domain"] for entry in found} == {"crypto"}
    for field in ("schema", "prefix", "vault", "runtime"):
        assert found[0][field] != found[1][field]
