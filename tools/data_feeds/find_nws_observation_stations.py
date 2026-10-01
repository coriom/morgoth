"""Resolve NWS observation stations linked to a US forecast grid point."""
from __future__ import annotations

import re
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool
from tools.data_feeds.weather_http import WeatherResponseCache, coordinates, nws_station_id


def parse_nws_station_link(data: dict[str, Any]) -> str:
    """Accept only the official NWS gridpoint-stations link, never arbitrary URLs."""
    try:
        raw = data["properties"]["observationStations"]
        url = httpx.URL(raw)
        if (url.scheme != "https" or url.host != "api.weather.gov" or url.query
                or not re.fullmatch(r"/gridpoints/[A-Z]{3}/\d+,\d+/stations", url.path)):
            raise ValueError("invalid NWS station link")
        return str(url)
    except (KeyError, TypeError, httpx.InvalidURL) as exc:
        raise ValueError("malformed NWS point response") from exc


def parse_nws_stations(data: dict[str, Any], latitude: str, longitude: str) -> dict[str, Any]:
    """Preserve NWS association order; do not claim stations are nearest."""
    try:
        features = data["features"]
        if not isinstance(features, list) or not features:
            raise ValueError("no NWS stations for point")
        stations = []
        for item in features[:8]:
            props = item["properties"]
            identifier = nws_station_id(props["stationIdentifier"])
            geometry = item.get("geometry") or {}
            coords = geometry.get("coordinates")
            station = {"station_id": identifier, "name": props.get("name")}
            if isinstance(coords, list) and len(coords) >= 2:
                station["latitude"] = float(coords[1])
                station["longitude"] = float(coords[0])
            stations.append(station)
        return {"source": "NWS", "requested_latitude": float(latitude),
                "requested_longitude": float(longitude), "stations": stations,
                "ordering": "NWS gridpoint association order; distance not asserted"}
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError("malformed NWS station collection") from exc


class FindNwsObservationStationsTool(BaseTool):
    """Discover NWS station IDs for a point using the official linked API."""

    name = "find_nws_observation_stations"
    description = ("Find NWS observation stations associated with a US latitude/longitude. "
                   "Returns station IDs in NWS order, not a nearest-station ranking.")
    is_data_source = False
    api_endpoints = ("api.weather.gov/points", "api.weather.gov/gridpoints")
    digest_fields = ()
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
        """Close only the client this tool created."""
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Resolve one point then its linked station collection."""
        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")
        try:
            lat, lon = coordinates(kwargs.get("latitude"), kwargs.get("longitude"))
            point = await self._cache.get(self._client, f"https://api.weather.gov/points/{lat},{lon}")
            link = parse_nws_station_link(point)
            stations = await self._cache.get(self._client, link)
            return self.success(parse_nws_stations(stations, lat, lon), source="NWS")
        except (ValueError, httpx.HTTPError) as exc:
            return self.failure(f"NWS station discovery unavailable: {type(exc).__name__}", source="NWS")
