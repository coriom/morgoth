"""Market API routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/market", tags=["market"])


@router.get("/prices")
async def get_prices(request: Request) -> dict[str, Any]:
    """Return current prices for a small default watchlist."""

    tool_router = request.app.state.tool_router
    symbols = ["bitcoin", "ethereum", "solana"]
    items = []
    for symbol in symbols:
        items.append(await tool_router.execute_tool("get_crypto_price", {"symbol": symbol}))
    return {"items": items}


@router.get("/history/{symbol}")
async def get_history(symbol: str, request: Request, limit: int = 50) -> dict[str, Any]:
    """Return stored market history for a symbol."""

    items = await request.app.state.persistent_memory.get_market_history(symbol.upper(), limit=limit)
    return {"items": items}
