"""Real non-crypto Domain and independent Project rail proof."""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from urllib.parse import urlparse
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest
import yaml

from analysis.measurement_coverage import measurement_blind_spots
from analysis.scorer_registry import resolve_scorer
from core.domain import _load, resolve_subject_entity, semantic_window_hours, subject_semantic_class
from core.metric_recorder import ScheduleState, snapshot_interval_secs, snapshot_once
from core.tool_rail import effective_tool_rail, installed_catalog


ROOT = Path(__file__).resolve().parents[1]
WEATHER = {"get_weather_forecast_met", "find_nws_observation_stations", "get_nws_weather_observation"}


def weather_manifest(root: Path, project_id: str) -> dict:
    return {"id": project_id, "name": "Weather test", "domain": "weather",
            "postgres_schema": f"project_{project_id}", "chroma_prefix": f"{project_id}_",
            "vault_dir": str(root / project_id / "vault"),
            "runtime_dir": str(root / project_id / "runtime")}


def make_project(root: Path, project_id: str) -> None:
    path = root / "projects" / project_id / "project.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(weather_manifest(root, project_id)), encoding="utf-8")


def child_env(root: Path, project_id: str | None = None, dsn: str | None = None) -> dict[str, str]:
    env = {"HOME": str(root), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": str(ROOT), "MORGOTH_HOME": str(root)}
    if project_id:
        env["MORGOTH_PROJECT"] = project_id
    if dsn:
        if urlparse(dsn).path != "/morgoth_test":
            raise ValueError("only morgoth_test is allowed")
        env["MORGOTH_TEST_POSTGRES_URL"] = dsn
    return env


def test_weather_pack_is_complete_and_has_no_crypto_rail():
    domain = _load("weather")
    rail = effective_tool_rail(domain)
    assert set(domain.rail_tools) == WEATHER
    assert WEATHER <= rail.installed
    assert "get_deribit_btc_perpetual" in rail.installed
    assert "get_deribit_btc_perpetual" not in rail.allowed
    assert set(rail.sources) == {"get_weather_forecast_met", "get_nws_weather_observation"}
    assert measurement_blind_spots(domain) == []
    assert domain.tool_sources == {"get_weather_forecast_met": "MET Norway",
                                   "get_nws_weather_observation": "NWS"}
    assert domain.field_units["get_weather_forecast_met"]["precipitation_amount_mm"] == "millimeter"
    assert domain.field_units["get_nws_weather_observation"]["wind_speed_mps"] == "mps"
    assert resolve_subject_entity("temperature at station KDCA", domain) == "location"
    assert subject_semantic_class("rainfall at station KDCA", domain) == "precipitation"
    assert semantic_window_hours("temperature", domain) == 3
    assert semantic_window_hours("precipitation", domain) == 6
    assert snapshot_interval_secs(domain) == 3600
    assert ScheduleState(domain.metric_collections).due_tools(3600) == ("get_nws_weather_observation",)
    assert resolve_scorer(domain, "descriptive") is None


@pytest.mark.asyncio
async def test_generic_metric_recorder_consumes_weather_declaration_only():
    domain = _load("weather")
    router = SimpleNamespace(execute_tool=AsyncMock(return_value={
        "success": True, "result": {"temperature_celsius": 20.0}}))
    memory = SimpleNamespace(record_metric_sample=AsyncMock())
    assert await snapshot_once(memory, router, domain=domain) == 1
    router.execute_tool.assert_awaited_once_with(
        "get_nws_weather_observation", {"station_id": "KDCA"})
    assert memory.record_metric_sample.await_args.args[0:2] == (
        "observed_temperature_celsius", 20.0)


def test_crypto_pack_stays_weather_free():
    domain = _load("crypto")
    rail = effective_tool_rail(domain)
    assert WEATHER <= rail.installed
    assert not WEATHER & rail.allowed
    assert not WEATHER & rail.sources
    assert not WEATHER & set(rail.chat)
    assert len(domain.rail_tools) == 15  # unchanged pack; no weather activation


def test_two_fresh_processes_catalog_vs_rails(tmp_path):
    make_project(tmp_path, "weather_probe")
    results = []
    for project_id in (None, "weather_probe"):
        proc = subprocess.run([sys.executable, str(ROOT / "tests/weather_process_probe.py")],
                              cwd=tmp_path, env=child_env(tmp_path, project_id),
                              capture_output=True, text=True, timeout=45, check=False)
        assert proc.returncode == 0, proc.stdout[-500:] + proc.stderr[-500:]
        results.append(json.loads(proc.stdout))
    assert [r["domain"] for r in results] == ["crypto", "weather"]
    assert all(r["catalog_both"] and r["environment_clean"] for r in results)
    for field in ("schema", "chroma", "vault", "runtime"):
        assert results[0][field] != results[1][field]
    assert not WEATHER & set(results[0]["registered"])
    assert WEATHER <= set(results[1]["registered"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weather_project_persists_only_in_morgoth_test(tmp_path):
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL", "")
    assert urlparse(dsn).path == "/morgoth_test"
    project_id = f"weather_{uuid.uuid4().hex[:8]}"
    make_project(tmp_path, project_id)
    schema = f"project_{project_id}"
    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        assert await conn.fetchval("SELECT current_database()") == "morgoth_test"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "tests/weather_process_probe.py"),
            cwd=tmp_path, env=child_env(tmp_path, project_id, dsn),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        assert proc.returncode == 0, stdout[-500:] or stderr[-500:]
        result = json.loads(stdout)
        assert result["database_isolated"] and result["environment_clean"]
        assert result["schema"] == schema
        assert not any(name.startswith("get_crypto_") for name in result["registered"])
        rows = await conn.fetch(f'SELECT value FROM "{schema}".knowledge WHERE category = $1', "weather-proof")
        assert [row["value"] for row in rows] == ["station KDCA"]
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


def test_generic_runtime_has_no_weather_provider_literals():
    modules = ("core/brain.py", "core/contradictions.py", "core/metric_recorder.py",
               "core/objective_gen_context.py", "core/campaign.py", "core/tool_rail.py",
               "core/tool_router.py", "api/server.py")
    banned = re.compile(r"\b(?:MET Norway|NWS|precipitation|get_weather_forecast_met|get_nws_weather_observation)\b", re.I)
    for path in modules:
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)}
        literals = [(node.lineno, node.value) for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docs and banned.search(node.value)]
        assert not literals, (path, literals)
