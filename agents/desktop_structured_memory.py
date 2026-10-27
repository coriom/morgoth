"""Optional, bounded Desktop reads of existing Project structured memory.

No PersistentMemory object is constructed: that class initializes/migrates tables.
The operator supplies a private local Unix-socket read-role configuration.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

import asyncpg
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core.project import ProjectConfig
from project_manager import ProjectManager, ProjectManagerError

PREFIX = "/desktop-memory/v1"
SOURCE = "PROJECT_STRUCTURED_POSTGRESQL"
IDENT = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
SAFE_SOURCE = re.compile(r"[a-zA-Z0-9_.:-]{1,80}\Z")
STATUSES = frozenset({"active", "contradicted", "quarantined", "superseded"})
CONFIDENCES = frozenset({"low", "medium", "high"})
THESIS_COLUMNS = frozenset({"thesis_id", "subject", "claim", "confidence", "evidence", "status",
                            "objective_id", "created_at", "canonical_subject", "quarantine_reason",
                            "superseded_by", "code_version"})
CONTRADICTION_COLUMNS = frozenset({"contradiction_id", "subject_group", "thesis_id_a",
                                   "thesis_id_b", "detected_at", "resolution"})
# Project-scoped fixed projections send at most one character beyond each DTO limit.
# That extra character makes overlong records fail validation without transferring
# their full content or silently truncating it into a plausible claim.
THESIS_SELECT = ("thesis_id,left(subject,161) AS subject,left(claim,2049) AS claim,"
                 "left(confidence,25) AS confidence,left(evidence::text,16385) AS evidence,"
                 "left(status,25) AS status,left(objective_id,81) AS objective_id,created_at,"
                 "left(code_version,65) AS code_version,"
                 "left(quarantine_reason,241) AS quarantine_reason,superseded_by")
CONTRADICTION_SELECT = ("contradiction_id,left(subject_group,161) AS subject_group,"
                        "thesis_id_a,thesis_id_b,detected_at,left(resolution,241) AS resolution")


class MemoryReadUnavailable(RuntimeError):
    """Fixed error code; SQL and credentials never appear in HTTP errors."""


class EvidenceSource(BaseModel):
    """Only a bounded source identifier, never the unrestricted evidence JSON."""
    model_config = ConfigDict(extra="forbid")
    source: str = Field(max_length=80)


class ThesisRead(BaseModel):
    """Recorded claim and state; neither means human verification."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    project_id: str
    thesis_id: UUID
    subject: str = Field(max_length=160)
    claim: str = Field(max_length=2048)
    confidence: str
    status: str
    created_at: datetime
    code_version: str | None = Field(default=None, max_length=64)
    objective_id: str | None = Field(default=None, max_length=80)
    quarantine_reason: str | None = Field(default=None, max_length=240)
    superseded_by: UUID | None
    evidence_sources: list[EvidenceSource]
    evidence_count: int
    evidence_omitted: bool
    source: str = SOURCE


class ContradictionRead(BaseModel):
    """Explicit stored relationship, with nullable missing thesis references."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    project_id: str
    contradiction_id: UUID
    subject_group: str | None = Field(default=None, max_length=160)
    thesis_id_a: UUID
    thesis_id_b: UUID
    detected_at: datetime
    resolution: str | None = Field(default=None, max_length=240)
    source: str = SOURCE


class ThesisPage(BaseModel):
    """One deterministic, bounded thesis page."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    project_id: str
    source: str = SOURCE
    read_at: datetime
    theses: list[ThesisRead]
    next_offset: int | None


