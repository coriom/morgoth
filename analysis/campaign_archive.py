"""Campaign-owned PostgreSQL evidence export; no initialization or inference.

Global snapshots/logs are not campaign-owned and are deliberately excluded.
Missing classes are declared. Suspected credentials reject the entire export,
without disclosing values or silently changing persisted research content.
"""
from __future__ import annotations

import asyncio
from collections import Counter
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any
from uuid import UUID

import asyncpg

from core.storage_namespace import validate_identifier


class ArchiveError(ValueError):
    """Safe diagnostic; must never contain database contents or credentials."""


_SECRET_KEY = re.compile(r"(?:password|passwd|secret|api_?key|authorization|credential|access_?(?:token|key)|refresh_?token|private_?key)", re.I)
_SECRET_TEXT = re.compile(
    r"-----BEGIN (?:\w+ )?PRIVATE KEY-----|\b(?:sk-[\w-]{12,}|gh[pousr]_[\w]{15,})"
    r"|\bBearer\s+\S+|\b(?:password|api_?key|access_?token|refresh_?token|secret)[\"']?\s*[=:]\s*\S+"
    r"|[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"
    r"|\beyJ[\w-]+\.eyJ[\w-]+\.[\w-]+", re.I,
)


def _check_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _SECRET_KEY.search(str(key)) and child not in (None, "", [], {}):
                raise ArchiveError("suspected sensitive content: archive refused")
            _check_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _check_secrets(child)
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise ArchiveError("suspected sensitive content: archive refused")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Full canonical record is the tie-breaker even without a declared PK.
    return sorted(rows, key=lambda r: (str(r.get("created_at") or r.get("occurred_at") or r.get("updated_at") or ""), _canonical(r)))


