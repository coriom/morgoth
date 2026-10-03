"""Fresh-process, fixture-only temporal capture against morgoth_test."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse


async def main() -> None:
    """Capture only fixture facts for a selected isolated test Project."""
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL", "")
    if urlparse(dsn).path != "/morgoth_test":
        raise RuntimeError("temporal probe requires morgoth_test")
    from core.project import current_project
    from core.domain import current_domain
    from core.tool_router import ToolRouter
    from memory.persistent import PersistentMemory
    from tools.data_feeds.get_weather_forecast_met import parse_met_forecast
    from tools.data_feeds.get_nws_weather_observation import parse_nws_observation

    project = current_project()
    domain = current_domain()
    pm = PersistentMemory(SimpleNamespace(postgres_url=dsn))
    try:
        await pm.initialize()
        assert project.postgres_schema != "public"
        assert (await pm.fetchrow("SELECT current_database() AS db"))["db"] == "morgoth_test"
        if os.environ.get("FACT_PROBE_CAPTURE") == "1":
            assert domain.name == "weather"
            fixtures = Path(__file__).parent / "fixtures/weather"
            met = parse_met_forecast(json.loads((fixtures / "met_compact.json").read_text()),
                                     "38.8512", "-77.0402")
            nws = parse_nws_observation(json.loads((fixtures / "nws_observation.json").read_text()), "KDCA")
            router = ToolRouter(pm)
            for name, payload in (("get_weather_forecast_met", met),
                                  ("get_nws_weather_observation", nws)):
                router.register(SimpleNamespace(name=name, parameters={},
                                                execute=AsyncMock(return_value={"success": True, "result": payload})))
                assert (await router.execute_tool(name, {}))["success"]
            first_prediction = (await pm.list_temporal_facts(kind="prediction"))[0]
            # Same provider version, acquired again: no artificial new forecast.
            assert (await router.execute_tool("get_weather_forecast_met", {}))["success"]
            assert (await pm.list_temporal_facts(kind="prediction"))[0]["acquired_at"] == first_prediction["acquired_at"]
        facts = await pm.list_temporal_facts()
        predictions = [f for f in facts if f["kind"] == "prediction"]
        observations = [f for f in facts if f["kind"] == "observation"]
        assert all(f["project_id"] == project.id and f["domain_id"] == domain.name for f in facts)
        assert all(f["prospective_eligible"] is False for f in facts)
        if predictions:
            assert len(await pm.list_temporal_facts(
                kind="prediction", source="MET Norway", entity="location",
                dimensions={"latitude": 38.8512},
                valid_from=predictions[0]["valid_at"], valid_to=predictions[0]["valid_at"],
                acquired_from=predictions[0]["acquired_at"],
                acquired_to=predictions[0]["acquired_at"])) == 1
        print(json.dumps({"project": project.id, "domain": domain.name,
                          "predictions": len(predictions), "observations": len(observations),
                          "met_updated_semantics": all(f["source_updated_at"] is not None for f in predictions),
                          "nws_valid_semantics": all(f["valid_at"] < f["acquired_at"] for f in observations),
                          "retrospective_ineligible": all(f["valid_at"] < f["acquired_at"] for f in predictions),
                          "count_by_filter": len(await pm.list_temporal_facts(kind="prediction", metric="temperature")),
                          "no_scorer_run": True}))
    finally:
        await pm.close()


if __name__ == "__main__":
    asyncio.run(main())
