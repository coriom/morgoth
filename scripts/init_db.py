"""Initialize PostgreSQL tables for Morgoth."""

from __future__ import annotations

import asyncio

from loguru import logger

from core.config import load_config
from memory.persistent import PersistentMemory


async def main() -> None:
    """Initialize the PostgreSQL schema required by Morgoth."""

    config = await load_config()
    persistent_memory = PersistentMemory(config)
    await persistent_memory.initialize()
    await persistent_memory.close()
    logger.info("Database initialization complete")


if __name__ == "__main__":
    asyncio.run(main())
