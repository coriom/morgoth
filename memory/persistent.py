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
            try:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS contradictions (
                        contradiction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        subject_group TEXT,
                        thesis_id_a UUID NOT NULL,
                        thesis_id_b UUID NOT NULL,
                        detected_at TIMESTAMPTZ DEFAULT NOW()
                    );
                    """
                )
            except Exception as exc:
                logger.warning("Could not ensure contradictions table (non-fatal): {}", exc)
            # Temporal-semantics columns for the detector (step: revision vs
            # contradiction). superseded_by points at the newer thesis that
            # replaced this one; resolution on a contradiction row records
            # why an existing pair was voided (timeframe guard, supersession
            # reclassification, ...).
            for alter_sql in (
                "ALTER TABLE theses ADD COLUMN IF NOT EXISTS superseded_by UUID NULL;",
                "ALTER TABLE contradictions ADD COLUMN IF NOT EXISTS resolution TEXT NULL;",
                "ALTER TABLE self_modify_proposals ADD COLUMN IF NOT EXISTS "
                "proposed_by TEXT NOT NULL DEFAULT 'human';",
                "ALTER TABLE self_modify_proposals ADD COLUMN IF NOT EXISTS "
                "engine TEXT NOT NULL DEFAULT 'ollama';",
                # retry_of points at the proposal_id of a preceding
                # pre-submit reject that this attempt is correcting.
                # NULL for first attempts. Used by the reflect
                # retry-with-feedback loop and by the calibration axis
                # to distinguish first-shot proposals from corrections.
                "ALTER TABLE self_modify_proposals ADD COLUMN IF NOT EXISTS "
                "retry_of UUID NULL;",
            ):
                try:
                    await connection.execute(alter_sql)
                except Exception as exc:
                    logger.warning(
                        "Could not apply schema alter (non-fatal): {} :: {}",
                        alter_sql, exc,
                    )
            # Operator focus directive: one active row at a time (WHERE
            # cleared_at IS NULL). History is preserved — a directive is
            # never deleted, just tombstoned with cleared_at.
            try:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS focus_directives (
                        focus_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        directive TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        cleared_at TIMESTAMPTZ NULL
                    );
                    """
                )
            except Exception as exc:
                logger.warning(
                    "Could not ensure focus_directives table (non-fatal): {}", exc
                )
            try:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS self_modify_proposals (
                        proposal_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW(),
                        target_path TEXT NOT NULL,
                        change_type TEXT NOT NULL,
                        content TEXT NOT NULL,
                        rationale TEXT,
                        status TEXT NOT NULL DEFAULT 'submitted',
                        status_reason TEXT
                    );
                    """
                )
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS self_modify_proposals_status_idx "
                    "ON self_modify_proposals (status);"
                )
            except Exception as exc:
                logger.warning(
                    "Could not ensure self_modify_proposals table (non-fatal): {}", exc
                )
            # Shadow Gate 2.5 — LLM verifier verdicts on non-deterministic
            # axes. RECORDED, NEVER ENFORCED. Blind by construction: the
            # shadow input excludes proposal status/status_reason so the
            # verifier can't cheat off the operator's own decision. Rows
            # are append-only calibration data — the shadow never mutates
            # the proposal.
            try:
                await connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS shadow_verdicts (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        proposal_id UUID NOT NULL,
                        verdict TEXT NOT NULL,
                        axes JSONB NOT NULL DEFAULT '{}'::jsonb,
                        reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
                        engine TEXT NOT NULL,
                        prompt_version TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS shadow_verdicts_proposal_idx "
                    "ON shadow_verdicts (proposal_id);"
                )
            except Exception as exc:
                logger.warning(
                    "Could not ensure shadow_verdicts table (non-fatal): {}", exc
                )

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

    async def timeout_stale_objectives(
        self,
        max_age_days: float,
    ) -> list[dict[str, Any]]:
        """Terminate non-terminal objectives older than ``max_age_days``.

        Runs one atomic UPDATE ... RETURNING, transitioning any
        ``pending`` or ``in_progress`` row whose ``created_at`` is
        STRICTLY OLDER than ``NOW() - max_age_days * INTERVAL '1 day'``
        into the ``stale_timeout`` terminal status. Returns the
        terminated rows' identifying fields for a logging pass.

        MAX_CYCLES bounds an objective's active lifetime to <1h of
        cycling, so any non-terminal row days old is by construction
        abandoned — no updated_at column exists (see schema); the
        created_at test is safe and requires no schema change. The
        SELECTOR only reads ``status='pending'``, so an
        ``in_progress`` row stuck by a lost cycle-end update becomes
        permanently unreachable without this sweep.

        Strict inequality on the boundary means a fresh row created
        EXACTLY N days ago survives — the test lock covers it.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE objectives SET status = 'stale_timeout' "
                "WHERE status IN ('pending', 'in_progress') "
                "AND created_at < NOW() - ($1::float * INTERVAL '1 day') "
                "RETURNING objective_id, title, created_at, cycle_count",
                float(max_age_days),
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
            # Substring + case-insensitive so /api/theses?subject=BTC matches
            # "BTC short-term price" as well as "Bitcoin BTC volume".
            clauses.append(f"subject ILIKE '%' || ${idx} || '%'")
            params.append(subject)
            idx += 1
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        query = f"SELECT * FROM theses {where} ORDER BY created_at DESC LIMIT ${idx}"
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
        return [dict(row) for row in rows]

    async def get_contradictions(
        self,
        limit: int = 50,
        *,
        unresolved_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return contradictions with their two theses pre-joined (newest first).

        LEFT JOIN so a contradiction whose thesis row was deleted yields nulls
        on that side instead of disappearing from the result set — surfaces
        partial state to the API rather than hiding it.

        ``unresolved_only=True`` filters to rows where ``resolution IS NULL``,
        i.e. genuine live contradictions (used by /api/contradictions and
        the wiki). ``resolution`` is included in every returned row so
        downstream callers can see the reason a pair was voided.
        """
        pool = self._require_pool()
        where = "WHERE c.resolution IS NULL " if unresolved_only else ""
        query = (
            "SELECT "
            "  c.contradiction_id, c.subject_group, c.detected_at, "
            "  c.resolution, "
            "  c.thesis_id_a, "
            "  ta.subject AS subject_a, ta.claim AS claim_a, "
            "  ta.confidence AS confidence_a, ta.status AS status_a, "
            "  ta.evidence AS evidence_a, ta.objective_id AS objective_id_a, "
            "  ta.created_at AS created_at_a, "
            "  c.thesis_id_b, "
            "  tb.subject AS subject_b, tb.claim AS claim_b, "
            "  tb.confidence AS confidence_b, tb.status AS status_b, "
            "  tb.evidence AS evidence_b, tb.objective_id AS objective_id_b, "
            "  tb.created_at AS created_at_b "
            "FROM contradictions c "
            "LEFT JOIN theses ta ON ta.thesis_id = c.thesis_id_a "
            "LEFT JOIN theses tb ON tb.thesis_id = c.thesis_id_b "
            f"{where}"
            "ORDER BY c.detected_at DESC LIMIT $1"
        )
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, limit)
        return [dict(row) for row in rows]

    async def update_thesis_status(self, thesis_id: str, status: str) -> bool:
        """Set a thesis status (e.g. 'active' -> 'contradicted'). Returns True if updated."""
        pool = self._require_pool()
        import uuid as _uuid
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE theses SET status = $1 WHERE thesis_id = $2 RETURNING thesis_id",
                status,
                _uuid.UUID(str(thesis_id)),
            )
        return row is not None

    async def mark_thesis_superseded(
        self,
        older_thesis_id: str,
        newer_thesis_id: str,
    ) -> bool:
        """Flip an older thesis to 'superseded' and record which thesis replaced it.

        Used by the detector's temporal-semantics branch: when an opposing
        claim exists across a rolling metric with a gap ≥ CONTRADICTION_WINDOW_HOURS,
        the older reading is a stale point on a time-series, not a live
        contradiction. Both columns are set atomically.
        """
        pool = self._require_pool()
        import uuid as _uuid
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE theses SET status = 'superseded', superseded_by = $1 "
                "WHERE thesis_id = $2 RETURNING thesis_id",
                _uuid.UUID(str(newer_thesis_id)),
                _uuid.UUID(str(older_thesis_id)),
            )
        return row is not None

    async def set_contradiction_resolution(
        self,
        contradiction_id: str,
        resolution: str,
    ) -> bool:
        """Set an audit-trail reason on a contradiction row.

        Used by scripts/remediate_contradictions.py to void existing pairs
        without deleting the row (rows are never destroyed — resolution is
        the audit trail). Live API + wiki filter resolution IS NULL when
        listing 'unresolved' contradictions.
        """
        pool = self._require_pool()
        import uuid as _uuid
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE contradictions SET resolution = $1 "
                "WHERE contradiction_id = $2 RETURNING contradiction_id",
                resolution,
                _uuid.UUID(str(contradiction_id)),
            )
        return row is not None

    async def set_focus_directive(self, directive: str) -> str:
        """Replace the active focus directive; return the new row's id.

        Atomically tombstones any currently-active row (sets cleared_at =
        NOW()) and inserts the new directive as the sole active row.
        History is preserved — the previous row remains for audit.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE focus_directives SET cleared_at = NOW() "
                    "WHERE cleared_at IS NULL"
                )
                row = await conn.fetchrow(
                    "INSERT INTO focus_directives (directive) "
                    "VALUES ($1) RETURNING focus_id",
                    directive,
                )
        return str(row["focus_id"])

    async def get_active_focus(self) -> dict[str, Any] | None:
        """Return the single active directive row or None.

        Returns a dict with ``focus_id``, ``directive``, and ``created_at``
        so callers can display "since when". Uses LIMIT 1 defensively;
        set_focus_directive enforces at-most-one-active but a manual
        override on the DB could break that.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT focus_id, directive, created_at FROM focus_directives "
                "WHERE cleared_at IS NULL ORDER BY created_at DESC LIMIT 1"
            )
        return dict(row) if row else None

    async def clear_focus_directive(self) -> bool:
        """Tombstone the active directive. Return True if one existed."""
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE focus_directives SET cleared_at = NOW() "
                "WHERE cleared_at IS NULL RETURNING focus_id"
            )
        return row is not None

    async def record_contradiction(
        self,
        thesis_id_a: str,
        thesis_id_b: str,
        subject_group: str | None = None,
    ) -> str:
        """Insert a contradiction row linking two theses. Returns the new id."""
        pool = self._require_pool()
        import uuid as _uuid
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "INSERT INTO contradictions (subject_group, thesis_id_a, thesis_id_b) "
                "VALUES ($1, $2, $3) RETURNING contradiction_id",
                subject_group,
                _uuid.UUID(str(thesis_id_a)),
                _uuid.UUID(str(thesis_id_b)),
            )
        return str(row["contradiction_id"]) if row else ""

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

    async def record_shadow_verdict(
        self,
        *,
        proposal_id: str,
        verdict: str,
        axes: dict[str, Any],
        reasons: list[str],
        engine: str,
        prompt_version: str,
    ) -> str:
        """Persist one shadow verdict row and return its id.

        Append-only calibration data — never updates or deletes. See
        ``self_modify.shadow`` for the caller.
        """
        import uuid as _uuid
        pool = self._require_pool()
        verdict_id = str(_uuid.uuid4())
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO shadow_verdicts
                    (id, proposal_id, verdict, axes, reasons, engine,
                     prompt_version)
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7)
                """,
                _uuid.UUID(verdict_id),
                _uuid.UUID(proposal_id),
                verdict,
                json.dumps(axes),
                json.dumps(reasons),
                engine,
                prompt_version,
            )
        return verdict_id

    async def get_shadow_verdicts(self, proposal_id: str) -> list[dict[str, Any]]:
        """Return all shadow verdicts for a proposal, newest first."""
        import uuid as _uuid
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM shadow_verdicts WHERE proposal_id = $1 "
                "ORDER BY created_at DESC",
                _uuid.UUID(proposal_id),
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            for k in ("axes", "reasons"):
                v = d.get(k)
                if isinstance(v, str):
                    try:
                        d[k] = json.loads(v)
                    except json.JSONDecodeError:
                        pass
            out.append(d)
        return out

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
