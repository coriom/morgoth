"""morgoth_test proof that finalization reads Project-owned committed evidence."""

from __future__ import annotations

import os
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlparse

import asyncpg
import pytest

from core.brain import Brain
from core.config import load_config
from core.objectives import Objective, ObjectiveCategory, ObjectivesManager
from core.project import current_project, load_projects
from core.runtime import RuntimeHome
from memory.persistent import PersistentMemory
from project_manager import ProjectManager


@pytest.mark.integration
@pytest.mark.asyncio
async def test_finalizer_uses_managed_project_payload_and_preserves_model_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    assert urlparse(dsn).path == "/morgoth_test"
    home = tmp_path / "home"
    manager = ProjectManager(RuntimeHome(home, home / "projects"))
    manager.create_project("finish_weather", name="Finish Weather", domain="weather")
    project = next(p for p in load_projects(home / "projects") if p.id == "finish_weather")
    monkeypatch.setenv("MORGOTH_HOME", str(home))
    monkeypatch.setenv("MORGOTH_PROJECT", project.id)
    for name, value in {
        "POSTGRES_URL": dsn, "OLLAMA_BASE_URL": "http://127.0.0.1:65535",
        "OLLAMA_PRIMARY_MODEL": "fixture", "OLLAMA_AGENT_MODEL": "fixture",
        "SECRET_KEY": "fixture-secret", "MAX_CONCURRENT_AGENTS": "1",
        "LOG_RETENTION_DAYS": "1", "LOG_LEVEL_THOUGHT": "false",
    }.items():
        monkeypatch.setenv(name, value)
    current_project.cache_clear()
    memory = PersistentMemory(await load_config())
    admin = await asyncpg.connect(dsn, timeout=5)
    try:
        await memory.initialize()
        objective = await ObjectivesManager(memory._config, memory).create_objective(
            Objective(title="Temperature", description="Fixture objective",
                      category=ObjectiveCategory.RESEARCH, generated_by="human")
        )
        claimed = await memory.claim_next_objective(limit=1)
        assert len(claimed) == 1
        assert str(claimed[0]["objective_id"]) == objective.objective_id
        count = await memory.increment_cycle_count(objective.objective_id)
        for source in ("get_weather_forecast_met", "get_nws_weather_observation"):
            await memory.add_source_used(objective.objective_id, source)
        await memory.update_objective(objective.objective_id, evidence={
            "type": "cycle_payload", "cycle": count,
            "tool_results": [{"tool": "get_weather_forecast_met", "success": True,
                              "result": {"temperature_celsius": 20}}],
        })
        await memory.update_objective(objective.objective_id, status="done",
                                      evidence={"summary": "Human-readable model summary"})
        row = await memory.get_objective(objective.objective_id)
        brain = Brain(config=memory._config, llm_client=MagicMock(),
                      persistent_memory=memory, episodic_memory=MagicMock(),
                      scheduler=MagicMock(), tool_router=MagicMock(),
                      agent_manager=MagicMock(), notifier=MagicMock())
        brain._episodic_memory.query = AsyncMock(return_value=[])
        seen: list[str] = []

        async def synth(_obj, sources, findings):
            assert len(set(sources)) == 2
            seen.extend(findings)
            return "Fixture synthesis"

        brain._synthesize_objective = AsyncMock(side_effect=synth)
        brain._extract_theses = AsyncMock(return_value=[])
        brain.detect_contradictions = AsyncMock(return_value=[])
        await brain._finalize_objective(row, count, forced=False)
        stored = await memory.get_objective(objective.objective_id)
        evidence = json.loads(stored["evidence"]) if isinstance(stored["evidence"], str) else stored["evidence"]
        assert stored["status"] == "done"
        assert any(entry.get("summary") == "Human-readable model summary"
                   for entry in evidence)
        assert any(entry.get("type") == "synthesis" for entry in evidence)
        assert any("temperature_celsius" in item for item in seen)
        assert set(await memory.get_sources_used(objective.objective_id)) == {
            "get_weather_forecast_met", "get_nws_weather_observation"}
        async with memory._pool.acquire() as conn:
            assert await conn.fetchval("SHOW search_path") == project.postgres_schema
    finally:
        await memory.close()
        await admin.execute(f'DROP SCHEMA IF EXISTS "{project.postgres_schema}" CASCADE')
        await admin.close()
        current_project.cache_clear()