async def read_campaign_archive(
    connection: Any, campaign_id: str, *, schema: str, project_id: str,
    exporter_version: str, validity: str = "NOT_ASSESSED", forensic_only: bool = False,
    exported_at: datetime | None = None,
) -> dict[str, Any]:
    """Read one consistent, read-only snapshot scoped to the canonical namespace."""
    try:
        cid = str(UUID(campaign_id))
    except ValueError:
        raise ArchiveError("campaign id must be a full UUID") from None
    schema = validate_identifier(schema)
    if validity not in ("NOT_ASSESSED", "COMPLETE", "PARTIAL", "EMPTY/FAILED"):
        raise ArchiveError("invalid validity annotation")
    if validity in ("PARTIAL", "EMPTY/FAILED"):
        forensic_only = True
    records: dict[str, list[dict[str, Any]]] = {}
    missing: list[dict[str, str]] = []
    async with connection.transaction(isolation="repeatable_read", readonly=True):
        columns = await connection.fetch(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = $1", schema,
        )
        tables: dict[str, set[str]] = {}
        for column in columns:
            tables.setdefault(column["table_name"], set()).add(column["column_name"])
        for required in ("campaigns", "objectives", "theses"):
            if required not in tables:
                raise ArchiveError("required campaign tables missing in selected Project")

        async def rows(table: str, where: str, *args: Any) -> list[dict[str, Any]]:
            # Identifiers/WHERE are constants below, never archive inputs.
            found = await connection.fetch(f'SELECT to_jsonb(r) AS record FROM "{schema}"."{table}" r WHERE {where}', *args)
            return _ordered([json.loads(r["record"]) if isinstance(r["record"], str) else dict(r["record"]) for r in found])

        campaigns = await rows("campaigns", "r.campaign_id::text = $1", cid)
        if len(campaigns) != 1:
            raise ArchiveError("campaign not found in selected Project")
        campaign = campaigns[0]
        records["objectives"] = await rows("objectives", "r.campaign_id::text = $1", cid)
        objective_ids = sorted(str(r["objective_id"]) for r in records["objectives"])
        records["theses"] = await rows("theses", "r.objective_id::text = ANY($1::text[])", objective_ids)
        thesis_ids = sorted(str(r["thesis_id"]) for r in records["theses"])
        optional = (
            ("campaign_data_gaps", {"campaign_id"}, "r.campaign_id::text = $1", cid),
            ("numeric_fidelity_events", {"objective_id"}, "r.objective_id::text = ANY($1::text[])", objective_ids),
            ("field_confusion_events", {"thesis_id"}, "r.thesis_id::text = ANY($1::text[])", thesis_ids),
            ("contradictions", {"thesis_id_a", "thesis_id_b"}, "r.thesis_id_a::text = ANY($1::text[]) AND r.thesis_id_b::text = ANY($1::text[])", thesis_ids),
        )
        for table, links, where, key in optional:
            if not links.issubset(tables.get(table, set())):
                missing.append({"category": table, "reason": "table or required ownership columns unavailable"})
                records[table] = []
            else:
                records[table] = await rows(table, where, key)

    # Preserve original evidence arrays exactly; indexed projections expose their
    # relationship without inventing event timestamps or missing cycle records.
    records["objective_evidence"] = []
    records["cycle_payloads"] = []
    for objective in records["objectives"]:
        evidence = objective.get("evidence") or []
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        if not isinstance(evidence, list):
            raise ArchiveError("unsupported objective evidence format")
        for index, entry in enumerate(evidence):
            linked = {"objective_id": objective["objective_id"], "evidence_index": index, "record": entry}
            records["objective_evidence"].append(linked)
            if isinstance(entry, dict) and entry.get("type") == "cycle_payload":
                records["cycle_payloads"].append(linked)
    for key in ("objective_evidence", "cycle_payloads"):
        records[key].sort(key=lambda r: (r["objective_id"], r["evidence_index"]))

    versions: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("code_version", "git_sha") and isinstance(child, str) and child:
                    versions.add(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(campaign)
    visit(records)
    omissions = {
        "chroma_findings": "not exported: no read-only Chroma initialization contract; objective evidence retained",
        "source_snapshots": "global collector rows have no campaign/objective ownership link; time overlap is not ownership",
        "metric_series": "global observations have no campaign/objective ownership link",
        "session_gaps": "global startup ledger, not campaign-owned; open downtime cannot be reconstructed from owned rows",
        "connectivity_transitions": "global transitions have no campaign ownership link",
        "provider_health": "global provider ledger excluded, including diagnostic credential context",
        "resource_samples": "global resource samples have no campaign ownership link",
        "raw_logs": "no campaign ownership link; system and application logs excluded",
        "cross_campaign_contradictions": "only pairs whose two theses belong to this campaign are exported",
        "unlinked_measurement_events": "events without matching objective/thesis identifiers cannot be assigned to this campaign",
        "complete_cycle_timeline": "cycle counters/payloads do not timestamp every cycle or prove uninterrupted execution",
    }
    if not versions or any(not r.get("code_version") for r in records["theses"]):
        omissions["complete_code_provenance"] = "absent/NULL producer versions cannot be inferred from exporter HEAD"
    omissions["objective_code_provenance"] = "legacy objectives/payloads are not consistently stamped with producer versions"
    missing += [{"category": k, "reason": v} for k, v in omissions.items()]
    statuses = Counter(r.get("status", "unknown") for r in records["objectives"])
    success = Counter()
    for entry in records["cycle_payloads"]:
        for tool in entry["record"].get("tool_results") or []:
            if isinstance(tool, dict) and tool.get("success") is True and not tool.get("error"):
                success[str(tool.get("tool"))] += 1
    artifact = {
        "archive_schema_version": 1,
        "exported_at": (exported_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "project_id": project_id, "postgres_schema": schema,
        "campaign": campaign, "records": records,
        "code_versions_in_records": sorted(versions), "exporter_code_version": exporter_version,
        "record_counts": {"campaign": 1, **{key: len(value) for key, value in records.items()}},
        "validity_metadata": {
            "classification": validity, "classification_origin": "operator_annotation" if validity != "NOT_ASSESSED" else "not_assessed",
            "forensic_only": forensic_only, "continuity_proven": False,
            "objective_status_counts": dict(statuses),
            "objective_cycle_counter_sum": sum(r.get("cycle_count") or 0 for r in records["objectives"]),
            "recorded_tool_successes": dict(success),
            "note": "Counters are not a complete execution timeline; tool results are not HTTP request counts.",
        },
        "unreconstructed_categories": sorted(missing, key=lambda r: r["category"]),
    }
    _check_secrets(artifact)
    return artifact


def _rename_new(source: Path, target: Path) -> None:
    # Linux runtime contract: atomic RENAME_NOREPLACE also rejects concurrent
    # writers and dangling symlinks. Ordinary os.rename could overwrite them.
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise ArchiveError("atomic no-overwrite rename unavailable on this platform")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
        raise OSError(ctypes.get_errno(), "archive publication refused")


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_campaign_archive(artifact: dict[str, Any], output: Path) -> str:
    """Publish fsynced 0600 JSON and checksum without replacing any destination.

    Publish checksum first, JSON last as completion marker. Two renames cannot
    form one filesystem transaction: a crash may leave a checksum-only artifact;
    retry fails closed. No pre-existing file is ever removed or overwritten.
    """
    output = Path(output).absolute()
    checksum = Path(str(output) + ".sha256")
    if any(c in output.name for c in "\n\r\\"):
        raise ArchiveError("unsupported archive filename")
    if os.path.lexists(output) or os.path.lexists(checksum):
        raise ArchiveError("archive or checksum already exists")
    _check_secrets(output.name)
    _check_secrets(artifact)
    data = (json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    temporary: list[Path] = []
    try:
        for payload in (data, f"{digest}  {output.name}\n".encode("utf-8")):
            fd, name = tempfile.mkstemp(prefix=".campaign-archive-", dir=output.parent)
            temporary.append(Path(name))
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        _rename_new(temporary[1], checksum)
        _fsync_directory(output.parent)
        _rename_new(temporary[0], output)
        _fsync_directory(output.parent)
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return digest


async def archive_command(campaign_id: str, output: Path, *, validity: str = "NOT_ASSESSED", forensic_only: bool = False) -> str:
    """Read via asyncpg directly: never load_config, PM.initialize or Chroma."""
    from core.project import current_namespace
    from core.config import _load_environment
    from core.version import get_code_version
    project = current_namespace()
    # Same legacy dotenv precedence as normal configuration, without creating
    # runtime/log/Chroma directories or loading provider credentials into output.
    if project.is_legacy:
        await asyncio.to_thread(_load_environment)
    dsn = os.environ.get("POSTGRES_URL")
    if not dsn:
        raise ArchiveError("POSTGRES_URL is required for archive")
    connection = await asyncpg.connect(dsn, server_settings={
        "default_transaction_read_only": "on", "statement_timeout": "30000",
        "search_path": validate_identifier(project.postgres_schema), "timezone": "UTC",
    })
    try:
        artifact = await read_campaign_archive(
            connection, campaign_id, schema=project.postgres_schema, project_id=project.id,
            exporter_version=await asyncio.to_thread(get_code_version), validity=validity, forensic_only=forensic_only,
        )
    finally:
        await connection.close()
    return await asyncio.to_thread(write_campaign_archive, artifact, output)
