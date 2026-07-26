"""FRED economic data connector tools."""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


FRED_BASE_URL = "https://api.stlouisfed.org/fred"


def _key_safe_status_error(response: httpx.Response, api_key: str) -> str | None:
    """Return a sanitized error string on non-2xx, else None.

    httpx's ``response.raise_for_status()`` embeds ``response.request.url``
    (with the ``api_key=...`` query string) in the exception message. That
    exception would flow to the framework's logger, printing the secret.
    Build the error text ourselves so the ONLY things we surface are the
    HTTP status and a URL with the key redacted.
    """
    if 200 <= response.status_code < 300:
        return None
    url = str(response.url)
    if api_key:
        url = url.replace(api_key, "***REDACTED***")
    return f"fred HTTP {response.status_code} on {url}"


class FredSeriesSummary(BaseModel):
    """Normalized FRED series search result."""

    series_id: str
    title: str
    frequency: str | None = None
    units: str | None = None
    seasonal_adjustment: str | None = None
    popularity: int | None = None


class FredObservation(BaseModel):
    """Normalized FRED observation."""

    date: str
    value: float | None
    realtime_start: str | None = None
    realtime_end: str | None = None


class FredSeriesSearchTool(BaseTool):
    """Search FRED economic data series by text query."""

    name = "fred_series_search"
    description = "Search FRED economic indicator series by query text, such as CPI, unemployment, GDP, or Fed funds."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        },
        "required": ["query"],
    }

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Search FRED series and return normalized metadata."""

        self._ensure_internet_allowed()
        api_key = self._api_key()
        if not api_key:
            return self.failure("FRED_API_KEY is required for FRED API access", source="fred")

        query = str(kwargs["query"]).strip()
        limit = int(kwargs.get("limit", 10))
        response = await self._client.get(
            f"{FRED_BASE_URL}/series/search",
            params={
                "api_key": api_key,
                "file_type": "json",
                "search_text": query,
                "limit": limit,
            },
        )
        err = _key_safe_status_error(response, api_key)
        if err is not None:
            return self.failure(err, source="fred")
        payload = response.json()
        items = [self._normalize_series(item).model_dump() for item in payload.get("seriess", [])[:limit]]
        return self.success(items, source="fred", query=query)

    def _ensure_internet_allowed(self) -> None:
        """Raise when internet access is disabled."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

    def _api_key(self) -> str:
        """Return the FRED API key from the process environment."""

        return os.getenv("FRED_API_KEY", "").strip()

    def _normalize_series(self, item: dict[str, Any]) -> FredSeriesSummary:
        """Normalize one FRED search result."""

        return FredSeriesSummary(
            series_id=str(item.get("id", "")),
            title=str(item.get("title", "")),
            frequency=item.get("frequency_short") or item.get("frequency"),
            units=item.get("units_short") or item.get("units"),
            seasonal_adjustment=item.get("seasonal_adjustment_short") or item.get("seasonal_adjustment"),
            popularity=self._optional_int(item.get("popularity")),
        )

    def _optional_int(self, value: Any) -> int | None:
        """Convert a value to int when possible."""

        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class FredSeriesObservationsTool(BaseTool):
    """Fetch observations for a FRED economic data series."""

    name = "fred_series_observations"
    description = "Fetch recent observations for a FRED economic indicator series, for example CPIAUCSL or UNRATE."
    parameters = {
        "type": "object",
        "properties": {
            "series_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            "observation_start": {"type": "string"},
            "observation_end": {"type": "string"},
            "frequency": {"type": "string"},
        },
        "required": ["series_id"],
    }

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch and normalize FRED observations."""

        self._ensure_internet_allowed()
        api_key = self._api_key()
        if not api_key:
            return self.failure("FRED_API_KEY is required for FRED API access", source="fred")

        series_id = str(kwargs["series_id"]).upper().strip()
        limit = int(kwargs.get("limit", 100))
        params: dict[str, Any] = {
            "api_key": api_key,
            "file_type": "json",
            "series_id": series_id,
            "sort_order": "desc",
            "limit": limit,
        }
        for key in ("observation_start", "observation_end", "frequency"):
            if kwargs.get(key):
                params[key] = kwargs[key]

        response = await self._client.get(f"{FRED_BASE_URL}/series/observations", params=params)
        err = _key_safe_status_error(response, api_key)
        if err is not None:
            return self.failure(err, source="fred")
        payload = response.json()
        observations = [self._normalize_observation(item).model_dump() for item in payload.get("observations", [])]
        observations.reverse()
        return self.success(
            {"series_id": series_id, "observations": observations},
            source="fred",
            count=len(observations),
        )

    def _ensure_internet_allowed(self) -> None:
        """Raise when internet access is disabled."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

    def _api_key(self) -> str:
        """Return the FRED API key from the process environment."""

        return os.getenv("FRED_API_KEY", "").strip()

    def _normalize_observation(self, item: dict[str, Any]) -> FredObservation:
        """Normalize one FRED observation."""

        return FredObservation(
            date=str(item.get("date", "")),
            value=self._optional_float(item.get("value")),
            realtime_start=item.get("realtime_start"),
            realtime_end=item.get("realtime_end"),
        )

    def _optional_float(self, value: Any) -> float | None:
        """Convert FRED values to floats, treating missing '.' values as null."""

        if value in (None, "."):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
