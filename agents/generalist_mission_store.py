"""Optional Project-local mission storage on the existing PersistentMemory pool.

This schema is deliberately not part of default bootstrap or autonomous START.
It can only be provisioned for an explicitly selected managed Project.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from core.project import ProjectConfig, current_namespace
from memory.persistent import PersistentMemory


class MissionStoreError(RuntimeError):
    """Fixed-code mission storage refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_DDL = (
    """CREATE TABLE IF NOT EXISTS generalist_missions (
        mission_id UUID PRIMARY KEY, project_id TEXT NOT NULL, user_id TEXT NOT NULL,
        state TEXT NOT NULL, record JSONB NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""",
    """CREATE TABLE IF NOT EXISTS generalist_capabilities (
        project_id TEXT NOT NULL, user_id TEXT NOT NULL, capability_id TEXT NOT NULL,
        version TEXT NOT NULL, document_digest TEXT NOT NULL, record JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (project_id, capability_id, version, document_digest)
    )""",
    """CREATE TABLE IF NOT EXISTS generalist_execution_receipts (
        project_id TEXT NOT NULL, user_id TEXT NOT NULL, mission_id UUID NOT NULL
            REFERENCES generalist_missions(mission_id), idempotency_key TEXT NOT NULL,
        result JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (project_id, mission_id, idempotency_key)
    )""",
    """CREATE TABLE IF NOT EXISTS generalist_approval_grants (
        grant_id UUID PRIMARY KEY, project_id TEXT NOT NULL, mission_id UUID NOT NULL
            REFERENCES generalist_missions(mission_id), capability_id TEXT NOT NULL,
        version TEXT NOT NULL, document_digest TEXT NOT NULL, operation TEXT NOT NULL,
        method TEXT NOT NULL, host TEXT NOT NULL, endpoint TEXT NOT NULL,
        query_constraints JSONB NOT NULL, scopes TEXT[] NOT NULL,
        approval_provenance TEXT NOT NULL, approved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL, max_invocations INTEGER NOT NULL CHECK (max_invocations = 1),
        used_invocations INTEGER NOT NULL DEFAULT 0 CHECK (used_invocations BETWEEN 0 AND 1),
        max_response_bytes INTEGER NOT NULL CHECK (max_response_bytes = 131072),
        max_duration_seconds INTEGER NOT NULL CHECK (max_duration_seconds = 8),
        revoked BOOLEAN NOT NULL DEFAULT FALSE
    )""",
)


