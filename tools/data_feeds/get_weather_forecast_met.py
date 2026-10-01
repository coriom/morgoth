"""MET Norway Locationforecast 2.0 compact forecast for one coordinate."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool
from tools.data_feeds.weather_http import WeatherResponseCache, coordinates, scalar, timestamp


_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
_UNITS = {"air_temperature": "celsius", "wind_speed": "m/s",
          "wind_from_direction": "degrees", "precipitation_amount": "mm"}


def parse_met_forecast(data: dict[str, Any], latitude: str, longitude: str) -> dict[str, Any]:
    """Extract bounded instant and one-hour-period facts from official GeoJSON."""
    try:
        properties = data["properties"]
        meta = properties["meta"]
        units = meta["units"]
        issued_at = timestamp(meta["updated_at"])
        series = properties["timeseries"]
        if not isinstance(series, list) or not series:
            raise ValueError("empty forecast timeseries")
        for field, unit in _UNITS.items():
            if units.get(field) != unit:
                raise ValueError(f"unexpected MET unit for {field}")
        rows: list[dict[str, Any]] = []
        for item in series[:12]:
            valid_at = timestamp(item["time"])
            instant = item["data"]["instant"]["details"]
            if not isinstance(instant, dict):
                raise ValueError("invalid forecast instant")
            row: dict[str, Any] = {
                "valid_at": valid_at,
                "air_temperature_celsius": scalar(instant.get("air_temperature")),
                "wind_speed_mps": scalar(instant.get("wind_speed")),
                "wind_from_direction_degrees": scalar(instant.get("wind_from_direction")),
            }
            period = item["data"].get("next_1_hours")
            if period is not None:
                amount = scalar(period["details"].get("precipitation_amount"))
                end_at = (datetime.fromisoformat(valid_at.replace("Z", "+00:00"))
                          + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
                row["next_1_hours"] = {"start_at": valid_at, "end_at": end_at,
                                       "precipitation_amount_mm": amount}
            rows.append(row)
        first = rows[0]
        return {
            "source": "MET Norway", "latitude": float(latitude), "longitude": float(longitude),
            "attribution": "Data from MET Norway",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "modified_from_source": True,
            "issued_at": issued_at, "valid_at": first["valid_at"],
            "air_temperature_celsius": first["air_temperature_celsius"],
            "wind_speed_mps": first["wind_speed_mps"],
            "wind_from_direction_degrees": first["wind_from_direction_degrees"],
            "precipitation_amount_mm": first.get("next_1_hours", {}).get("precipitation_amount_mm"),
            "precipitation_period_start_at": first.get("next_1_hours", {}).get("start_at"),
            "precipitation_period_end_at": first.get("next_1_hours", {}).get("end_at"),
            "records": rows,
            "units": {"air_temperature_celsius": "celsius", "wind_speed_mps": "mps",
                      "wind_from_direction_degrees": "degrees", "precipitation_amount_mm": "millimeter"},
        }
    except (KeyError, TypeError, AttributeError, IndexError) as exc:
        raise ValueError("malformed MET forecast response") from exc


class GetWeatherForecastMetTool(BaseTool):
    """Fetch a bounded MET forecast with instant and period values separated."""

    name = "get_weather_forecast_met"
    description = ("Forecast temperature, wind and next-hour precipitation from MET Norway "
                   "for validated latitude/longitude. Precipitation is a period, not an instant.")
    is_data_source = True
    api_endpoints = ("api.met.no/weatherapi/locationforecast/2.0/compact",)
    digest_fields = ("air_temperature_celsius", "wind_speed_mps",
                     "wind_from_direction_degrees", "precipitation_amount_mm")
    parameters = {"type": "object", "properties": {
        "latitude": {"type": "number", "description": "WGS84 latitude"},
        "longitude": {"type": "number", "description": "WGS84 longitude"}},
        "required": ["latitude", "longitude"]}

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=20.0)
        self._owns_client = client is None
        self._cache = WeatherResponseCache()

    async def close(self) -> None:
        """Close only the HTTP client this tool created."""
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Return normalized forecast facts; no partial malformed success."""
        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")
        try:
            lat, lon = coordinates(kwargs.get("latitude"), kwargs.get("longitude"))
            data = await self._cache.get(self._client, f"{_URL}?lat={lat}&lon={lon}")
            return self.success(parse_met_forecast(data, lat, lon), source="MET Norway")
        except (ValueError, httpx.HTTPError) as exc:
            return self.failure(f"MET forecast unavailable: {type(exc).__name__}", source="MET Norway")