class ContradictionPage(BaseModel):
    """One deterministic, bounded contradiction page."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    project_id: str
    source: str = SOURCE
    read_at: datetime
    contradictions: list[ContradictionRead]
    next_offset: int | None


class ReaderConfig(BaseModel):
    """Native-only local socket and dedicated database role."""
    model_config = ConfigDict(extra="forbid")
    schema_version: int
    socket_dir: str
    port: int
    database: str
    role: str


def load_reader_config(path: Path) -> ReaderConfig:
    """Read one operator-created, owner-private regular file without following links."""
    if not path.is_absolute() or ".." in path.parts:
        raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
    try:
        parent = path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) & 0o077):
            raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid() or meta.st_size > 4096 or stat.S_IMODE(meta.st_mode) & 0o077:
                raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        value = json.loads(raw)
        config = ReaderConfig.model_validate(value)
        socket = Path(config.socket_dir)
        if not socket.is_absolute() or ".." in socket.parts:
            raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
        info = socket.lstat()
        if (config.schema_version != 1 or not 1024 <= config.port <= 65535
                or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or not IDENT.fullmatch(config.database) or not IDENT.fullmatch(config.role)):
            raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
        return config
    except (OSError, ValueError, TypeError):
        raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY") from None


def _bounded(value: object, limit: int, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    return value


def _thesis(row: asyncpg.Record, project_id: str) -> ThesisRead:
    evidence = row["evidence"]
    if isinstance(evidence, str):
        try:
            if len(evidence) > 16_384:
                raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
            evidence = json.loads(evidence)
        except ValueError:
            raise MemoryReadUnavailable("INCOMPATIBLE_RECORD") from None
    if not isinstance(evidence, list) or len(evidence) > 32:
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    sources: list[EvidenceSource] = []
    omitted = False
    for entry in evidence:
        if not isinstance(entry, dict):
            raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
        source = entry.get("source")
        if isinstance(source, str) and SAFE_SOURCE.fullmatch(source) and len(sources) < 8:
            sources.append(EvidenceSource(source=source))
        else:
            omitted = True
    status = _bounded(row["status"], 24)
    confidence = _bounded(row["confidence"], 24)
    if status not in STATUSES or confidence not in CONFIDENCES:
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    superseded = row["superseded_by"]
    if superseded is not None and not isinstance(superseded, UUID):
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    if status == "superseded" and superseded is None:
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    created = row["created_at"]
    if not isinstance(created, datetime) or created.tzinfo is None:
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    return ThesisRead(project_id=project_id, thesis_id=row["thesis_id"],
        subject=_bounded(row["subject"], 160), claim=_bounded(row["claim"], 2048),
        confidence=confidence, status=status, created_at=created,
        code_version=_bounded(row["code_version"], 64, nullable=True),
        objective_id=_bounded(row["objective_id"], 80, nullable=True),
        quarantine_reason=_bounded(row["quarantine_reason"], 240, nullable=True),
        superseded_by=superseded, evidence_sources=sources,
        evidence_count=len(evidence), evidence_omitted=omitted or len(sources) < len(evidence))


def _contradiction(row: asyncpg.Record, project_id: str) -> ContradictionRead:
    detected = row["detected_at"]
    if not isinstance(detected, datetime) or detected.tzinfo is None:
        raise MemoryReadUnavailable("INCOMPATIBLE_RECORD")
    return ContradictionRead(project_id=project_id, contradiction_id=row["contradiction_id"],
        subject_group=_bounded(row["subject_group"], 160, nullable=True),
        thesis_id_a=row["thesis_id_a"], thesis_id_b=row["thesis_id_b"],
        detected_at=detected, resolution=_bounded(row["resolution"], 240, nullable=True))


class StructuredMemoryReader:
    """One read-only connection per request, always qualified for a catalog Project."""
    def __init__(self, config: ReaderConfig) -> None:
        self.config = config

    async def _connect(self, project: ProjectConfig) -> asyncpg.Connection:
        if project.is_legacy or project.postgres_schema != "p_" + hashlib.sha256(project.id.encode("ascii")).hexdigest()[:60]:
            raise MemoryReadUnavailable("MANAGED_PROJECT_REQUIRED")
        try:
            conn = await asyncpg.connect(host=self.config.socket_dir, port=self.config.port,
                database=self.config.database, user=self.config.role,
                timeout=3, command_timeout=5,
                server_settings={"search_path": "pg_catalog", "statement_timeout": "4000"})
            return conn
        except (asyncpg.PostgresError, OSError, TimeoutError):
            raise MemoryReadUnavailable("SOURCE_UNAVAILABLE") from None

    async def _qualified(self, conn: asyncpg.Connection, schema: str) -> None:
        role = await conn.fetchrow("SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname=current_user")
        if role is None or any(role.values()):
            raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)", schema)
        if not exists:
            raise MemoryReadUnavailable("SOURCE_UNINITIALIZED_OR_INCOMPATIBLE")
        permitted = await conn.fetchval("SELECT has_schema_privilege(current_user,$1,'USAGE')", schema)
        if not permitted:
            raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
        rows = await conn.fetch("SELECT c.relname AS table_name, a.attname AS column_name "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped "
            "WHERE n.nspname=$1 AND c.relkind IN ('r','p') "
            "AND c.relname IN ('theses','contradictions')", schema)
        columns: dict[str, set[str]] = {"theses": set(), "contradictions": set()}
        for row in rows:
            columns[row["table_name"]].add(row["column_name"])
        if not THESIS_COLUMNS <= columns["theses"] or not CONTRADICTION_COLUMNS <= columns["contradictions"]:
            raise MemoryReadUnavailable("SOURCE_UNINITIALIZED_OR_INCOMPATIBLE")
        for table in ("theses", "contradictions"):
            relation = f'"{schema}"."{table}"'
            allowed = await conn.fetchrow("SELECT has_table_privilege(current_user,$1,'SELECT') AS read, "
                "has_table_privilege(current_user,$1,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS write", relation)
            if not allowed["read"] or allowed["write"]:
                raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")
        foreign = await conn.fetchval("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind IN ('r','p','v','m') AND n.nspname NOT IN ($1,'pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND has_table_privilege(current_user,c.oid,'SELECT')", schema)
        if foreign:
            raise MemoryReadUnavailable("INVALID_SOURCE_AUTHORITY")

    async def _run(self, project: ProjectConfig, query: str, *args: object) -> list[asyncpg.Record]:
        conn = await self._connect(project)
        try:
            async with conn.transaction(readonly=True):
                await self._qualified(conn, project.postgres_schema)
                return await conn.fetch(query, *args)
        except MemoryReadUnavailable:
            raise
        except (asyncpg.PostgresError, TimeoutError, ValueError):
            raise MemoryReadUnavailable("SOURCE_UNAVAILABLE") from None
        finally:
            await conn.close(timeout=1)

    async def theses(self, project: ProjectConfig, *, offset: int, limit: int,
                     query: str = "", status: str = "", confidence: str = "") -> ThesisPage:
        """List recorded claims with bounded filtering and stable ordering."""
        if not 0 <= offset <= 480 or not 1 <= limit <= 20 or len(query) > 80 or any(ord(c)<32 for c in query) or (status and status not in STATUSES) or (confidence and confidence not in CONFIDENCES):
            raise MemoryReadUnavailable("INVALID_FILTER")
        schema = project.postgres_schema
        rows = await self._run(project,
            f'SELECT {THESIS_SELECT} FROM "{schema}".theses '
            "WHERE ($1='' OR subject ILIKE '%' || $1 || '%') AND ($2='' OR status=$2) AND ($3='' OR confidence=$3) "
            "ORDER BY created_at DESC, thesis_id DESC LIMIT $4 OFFSET $5",
            query, status, confidence, limit+1, offset)
        return ThesisPage(project_id=project.id, read_at=datetime.now(timezone.utc),
            theses=[_thesis(row, project.id) for row in rows[:limit]],
            next_offset=offset+limit if len(rows)>limit else None)

    async def thesis(self, project: ProjectConfig, identity: UUID) -> ThesisRead | None:
        """Read one exact thesis identity in its canonical Project schema."""
        schema = project.postgres_schema
        rows = await self._run(project,
            f'SELECT {THESIS_SELECT} FROM "{schema}".theses WHERE thesis_id=$1', identity)
        return _thesis(rows[0], project.id) if rows else None

    async def contradictions(self, project: ProjectConfig, *, offset: int, limit: int) -> ContradictionPage:
        """List all recorded contradictions, including resolved history."""
        if not 0 <= offset <= 480 or not 1 <= limit <= 20:
            raise MemoryReadUnavailable("INVALID_FILTER")
        schema = project.postgres_schema
        rows = await self._run(project,
            f'SELECT {CONTRADICTION_SELECT} FROM "{schema}".contradictions '
            'ORDER BY detected_at DESC, contradiction_id DESC LIMIT $1 OFFSET $2', limit+1, offset)
        return ContradictionPage(project_id=project.id, read_at=datetime.now(timezone.utc),
            contradictions=[_contradiction(row, project.id) for row in rows[:limit]],
            next_offset=offset+limit if len(rows)>limit else None)

    async def contradiction(self, project: ProjectConfig, identity: UUID) -> ContradictionRead | None:
        """Read one exact contradiction identity in its canonical Project schema."""
        schema = project.postgres_schema
        rows = await self._run(project,
            f'SELECT {CONTRADICTION_SELECT} FROM "{schema}".contradictions WHERE contradiction_id=$1', identity)
        return _contradiction(rows[0], project.id) if rows else None


def install_structured_memory_routes(app: FastAPI, manager: ProjectManager,
                                     reader: StructuredMemoryReader | None) -> None:
    """Mount authenticated fixed reads; absent authority reports unavailable."""
    def project(project_id: str) -> ProjectConfig:
        manager.get_project(project_id)
        found = next((item for item in manager._catalog() if item.id == project_id), None)
        if found is None:
            raise ProjectManagerError("UNKNOWN_PROJECT", "Project unavailable")
        return found

    def refusal(exc: MemoryReadUnavailable) -> JSONResponse:
        code = str(exc)
        status = 422 if code in {"INVALID_FILTER", "MANAGED_PROJECT_REQUIRED"} else 409 if code == "INCOMPATIBLE_RECORD" else 503
        return JSONResponse(status_code=status, content={"error":{"code":code,"message":"structured memory unavailable"}})

    def source() -> StructuredMemoryReader:
        if reader is None:
            raise MemoryReadUnavailable("SOURCE_NOT_AUTHORIZED")
        return reader

    @app.get(f"{PREFIX}/projects/{{project_id}}/theses", response_model=ThesisPage)
    async def list_theses(project_id: str, offset: int = Query(0, ge=0, le=480), limit: int = Query(20, ge=1, le=20),
                          query: str = Query("", max_length=80), status: str = "", confidence: str = "") -> ThesisPage | JSONResponse:
        """List one page of recorded Project theses."""
        try:
            selected = project(project_id)
            return await source().theses(selected, offset=offset, limit=limit, query=query, status=status, confidence=confidence)
        except MemoryReadUnavailable as exc:
            return refusal(exc)

    @app.get(f"{PREFIX}/projects/{{project_id}}/theses/{{thesis_id}}", response_model=ThesisRead)
    async def get_thesis(project_id: str, thesis_id: UUID) -> ThesisRead | JSONResponse:
        """Read one exact recorded thesis."""
        try:
            selected = project(project_id)
            found = await source().thesis(selected, thesis_id)
        except MemoryReadUnavailable as exc:
            return refusal(exc)
        return found if found is not None else JSONResponse(status_code=404, content={"error":{"code":"UNKNOWN_THESIS","message":"thesis unavailable"}})

    @app.get(f"{PREFIX}/projects/{{project_id}}/contradictions", response_model=ContradictionPage)
    async def list_contradictions(project_id: str, offset: int = Query(0, ge=0, le=480), limit: int = Query(20, ge=1, le=20)) -> ContradictionPage | JSONResponse:
        """List recorded relationships without inferential linking."""
        try:
            selected = project(project_id)
            return await source().contradictions(selected, offset=offset, limit=limit)
        except MemoryReadUnavailable as exc:
            return refusal(exc)

    @app.get(f"{PREFIX}/projects/{{project_id}}/contradictions/{{contradiction_id}}", response_model=ContradictionRead)
    async def get_contradiction(project_id: str, contradiction_id: UUID) -> ContradictionRead | JSONResponse:
        """Read one exact relationship."""
        try:
            selected = project(project_id)
            found = await source().contradiction(selected, contradiction_id)
        except MemoryReadUnavailable as exc:
            return refusal(exc)
        return found if found is not None else JSONResponse(status_code=404, content={"error":{"code":"UNKNOWN_CONTRADICTION","message":"contradiction unavailable"}})