class ProjectMissionStore:
    """CAS and atomic local receipts in one disposable Project schema."""

    def __init__(self, memory: PersistentMemory, project: ProjectConfig) -> None:
        if project.is_legacy or project.postgres_schema == "public":
            raise MissionStoreError("MANAGED_PROJECT_REQUIRED")
        self._memory = memory
        self.project = project
        self._test_database_verified = False
        self._verified_pool: Any = None

    def _pool(self) -> Any:
        """Refuse namespace drift before using the already scoped pool."""
        actual = current_namespace()
        if (actual.id, actual.postgres_schema) != (self.project.id, self.project.postgres_schema):
            raise MissionStoreError("PROJECT_NAMESPACE_MISMATCH")
        if not self._test_database_verified:
            raise MissionStoreError("DISPOSABLE_TEST_DATABASE_REQUIRED")
        pool = self._memory._require_pool()
        if pool is not self._verified_pool:
            raise MissionStoreError("DISPOSABLE_TEST_DATABASE_REQUIRED")
        return pool

    async def initialize_optional(self) -> None:
        """Provision only the opt-in tables in this managed Project schema."""
        actual = current_namespace()
        if (actual.id, actual.postgres_schema) != (self.project.id, self.project.postgres_schema):
            raise MissionStoreError("PROJECT_NAMESPACE_MISMATCH")
        pool = self._memory._require_pool()
        async with pool.acquire() as conn:
            database = await conn.fetchval("SELECT current_database()")
            if database != "morgoth_test":
                raise MissionStoreError("DISPOSABLE_TEST_DATABASE_REQUIRED")
            schema = await conn.fetchval("SELECT current_schema()")
            if schema != self.project.postgres_schema:
                raise MissionStoreError("PROJECT_NAMESPACE_MISMATCH")
            async with conn.transaction():
                for statement in _DDL:
                    await conn.execute(statement)
        self._test_database_verified = True
        self._verified_pool = pool

    async def create_mission(self, mission_id: UUID, user_id: str, state: str, record: str) -> bool:
        """Insert a new mission once; caller supplies validated JSON text."""
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO generalist_missions(mission_id, project_id, user_id, state, record)
                   VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING RETURNING revision""",
                mission_id, self.project.id, user_id, state, record,
            )
            return row is not None

    async def get_mission(self, mission_id: UUID) -> tuple[str, int] | None:
        """Read a mission only from the matching Project."""
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT record::text AS record, revision FROM generalist_missions WHERE mission_id=$1 AND project_id=$2",
                mission_id, self.project.id,
            )
        return (row["record"], row["revision"]) if row else None

    async def save_mission(self, mission_id: UUID, expected_revision: int, state: str, record: str) -> bool:
        """Checkpoint with compare-and-swap to reject concurrent stale writers."""
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """UPDATE generalist_missions SET state=$4, record=$5::jsonb,
                   revision=revision+1, updated_at=NOW()
                   WHERE mission_id=$1 AND project_id=$2 AND revision=$3 RETURNING revision""",
                mission_id, self.project.id, expected_revision, state, record,
            )
            return row is not None

    async def save_capability(self, user_id: str, capability_id: str, version: str,
                              digest: str, record: str) -> None:
        """Persist a validated proposal or fixture-only approval; no production grant API."""
        from agents.generalist_mission_engine import CapabilityRecord, CapabilityState
        try:
            parsed = CapabilityRecord.model_validate_json(record)
        except (ValueError, ValidationError):
            raise MissionStoreError("CAPABILITY_RECORD_INVALID") from None
        if ((parsed.project_id, parsed.user_id, parsed.capability_id,
             parsed.version, parsed.documentation_digest) !=
            (self.project.id, user_id, capability_id, version, digest)):
            raise MissionStoreError("CAPABILITY_RECORD_INVALID")
        if parsed.state is CapabilityState.APPROVED:
            raise MissionStoreError("PRODUCTION_APPROVAL_UNAVAILABLE")
        if parsed.state is CapabilityState.TEST_ONLY_APPROVED and (
            parsed.approval_provenance != "synthetic_fixture_only" or parsed.validation_state != "PASSED"
        ):
            raise MissionStoreError("CAPABILITY_RECORD_INVALID")
        pool = self._pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO generalist_capabilities
                   (project_id,user_id,capability_id,version,document_digest,record)
                   VALUES ($1,$2,$3,$4,$5,$6::jsonb)
                   ON CONFLICT (project_id,capability_id,version,document_digest)
                   DO UPDATE SET record=EXCLUDED.record, updated_at=NOW()
                   WHERE generalist_capabilities.record->>'state' NOT IN
                         ('TEST_ONLY_APPROVED','APPROVED','REVOKED','STALE')
                      OR EXCLUDED.record->>'state' IN ('REVOKED','STALE')""",
                self.project.id, user_id, capability_id, version, digest, record,
            )

    async def get_capability(self, capability_id: str, version: str, digest: str) -> str | None:
        """Read only the exact Project, version and document digest."""
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT record::text AS record FROM generalist_capabilities
                   WHERE project_id=$1 AND capability_id=$2 AND version=$3 AND document_digest=$4""",
                self.project.id, capability_id, version, digest,
            )
        return row["record"] if row else None

    async def list_capability_identities(self) -> list[tuple[str, str, str]]:
        """Bounded Project-only inventory for opt-in gap assessment."""
        pool = self._pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT capability_id, version, document_digest
                   FROM generalist_capabilities WHERE project_id=$1
                   ORDER BY capability_id, version, document_digest LIMIT 65""",
                self.project.id,
            )
        if len(rows) > 64:
            raise MissionStoreError("CAPABILITY_INVENTORY_TOO_LARGE")
        return [(row["capability_id"], row["version"], row["document_digest"])
                for row in rows]

    async def get_receipt(self, mission_id: UUID, key: str) -> str | None:
        """Find a committed local result before considering duplicate work."""
        pool = self._pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT result::text AS result FROM generalist_execution_receipts
                   WHERE project_id=$1 AND mission_id=$2 AND idempotency_key=$3""",
                self.project.id, mission_id, key,
            )
        return row["result"] if row else None

    async def commit_execution(self, mission_id: UUID, user_id: str, key: str,
                               expected_revision: int, state: str, record: str,
                               result: str) -> tuple[bool, str]:
        """Atomically persist a confirmed result and mission checkpoint once.

        External callers must claim an ATTEMPTED checkpoint before I/O. This
        local receipt is neither a remote exactly-once proof nor a grant.
        """
        pool = self._pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchrow(
                    """SELECT result::text AS result FROM generalist_execution_receipts
                       WHERE project_id=$1 AND mission_id=$2 AND idempotency_key=$3""",
                    self.project.id, mission_id, key,
                )
                if old:
                    return False, old["result"]
                inserted = await conn.fetchrow(
                    """INSERT INTO generalist_execution_receipts
                       (project_id,user_id,mission_id,idempotency_key,result)
                       VALUES ($1,$2,$3,$4,$5::jsonb)
                       ON CONFLICT DO NOTHING RETURNING idempotency_key""",
                    self.project.id, user_id, mission_id, key, result,
                )
                if inserted is None:
                    old = await conn.fetchrow(
                        """SELECT result::text AS result FROM generalist_execution_receipts
                           WHERE project_id=$1 AND mission_id=$2 AND idempotency_key=$3""",
                        self.project.id, mission_id, key,
                    )
                    if old is None:
                        raise MissionStoreError("RECEIPT_CONFLICT")
                    return False, old["result"]
                updated = await conn.fetchrow(
                    """UPDATE generalist_missions SET state=$4, record=$5::jsonb,
                       revision=revision+1, updated_at=NOW()
                       WHERE mission_id=$1 AND project_id=$2 AND revision=$3 RETURNING revision""",
                    mission_id, self.project.id, expected_revision, state, record,
                )
                if updated is None:
                    raise MissionStoreError("MISSION_CONCURRENT_CHANGE")
                return True, result
