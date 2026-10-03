"""Read-only Domain verification: python -m scripts.verify_facts --help.

The active Domain selects a trusted scorer. This command only obtains one
Project-scoped, bounded, repeatable-read fact corpus and renders its result.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from types import MappingProxyType
from typing import Any, Mapping

import asyncpg

from analysis.scorer_registry import resolve_scorer
from core.domain import current_domain
from core.project import current_namespace
from core.storage_namespace import validate_identifier
from core.temporal_facts import _utc
from memory.persistent import TemporalFactQueryOverflow


class FactsUnavailable(ValueError):
    """The selected Project has no temporal-fact table to evaluate."""


def _code_sha() -> str:
    """Report this checkout's full commit SHA without reading application state."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha) else "unknown"


def _timestamp(raw: str) -> datetime:
    """Require a timezone-aware ISO instant; never assume local time."""
    try:
        return _utc(raw, "verification timestamp")
    except ValueError:
        raise argparse.ArgumentTypeError("verification timestamp must be timezone-aware ISO-8601") from None


def _distance(raw: str) -> float:
    """Parse a finite, bounded nonzero spatial tolerance."""
    try:
        value = float(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid station distance") from None
    if not math.isfinite(value) or not 0 < value <= 1000:
        raise argparse.ArgumentTypeError("station distance must be in (0, 1000] km")
    return value


def _offset(raw: str) -> int:
    """Parse a bounded positive whole-minute temporal tolerance."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid observation offset") from None
    if not 0 < value <= 1440:
        raise argparse.ArgumentTypeError("observation offset must be in (0, 1440] minutes")
    return value


async def read_fact_corpus(
    connection: asyncpg.Connection, *, schema: str, project_id: str, domain_id: str,
    from_at: datetime, to_at: datetime, as_of: datetime, max_offset_seconds: int,
) -> tuple[Mapping[str, Any], ...]:
    """Read one complete corpus, or fail; never initialize or write state.

    The caller holds one repeatable-read, read-only transaction across the
    existence check and bounded SELECT. No independent pool calls are made.
    """
    schema = validate_identifier(schema)
    if (isinstance(max_offset_seconds, bool) or not isinstance(max_offset_seconds, int)
            or not 0 < max_offset_seconds <= 86400 or from_at >= to_at):
        raise ValueError("invalid verification query bounds")
    margin = timedelta(seconds=max_offset_seconds)
    exists = await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = $1 AND table_name = 'temporal_facts')", schema,
    )
    if not exists:
        raise FactsUnavailable("temporal facts unavailable for selected Project")
    rows = await connection.fetch(
        f'SELECT * FROM "{schema}".temporal_facts '
        "WHERE project_id = $1 AND domain_id = $2 AND acquired_at <= $3 "
        "AND ((kind = 'prediction' AND valid_at >= $4 AND valid_at < $5) "
        "OR (kind = 'observation' AND valid_at >= $6 AND valid_at < $7)) "
        "ORDER BY valid_at, acquired_at, semantic_key LIMIT 10001",
        project_id, domain_id, as_of, from_at, to_at, from_at - margin, to_at + margin,
    )
    if len(rows) > 10000:
        raise TemporalFactQueryOverflow("verification corpus exceeds 10000 facts; narrow interval")
    return tuple(MappingProxyType(dict(row)) for row in rows)


async def evaluate(
    *, from_at: datetime, to_at: datetime, as_of: datetime,
    max_station_distance_km: float = 10.0, max_observation_offset_seconds: int = 1800,
) -> dict[str, Any]:
    """Resolve the selected Domain and dispatch without any fallback scorer."""
    from_at = _utc(from_at, "from_at")
    to_at = _utc(to_at, "to_at")
    as_of = _utc(as_of, "as_of")
    project = current_namespace()
    domain = current_domain()
    if domain.name != project.domain:
        raise ValueError("selected Project and Domain disagree")
    scorer = resolve_scorer(domain, "verification")
    if scorer is None:
        return {"status": "UNSUPPORTED", "project_id": project.id,
                "domain_id": domain.name, "role": "verification"}
    if (from_at >= to_at or isinstance(max_observation_offset_seconds, bool)
            or not isinstance(max_observation_offset_seconds, int)
            or not 0 < max_observation_offset_seconds <= 86400
            or isinstance(max_station_distance_km, bool)
            or not isinstance(max_station_distance_km, (float, int))
            or not math.isfinite(max_station_distance_km)
            or not 0 < max_station_distance_km <= 1000):
        raise ValueError("invalid verification options")
    if project.is_legacy:
        # Same read-only credential lookup as campaign archive; no load_config,
        # PersistentMemory.initialize, runtime directory creation or collector.
        from core.config import _load_environment
        await asyncio.to_thread(_load_environment)
    dsn = os.environ.get("POSTGRES_URL")
    if not dsn:
        raise FactsUnavailable("POSTGRES_URL is required for verification")
    schema = validate_identifier(project.postgres_schema)
    connection = await asyncpg.connect(dsn, server_settings={
        "default_transaction_read_only": "on", "statement_timeout": "30000",
        "search_path": schema, "timezone": "UTC",
    })
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            facts = await read_fact_corpus(
                connection, schema=schema, project_id=project.id, domain_id=domain.name,
                from_at=from_at, to_at=to_at, as_of=as_of,
                max_offset_seconds=max_observation_offset_seconds,
            )
    finally:
        await connection.close()
    scored = scorer(
        facts, from_at=from_at, to_at=to_at, as_of=as_of,
        max_station_distance_km=max_station_distance_km,
        max_observation_offset_seconds=max_observation_offset_seconds,
    )
    return {"status": "OK", "code_sha": await asyncio.to_thread(_code_sha),
            "project_id": project.id, "domain_id": domain.name, **scored}


def _human(report: Mapping[str, Any]) -> str:
    if report["status"] == "UNSUPPORTED":
        return f"Verification unsupported for Project {report['project_id']} / Domain {report['domain_id']}"
    counts, metrics = report["counts"], report["metrics"]
    interval = report["interval"]
    return "\n".join((
        f"Verification: {report['domain_id']} / {report['metric']} ({report['policy_version']})",
        f"Period: {interval['from']} to {interval['to_exclusive']} [exclusive]; as_of: {report['as_of']}",
        f"Forecasts: {counts['candidate_predictions']}  due: {counts['due_prospective']}  "
        f"matched: {counts['matched_pairs']}  unmatched: {counts['unmatched_due']}",
        f"Match rate (matched / due): {report['match_rate']}",
        f"MAE: {metrics['mae_celsius']}  bias: {metrics['bias_celsius']}  "
        f"RMSE: {metrics['rmse_celsius']}",
        f"Diagnostics: {json.dumps(report['unmatched_by_reason'], sort_keys=True)}",
    ))


def main(argv: list[str] | None = None) -> int:
    """Run a bounded read-only verification and print no credential details."""
    parser = argparse.ArgumentParser(prog="python -m scripts.verify_facts")
    parser.add_argument("--from", dest="from_at", required=True, type=_timestamp)
    parser.add_argument("--to", dest="to_at", required=True, type=_timestamp)
    parser.add_argument("--as-of", dest="as_of", required=True, type=_timestamp)
    parser.add_argument("--max-distance-km", type=_distance, default=10.0)
    parser.add_argument("--max-offset-minutes", type=_offset, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(evaluate(
            from_at=args.from_at, to_at=args.to_at, as_of=args.as_of,
            max_station_distance_km=args.max_distance_km,
            max_observation_offset_seconds=args.max_offset_minutes * 60,
        ))
    except (FactsUnavailable, TemporalFactQueryOverflow, ValueError) as exc:
        print(f"verification unavailable: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # asyncpg/OS exceptions may include connection strings or raw rows.
        print("verification failed without a partial score", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False) if args.json else _human(report))
    return 0 if report["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
