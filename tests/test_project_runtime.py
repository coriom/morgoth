"""Project storage must isolate instances even when their Domain is identical."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from pydantic import ValidationError

from core import project as P
from core.runtime import ENGINE_ROOT, runtime_home
from core.storage_namespace import COLLECTIONS, collection_names


def manifest(root: Path, name: str = "alpha", **changes) -> dict:
    """Synthetic non-secret project manifest, never production configuration."""
    return dict(id=name, name=f"Test {name}", domain="crypto",
                postgres_schema=f"project_{name}", chroma_prefix=f"{name}_",
                vault_dir=str(root / name / "vault"), runtime_dir=str(root / name / "state"), **changes)


def catalog(root: Path, *names: str) -> None:
    """Write test-only projects using the SAME existing crypto domain."""
    for name in names:
        path = root / "projects" / name / "project.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(manifest(root, name)), encoding="utf-8")


def child_env(root: Path, name: str, *, database: str | None = None) -> dict[str, str]:
    """Explicit process contract; no os.environ merge, credentials or dotenv."""
    values = {"HOME": str(root), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
              "PYTHONPATH": str(ENGINE_ROOT), "MORGOTH_HOME": str(root),
              "MORGOTH_PROJECT": name}
    if database is not None:
        from urllib.parse import urlparse
        if urlparse(database).path != "/morgoth_test":
            raise ValueError("this proof requires morgoth_test")
        values["MORGOTH_TEST_POSTGRES_URL"] = database
    return values


@pytest.fixture
def selected(tmp_path, monkeypatch):
    catalog(tmp_path, "alpha", "beta")
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path))
    monkeypatch.setenv("MORGOTH_PROJECT", "alpha")
    monkeypatch.delenv("MORGOTH_DOMAIN", raising=False)
    return P.current_project()


def test_immutable_and_serializable(tmp_path):
    p = P.ProjectConfig(**manifest(tmp_path, metadata={"purpose": "test"}, llm_overrides={"thesis": "ollama:example"}))
    with pytest.raises(ValidationError):
        p.name = "changed"
    for mapping in (p.metadata, p.llm_overrides):
        with pytest.raises(TypeError):
            mapping["new"] = "changed"
    assert json.loads(p.model_dump_json())["metadata"] == {"purpose": "test"}


@pytest.mark.parametrize("field", ["postgres_schema", "chroma_prefix", "vault_dir", "runtime_dir"])
def test_new_project_requires_every_namespace(tmp_path, field):
    data = manifest(tmp_path)
    del data[field]
    with pytest.raises(ValidationError):
        P.ProjectConfig(**data)


@pytest.mark.parametrize("schema", ["public", "pg_temp", "information_schema", "x;drop", 'x"', "../x", "UPPER", "x" * 64, "", "a.b"])
def test_unsafe_or_shared_schema_rejected(tmp_path, schema):
    data = manifest(tmp_path)
    data["postgres_schema"] = schema
    with pytest.raises(ValidationError):
        P.ProjectConfig(**data)


@pytest.mark.parametrize("field,value", [("chroma_prefix", ""), ("chroma_prefix", "no-dash_"), ("vault_dir", "relative"), ("runtime_dir", "/"), ("runtime_dir", "/tmp/.."), ("vault_dir", "$HOME/vault"), ("id", "../alpha"), ("domain", "../crypto"), ("name", " "), ("llm_overrides", {"unknown": "ollama"}), ("llm_overrides", {"thesis": "unknown:default"})])
def test_invalid_configuration_rejected(tmp_path, field, value):
    data = manifest(tmp_path)
    data[field] = value
    with pytest.raises(ValidationError):
        P.ProjectConfig(**data)


@pytest.mark.parametrize("field", ["id", "postgres_schema", "chroma_prefix", "vault_dir", "runtime_dir", "workspace_root"])
def test_catalog_collision_rejected(tmp_path, field):
    a = P.ProjectConfig(**manifest(tmp_path, "alpha"))
    data = manifest(tmp_path, "beta")
    data[field] = str(a.runtime_dir) if field == "workspace_root" else getattr(a, field)
    b = P.ProjectConfig(**data)
    with pytest.raises(P.ProjectConfigError):
        P.validate_projects((a, b))


@pytest.mark.parametrize("left,right", [("vault_dir", "vault_dir"), ("runtime_dir", "runtime_dir"), ("vault_dir", "runtime_dir"), ("runtime_dir", "vault_dir")])
def test_nested_storage_rejected(tmp_path, left, right):
    a = P.ProjectConfig(**manifest(tmp_path, "alpha"))
    data = manifest(tmp_path, "beta")
    data[right] = str(getattr(a, left) / "nested")
    with pytest.raises(P.ProjectConfigError):
        P.validate_projects((a, P.ProjectConfig(**data)))


def test_symlink_alias_collision_rejected(tmp_path):
    a = P.ProjectConfig(**manifest(tmp_path, "alpha"))
    a.vault_dir.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(a.vault_dir, target_is_directory=True)
    data = manifest(tmp_path, "beta")
    data["vault_dir"] = str(alias)
    with pytest.raises(P.ProjectConfigError):
        P.validate_projects((a, P.ProjectConfig(**data)))


def test_reserved_installation_paths_rejected(tmp_path):
    data = manifest(tmp_path)
    data["runtime_dir"] = str(ENGINE_ROOT / "other_state")
    with pytest.raises(P.ProjectConfigError):
        P.validate_projects((P.ProjectConfig(**data),))


def test_legacy_exact_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path))
    monkeypatch.delenv("MORGOTH_PROJECT", raising=False)
    p = P.current_project()
    assert p.id == "default" and p.domain == "crypto"
    assert p.postgres_schema == "public" and p.chroma_prefix == ""
    assert p.vault_dir == Path.home() / "Morgoth" / "vault"
    assert p.runtime_dir == ENGINE_ROOT / "data"
    assert p.chroma_dir == ENGINE_ROOT / "data/chroma_db"
    assert p.ui_token_dir == Path.home() / ".morgoth"
    assert collection_names(p.chroma_prefix) == COLLECTIONS
    assert runtime_home().projects_root == tmp_path / "projects"


@pytest.mark.parametrize("selection", ["missing", "", "../alpha"])
def test_unknown_or_invalid_selection_fails_closed(tmp_path, monkeypatch, selection):
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path))
    monkeypatch.setenv("MORGOTH_PROJECT", selection)
    with pytest.raises(P.ProjectConfigError):
        P.current_project()


def test_one_project_per_process(selected, monkeypatch):
    monkeypatch.setenv("MORGOTH_PROJECT", "beta")
    assert P.current_project() is selected


def test_conflicting_domain_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path))
    monkeypatch.setenv("MORGOTH_DOMAIN", "other")
    with pytest.raises(P.ProjectConfigError, match="conflicts"):
        P.current_project()


def test_invalid_neighbour_catalog_fails_closed(tmp_path, monkeypatch):
    catalog(tmp_path, "alpha", "beta")
    path = tmp_path / "projects/beta/project.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["postgres_schema"] = "project_alpha"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path))
    monkeypatch.setenv("MORGOTH_PROJECT", "alpha")
    with pytest.raises(P.ProjectConfigError):
        P.current_project()


def test_two_projects_same_domain(selected, tmp_path):
    from memory.episodic import EpisodicMemory
    projects = P.load_projects(tmp_path / "projects")[1:]
    a, b = projects
    assert a.domain == b.domain == "crypto"
    assert a.postgres_schema != b.postgres_schema
    assert not set(collection_names(a.chroma_prefix)) & set(collection_names(b.chroma_prefix))
    assert a.vault_dir != b.vault_dir and a.runtime_dir != b.runtime_dir
    assert a.chroma_dir != b.chroma_dir and a.ui_token_dir != b.ui_token_dir
    em = EpisodicMemory(ENGINE_ROOT / "data/chroma_db")
    assert em._persist_directory == selected.chroma_dir
    with pytest.raises(ValueError):
        em._physical_name("beta_research")


@pytest.mark.asyncio
async def test_postgres_pool_restores_namespace_every_checkout(selected, monkeypatch):
    from memory import persistent
    seen = []
    connection = SimpleNamespace(execute=AsyncMock(side_effect=lambda sql, *a: (seen.append(sql) or "SELECT 1")))
    class Lease:
        async def __aenter__(self):
            await captured["setup"](connection)
            return connection
        async def __aexit__(self, *args):
            return False
    captured = {}
    async def pool(**kw):
        captured.update(kw)
        await kw["init"](connection)
        return SimpleNamespace(acquire=Lease)
    monkeypatch.setattr(persistent.asyncpg, "create_pool", pool)
    pm = persistent.PersistentMemory(SimpleNamespace(postgres_url="unused-test-dsn"))
    await pm.initialize()
    await pm.execute("SELECT 1")
    assert captured["server_settings"] == {"search_path": selected.postgres_schema}
    assert seen.count('SET search_path TO "project_alpha"') >= 3
    assert 'CREATE SCHEMA IF NOT EXISTS "project_alpha"' in seen
    assert all("public." not in sql and ", public" not in sql for sql in seen)
    assert all("CREATE EXTENSION" not in sql for sql in seen)


@pytest.mark.asyncio
async def test_unsafe_schema_rejected_before_sql(monkeypatch):
    from memory import persistent
    monkeypatch.setattr(P, "current_namespace", lambda: SimpleNamespace(postgres_schema='x";DROP SCHEMA public'))
    pool = AsyncMock()
    monkeypatch.setattr(persistent.asyncpg, "create_pool", pool)
    with pytest.raises(ValueError):
        await persistent.PersistentMemory(MagicMock()).initialize()
    pool.assert_not_called()


@pytest.mark.asyncio
async def test_new_project_config_never_loads_production_dotenv(selected, monkeypatch):
    from core import config
    # Only fixture values, none loaded from production configuration.
    from dotenv import dotenv_values
    for key, value in dotenv_values(Path(__file__).parent / "fixtures/test.env").items():
        if value is not None:
            monkeypatch.setenv(key, value)
    blocked = MagicMock(side_effect=AssertionError("production dotenv access forbidden"))
    monkeypatch.setattr(config, "_load_environment", blocked)
    cfg = await config.load_config()
    assert cfg.data_dir == selected.runtime_dir
    assert cfg.logs_dir == selected.runtime_dir / "logs"
    assert cfg.root_dir == selected.runtime_dir / "workspace"
    assert cfg.chroma_dir == selected.chroma_dir
    assert not cfg.permissions.permissions.can_self_modify
    blocked.assert_not_called()


@pytest.mark.asyncio
async def test_new_project_cannot_write_global_permissions_or_backup(selected, monkeypatch):
    from api.routes.admin import patch_permissions
    from fastapi import HTTPException
    from core import backup_watchdog
    with pytest.raises(HTTPException) as exc:
        await patch_permissions(MagicMock(), MagicMock())
    assert exc.value.status_code == 409
    spawn = AsyncMock()
    monkeypatch.setattr(backup_watchdog, "_spawn_backup_script", spawn)
    await backup_watchdog.catch_up_if_stale()
    spawn.assert_not_called()


def test_project_llm_overrides_and_explicit_env_precedence(tmp_path, monkeypatch):
    from core.llm import registry
    p = P.ProjectConfig(**manifest(tmp_path, llm_overrides={"thesis": "ollama:project-model"}))
    monkeypatch.setattr(P, "current_project", lambda: p)
    for key in ("MORGOTH_LLM_THESIS", "THESIS_GENERATOR"):
        monkeypatch.delenv(key, raising=False)
    assert registry.resolve("thesis") == ("ollama", "project-model")
    assert next(r for r in registry.routing_table() if r["task"] == "thesis")["source"] == "project"
    monkeypatch.setenv("MORGOTH_LLM_THESIS", "codex-cli:default")
    assert registry.resolve("thesis") == ("codex-cli", "default")


def test_subprocess_environments_do_not_inherit_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_PARENT_ONLY", "not-a-secret-canary")
    monkeypatch.setenv("POSTGRES_URL", "must-not-propagate")
    env = child_env(tmp_path, "alpha")
    assert set(env) == {"HOME", "PATH", "LANG", "PYTHONPATH", "MORGOTH_HOME", "MORGOTH_PROJECT"}
    assert "POSTGRES_URL" not in env and "SYNTHETIC_PARENT_ONLY" not in env
    with pytest.raises(ValueError):
        child_env(tmp_path, "alpha", database="postgresql:///morgoth")


def test_two_independent_subprocesses_same_crypto(tmp_path, monkeypatch):
    catalog(tmp_path, "alpha", "beta")
    monkeypatch.setenv("SYNTHETIC_PARENT_ONLY", "not-a-secret-canary")
    outputs = []
    for name in ("alpha", "beta"):
        result = subprocess.run([sys.executable, str(ENGINE_ROOT / "tests/project_process_probe.py")],
                                cwd=tmp_path, env=child_env(tmp_path, name),
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, "project child failed (diagnostics intentionally withheld)"
        outputs.append(json.loads(result.stdout))
    a, b = outputs
    assert a["domain"] == b["domain"] == "crypto"
    for field in ("schema", "collections", "vault", "runtime", "chroma", "token"):
        assert a[field] != b[field]
    assert a["environment_clean"] and b["environment_clean"]


def test_rotating_log_uses_project_state(selected, monkeypatch):
    import main
    add = MagicMock()
    monkeypatch.setattr(main.logger, "add", add)
    main._wire_log_rotation()
    assert add.call_args.args[0] == str(selected.runtime_dir / "logs/morgoth.log")


def test_legacy_rotating_log_path_unchanged(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv("MORGOTH_HOME", str(tmp_path))
    monkeypatch.delenv("MORGOTH_PROJECT", raising=False)
    add = MagicMock()
    monkeypatch.setattr(main.logger, "add", add)
    main._wire_log_rotation()
    assert add.call_args.args[0] == "logs/morgoth.log"


@pytest.mark.parametrize("spec", ["ollama:local-model", "codex-cli:default", "claude-cli:default"])
def test_reflect_project_route_resolution_only(tmp_path, monkeypatch, spec):
    """Pure selection; never execute reflect, LLMs or proposals."""
    from self_modify.reflect_llm import resolve_provider
    p = P.ProjectConfig(**manifest(tmp_path, llm_overrides={"reflect": spec}))
    monkeypatch.setattr(P, "current_project", lambda: p)
    monkeypatch.delenv("REFLECT_PROVIDER", raising=False)
    monkeypatch.delenv("MORGOTH_LLM_REFLECT", raising=False)
    expected = "claude-cli" if spec == "claude-cli:default" else spec
    assert resolve_provider(None) == expected
    assert resolve_provider("ollama") == "ollama"


def test_runtime_sql_has_no_hardcoded_public_table_escape():
    """Regression lock for qualified public SQL that bypasses search_path."""
    import ast
    import re
    pattern = re.compile(r'\b(?:from|join|into|update|table(?:\s+if\s+not\s+exists)?|references)\s+"?public"?\s*\.', re.I)
    for directory in ("memory", "core", "api", "analysis", "self_modify", "scripts"):
        for path in (ENGINE_ROOT / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert not pattern.search(node.value), f"shared SQL namespace escape in {path.name}:{node.lineno}"


def test_legacy_token_symlink_alias_is_reserved(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    data = manifest(tmp_path)
    token_target = Path(data["runtime_dir"])
    token_target.mkdir(parents=True)
    (home / ".morgoth").symlink_to(token_target, target_is_directory=True)
    with pytest.raises(P.ProjectConfigError):
        P.validate_projects((P.ProjectConfig(**data),))
