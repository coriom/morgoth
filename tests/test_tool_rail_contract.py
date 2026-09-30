"""Catalog != Domain rail: registration, exposure, execution and apply proof."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from core import domain as D
from core.tool_rail import (STATIC_TOOLS, ToolKind, ToolRailError,
                            effective_tool_rail, validate_declared_rail)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ("fixture_crypto_only", "fixture_other_domain_only")


def _fixture_module(name: str) -> str:
    return f'''from tools.base_tool import BaseTool
class {name.title().replace("_", "")}(BaseTool):
    name = "{name}"
    description = "Synthetic source with no network access."
    is_data_source = True
    is_chat_tool = True
    digest_fields = ("value",)
    parameters = {{"type": "object", "properties": {{}}}}
    def __init__(self, config):
        pass
    async def execute(self, **kwargs):
        return self.success({{"value": 1}})
'''


def _fixture_tree(root: Path) -> tuple[Path, Path]:
    packs = root / "packs"
    (packs / "crypto").mkdir(parents=True)
    shutil.copyfile(ROOT / "domains/crypto/domain.yaml", packs / "crypto/domain.yaml")
    modules = root / "fixture_modules"
    modules.mkdir()
    for name in FIXTURES:
        (modules / f"{name}.py").write_text(_fixture_module(name), encoding="utf-8")
    for project_id, domain_id, tool in (("project_a", "fixture_a", FIXTURES[0]),
                                         ("project_b", "fixture_b", FIXTURES[1])):
        packdir = packs / domain_id
        packdir.mkdir()
        (packdir / "domain.yaml").write_text(
            yaml.safe_dump({"name": domain_id, "rail": {"tools": [tool]},
                            "semantic_windows_hours": {"default": 4.0}}),
            encoding="utf-8")
        manifest = {"id": project_id, "name": project_id, "domain": domain_id,
                    "postgres_schema": f"rail_{project_id}",
                    "chroma_prefix": f"rail_{project_id}_",
                    "vault_dir": str(root / project_id / "vault"),
                    "runtime_dir": str(root / project_id / "runtime")}
        path = root / "projects" / project_id / "project.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return packs, modules


def test_two_projects_have_distinct_effective_rails_in_subprocesses(tmp_path):
    """Explicit child env; no production configuration, DB, HTTP or LLM."""
    packs, modules = _fixture_tree(tmp_path)
    outputs = []
    for project_id in ("project_a", "project_b"):
        env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "PYTHONPATH": str(ROOT), "MORGOTH_HOME": str(tmp_path),
               "MORGOTH_PROJECT": project_id}
        proc = subprocess.run([sys.executable, str(ROOT / "tests/rail_process_probe.py"),
                               str(packs), str(modules)], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=45, check=False)
        assert proc.returncode == 0, proc.stderr[-1200:]
        outputs.append(json.loads(proc.stdout))
    assert {r["selected"] for r in outputs} == set(FIXTURES)
    assert all(r["installed_both"] and r["active_only_selected"] for r in outputs)
    for key in ("schema", "chroma", "vault", "runtime"):
        assert outputs[0][key] != outputs[1][key]


def test_policy_is_allowlist_not_discovery():
    catalog = {**STATIC_TOOLS,
               FIXTURES[0]: ToolKind(False, True, True),
               FIXTURES[1]: ToolKind(False, True, True)}
    for chosen, hidden in ((FIXTURES[0], FIXTURES[1]), (FIXTURES[1], FIXTURES[0])):
        rail = effective_tool_rail(SimpleNamespace(rail_tools=(chosen,)), catalog)
        assert chosen in rail.installed and hidden in rail.installed
        assert chosen in rail.allowed and hidden not in rail.allowed
        assert chosen in rail.sources and hidden not in rail.sources
        assert chosen in rail.chat and hidden not in rail.chat
        assert rail.denial_code(hidden) == "TOOL_NOT_ALLOWED_FOR_DOMAIN"
        assert rail.denial_code("absent_tool") == "UNKNOWN_TOOL"


@pytest.mark.asyncio
async def test_exposure_and_execution_recheck_policy_after_registry_tamper():
    """Raw installed objects cannot bypass the router/API authority check."""
    from api.routes.tools import list_tools
    from core.tool_router import ToolRouter, ToolAccessError
    catalog = {**STATIC_TOOLS,
               FIXTURES[0]: ToolKind(False, True, True),
               FIXTURES[1]: ToolKind(False, True, True)}
    policy = effective_tool_rail(SimpleNamespace(rail_tools=(FIXTURES[0],)), catalog)
    router = ToolRouter(policy=policy)
    hidden = SimpleNamespace(name=FIXTURES[1], execute=AsyncMock(),
                             to_ollama_schema=lambda: {"function": {"name": FIXTURES[1]}})
    router._tools[FIXTURES[1]] = hidden  # simulate bypassing register()
    assert FIXTURES[1] not in router.list_names()
    assert router.has_tool(FIXTURES[1]) is False
    assert router.get_schemas() == []
    with pytest.raises(ToolAccessError, match="TOOL_NOT_ALLOWED_FOR_DOMAIN"):
        router.get_schemas([FIXTURES[1]])
    assert (await router.execute_tool(FIXTURES[1], {}))["error"] == "TOOL_NOT_ALLOWED_FOR_DOMAIN"
    hidden.execute.assert_not_awaited()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tool_router=router)))
    assert await list_tools(request) == []


@pytest.mark.parametrize("names", [("missing_tool",), ("remember",)])
def test_declared_rail_requires_installed_non_global(names):
    with pytest.raises(ToolRailError):
        validate_declared_rail(names, STATIC_TOOLS)


@pytest.mark.parametrize("rail,extra", [
    ({"tools": [FIXTURES[0], FIXTURES[0]]}, {}),
    ({"tools": ["bad-tool"]}, {}),
    ({"tools": ["not_installed"]}, {}),
    ({"tools": ["remember"]}, {}),
    ({"tools": [FIXTURES[0]]}, {"source_cache_config": {FIXTURES[1]: [60, 120]}}),
    ({"tools": [FIXTURES[0]]}, {"test_bootstrap_tool_defaults": {FIXTURES[1]: {}}}),
    ({"tools": [FIXTURES[0]]}, {"metric_names": {"m": "m"},
                               "metric_field_map": {"value": "m"},
                               "metric_collections": {FIXTURES[1]: {
                                   "source": "synthetic", "interval_secs": 120,
                                   "fields": ["value"], "args": {}}}}),
])
def test_malformed_or_inactive_pack_fails_at_load(tmp_path, monkeypatch, rail, extra):
    from core import tool_rail
    catalog = {**tool_rail.installed_catalog(),
               FIXTURES[0]: ToolKind(False, True, True),
               FIXTURES[1]: ToolKind(False, True, True)}
    monkeypatch.setattr(tool_rail, "installed_catalog", lambda: catalog)
    directory = tmp_path / "packs" / "fixture_a"
    directory.mkdir(parents=True)
    (directory / "domain.yaml").write_text(
        yaml.safe_dump({"name": "fixture_a", "rail": rail, **extra}), encoding="utf-8")
    monkeypatch.setattr(D, "_DOMAINS_ROOT", tmp_path / "packs")
    with pytest.raises(D.DomainPackError):
        D._load("fixture_a")


def test_measurement_only_audits_active_installed_sources(monkeypatch):
    from analysis.measurement_coverage import measurement_blind_spots
    from tools.discovery import discover_data_feed_tools
    from core.domain import current_domain
    installed = discover_data_feed_tools()
    fake = [type(name, (), {"name": name, "is_data_source": True,
                            "is_chat_tool": True, "digest_fields": ("value",)})
            for name in FIXTURES]
    monkeypatch.setattr("tools.discovery.discover_data_feed_tools", lambda: [*installed, *fake])
    domain = replace(current_domain(), rail_tools=(*current_domain().rail_tools, FIXTURES[0]))
    spots = measurement_blind_spots(domain)
    assert any(s["tool"] == FIXTURES[0] for s in spots)
    assert all(s["tool"] != FIXTURES[1] for s in spots)


@pytest.mark.asyncio
async def test_apply_health_checks_installed_catalog_not_active_rail(monkeypatch):
    from self_modify import apply
    visited = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def get(self, url):
            visited.append(url)
            body = {"ready": True} if url.endswith("/api/brain/status") else [
                {"name": FIXTURES[1], "active": False}]
            return SimpleNamespace(status_code=200, json=lambda: body)
    monkeypatch.setattr(apply.httpx, "AsyncClient", lambda **_: Client())
    assert await apply._wait_for_ready_and_tool(FIXTURES[1]) is True
    assert any(url.endswith("/api/tools/catalog") for url in visited)
    assert not any(url.endswith("/api/tools") for url in visited)
