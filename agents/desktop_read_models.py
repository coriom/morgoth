"""Disposable-test-only, Project-scoped Desktop read projections.

No caller-controlled SQL, writes, production database, or general-purpose query API.
The native Management boundary provides authentication separately.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import pwd
import re
from uuid import UUID

import asyncpg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.generalist_mission_engine import CapabilityRecord, MissionRecord
from core.project import ProjectConfig

_SAFE_CODE = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")


class ReadUnavailable(RuntimeError):
    """Fixed-code failure for unavailable or incompatible test records."""


class MissionSummary(BaseModel):
    """Explicit WebView-safe mission fields; no raw checkpoint or receipt body."""

    model_config = ConfigDict(extra="forbid")
    mission_id: UUID
    project_id: str
    objective: str = Field(max_length=240)
    state: str
    revision: int
    stages: list[str]
    capability_gaps: list[str]
    failure_codes: list[str]
    resume_eligible: bool
    receipt_count: int
    created_at: datetime
    updated_at: datetime
    evidence_kind: str = "HISTORICAL_CHECKPOINT"


class MissionPage(BaseModel):
    """Bounded ordered page of historical mission checkpoints."""

    project_id: str
    missions: list[MissionSummary]
    next_offset: int | None
    source: str = "DISPOSABLE_POSTGRESQL"


class CapabilitySummary(BaseModel):
    """Validation and approval remain distinct, with no invocation contract."""

    capability_id: str
    project_id: str
    version: str
    documentation_digest: str
    state: str
    validation_state: str
    approval_kind: str
    source_identity: str
    updated_at: datetime
    source: str = "DISPOSABLE_POSTGRESQL"


class CapabilityPage(BaseModel):
    """Bounded capability inventory for one managed test Project."""

    project_id: str
    capabilities: list[CapabilitySummary]
    next_offset: int | None
    source: str = "DISPOSABLE_POSTGRESQL"


def _code(value: str) -> str:
    if not _SAFE_CODE.fullmatch(value):
        raise ReadUnavailable("INCOMPATIBLE_RECORD")
    return value


def _mission(row: asyncpg.Record, project_id: str) -> MissionSummary:
    raw = row["record"]
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ReadUnavailable("INCOMPATIBLE_RECORD")
    try:
        item = MissionRecord.model_validate_json(raw)
        if item.project_id != project_id or item.mission_id != row["mission_id"] or item.state.value != row["state"]:
            raise ReadUnavailable("INCOMPATIBLE_RECORD")
        stages = [_code(value) for value in item.state_history[-16:]]
        gaps = [_code(value) for value in item.capability_gaps[:16]]
        failures = [_code(value) for value in item.failure_reasons[-4:]]
        if len(item.capability_gaps) > 16 or not stages:
            raise ReadUnavailable("INCOMPATIBLE_RECORD")
        return MissionSummary(mission_id=item.mission_id, project_id=project_id, objective=item.objective,
                              state=item.state.value, revision=row["revision"], stages=stages,
                              capability_gaps=gaps, failure_codes=failures,
                              resume_eligible=item.resume_eligible, receipt_count=row["receipt_count"],
                              created_at=row["created_at"], updated_at=row["updated_at"])
    except (ValueError, ValidationError, KeyError, TypeError):
        raise ReadUnavailable("INCOMPATIBLE_RECORD") from None


def _capability(row: asyncpg.Record, project_id: str) -> CapabilitySummary:
    raw = row["record"]
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ReadUnavailable("INCOMPATIBLE_RECORD")
    try:
        item = CapabilityRecord.model_validate_json(raw)
        if (item.project_id != project_id or item.capability_id != row["capability_id"]
                or item.version != row["version"] or item.documentation_digest != row["document_digest"]
                or not _HEX.fullmatch(item.documentation_digest)):
            raise ReadUnavailable("INCOMPATIBLE_RECORD")
        _code(item.capability_id)
        _code(item.version)
        _code(item.validation_state)
        approval = "TEST_FIXTURE_ONLY" if item.state.value == "TEST_ONLY_APPROVED" else "NONE_RECORDED"
        source = item.documentation_identity if _SAFE_CODE.fullmatch(item.documentation_identity) else "REDACTED"
        return CapabilitySummary(capability_id=item.capability_id, project_id=project_id,
                                 version=item.version, documentation_digest=item.documentation_digest,
                                 state=item.state.value, validation_state=item.validation_state,
                                 approval_kind=approval, source_identity=source,
                                 updated_at=row["updated_at"])
    except (ValueError, ValidationError, KeyError, TypeError):
        raise ReadUnavailable("INCOMPATIBLE_RECORD") from None


class DesktopReadStore:
    """Open one read-only transaction per request against an owned Unix-socket test cluster."""

    def __init__(self, socket_dir: Path) -> None:
        if not socket_dir.is_absolute() or socket_dir.is_symlink() or not socket_dir.is_dir():
            raise ReadUnavailable("TEST_SOCKET_UNAVAILABLE")
        info = socket_dir.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReadUnavailable("TEST_SOCKET_UNAVAILABLE")
        self._socket_dir = socket_dir

    async def _connect(self) -> asyncpg.Connection:
        try:
            conn = await asyncpg.connect(host=str(self._socket_dir), database="morgoth_test",
                                         user=pwd.getpwuid(os.getuid()).pw_name, timeout=2,
                                         command_timeout=3)
            if await conn.fetchval("SELECT current_database()") != "morgoth_test":
                await conn.close()
                raise ReadUnavailable("TEST_DATABASE_REQUIRED")
            return conn
        except (OSError, asyncpg.PostgresError, TimeoutError):
            raise ReadUnavailable("READ_MODEL_UNAVAILABLE") from None

    @staticmethod
    def _project(project: ProjectConfig) -> str:
        if project.is_legacy or project.postgres_schema == "public" or not _SAFE_CODE.fullmatch(project.postgres_schema):
            raise ReadUnavailable("MANAGED_PROJECT_REQUIRED")
        return project.postgres_schema

    async def missions(self, project: ProjectConfig, *, limit: int = 10, offset: int = 0) -> MissionPage:
        """Return a bounded Project-only page; an absent table is unavailable, not empty."""
        schema = self._project(project)
        if not 1 <= limit <= 10 or not 0 <= offset <= 1000:
            raise ReadUnavailable("INVALID_PAGE")
        conn = await self._connect()
        try:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(f'''SELECT m.mission_id, m.state, m.record::text AS record,
                    m.revision, m.created_at, m.updated_at,
                    (SELECT count(*)::int FROM "{schema}".generalist_execution_receipts r
                     WHERE r.project_id=m.project_id AND r.mission_id=m.mission_id) AS receipt_count
                    FROM "{schema}".generalist_missions m WHERE m.project_id=$1
                    ORDER BY m.updated_at DESC, m.mission_id DESC LIMIT $2 OFFSET $3''',
                    project.id, limit + 1, offset)
            return MissionPage(project_id=project.id, missions=[_mission(r, project.id) for r in rows[:limit]],
                               next_offset=offset + limit if len(rows) > limit else None)
        except (asyncpg.PostgresError, TimeoutError):
            raise ReadUnavailable("READ_MODEL_UNAVAILABLE") from None
        finally:
            await conn.close()

    async def mission(self, project: ProjectConfig, mission_id: UUID) -> MissionSummary | None:
        """Return an exact Project-bound historical checkpoint or no record."""
        schema = self._project(project)
        conn = await self._connect()
        try:
            async with conn.transaction(readonly=True):
                row = await conn.fetchrow(f'''SELECT m.mission_id, m.state, m.record::text AS record,
                    m.revision, m.created_at, m.updated_at,
                    (SELECT count(*)::int FROM "{schema}".generalist_execution_receipts r
                     WHERE r.project_id=m.project_id AND r.mission_id=m.mission_id) AS receipt_count
                    FROM "{schema}".generalist_missions m
                    WHERE m.project_id=$1 AND m.mission_id=$2''', project.id, mission_id)
            return _mission(row, project.id) if row else None
        except (asyncpg.PostgresError, TimeoutError):
            raise ReadUnavailable("READ_MODEL_UNAVAILABLE") from None
        finally:
            await conn.close()

    async def capabilities(self, project: ProjectConfig, *, limit: int = 10, offset: int = 0) -> CapabilityPage:
        """Return validation state without implying a production grant."""
        schema = self._project(project)
        if not 1 <= limit <= 10 or not 0 <= offset <= 1000:
            raise ReadUnavailable("INVALID_PAGE")
        conn = await self._connect()
        try:
            async with conn.transaction(readonly=True):
                rows = await conn.fetch(f'''SELECT capability_id, version, document_digest,
                    record::text AS record, updated_at FROM "{schema}".generalist_capabilities
                    WHERE project_id=$1 ORDER BY updated_at DESC, capability_id, version, document_digest
                    LIMIT $2 OFFSET $3''', project.id, limit + 1, offset)
            return CapabilityPage(project_id=project.id,
                                  capabilities=[_capability(r, project.id) for r in rows[:limit]],
                                  next_offset=offset + limit if len(rows) > limit else None)
        except (asyncpg.PostgresError, TimeoutError):
            raise ReadUnavailable("READ_MODEL_UNAVAILABLE") from None
        finally:
            await conn.close()
