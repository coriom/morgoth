"""Latest actual NWS station observation, never a forecast substitute."""
from __future__ import annotations

from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool
from tools.data_feeds.weather_http import WeatherResponseCache, nws_station_id, scalar, timestamp


def _quantity(props: dict[str, Any], name: str, accepted: set[str]) -> tuple[float | None, str | None]:
    raw = props.get(name)
    if raw is None:
        return None, None
    if not isinstance(raw, dict):
        raise ValueError(f"missing NWS quantity object: {name}")
    unit = raw.get("unitCode")
    value = scalar(raw.get("value"))
    if value is not None and unit not in accepted:
        raise ValueError(f"unexpected NWS unit for {name}")
    return value, unit if isinstance(unit, str) else None


def parse_nws_observation(data: dict[str, Any], station_id: str) -> dict[str, Any]:
    """Normalize NWS SI units, retaining source unit codes for audit."""
    try:
        props = data["properties"]
        reported_id = props.get("stationId")
        if reported_id and reported_id != station_id:
            raise ValueError("NWS station ID mismatch")
        observed_at = timestamp(props["timestamp"])
        temp, temp_unit = _quantity(props, "temperature", {"wmoUnit:degC"})
        speed, speed_unit = _quantity(props, "windSpeed", {"wmoUnit:km_h-1", "wmoUnit:m_s-1"})
        direction, direction_unit = _quantity(props, "windDirection", {"wmoUnit:degree_(angle)"})
        if speed is not None and speed_unit == "wmoUnit:km_h-1":
            speed /= 3.6  # exact km/h → m/s conversion; tested below
        geometry = data.get("geometry") or {}
        coords = geometry.get("coordinates")
        latitude = longitude = None
        if isinstance(coords, list) and len(coords) >= 2:
            longitude, latitude = scalar(coords[0]), scalar(coords[1])
        return {
            "source": "NWS", "station_id": station_id,
            "latitude": latitude, "longitude": longitude, "observed_at": observed_at,
            "temperature_celsius": temp, "wind_speed_mps": speed,
            "wind_from_direction_degrees": direction,
            "units": {"temperature_celsius": "celsius", "wind_speed_mps": "mps",
                      "wind_from_direction_degrees": "degrees"},
            "source_units": {"temperature": temp_unit, "windSpeed": speed_unit,
                             "windDirection": direction_unit},
        }
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("malformed NWS observation") from exc


class GetNwsWeatherObservationTool(BaseTool):
    """Fetch the latest observed temperature and wind at one NWS station."""

    name = "get_nws_weather_observation"
    description = ("Read the latest actual NWS station temperature and wind. "
                   "Use find_nws_observation_stations first for a US point; null means missing.")
    is_data_source = True
    api_endpoints = ("api.weather.gov/stations",)
    digest_fields = ("temperature_celsius", "wind_speed_mps", "wind_from_direction_degrees")
    parameters = {"type": "object", "properties": {
        "station_id": {"type": "string", "description": "NWS station identifier, e.g. KDCA"}},
        "required": ["station_id"]}

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._cache = WeatherResponseCache(fallback_seconds=120)

    async def close(self) -> None:
        """Close only the client this tool created."""
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return normalized actual observations; no secret or raw HTTP body."""
        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")
        try:
            station = nws_station_id(kwargs.get("station_id"))
            url = f"https://api.weather.gov/stations/{station}/observations/latest"
            data = await self._cache.get(self._client, url)
            return self.success(parse_nws_observation(data, station), source="NWS")
        except (ValueError, httpx.HTTPError) as exc:
            return self.failure(f"NWS observation unavailable: {type(exc).__name__}", source="NWS")
