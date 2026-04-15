"""Initialize PostgreSQL tables for Morgoth."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from loguru import logger

from core.config import load_config
from memory.persistent import PersistentMemory


EXTRA_TABLE_STATEMENTS: Sequence[str] = (
    """
    CREATE TABLE IF NOT EXISTS objectives (
        objective_id UUID PRIMARY KEY,
        title VARCHAR(500) NOT NULL,
        description TEXT NOT NULL,
        category VARCHAR(100) NOT NULL,
        priority INTEGER NOT NULL DEFAULT 2,
        generated_by VARCHAR(100) NOT NULL DEFAULT 'morgoth',
        status VARCHAR(50) NOT NULL DEFAULT 'pending',
        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        user_id VARCHAR(100) NOT NULL DEFAULT 'default'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS ui_widgets (
        widget_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        widget_key VARCHAR(150) NOT NULL UNIQUE,
        title VARCHAR(255) NOT NULL,
        description TEXT,
        placement VARCHAR(100) NOT NULL DEFAULT 'dashboard',
        component_name VARCHAR(255) NOT NULL,
        config JSONB NOT NULL DEFAULT '{}'::jsonb,
        status VARCHAR(50) NOT NULL DEFAULT 'draft',
        generated_by VARCHAR(100) NOT NULL DEFAULT 'morgoth',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        user_id VARCHAR(100) NOT NULL DEFAULT 'default'
    );
    """,
)


async def main() -> None:
    """Initialize the PostgreSQL schema required by Morgoth."""

    config = await load_config()
    persistent_memory = PersistentMemory(config)
    try:
        await persistent_memory.initialize()
        for statement in EXTRA_TABLE_STATEMENTS:
            await persistent_memory.execute(statement)
        logger.info("Database initialization complete")
    finally:
        await persistent_memory.close()


if __name__ == "__main__":
    asyncio.run(main())
