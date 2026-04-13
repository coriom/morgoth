"""Async PostgreSQL persistence layer for Morgoth."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

import asyncpg
from asyncpg import Pool, Record
from loguru import logger
from pydantic import BaseModel

from core.config import AppConfig


CREATE_EXTENSION_SQL = 'CREATE EXTENSION IF NOT EXISTS "pgcrypto";'

TABLE_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tasks (
        task_id UUID PRIMARY KEY,
        type VARCHAR(20),
        priority INTEGER,
        description TEXT,
        agent_id UUID,
        created_by VARCHAR(50),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        scheduled_at TIMESTAMPTZ,
        recurrence_cron VARCHAR(100),
        status VARCHAR(20),
        result JSONB,
        user_id VARCHAR(100) DEFAULT 'default'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        agent_id UUID PRIMARY KEY,
        name VARCHAR(100),
        agent_type VARCHAR(20),
        status VARCHAR(20),
        model VARCHAR(100),
        tools JSONB,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        stopped_at TIMESTAMPTZ,
        user_id VARCHAR(100) DEFAULT 'default'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS logs (
        log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        level VARCHAR(20),
        agent VARCHAR(100),
        content TEXT,
        tokens_used INTEGER,
        duration_ms INTEGER,
        user_id VARCHAR(100) DEFAULT 'default'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge (
        fact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        category VARCHAR(100),
        key VARCHAR(255),
        value TEXT,
        source VARCHAR(255),
        confidence FLOAT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW(),
        user_id VARCHAR(100) DEFAULT 'default'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS self_modifications (
        mod_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        file_path VARCHAR(500),
        diff TEXT,
        reason TEXT,
        test_result JSONB,
        approved_by VARCHAR(50),
        user_id VARCHAR(100) DEFAULT 'default'
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        timestamp TIMESTAMPTZ DEFAULT NOW(),
        symbol VARCHAR(20),
        price FLOAT,
        change_24h FLOAT,
        volume_24h FLOAT,
        metadata JSONB
    );
    """,
)


class QueryResult(BaseModel):
    """Normalized result for database writes."""

    status: str
    rows_affected: int = 0


