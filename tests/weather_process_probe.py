"""Fresh-process Weather/Crypto rail and opt-in morgoth_test proof."""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse


async def main() -> None:
    """Use explicit test Project selection; never load dotenv or call HTTP/LLMs."""
    from core.project import current_project, current_namespace
    from core.domain import current_domain, resolve_subject_entity, subject_semantic_class, semantic_window_hours
    from core.tool_rail import STATIC_TOOLS, installed_catalog
    from core.brain import CHAT_TOOL_NAMES, DATA_SOURCE_TOOLS
    from core.metric_recorder import ScheduleState, snapshot_interval_secs
    from analysis.scorer_registry import resolve_scorer
    from api.server import build_tool_router
    from api.routes.tools import list_tools, installed_tools
    from unittest.mock import MagicMock

    project = current_project()
    domain = current_domain()
    assert current_namespace() is project
    installed = installed_catalog()
    weather = {"get_weather_forecast_met", "find_nws_observation_stations", "get_nws_weather_observation"}
    crypto = {"get_bitcoin_futures_funding", "get_deribit_btc_perpetual"}
    assert weather | crypto <= set(installed)
    router = build_tool_router(*[MagicMock() for _ in range(5)])
    globals_ = {name for name, kind in STATIC_TOOLS.items() if kind.global_internal}
    assert set(router.list_names()) == globals_ | set(domain.rail_tools)
    assert set(DATA_SOURCE_TOOLS) <= set(domain.rail_tools)
    assert set(CHAT_TOOL_NAMES) <= set(router.list_names())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(tool_router=router)))
    exposed = {item["name"] for item in await list_tools(request)}
    catalog = {item["name"]: item["active"] for item in await installed_tools(request)}
    assert exposed == set(router.list_names())
    assert all(catalog[name] == (name in exposed) for name in weather | crypto)
    if domain.name == "weather":
        assert weather <= exposed and not crypto & exposed
        assert not crypto & {s["function"]["name"] for s in router.get_schemas()}
        assert (await router.execute_tool("get_deribit_btc_perpetual", {}))["error"] == "TOOL_NOT_ALLOWED_FOR_DOMAIN"
        assert resolve_subject_entity("temperature at station KDCA", domain) == "location"
        assert subject_semantic_class("temperature at station KDCA", domain) == "temperature"
        assert semantic_window_hours("temperature", domain) == 3
        assert snapshot_interval_secs(domain) == 3600
        assert ScheduleState(domain.metric_collections).due_tools(3600) == ("get_nws_weather_observation",)
        assert resolve_scorer(domain, "descriptive") is None
    else:
        assert domain.name == "crypto"
        assert not weather & exposed
        assert (await router.execute_tool("get_weather_forecast_met", {}))["error"] == "TOOL_NOT_ALLOWED_FOR_DOMAIN"

    result = {"project": project.id, "domain": domain.name,
              "schema": project.postgres_schema, "chroma": project.chroma_prefix,
              "vault": str(project.vault_dir), "runtime": str(project.runtime_dir),
              "registered": sorted(router.list_names()), "catalog_both": True,
              "environment_clean": "POSTGRES_URL" not in os.environ and "SYNTHETIC_PARENT_ONLY" not in os.environ}
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    if dsn:
        if urlparse(dsn).path != "/morgoth_test" or domain.name != "weather":
            raise RuntimeError("refusing non-Weather or non-test storage")
        from memory.persistent import PersistentMemory
        pm = PersistentMemory(SimpleNamespace(postgres_url=dsn))
        try:
            await pm.initialize()
            await pm.execute("INSERT INTO knowledge (category, key, value) VALUES ('weather-proof', 'location', $1)", "station KDCA")
            rows = await pm.fetch("SELECT value FROM knowledge WHERE category = 'weather-proof' AND key = 'location'")
            assert [row["value"] for row in rows] == ["station KDCA"]
            row = await pm.fetchrow("SELECT current_schema() AS schema")
            assert row["schema"] == project.postgres_schema
            result["database_isolated"] = True
        finally:
            await pm.close()
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        import traceback
        frames = traceback.extract_tb(exc.__traceback__)
        print(json.dumps({"failure_type": type(exc).__name__, "line": frames[-1].lineno}))
        raise SystemExit(1) from None
