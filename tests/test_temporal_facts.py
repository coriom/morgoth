"""Prospective temporal-fact semantics, source fixtures and arbitrary scorer roles."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from analysis import scorer_registry
from core import domain as domain_mod
from core.domain import DomainPackError, _load
from core.temporal_facts import TemporalFact, extract_temporal_facts
from core.tool_rail import effective_tool_rail
from core.tool_router import ToolRouter
from tools.data_feeds.get_nws_weather_observation import parse_nws_observation
from tools.data_feeds.get_weather_forecast_met import parse_met_forecast


FIXTURES = Path(__file__).parent / "fixtures/weather"


def _time(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 1, hour, minute, tzinfo=timezone.utc)


def _fact(**changes) -> TemporalFact:
    values = dict(kind="prediction", project_id="alpha", domain_id="neutral",
                  source="Provider", tool="fixture_tool", metric="temperature", value=20.0,
                  unit="celsius", entity="location", dimensions={"latitude": 1.0},
                  acquired_at=_time(10), valid_at=_time(12),
                  source_updated_at=_time(9), code_version="abc123")
    values.update(changes)
    return TemporalFact(**values)


def test_prospective_retrospective_and_observation_invariants():
    assert _fact().prospective_eligible
    assert not _fact(acquired_at=_time(14)).prospective_eligible
    assert not _fact(kind="observation", acquired_at=_time(12, 20),
                     valid_at=_time(12, 3)).prospective_eligible


def test_semantic_identity_deduplicates_provider_version_but_keeps_revisions():
    first = _fact()
    assert first.semantic_key == _fact(acquired_at=_time(11)).semantic_key
    assert first.semantic_key != _fact(source_updated_at=_time(10)).semantic_key
    assert first.semantic_key != _fact(source="Other provider").semantic_key
    assert first.semantic_key != _fact(value=21.0).semantic_key
    assert first.semantic_key != _fact(project_id="beta").semantic_key
    with pytest.raises(TypeError):
        first.dimensions["latitude"] = 2.0


@pytest.mark.parametrize("changes", [
    {"dimensions": {"nested": {"x": 1}}},
    {"dimensions": {"api_key": "synthetic"}},
    {"dimensions": {"label": "x" * 129}},
    {"dimensions": {"latitude": float("nan")}},
    {"valid_at": None},
    {"value": float("inf")},
    {"unit": ""},
])
def test_malformed_facts_rejected(changes):
    with pytest.raises(ValueError):
        _fact(**changes)


def test_weather_fixture_extracts_real_parser_outputs_without_scoring():
    domain = _load("weather")
    project = SimpleNamespace(id="weather_test")
    raw_met = json.loads((FIXTURES / "met_compact.json").read_text())
    met = parse_met_forecast(raw_met, "38.8512", "-77.0402")
    assert met["source_updated_at"] == "2026-09-30T12:00:00Z"
    assert "issued_at" not in met
    predictions = extract_temporal_facts(domain, project, "get_weather_forecast_met", met, _time(10))
    assert len(predictions) == sum(row["air_temperature_celsius"] is not None for row in met["records"])
    assert all(f.kind == "prediction" and f.metric == "temperature" and f.unit == "celsius"
               and f.source == "MET Norway" for f in predictions)
    assert predictions[0].source_updated_at.isoformat() == "2026-09-30T12:00:00+00:00"
    assert predictions[0].dimensions == {"latitude": 38.8512, "longitude": -77.0402}
    assert predictions[0].valid_at.isoformat() == "2026-09-30T13:00:00+00:00"
    raw_nws = json.loads((FIXTURES / "nws_observation.json").read_text())
    nws = parse_nws_observation(raw_nws, "KDCA")
    observations = extract_temporal_facts(domain, project, "get_nws_weather_observation", nws, _time(14))
    assert len(observations) == 1
    assert observations[0].source == "NWS" and observations[0].value == 20.0
    assert observations[0].dimensions["station_id"] == "KDCA"
    assert observations[0].valid_at.isoformat() == "2026-09-30T13:52:00+00:00"
    assert observations[0].source_updated_at is None
    assert extract_temporal_facts(_load("crypto"), SimpleNamespace(id="default"),
                                  "get_weather_forecast_met", met, _time(10)) == ()


def test_declared_capture_rejects_missing_valid_at_or_unit():
    domain = _load("weather")
    met = parse_met_forecast(json.loads((FIXTURES / "met_compact.json").read_text()),
                             "38.8512", "-77.0402")
    del met["records"][0]["valid_at"]
    with pytest.raises(ValueError, match="valid_at"):
        extract_temporal_facts(domain, SimpleNamespace(id="weather_test"),
                               "get_weather_forecast_met", met, _time(10))
    from dataclasses import replace
    domain = replace(domain, field_units={"get_weather_forecast_met": {}})
    with pytest.raises(ValueError, match="unit"):
        extract_temporal_facts(domain, SimpleNamespace(id="weather_test"),
                               "get_weather_forecast_met", met, _time(10))


@pytest.mark.asyncio
async def test_router_capture_is_rail_gated_and_does_not_change_tool_result(monkeypatch):
    import importlib
    domain = _load("weather")
    monkeypatch.setattr(importlib.import_module("core.domain"), "current_domain", lambda: domain)
    project_mod = importlib.import_module("core.project")
    monkeypatch.setattr(project_mod, "current_project", lambda: SimpleNamespace(id="weather_test"))
    payload = parse_nws_observation(json.loads((FIXTURES / "nws_observation.json").read_text()), "KDCA")
    result = {"success": True, "result": payload}
    pm = SimpleNamespace(insert_temporal_fact=AsyncMock())
    router = ToolRouter(pm, effective_tool_rail(domain))
    tool = SimpleNamespace(name="get_nws_weather_observation",
                           parameters={"type": "object", "properties": {"station_id": {}}, "required": ["station_id"]},
                           execute=AsyncMock(return_value=result))
    router.register(tool)
    assert await router.execute_tool(tool.name, {"station_id": "KDCA"}) is result
    assert pm.insert_temporal_fact.await_count == 1
    pm.insert_temporal_fact.side_effect = ValueError("synthetic failure")
    assert await router.execute_tool(tool.name, {"station_id": "KDCA"}) is result
    malformed = {"success": True, "result": {**payload, "observed_at": None}}
    tool.execute.return_value = malformed
    assert await router.execute_tool(tool.name, {"station_id": "KDCA"}) is malformed
    assert (await router.execute_tool("get_deribit_btc_perpetual", {}))["error"] == "TOOL_NOT_ALLOWED_FOR_DOMAIN"


@pytest.mark.parametrize("change", ["inactive_tool", "missing_unit", "invalid_selector"])
def test_domain_capture_declarations_fail_closed(tmp_path, monkeypatch, change):
    import yaml
    pack = yaml.safe_load((Path(__file__).parents[1] / "domains/weather/domain.yaml").read_text())
    if change == "inactive_tool":
        pack["fact_captures"]["unknown_tool"] = pack["fact_captures"].pop("get_weather_forecast_met")
    elif change == "missing_unit":
        del pack["field_units"]["get_weather_forecast_met"]["air_temperature_celsius"]
    else:
        pack["fact_captures"]["get_weather_forecast_met"][0]["valid_at"] = "../../escape"
    path = tmp_path / "weather" / "domain.yaml"
    path.parent.mkdir()
    path.write_text(yaml.safe_dump(pack), encoding="utf-8")
    monkeypatch.setattr(domain_mod, "_DOMAINS_ROOT", tmp_path)
    with pytest.raises(DomainPackError):
        domain_mod._load("weather")


def test_arbitrary_role_is_registry_selected_and_unknown_implementation_fails(tmp_path, monkeypatch):
    crypto = _load("crypto")
    path = tmp_path / "fixture" / "domain.yaml"
    path.parent.mkdir()
    path.write_text("name: fixture\nrail: {tools: []}\nscorers:\n  verification: fixture_verifier\n", encoding="utf-8")
    monkeypatch.setattr(domain_mod, "_DOMAINS_ROOT", tmp_path)
    monkeypatch.setitem(scorer_registry._IMPLEMENTATIONS, "fixture_verifier", ("math", "sqrt"))
    domain = domain_mod._load("fixture")
    assert scorer_registry.resolve_scorer(domain, "verification") is math.sqrt
    assert scorer_registry.resolve_scorer(crypto, "descriptive") is not None
    path.write_text("name: fixture\nrail: {tools: []}\nscorers:\n  verification: unknown\n")
    with pytest.raises(DomainPackError, match="unknown scorer implementation"):
        domain_mod._load("fixture")
    path.write_text("name: fixture\nrail: {tools: []}\nscorers:\n  verification: fixture_verifier\n  verification: fixture_verifier\n")
    with pytest.raises(DomainPackError, match="duplicate"):
        domain_mod._load("fixture")
    path.write_text("name: fixture\nrail: {tools: []}\nscorers:\n  bad-role: fixture_verifier\n")
    with pytest.raises(DomainPackError, match="role"):
        domain_mod._load("fixture")
