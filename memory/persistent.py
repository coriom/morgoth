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
            try:
                await connection.execute(
                    "ALTER TABLE objectives ADD COLUMN IF NOT EXISTS cycle_count INTEGER DEFAULT 0;"
                )
            except Exception as exc:
                logger.warning("Could not add cycle_count column (objectives table may not exist yet): {}", exc)
            try:
                await connection.execute(
                    "ALTER TABLE objectives ADD COLUMN IF NOT EXISTS sources_used JSONB DEFAULT '[]'::jsonb;"
                )
            except Exception as exc:
                logger.warning("Could not add sources_used column (objectives table may not exist yet): {}", exc)
            try:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS theses (
                        thesis_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        subject TEXT NOT NULL,
                        claim TEXT NOT NULL,
                        confidence TEXT DEFAULT 'medium',
                        evidence JSONB DEFAULT '[]'::jsonb,
                        status TEXT DEFAULT 'active',
                        objective_id TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS theses_subject_idx ON theses (subject);"
                )
            except Exception as exc:
                logger.warning("Could not ensure theses table (non-fatal): {}", exc)

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

    async def get_objectives(
        self,
        status: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Return objectives from PostgreSQL, optionally filtered by status."""

        pool = self._require_pool()
        async with pool.acquire() as conn:
            if status is not None:
                rows = await conn.fetch(
                    "SELECT * FROM objectives WHERE status = $1 "
                    "ORDER BY priority ASC, created_at ASC LIMIT $2",
                    status,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM objectives "
                    "ORDER BY priority ASC, created_at ASC LIMIT $1",
                    limit,
                )
        return [dict(row) for row in rows]

    async def update_objective(
        self,
        objective_id: str,
        status: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update an objective's status and/or append an evidence entry, return the updated row.

        evidence is a JSONB array that grows over time; each call appends one entry via ||.
        Callers pass a single dict; this method wraps it in a list before concatenating.
        """

        import uuid as _uuid

        pool = self._require_pool()
        async with pool.acquire() as conn:
            set_clauses: list[str] = []
            params: list[Any] = []
            idx = 1

            if status is not None:
                set_clauses.append(f"status = ${idx}")
                params.append(status)
                idx += 1
                if status == "done":
                    set_clauses.append("completed_at = NOW()")

            if evidence is not None:
                set_clauses.append(f"evidence = evidence || ${idx}::jsonb")
                params.append(json.dumps([evidence]))
                idx += 1

            if not set_clauses:
                row = await conn.fetchrow(
                    "SELECT * FROM objectives WHERE objective_id = $1",
                    _uuid.UUID(objective_id),
                )
            else:
                params.append(_uuid.UUID(objective_id))
                query = (
                    f"UPDATE objectives SET {', '.join(set_clauses)} "
                    f"WHERE objective_id = ${idx} RETURNING *"
                )
                row = await conn.fetchrow(query, *params)

        if row is None:
            raise ValueError(f"Objective {objective_id} not found")
        return dict(row)

    async def increment_cycle_count(self, objective_id: str) -> int:
        """Atomically increment cycle_count for an objective and return the new value."""

        import uuid as _uuid

        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE objectives SET cycle_count = cycle_count + 1 "
                "WHERE objective_id = $1 RETURNING cycle_count",
                _uuid.UUID(str(objective_id)),
            )
        return row["cycle_count"] if row else 0

    async def get_sources_used(self, objective_id: str) -> list[str]:
        """Return the list of distinct data-source tool names used for an objective."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT sources_used FROM objectives WHERE objective_id = $1",
                objective_id,
            )
        if not row or row["sources_used"] is None:
            return []
        val = row["sources_used"]
        return list(val) if isinstance(val, list) else json.loads(val)

    async def add_source_used(self, objective_id: str, source: str) -> list[str]:
        """Append a data-source tool name to sources_used (no duplicates). Returns updated list."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE objectives "
                "SET sources_used = ("
                "  SELECT to_jsonb(array(SELECT DISTINCT "
                "    jsonb_array_elements_text("
                "      coalesce(sources_used,'[]'::jsonb) || "
                "      to_jsonb($2::text)"
                "  )))"
                ") "
                "WHERE objective_id = $1 RETURNING sources_used",
                objective_id,
                source,
            )
        if not row or row["sources_used"] is None:
            return []
        val = row["sources_used"]
        return list(val) if isinstance(val, list) else json.loads(val)

    async def add_thesis(
        self,
        subject: str,
        claim: str,
        confidence: str = "medium",
        evidence: list[dict[str, Any]] | None = None,
        objective_id: str | None = None,
    ) -> str:
        """Insert a thesis row and return its thesis_id as a string."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO theses (subject, claim, confidence, evidence, objective_id) "
                "VALUES ($1, $2, $3, $4::jsonb, $5) RETURNING thesis_id",
                subject,
                claim,
                confidence,
                json.dumps(evidence or []),
                objective_id,
            )
        return str(row["thesis_id"]) if row else ""

    async def get_theses(
        self,
        status: str | None = None,
        subject: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return theses optionally filtered by status and/or subject (newest first)."""
        pool = self._require_pool()
        clauses: list[str] = []
        params: list[Any] = []
        idx = 1
        if status is not None:
            clauses.append(f"status = ${idx}")
            params.append(status)
            idx += 1
        if subject is not None:
            clauses.append(f"subject = ${idx}")
            params.append(subject)
            idx += 1
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        query = f"SELECT * FROM theses {where} ORDER BY created_at DESC LIMIT ${idx}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]

    async def create_objective(
        self,
        title: str,
        description: str,
        priority: int = 3,
    ) -> dict[str, Any]:
        """Insert a new objective into PostgreSQL and return the created row."""

        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO objectives "
                "(objective_id, title, description, category, priority, generated_by, status, user_id) "
                "VALUES (gen_random_uuid(), $1, $2, 'research', $3, 'morgoth_autonomous', 'pending', 'default') "
                "RETURNING *",
                title,
                description,
                priority,
            )
        return dict(row)

    async def get_objectives_stats(self) -> dict[str, Any]:
        """Return aggregated statistics about objectives, computed in SQL."""

        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    COUNT(*) AS total,
                    status,
                    AVG(cycle_count) AS avg_cycles,
                    COUNT(*) FILTER (WHERE evidence @> '[{"auto_completed": true}]'::jsonb) AS auto_done,
                    COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '24 hours') AS last_24h
                FROM objectives
                GROUP BY status
                """
            )
            title_rows = await conn.fetch(
                """
                SELECT title FROM objectives
                GROUP BY title
                ORDER BY MAX(created_at) DESC
                LIMIT 20
                """
            )

        total = 0
        by_status: dict[str, int] = {}
        auto_completed = 0
        manually_completed = 0
        weighted_cycles = 0.0
        last_24h_count = 0

        for row in rows:
            status = row["status"]
            count = int(row["total"])
            total += count
            by_status[status] = count
            auto_completed += int(row["auto_done"])
            if status == "done":
                manually_completed += count - int(row["auto_done"])
            last_24h_count += int(row["last_24h"])
            if row["avg_cycles"] is not None:
                weighted_cycles += float(row["avg_cycles"]) * count

        avg_cycles = weighted_cycles / total if total > 0 else 0.0
        topics = [row["title"] for row in title_rows]

        return {
            "total": total,
            "by_status": by_status,
            "auto_completed": auto_completed,
            "manually_completed": manually_completed,
            "topics": topics,
            "avg_cycles_per_objective": avg_cycles,
            "objectives_per_hour": last_24h_count / 24,
        }

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
