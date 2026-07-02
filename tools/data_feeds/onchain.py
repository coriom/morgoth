"""Bitcoin on-chain data tool backed by mempool.space (free, no API key).

Closes the gap that caused Morgoth to hallucinate inert on-chain tool calls
(e.g. get_crypto_trend) on Bitcoin objectives: it now has a real source for
hash rate, difficulty, fees, and mempool state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


MEMPOOL_BASE_URL = "https://mempool.space"


class GetBitcoinOnchainTool(BaseTool):
    """Fetch a compact Bitcoin on-chain health digest from mempool.space."""

    name = "get_bitcoin_onchain"
    is_data_source = True
    description = (
        "Fetch real Bitcoin on-chain metrics: current hash rate, mining "
        "difficulty + next adjustment, recommended fees (sat/vB), and "
        "mempool size. Use this for any Bitcoin on-chain investigation "
        "(hash rate, difficulty, fees, transaction volume) instead of "
        "asking for a tool that does not exist."
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
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        """Fetch the on-chain digest. No required arguments."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        try:
            hashrate_resp = await self._client.get(
                f"{MEMPOOL_BASE_URL}/api/v1/mining/hashrate/3d"
            )
            hashrate_resp.raise_for_status()
            difficulty_resp = await self._client.get(
                f"{MEMPOOL_BASE_URL}/api/v1/difficulty-adjustment"
            )
            difficulty_resp.raise_for_status()
            fees_resp = await self._client.get(
                f"{MEMPOOL_BASE_URL}/api/v1/fees/recommended"
            )
            fees_resp.raise_for_status()
            mempool_resp = await self._client.get(f"{MEMPOOL_BASE_URL}/api/mempool")
            mempool_resp.raise_for_status()
        except httpx.HTTPError as exc:
            return self.failure(
                f"mempool.space request failed: {exc}", source="mempool.space"
            )

        hashrate_data = hashrate_resp.json()
        difficulty_data = difficulty_resp.json()
        fees_data = fees_resp.json()
        mempool_data = mempool_resp.json()

        result = {
            "hash_rate": hashrate_data.get("currentHashrate"),
            "difficulty": hashrate_data.get("currentDifficulty"),
            "next_difficulty_adjustment": {
                "progress_percent": difficulty_data.get("progressPercent"),
                "estimated_change_percent": difficulty_data.get("difficultyChange"),
                "remaining_blocks": difficulty_data.get("remainingBlocks"),
                "estimated_retarget_date_ms": difficulty_data.get("estimatedRetargetDate"),
            },
            "fees_sat_vb": {
                "fastest": fees_data.get("fastestFee"),
                "half_hour": fees_data.get("halfHourFee"),
                "hour": fees_data.get("hourFee"),
                "economy": fees_data.get("economyFee"),
                "minimum": fees_data.get("minimumFee"),
            },
            "mempool_tx_count": mempool_data.get("count"),
            "mempool_vsize": mempool_data.get("vsize"),
        }
        fetched_at = datetime.now(timezone.utc).isoformat()
        return self.success(result, source="mempool.space", fetched_at=fetched_at)
