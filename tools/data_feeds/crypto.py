"""Crypto market data tools backed by CoinGecko."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from core.config import AppConfig, PermissionDeniedError
from memory.persistent import PersistentMemory
from tools.base_tool import BaseTool


class CryptoPrice(BaseModel):
    """Normalized crypto price result."""

    symbol: str
    price: float
    change_24h: float | None = None
    volume_24h: float | None = None


class GetCryptoPriceTool(BaseTool):
    """Fetch current crypto prices from CoinGecko."""

    name = "get_crypto_price"
    description = "Fetch the current USD price and 24h metrics for a crypto asset."
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

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch and optionally persist the current market price."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        symbol = str(kwargs["symbol"]).lower()
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
        if self._persistent_memory is not None:
            await self._persistent_memory.insert_market_snapshot(
                {
                    "symbol": price.symbol,
                    "price": price.price,
                    "change_24h": price.change_24h,
                    "volume_24h": price.volume_24h,
                    "metadata": {"source": "coingecko"},
                }
            )
        return self.success(price.model_dump(), source="coingecko")

    def _headers(self) -> dict[str, str]:
        """Build request headers for CoinGecko."""

        headers: dict[str, str] = {}
        if self._config.coingecko_api_key:
            headers["x-cg-demo-api-key"] = self._config.coingecko_api_key
        return headers


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

        symbol = str(kwargs["symbol"]).lower()
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
