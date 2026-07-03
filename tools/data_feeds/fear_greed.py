"""Crypto Fear & Greed Index tool backed by alternative.me (free, no API key).

Gives Morgoth a market-sentiment source it currently lacks. The index is
a 0-100 composite (0 = extreme fear, 100 = extreme greed) refreshed
daily by alternative.me from volatility, momentum/volume, social media,
surveys, dominance, and trend data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


FNG_BASE_URL = "https://api.alternative.me"


class GetFearGreedIndexTool(BaseTool):
    """Fetch the latest Crypto Fear & Greed Index from alternative.me."""

    name = "get_fear_greed_index"
    is_data_source = True
    description = (
        "Fetch the current Crypto Fear & Greed Index (0-100 composite; "
        "0 = extreme fear, 100 = extreme greed) from alternative.me. "
        "Use this as a market-sentiment data source alongside on-chain "
        "and price data."
    )
    parameters = {
        "type": "object",
        "properties": {},
    }

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        """Fetch the latest F&G reading. No required arguments."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        try:
            resp = await self._client.get(f"{FNG_BASE_URL}/fng/?limit=1")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return self.failure(
                f"alternative.me F&G request failed: {exc}",
                source="alternative.me",
            )

        data = resp.json()
        entries = data.get("data") or []
        if not entries:
            return self.failure(
                "alternative.me returned no F&G entries",
                source="alternative.me",
            )
        latest = entries[0]
        result = {
            "value": int(latest.get("value")) if latest.get("value") is not None else None,
            "value_classification": latest.get("value_classification"),
            "timestamp": latest.get("timestamp"),
        }
        fetched_at = datetime.now(timezone.utc).isoformat()
        return self.success(result, source="alternative.me", fetched_at=fetched_at)
