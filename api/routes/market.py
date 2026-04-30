"""Market API routes."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Request

from tools.data_feeds.crypto import GetCryptoPriceTool


router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/prices")
async def get_prices(request: Request) -> dict[str, Any]:
    """Return current prices for a small default watchlist."""

    tool_router = request.app.state.tool_router
    price_tool = tool_router.get_tool("get_crypto_price")
    if not isinstance(price_tool, GetCryptoPriceTool):
        raise RuntimeError("get_crypto_price is not registered as GetCryptoPriceTool")

    symbols = ["bitcoin", "ethereum", "solana"]
    items = []
    for symbol in symbols:
        try:
            items.append(await tool_router.execute_tool("get_crypto_price", {"symbol": symbol}))
        except httpx.HTTPStatusError:
            cached = price_tool.get_cached_result(symbol)
            if cached is not None:
                items.append(cached)
            else:
                raise
    return {"items": items}


@router.get("/history/{symbol}")
async def get_history(symbol: str, request: Request, limit: int = 50) -> dict[str, Any]:
    """Return stored market history for a symbol."""

    items = await request.app.state.persistent_memory.get_market_history(symbol.upper(), limit=limit)
    return {"items": items}
