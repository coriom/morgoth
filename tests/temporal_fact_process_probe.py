"""Fresh-process, fixture-only temporal capture against morgoth_test."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
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
    from core.temporal_facts import TemporalFact, extract_temporal_facts
    from memory.persistent import PersistentMemory, TemporalFactQueryOverflow
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
            database_now = (await pm.fetchrow("SELECT clock_timestamp() AS now"))["now"]
            retrospective_met = deepcopy(met)
            future_at = database_now + timedelta(hours=2)
            met["records"][0]["valid_at"] = future_at.isoformat()
            router = ToolRouter(pm)
            tools = {}
            for name, payload in (("get_weather_forecast_met", met),
                                  ("get_nws_weather_observation", nws)):
                tool = SimpleNamespace(name=name, parameters={},
                                       execute=AsyncMock(return_value={"success": True, "result": payload}))
                router.register(tool)
                tools[name] = tool
            met_tool = tools["get_weather_forecast_met"]
            first_result = await router.execute_tool(met_tool.name, {}, bypass_cache=True)
            assert first_result["success"]
            assert first_result["metadata"]["temporal_fact_capture"] == {
                "status": "captured", "facts_processed": 1}
            first_prediction = (await pm.list_temporal_facts(kind="prediction"))[0]
            assert first_prediction["prospective_eligible"]
            assert database_now <= first_prediction["acquired_at"] < future_at
            # Re-acquiring one provider version never shifts its first database timestamp.
            assert (await router.execute_tool(met_tool.name, {}, bypass_cache=True))["success"]
            assert (await pm.list_temporal_facts(kind="prediction"))[0]["acquired_at"] == first_prediction["acquired_at"]
            revised = deepcopy(met)
            revised["source_updated_at"] = (database_now + timedelta(minutes=1)).isoformat()
            met_tool.execute.return_value = {"success": True, "result": revised}
            assert (await router.execute_tool(met_tool.name, {}, bypass_cache=True))["success"]
            predictions = await pm.list_temporal_facts(kind="prediction")
            assert len(predictions) == 2 and len({p["semantic_key"] for p in predictions}) == 2
            # Concurrent router acquisitions of one semantic version converge on one row.
            concurrent = deepcopy(revised)
            concurrent["source_updated_at"] = (database_now + timedelta(minutes=2)).isoformat()
            concurrent_fact = extract_temporal_facts(
                domain, project, met_tool.name, concurrent, datetime.now(timezone.utc),
            )[0]
            inserted_concurrently = await asyncio.gather(
                *(pm.insert_temporal_fact(concurrent_fact) for _ in range(6))
            )
            assert len({row["acquired_at"] for row in inserted_concurrently}) == 1
            assert len({row["semantic_key"] for row in inserted_concurrently}) == 1
            met_tool.execute.return_value = {"success": True, "result": concurrent}
            outcomes = await asyncio.gather(*(router.execute_tool(met_tool.name, {}, bypass_cache=True)
                                              for _ in range(6)))
            assert all(outcome["metadata"]["temporal_fact_capture"]["status"] == "captured"
                       for outcome in outcomes)
            predictions = await pm.list_temporal_facts(kind="prediction")
            assert len(predictions) == 3
            assert next(row for row in predictions if row["semantic_key"] == concurrent_fact.semantic_key)[
                "acquired_at"] == inserted_concurrently[0]["acquired_at"]
            # An old caller timestamp cannot backdate PostgreSQL's acquisition clock.
            caller_old = replace(TemporalFact(
                kind="prediction", project_id=project.id, domain_id=domain.name,
                source="MET Norway", tool=met_tool.name, metric="temperature",
                value=21.0, unit="celsius", entity="location",
                dimensions={"latitude": 38.8512, "longitude": -77.0402},
                acquired_at=datetime(2000, 1, 1, tzinfo=timezone.utc), valid_at=future_at,
                source_record_id="synthetic_old_caller"),
                acquired_at=datetime(2000, 1, 1, tzinfo=timezone.utc))
            db_before = (await pm.fetchrow("SELECT clock_timestamp() AS now"))["now"]
            inserted = await pm.insert_temporal_fact(caller_old)
            db_after = (await pm.fetchrow("SELECT clock_timestamp() AS now"))["now"]
            assert db_before <= inserted["acquired_at"] <= db_after
            assert inserted["acquired_at"] != caller_old.acquired_at
            # A retrospective provider fetch remains stored but is ineligible.
            met_tool.execute.return_value = {"success": True, "result": retrospective_met}
            assert (await router.execute_tool(met_tool.name, {}, bypass_cache=True))["success"]
            retrospective = [row for row in await pm.list_temporal_facts(kind="prediction")
                             if row["valid_at"] < database_now]
            assert len(retrospective) == 1 and not retrospective[0]["prospective_eligible"]
            obs_result = await router.execute_tool("get_nws_weather_observation", {}, bypass_cache=True)
            assert obs_result["metadata"]["temporal_fact_capture"]["status"] == "captured"
            observations = await pm.list_temporal_facts(kind="observation")
            assert len(observations) == 1 and observations[0]["valid_at"] < observations[0]["acquired_at"]
            # A deterministic SQL constraint fails the second fact in a two-fact
            # router batch. The first new fact must roll back; older facts stay.
            count_before = (await pm.fetchrow("SELECT count(*) AS n FROM temporal_facts"))["n"]
            await pm.execute("ALTER TABLE temporal_facts ADD CONSTRAINT synthetic_reject_value CHECK (value <> 9999)")
            try:
                bad = deepcopy(met)
                bad["source_updated_at"] = (database_now + timedelta(minutes=3)).isoformat()
                bad["records"] = [
                    {"valid_at": (future_at + timedelta(hours=1)).isoformat(),
                     "air_temperature_celsius": 22.0},
                    {"valid_at": (future_at + timedelta(hours=2)).isoformat(),
                     "air_temperature_celsius": 9999.0},
                ]
                met_tool.execute.return_value = {"success": True, "result": bad}
                failed = await router.execute_tool(met_tool.name, {}, bypass_cache=True)
                assert failed["success"] and failed["result"] is bad
                assert failed["metadata"]["temporal_fact_capture"] == {
                    "status": "failed", "facts_processed": 0}
                assert (await pm.fetchrow("SELECT count(*) AS n FROM temporal_facts"))["n"] == count_before
            finally:
                await pm.execute("ALTER TABLE temporal_facts DROP CONSTRAINT synthetic_reject_value")
            # A single bounded SELECT is complete at 10,000 and errors at 10,001.
            assert await pm.list_temporal_facts(metric="bulk_test") == []
            await pm.execute("""
                INSERT INTO temporal_facts
                    (semantic_key, project_id, domain_id, kind, source, tool, metric,
                     value, unit, entity, dimensions, valid_at)
                SELECT 'bulk' || lpad(n::text, 60, '0'), $1, $2, 'prediction',
                       'bulk_source', 'get_weather_forecast_met', 'bulk_test',
                       n::float, 'celsius', 'location', '{"latitude":38.8512}'::jsonb,
                       $3::timestamptz + n * interval '1 second'
                FROM generate_series(1, 10000) AS n
            """, project.id, domain.name, future_at)
            exact = await pm.list_temporal_facts(metric="bulk_test", source="bulk_source")
            assert len(exact) == 10000
            assert len({row["semantic_key"] for row in exact}) == 10000
            assert [row["value"] for row in exact] == list(range(1, 10001))
            await pm.execute("""
                INSERT INTO temporal_facts
                    (semantic_key, project_id, domain_id, kind, source, tool, metric,
                     value, unit, entity, dimensions, valid_at)
                VALUES ('bulk' || lpad('10001', 60, '0'), $1, $2, 'prediction',
                        'other_source', 'get_weather_forecast_met', 'bulk_test',
                        10001, 'celsius', 'location', '{"latitude":38.8512}'::jsonb,
                        $3::timestamptz + interval '10001 seconds')
            """, project.id, domain.name, future_at)
            try:
                await pm.list_temporal_facts(metric="bulk_test")
            except TemporalFactQueryOverflow:
                pass
            else:
                raise AssertionError("overflow must not masquerade as a complete corpus")
            assert len(await pm.list_temporal_facts(metric="bulk_test", source="bulk_source")) == 10000
            assert len(await pm.list_temporal_facts(metric="bulk_test", source="other_source")) == 1
            # All declared filters use the actual production query path.
            assert len(await pm.list_temporal_facts(
                kind="prediction", metric="temperature", source="MET Norway",
                entity="location", dimensions={"latitude": 38.8512},
                valid_from=future_at, valid_to=future_at,
                acquired_from=first_prediction["acquired_at"],
                acquired_to=db_after)) == 4
        # Each fresh Project has one own fact; no other Project's facts appear.
        own = TemporalFact(kind="observation", project_id=project.id, domain_id=domain.name,
                           source="fixture", tool="fixture_tool", metric="isolation",
                           value=1.0, unit="celsius", entity="location",
                           dimensions={"fixture": True}, acquired_at=datetime.now(timezone.utc),
                           valid_at=datetime.now(timezone.utc))
        await pm.insert_temporal_fact(own)
        own_rows = await pm.list_temporal_facts(metric="isolation")
        assert len(own_rows) == 1 and own_rows[0]["project_id"] == project.id
        assert own_rows[0]["domain_id"] == domain.name
        print(json.dumps({"project": project.id, "domain": domain.name,
                          "predictions": len(await pm.list_temporal_facts(metric="temperature", kind="prediction")),
                          "observations": len(await pm.list_temporal_facts(kind="observation", metric="temperature")),
                          "prospective_eligible": bool(os.environ.get("FACT_PROBE_CAPTURE") == "1"),
                          "bulk_overflow_checked": bool(os.environ.get("FACT_PROBE_CAPTURE") == "1"),
                          "own_isolation": len(own_rows) == 1,
                          "no_scorer_run": True}))
    finally:
        await pm.close()


if __name__ == "__main__":
    asyncio.run(main())
