"""Shared, non-secret HTTP identity and bounded response caching for weather feeds.

The cache is keyed by the full request URL, so coordinates and station IDs never
share responses. It honours provider expiry and conditional revalidation.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import math
import re
import time
from typing import Any

import httpx


USER_AGENT = "Morgoth/0.1 (+https://github.com/coriom/morgoth)"


def coordinates(latitude: Any, longitude: Any) -> tuple[str, str]:
    """Validate WGS84 coordinates and use at most four decimal places."""
    values = []
    for raw, limit in ((latitude, 90), (longitude, 180)):
        if isinstance(raw, bool):
            raise ValueError("coordinates must be finite numbers")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError("coordinates must be finite numbers") from None
        if not math.isfinite(value) or abs(value) > limit:
            raise ValueError("coordinates outside WGS84 bounds")
        values.append(f"{value:.4f}")
    return values[0], values[1]


def timestamp(value: Any) -> str:
    """Require a timezone-aware ISO timestamp; return canonical UTC text."""
    if not isinstance(value, str):
        raise ValueError("missing source timestamp")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid source timestamp") from None
    if dt.tzinfo is None:
        raise ValueError("source timestamp has no timezone")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def scalar(value: Any) -> float | None:
    """Return a finite numeric weather reading, retaining explicit nulls."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid source measurement")
    return float(value)


class WeatherResponseCache:
    """Small per-tool response cache; no disk writes or shared Project state."""

    def __init__(self, *, fallback_seconds: int = 300, capacity: int = 64) -> None:
        self._entries: OrderedDict[str, tuple[float, str | None, dict[str, Any]]] = OrderedDict()
        self._fallback_seconds = fallback_seconds
        self._capacity = capacity
        self._lock = asyncio.Lock()

    async def get(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        """Fetch JSON only when expired, revalidating with Last-Modified."""
        async with self._lock:
            return await self._fetch(client, url)

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        """Serialize same-client refreshes and reject off-provider redirects."""
        now = time.time()
        key = url
        cached = self._entries.get(key)
        if cached and cached[0] > now:
            self._entries.move_to_end(key)
            return cached[2]
        headers = {"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"}
        if cached and cached[1]:
            headers["If-Modified-Since"] = cached[1]
        origin = httpx.URL(url)
        for _ in range(3):
            response = await client.get(url, headers=headers, follow_redirects=False)
            if response.status_code == 304 or not response.is_redirect:
                break
            location = response.headers.get("Location")
            if not location:
                raise ValueError("weather redirect without destination")
            target = httpx.URL(url).join(location)
            if target.scheme != "https" or target.host != origin.host:
                raise ValueError("weather redirect outside provider")
            url = str(target)
        else:
            raise ValueError("too many weather redirects")
        if response.status_code == 304:
            if cached is None:
                raise ValueError("304 response without cached body")
            body = cached[2]
            last_modified = cached[1]
        else:
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("weather response must be an object")
            last_modified = response.headers.get("Last-Modified")
        expiry = now + self._fallback_seconds
        control = response.headers.get("Cache-Control", "").lower()
        if "no-store" in control:
            self._entries.pop(key, None)
            return body
        max_age = re.search(r"(?:^|,)\s*max-age=(\d+)", control)
        if "no-cache" in control:
            expiry = now
        elif max_age:
            age = response.headers.get("Age", "0")
            expiry = now + max(0, int(max_age.group(1)) - int(age) if age.isdigit() else int(max_age.group(1)))
        elif expires := response.headers.get("Expires"):
            from email.utils import parsedate_to_datetime
            try:
                expiry = max(now, parsedate_to_datetime(expires).timestamp())
            except (ValueError, TypeError):
                pass
        self._entries[key] = (expiry, last_modified, body)
        self._entries.move_to_end(key)
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return body


def nws_station_id(value: Any) -> str:
    """Allow only a station identifier, never a caller-supplied URL."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9]{3,8}", value):
        raise ValueError("invalid NWS station identifier")
    return value
