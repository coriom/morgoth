"""Two Project namespaces and fixture-only fact capture in morgoth_test."""
from __future__ import annotations

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


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_weather_fixture_capture_and_crypto_project_isolation(tmp_path):
    """Capture in Weather A; Weather B and Crypto see no temporal facts."""
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL", "")
    assert urlparse(dsn).path == "/morgoth_test"
    suffix = uuid.uuid4().hex[:8]
    projects = ((f"weather_a_{suffix}", "weather"),
                (f"weather_b_{suffix}", "weather"),
                (f"crypto_c_{suffix}", "crypto"))
    for project_id, domain in projects:
        path = tmp_path / "projects" / project_id / "project.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(yaml.safe_dump({
            "id": project_id, "name": project_id, "domain": domain,
            "postgres_schema": f"project_{project_id}", "chroma_prefix": f"{project_id}_",
            "vault_dir": str(tmp_path / project_id / "vault"),
            "runtime_dir": str(tmp_path / project_id / "runtime"),
        }), encoding="utf-8")
    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        assert await conn.fetchval("SELECT current_database()") == "morgoth_test"
        results = []
        for index, (project_id, _) in enumerate(projects):
            env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                   "PYTHONPATH": str(ROOT), "MORGOTH_HOME": str(tmp_path),
                   "MORGOTH_PROJECT": project_id, "MORGOTH_TEST_POSTGRES_URL": dsn,
                   "FACT_PROBE_CAPTURE": "1" if index == 0 else "0"}
            proc = subprocess.run([sys.executable, str(ROOT / "tests/temporal_fact_process_probe.py")],
                                  cwd=tmp_path, env=env, capture_output=True, text=True,
                                  timeout=60, check=False)
            assert proc.returncode == 0, proc.stderr[-600:]
            results.append(json.loads(proc.stdout))
        assert results[0]["predictions"] == 1 and results[0]["observations"] == 1
        assert results[0]["count_by_filter"] == 1
        assert results[0]["met_updated_semantics"] and results[0]["nws_valid_semantics"]
        assert results[0]["retrospective_ineligible"] and results[0]["no_scorer_run"]
        assert all(row["predictions"] == row["observations"] == 0 for row in results[1:])
    finally:
        for project_id, _ in projects:
            await conn.execute(f'DROP SCHEMA IF EXISTS "project_{project_id}" CASCADE')
        await conn.close()
