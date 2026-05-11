"""Crypto market data tools backed by CoinGecko."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel

from core.config import AppConfig, PermissionDeniedError
from memory.persistent import PersistentMemory
from tools.base_tool import BaseTool

SYMBOL_MAP = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "bnb": "binancecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "dot": "polkadot",
    "matic": "matic-network",
    "link": "chainlink",
    "avax": "avalanche-2",
    "doge": "dogecoin",
    "shib": "shiba-inu",
    "uni": "uniswap",
    "atom": "cosmos",
    "ltc": "litecoin",
}


def normalize_symbol(symbol: str) -> str:
    """Map ticker symbols to CoinGecko IDs; pass through unknown values."""
    s = symbol.lower().strip()
    return SYMBOL_MAP.get(s, s)


class CryptoPrice(BaseModel):
    """Normalized crypto price result."""

    symbol: str
    price: float
    change_24h: float | None = None
    volume_24h: float | None = None


class CachedCryptoPrice(BaseModel):
    """Cached tool result with the fetch timestamp."""

    fetched_at: datetime
    payload: dict[str, Any]


class GetCryptoPriceTool(BaseTool):
    """Fetch current crypto prices from CoinGecko."""

    name = "get_crypto_price"
    description = (
        "Fetch the current USD price and 24h metrics for a crypto asset. "
        "Accepts both ticker (btc, eth, sol) and full name (bitcoin, ethereum)."
    )
    parameters = {
        "type": "object",
        "properties": {"symbol": {"type": "string"}},
        "required": ["symbol"],
    }

    def __init__(
        self,
        config: AppConfig,
        persistent_memory: PersistentMemory | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._persistent_memory = persistent_memory
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._cache: dict[str, CachedCryptoPrice] = {}

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch and optionally persist the current market price."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        symbol = normalize_symbol(str(kwargs["symbol"]))
        cached = self.get_cached_result(symbol)
        if cached is not None and not self._is_cache_stale(cached):
            return cached

        try:
            return await self._fetch_and_cache(symbol)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and cached is not None:
                return self._mark_cached_response(cached, rate_limited=True)
            raise

    def get_cached_result(self, symbol: str) -> dict[str, Any] | None:
        """Return the cached result for a symbol if one exists."""

        cached = self._cache.get(symbol.lower())
        if cached is None:
            return None
        return self._mark_cached_response(cached.payload)

    def _headers(self) -> dict[str, str]:
        """Build request headers for CoinGecko."""

        headers: dict[str, str] = {}
        if self._config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self._config.coingecko_api_key
        return headers

    def _is_cache_stale(self, payload: dict[str, Any]) -> bool:
        """Return whether the cached payload is older than 60 seconds."""

        fetched_at_raw = payload.get("metadata", {}).get("fetched_at")
        if not isinstance(fetched_at_raw, str):
            return True
        fetched_at = datetime.fromisoformat(fetched_at_raw)
        return datetime.now(timezone.utc) - fetched_at > timedelta(seconds=60)

    async def _fetch_and_cache(self, symbol: str) -> dict[str, Any]:
        """Fetch fresh price data from CoinGecko and update the cache."""

        response = await self._client.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={
                "ids": symbol,
                "vs_currencies": "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
            },
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        if symbol not in payload:
            return self.failure(f"Unknown crypto symbol: {symbol}", symbol=symbol)

        price = CryptoPrice(
            symbol=symbol.upper(),
            price=payload[symbol]["usd"],
            change_24h=payload[symbol].get("usd_24h_change"),
            volume_24h=payload[symbol].get("usd_24h_vol"),
        )
        fetched_at = datetime.now(timezone.utc).isoformat()
        result = self.success(price.model_dump(), source="coingecko", fetched_at=fetched_at, cached=False)
        self._cache[symbol] = CachedCryptoPrice(fetched_at=datetime.fromisoformat(fetched_at), payload=result)

        if self._persistent_memory is not None:
            await self._persistent_memory.insert_market_snapshot(
                {
                    "symbol": price.symbol,
                    "price": price.price,
                    "change_24h": price.change_24h,
                    "volume_24h": price.volume_24h,
                    "metadata": {"source": "coingecko", "fetched_at": fetched_at},
                }
            )
        return result

    def _mark_cached_response(self, payload: dict[str, Any], *, rate_limited: bool = False) -> dict[str, Any]:
        """Return a cached response with cache metadata normalized."""

        metadata = dict(payload.get("metadata", {}))
        metadata["cached"] = True
        if rate_limited:
            metadata["rate_limited"] = True
        return {
            "success": payload.get("success", False),
            "result": payload.get("result"),
            "error": payload.get("error"),
            "metadata": metadata,
        }


class GetCryptoHistoryTool(BaseTool):
    """Fetch historical crypto price data from CoinGecko."""

    name = "get_crypto_history"
    description = "Fetch historical USD prices for a crypto asset over the past N days."
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
        },
        "required": ["symbol"],
    }

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch historical price points for a crypto asset."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        symbol = normalize_symbol(str(kwargs["symbol"]))
        days = int(kwargs.get("days", 30))
        response = await self._client.get(
            f"https://api.coingecko.com/api/v3/coins/{symbol}/market_chart",
            params={"vs_currency": "usd", "days": days},
            headers=self._headers(),
        )
        response.raise_for_status()
        payload = response.json()
        prices = [{"timestamp": item[0], "price": item[1]} for item in payload.get("prices", [])]
        return self.success({"symbol": symbol.upper(), "days": days, "prices": prices}, source="coingecko")

    def _headers(self) -> dict[str, str]:
        """Build request headers for CoinGecko."""

        headers: dict[str, str] = {}
        if self._config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self._config.coingecko_api_key
        return headers