class PersistentMemory:
    """Async PostgreSQL client that initializes Morgoth tables on startup."""

    def __init__(self, config: AppConfig) -> None:
        """Store configuration required for database access."""

        self._config = config
        self._pool: Pool | None = None

    async def initialize(self) -> None:
        """Create the connection pool and initialize required tables."""

        self._pool = await asyncpg.create_pool(dsn=self._config.postgres_url)
        async with self._pool.acquire() as connection:
            await connection.execute(CREATE_EXTENSION_SQL)
            for statement in TABLE_STATEMENTS:
                await connection.execute(statement)

        logger.info("PostgreSQL pool initialized and schema ensured")

    async def close(self) -> None:
        """Close the PostgreSQL connection pool."""

        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def execute(self, query: str, *args: Any) -> QueryResult:
        """Execute a write statement and normalize the status output."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            status = await connection.execute(query, *args)

        parts = status.split()
        rows_affected = int(parts[-1]) if parts and parts[-1].isdigit() else 0
        return QueryResult(status=status, rows_affected=rows_affected)

    async def fetch(self, query: str, *args: Any) -> list[Record]:
        """Run a read query and return raw asyncpg records."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Record | None:
        """Run a read query and return a single row."""

        pool = self._require_pool()
        async with pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def save_agent(self, payload: dict[str, Any]) -> QueryResult:
        """Upsert an agent record."""

        created_at = self._normalize_datetime(payload["created_at"])
        stopped_at = self._normalize_datetime(payload.get("stopped_at"))
        query = """
        INSERT INTO agents (agent_id, name, agent_type, status, model, tools, created_at, stopped_at, user_id)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9)
        ON CONFLICT (agent_id) DO UPDATE
        SET name = EXCLUDED.name,
            agent_type = EXCLUDED.agent_type,
            status = EXCLUDED.status,
            model = EXCLUDED.model,
            tools = EXCLUDED.tools,
            created_at = EXCLUDED.created_at,
            stopped_at = EXCLUDED.stopped_at,
            user_id = EXCLUDED.user_id
        """
        return await self.execute(
            query,
            payload["agent_id"],
            payload["name"],
            payload["agent_type"],
            payload["status"],
            payload["model"],
            json.dumps(payload["tools"]),
            created_at,
            stopped_at,
            payload["user_id"],
        )

    async def list_agents(self) -> list[dict[str, Any]]:
        """Return all agent rows as dictionaries."""

        rows = await self.fetch("SELECT * FROM agents ORDER BY created_at DESC")
        return [dict(row) for row in rows]

    async def save_task(self, payload: dict[str, Any]) -> QueryResult:
        """Upsert a task record."""

        created_at = self._normalize_datetime(payload["created_at"])
        scheduled_at = self._normalize_datetime(payload.get("scheduled_at"))
        query = """
        INSERT INTO tasks (
            task_id, type, priority, description, agent_id, created_by, created_at,
            scheduled_at, recurrence_cron, status, result, user_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
        ON CONFLICT (task_id) DO UPDATE
        SET type = EXCLUDED.type,
            priority = EXCLUDED.priority,
            description = EXCLUDED.description,
            agent_id = EXCLUDED.agent_id,
            created_by = EXCLUDED.created_by,
            created_at = EXCLUDED.created_at,
            scheduled_at = EXCLUDED.scheduled_at,
            recurrence_cron = EXCLUDED.recurrence_cron,
            status = EXCLUDED.status,
            result = EXCLUDED.result,
            user_id = EXCLUDED.user_id
        """
        return await self.execute(
            query,
            payload["task_id"],
            payload["type"],
            payload["priority"],
            payload["description"],
            payload.get("agent_id"),
            payload["created_by"],
            created_at,
            scheduled_at,
            payload.get("recurrence_cron"),
            payload["status"],
            json.dumps(payload.get("result")),
            payload["user_id"],
        )

    async def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        """Return task rows, optionally filtered by status."""

        if status is None:
            rows = await self.fetch("SELECT * FROM tasks ORDER BY created_at ASC")
        else:
            rows = await self.fetch("SELECT * FROM tasks WHERE status = $1 ORDER BY created_at ASC", status)
        return [self._normalize_task_row(row) for row in rows]

    async def insert_log(self, payload: dict[str, Any]) -> QueryResult:
        """Insert a log row."""

        query = """
        INSERT INTO logs (level, agent, content, tokens_used, duration_ms, user_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        """
        return await self.execute(
            query,
            payload["level"],
            payload["agent"],
            payload["content"],
            payload.get("tokens_used"),
            payload.get("duration_ms"),
            payload["user_id"],
        )

    async def list_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent logs from PostgreSQL."""

        rows = await self.fetch("SELECT * FROM logs ORDER BY timestamp DESC LIMIT $1", limit)
        return [dict(row) for row in rows]

    async def insert_market_snapshot(self, payload: dict[str, Any]) -> QueryResult:
        """Insert a market snapshot row."""

        query = """
        INSERT INTO market_snapshots (symbol, price, change_24h, volume_24h, metadata)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """
        return await self.execute(
            query,
            payload["symbol"],
            payload["price"],
            payload.get("change_24h"),
            payload.get("volume_24h"),
            json.dumps(payload.get("metadata", {})),
        )

    async def get_market_history(self, symbol: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent market snapshots for a symbol."""

        rows = await self.fetch(
            "SELECT * FROM market_snapshots WHERE symbol = $1 ORDER BY timestamp DESC LIMIT $2",
            symbol,
            limit,
        )
        return [dict(row) for row in rows]

    def _require_pool(self) -> Pool:
        """Return the initialized pool or raise an error."""

        if self._pool is None:
            raise RuntimeError("PersistentMemory.initialize() must be called before database operations")
        return self._pool

    def _normalize_datetime(self, value: datetime | str | None) -> datetime | None:
        """Convert ISO8601 strings to ``datetime`` before passing them to asyncpg."""

        if value is None or isinstance(value, datetime):
            return value
        return datetime.fromisoformat(value)

    def _normalize_task_row(self, row: Record) -> dict[str, Any]:
        """Normalize task rows read from PostgreSQL."""

        task_row = dict(row)
        task_row["result"] = json.loads(task_row["result"]) if task_row["result"] and task_row["result"] != "null" else None
        return task_row
