"""Actual Weather tool extraction against sanitized official-format fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tools.data_feeds.find_nws_observation_stations import (
    FindNwsObservationStationsTool, parse_nws_station_link, parse_nws_stations,
)
from tools.data_feeds.get_nws_weather_observation import (
    GetNwsWeatherObservationTool, parse_nws_observation,
)
from tools.data_feeds.get_weather_forecast_met import GetWeatherForecastMetTool, parse_met_forecast
from tools.data_feeds.weather_http import USER_AGENT, WeatherResponseCache, coordinates


FIXTURES = Path(__file__).parent / "fixtures/weather"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def config():
    return SimpleNamespace(permissions=SimpleNamespace(
        permissions=SimpleNamespace(can_access_internet=True)))


def fake_client(responses: dict[str, dict], calls: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["User-Agent"] == USER_AGENT
        assert request.url.scheme == "https"
        return httpx.Response(200, json=responses[str(request.url)],
                              headers={"Expires": "Fri, 01 Jan 2100 00:00:00 GMT"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_met_forecast_actual_tool_parser_and_period_identity():
    calls = []
    url = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=38.8512&lon=-77.0402"
    client = fake_client({url: fixture("met_compact.json")}, calls)
    tool = GetWeatherForecastMetTool(config(), client)
    result = await tool.execute(latitude=38.8512, longitude=-77.0402)
    assert result["success"] is True
    data = result["result"]
    assert data["source"] == "MET Norway" and data["issued_at"] == "2026-09-30T12:00:00Z"
    assert data["attribution"] == "Data from MET Norway"
    assert data["license_url"] == "https://creativecommons.org/licenses/by/4.0/"
    assert data["modified_from_source"] is True
    assert data["valid_at"] == "2026-09-30T13:00:00Z"
    assert data["air_temperature_celsius"] == 19.2
    assert data["wind_speed_mps"] == 5.0
    assert data["wind_from_direction_degrees"] == 270.0
    assert data["precipitation_amount_mm"] == 0.4
    assert data["precipitation_period_start_at"] == "2026-09-30T13:00:00Z"
    assert data["precipitation_period_end_at"] == "2026-09-30T14:00:00Z"
    assert "next_1_hours" not in data["records"][1]
    assert data["records"][1]["air_temperature_celsius"] is None
    again = await tool.execute(latitude=38.8512, longitude=-77.0402)
    assert again == result and len(calls) == 1  # URL-keyed HTTP cache
    await client.aclose()


@pytest.mark.parametrize("lat,lon", [(91, 0), (0, -181), (float("nan"), 0), (True, 0)])
@pytest.mark.asyncio
async def test_met_invalid_coordinates_do_not_request(lat, lon):
    calls = []
    client = fake_client({}, calls)
    result = await GetWeatherForecastMetTool(config(), client).execute(latitude=lat, longitude=lon)
    assert result["success"] is False and calls == []
    await client.aclose()


def test_met_malformed_units_and_missing_period():
    data = fixture("met_compact.json")
    data["properties"]["timeseries"][0]["data"].pop("next_1_hours")
    parsed = parse_met_forecast(data, "38.8512", "-77.0402")
    assert parsed["precipitation_amount_mm"] is None
    assert parsed["precipitation_period_end_at"] is None
    data["properties"]["meta"]["units"]["wind_speed"] = "knots"
    with pytest.raises(ValueError, match="unit"):
        parse_met_forecast(data, "38.8512", "-77.0402")
    with pytest.raises(ValueError):
        parse_met_forecast({}, "38.8512", "-77.0402")


def test_sanitized_official_live_samples_use_production_parsers():
    """One MET and linked NWS requests recorded 2026-10-01, then minimized."""
    met = parse_met_forecast(fixture("met_compact_official.json"), "38.8512", "-77.0402")
    assert met["source"] == "MET Norway" and met["issued_at"] and met["valid_at"]
    assert met["air_temperature_celsius"] is not None
    assert met["wind_speed_mps"] is not None
    point = fixture("nws_point_official.json")
    assert parse_nws_station_link(point).startswith("https://api.weather.gov/gridpoints/")
    stations = parse_nws_stations(fixture("nws_stations_official.json"), "38.8512", "-77.0402")
    station = stations["stations"][0]["station_id"]
    observed = parse_nws_observation(fixture("nws_observation_official.json"), station)
    assert observed["station_id"] == station and observed["observed_at"]
    assert observed["temperature_celsius"] is not None
    assert observed["wind_speed_mps"] is not None


@pytest.mark.asyncio
async def test_nws_linked_station_discovery_and_latest_observation():
    calls = []
    point_url = "https://api.weather.gov/points/38.8512,-77.0402"
    link = "https://api.weather.gov/gridpoints/LWX/97,70/stations"
    obs_url = "https://api.weather.gov/stations/KDCA/observations/latest"
    client = fake_client({point_url: fixture("nws_point.json"),
                          link: fixture("nws_stations.json"),
                          obs_url: fixture("nws_observation.json")}, calls)
    station_tool = FindNwsObservationStationsTool(config(), client)
    stations = await station_tool.execute(latitude=38.8512, longitude=-77.0402)
    assert stations["success"] is True
    assert stations["result"]["stations"][0]["station_id"] == "KDCA"
    assert "distance not asserted" in stations["result"]["ordering"]
    observation = await GetNwsWeatherObservationTool(config(), client).execute(station_id="KDCA")
    assert observation["success"] is True
    observed = observation["result"]
    assert observed["observed_at"] == "2026-09-30T13:52:00Z"
    assert observed["temperature_celsius"] == 20.0
    assert observed["wind_speed_mps"] == pytest.approx(5.0)
    assert observed["source_units"]["windSpeed"] == "wmoUnit:km_h-1"
    assert observed["wind_from_direction_degrees"] == 270.0
    assert len(calls) == 3
    await client.aclose()


def test_nws_null_missing_units_and_link_safety():
    obs = fixture("nws_observation.json")
    obs["properties"]["windSpeed"] = {"value": None, "unitCode": None}
    assert parse_nws_observation(obs, "KDCA")["wind_speed_mps"] is None
    obs["properties"]["windSpeed"] = None
    assert parse_nws_observation(obs, "KDCA")["wind_speed_mps"] is None
    obs["properties"]["temperature"]["unitCode"] = "wmoUnit:degF"
    with pytest.raises(ValueError, match="unit"):
        parse_nws_observation(obs, "KDCA")
    with pytest.raises(ValueError, match="mismatch"):
        parse_nws_observation(fixture("nws_observation.json"), "KJFK")
    point = fixture("nws_point.json")
    point["properties"]["observationStations"] = "https://example.com/steal"
    with pytest.raises(ValueError, match="link"):
        parse_nws_station_link(point)
    with pytest.raises(ValueError):
        coordinates(1, float("inf"))


@pytest.mark.asyncio
async def test_nws_station_id_rejected_before_http():
    calls = []
    client = fake_client({}, calls)
    result = await GetNwsWeatherObservationTool(config(), client).execute(station_id="../../other")
    assert result["success"] is False and calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_http_revalidation_uses_last_modified():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"ok": True}, headers={
                "Expires": "Thu, 01 Jan 1970 00:00:00 GMT",
                "Last-Modified": "Wed, 30 Sep 2026 12:00:00 GMT"})
        assert request.headers["If-Modified-Since"] == "Wed, 30 Sep 2026 12:00:00 GMT"
        return httpx.Response(304, headers={"Expires": "Fri, 01 Jan 2100 00:00:00 GMT"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = WeatherResponseCache()
    assert await cache.get(client, "https://api.met.no/example") == {"ok": True}
    assert await cache.get(client, "https://api.met.no/example") == {"ok": True}
    assert len(calls) == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_weather_redirect_cannot_leave_official_host():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://example.com/elsewhere"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="outside provider"):
        await WeatherResponseCache().get(client, "https://api.met.no/weatherapi/locationforecast/2.0/compact")
    assert len(calls) == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_weather_redirect_stays_on_provider_and_caches_original_url():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": "/final"})
        return httpx.Response(200, json={"ok": True},
                              headers={"Cache-Control": "max-age=3600"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = WeatherResponseCache()
    assert await cache.get(client, "https://api.met.no/start") == {"ok": True}
    assert await cache.get(client, "https://api.met.no/start") == {"ok": True}
    assert calls == ["https://api.met.no/start", "https://api.met.no/final"]
    await client.aclose()


@pytest.mark.asyncio
async def test_cache_control_max_age_and_no_store():
    calls = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"call": len(calls)},
                              headers={"Cache-Control": "max-age=3600"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    cache = WeatherResponseCache()
    assert (await cache.get(client, "https://api.weather.gov/a"))["call"] == 1
    assert (await cache.get(client, "https://api.weather.gov/a"))["call"] == 1
    assert len(calls) == 1
    await client.aclose()

    calls.clear()
    def no_store(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"call": len(calls)},
                              headers={"Cache-Control": "no-store"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(no_store))
    assert (await cache.get(client, "https://api.weather.gov/b"))["call"] == 1
    assert (await cache.get(client, "https://api.weather.gov/b"))["call"] == 2
    await client.aclose()
