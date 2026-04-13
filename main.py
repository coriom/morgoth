"""Main entry point for Morgoth."""

from __future__ import annotations

import asyncio

import uvicorn
from loguru import logger

from api.server import app


async def main() -> None:
    """Run the Morgoth API server and trigger the bootstrap protocol."""

    logger.info("Starting Morgoth Phase 1 runtime")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
