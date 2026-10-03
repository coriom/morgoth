"""Read-only verifier integration, exclusively on disposable morgoth_test schemas."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from urllib.parse import urlparse

import asyncpg
import pytest
import yaml

from memory.persistent import TABLE_STATEMENTS
from scripts.verify_facts import read_fact_corpus


ROOT = Path(__file__).resolve().parents[1]


def _manifest(home: Path, project_id: str, domain: str) -> str:
    path = home / "projects" / project_id / "project.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "id": project_id, "name": project_id, "domain": domain,
        "postgres_schema": f"project_{project_id}", "chroma_prefix": f"{project_id}_",
        "vault_dir": str(home / project_id / "vault"),
        "runtime_dir": str(home / project_id / "runtime"),
    }), encoding="utf-8")
    return f"project_{project_id}"


def _run(home: Path, dsn: str, project_id: str, start: datetime, end: datetime,
         as_of: datetime) -> subprocess.CompletedProcess[str]:
    """Never inherit credentials or production configuration."""
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "PYTHONPATH": str(ROOT), "MORGOTH_HOME": str(home),
           "MORGOTH_PROJECT": project_id, "POSTGRES_URL": dsn}
    return subprocess.run(
        [sys.executable, "-m", "scripts.verify_facts",
         "--from", start.isoformat(), "--to", end.isoformat(),
         "--as-of", as_of.isoformat(), "--json"],
        cwd=home, env=env, capture_output=True, text=True, timeout=45, check=False,
    )


async def _insert(conn: asyncpg.Connection, schema: str, project_id: str, label: str,
                  kind: str, value: float, valid_at: datetime) -> None:
    source = "MET Norway" if kind == "prediction" else "NWS"
    tool = "get_weather_forecast_met" if kind == "prediction" else "get_nws_weather_observation"
    dimensions = {"latitude": 38.8512, "longitude": -77.0402}
    if kind == "observation":
        dimensions["station_id"] = "SYNTHETIC_KDCA"
    await conn.execute(
        f'INSERT INTO "{schema}".temporal_facts '
        "(semantic_key, project_id, domain_id, kind, source, tool, metric, value, unit, "
        "entity, dimensions, valid_at, source_record_id) "
        "VALUES ($1, $2, 'weather', $3, $4, $5, 'temperature', $6, 'celsius', "
        "'location', $7::jsonb, $8, $9)",
        hashlib.sha256(f"{project_id}:{label}".encode()).hexdigest(), project_id,
        kind, source, tool, value, json.dumps(dimensions), valid_at,
        f"SYNTHETIC_{label}",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_only_dispatch_isolation_snapshot_and_overflow(tmp_path: Path) -> None:
    """Synthetic future target and injected as_of; no live provider claims."""
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL", "")
    assert urlparse(dsn).path == "/morgoth_test"
    suffix = uuid.uuid4().hex[:8]
    weather_a, weather_b = f"verify_a_{suffix}", f"verify_b_{suffix}"
    crypto, missing = f"verify_c_{suffix}", f"verify_missing_{suffix}"
    ids = ((weather_a, "weather"), (weather_b, "weather"),
           (crypto, "crypto"), (missing, "weather"))
    schemas = {project_id: _manifest(tmp_path, project_id, domain)
               for project_id, domain in ids}
    target = datetime(2027, 1, 2, 12, tzinfo=timezone.utc)
    start, end, as_of = target - timedelta(hours=1), target + timedelta(hours=1), target + timedelta(hours=2)
    conn = await asyncpg.connect(dsn, timeout=5)
    writer = await asyncpg.connect(dsn, timeout=5)
    try:
        assert await conn.fetchval("SELECT current_database()") == "morgoth_test"
        for project_id in (weather_a, weather_b, crypto):
            await conn.execute(f'CREATE SCHEMA "{schemas[project_id]}"')
            await conn.execute(f'SET search_path TO "{schemas[project_id]}"')
            await conn.execute(TABLE_STATEMENTS[0])
        await _insert(conn, schemas[weather_a], weather_a, "forecast_20", "prediction", 20, target)
        await _insert(conn, schemas[weather_a], weather_a, "observed_22", "observation", 22, target)
        before = await conn.fetchval(f'SELECT count(*) FROM "{schemas[weather_a]}".temporal_facts')
        success = _run(tmp_path, dsn, weather_a, start, end, as_of)
        assert success.returncode == 0, success.stderr
        report = json.loads(success.stdout)
        assert report["status"] == "OK" and report["project_id"] == weather_a
        assert report["domain_id"] == "weather"
        assert report["counts"]["matched_pairs"] == 1
        assert report["metrics"]["mae_celsius"] == 2
        assert report["details"][0]["signed_error"] == -2
        assert report["details"][0]["station_id"] == "SYNTHETIC_KDCA"
        assert len(report["input_corpus_digest_sha256"]) == 64
        assert json.loads(_run(tmp_path, dsn, weather_a, start, end, as_of).stdout) == report
        assert await conn.fetchval(f'SELECT count(*) FROM "{schemas[weather_a]}".temporal_facts') == before

        empty = _run(tmp_path, dsn, weather_b, start, end, as_of)
        assert empty.returncode == 0 and json.loads(empty.stdout)["counts"]["candidate_predictions"] == 0
        unsupported = _run(tmp_path, dsn, crypto, start, end, as_of)
        assert unsupported.returncode == 2 and json.loads(unsupported.stdout)["status"] == "UNSUPPORTED"
        import_check = subprocess.run(
            [sys.executable, "-c", "import sys; from core.domain import current_domain; "
             "from analysis.scorer_registry import resolve_scorer; "
             "assert resolve_scorer(current_domain(), 'verification') is None; "
             "assert 'analysis.weather_temperature_verification' not in sys.modules; "
             "assert 'analysis.thesis_backtest' not in sys.modules"],
            cwd=tmp_path,
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                 "PYTHONPATH": str(ROOT), "MORGOTH_HOME": str(tmp_path),
                 "MORGOTH_PROJECT": crypto},
            capture_output=True, text=True, timeout=30, check=False)
        assert import_check.returncode == 0, import_check.stderr
        unavailable = _run(tmp_path, dsn, missing, start, end, as_of)
        assert unavailable.returncode == 2 and "unavailable" in unavailable.stderr
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=$1 AND table_name='temporal_facts')", schemas[missing])

        # The actual reader keeps one snapshot while a separate writer commits.
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            first = await read_fact_corpus(
                conn, schema=schemas[weather_a], project_id=weather_a, domain_id="weather",
                from_at=start, to_at=end, as_of=as_of, max_offset_seconds=1800)
            await _insert(writer, schemas[weather_a], weather_a, "revision_21", "prediction", 21, target)
            second = await read_fact_corpus(
                conn, schema=schemas[weather_a], project_id=weather_a, domain_id="weather",
                from_at=start, to_at=end, as_of=as_of, max_offset_seconds=1800)
            assert first == second and len(first) == 2
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            later = await read_fact_corpus(
                conn, schema=schemas[weather_a], project_id=weather_a, domain_id="weather",
                from_at=start, to_at=end, as_of=as_of, max_offset_seconds=1800)
            assert len(later) == 3

        await writer.execute(
            f'INSERT INTO "{schemas[weather_a]}".temporal_facts '
            "(semantic_key, project_id, domain_id, kind, source, tool, metric, value, unit, "
            "entity, dimensions, valid_at, source_record_id) "
            "SELECT 'b' || lpad(n::text, 63, '0'), $1, 'weather', 'prediction', "
            "'MET Norway', 'get_weather_forecast_met', 'temperature', 20, 'celsius', "
            "'location', '{\"latitude\":38.8512,\"longitude\":-77.0402}'::jsonb, "
            "$2::timestamptz + n * interval '1 microsecond', 'SYNTHETIC_BULK' "
            "FROM generate_series(1, 10001) AS n",
            weather_a, target)
        overflow = _run(tmp_path, dsn, weather_a, start, end, as_of)
        assert overflow.returncode == 2
        assert not overflow.stdout and "10000 facts" in overflow.stderr
    finally:
        await writer.close()
        for project_id in (weather_a, weather_b, crypto):
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schemas[project_id]}" CASCADE')
        await conn.close()
