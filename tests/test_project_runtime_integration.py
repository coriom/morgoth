"""Opt-in two-process SQL proof, confined to the dedicated morgoth_test DB."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from urllib.parse import urlparse

import asyncpg
import pytest

from core.runtime import ENGINE_ROOT
from test_project_runtime import catalog, child_env


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_processes_same_domain_isolated_sql(tmp_path):
    """Check real pool resets, unqualified DDL/DML and public escape rejection."""
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL", "")
    assert urlparse(dsn).path == "/morgoth_test", "this proof requires morgoth_test"
    suffix = uuid.uuid4().hex[:8]
    names = (f"probe_{suffix}_a", f"probe_{suffix}_b")
    catalog(tmp_path, *names)
    conn = await asyncpg.connect(dsn, timeout=5)
    extensions_before = await conn.fetch("SELECT extname, extnamespace FROM pg_extension ORDER BY extname")
    created_sentinel = False
    children = []
    try:
        assert await conn.fetchval("SELECT current_database()") == "morgoth_test"
        # Refuse collision rather than deleting another test's state.
        assert not await conn.fetchval("SELECT to_regclass('public.project_public_escape_probe')")
        await conn.execute("CREATE TABLE public.project_public_escape_probe (value text)")
        created_sentinel = True
        await conn.execute("INSERT INTO public.project_public_escape_probe VALUES ('synthetic-public-marker')")
        for name in names:
            children.append(await asyncio.create_subprocess_exec(
                sys.executable, str(ENGINE_ROOT / "tests/project_process_probe.py"),
                cwd=tmp_path, env=child_env(tmp_path, name, database=dsn),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
        for child, name in zip(children, names):
            stdout, _ = await asyncio.wait_for(child.communicate(), timeout=45)
            if child.returncode:
                failure = json.loads(stdout) if stdout else {}
                pytest.fail(f"child isolation proof failed: {failure.get('failure_type', 'unknown')} at line {failure.get('line', 0)}")
            result = json.loads(stdout)
            assert result["database_isolated"] and result["environment_clean"]
            assert result["domain"] == "crypto"
            # IDs generated above are machine-safe; no untrusted SQL interpolation.
            rows = await conn.fetch(f'SELECT value FROM "project_{name}".knowledge WHERE key = $1', "same-key")
            assert [r["value"] for r in rows] == [name]
        assert await conn.fetchval("SELECT count(*) FROM public.project_public_escape_probe") == 1
    finally:
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
        for name in names:
            await conn.execute(f'DROP SCHEMA IF EXISTS "project_{name}" CASCADE')
        if created_sentinel:
            await conn.execute("DROP TABLE public.project_public_escape_probe")
        extensions_after = await conn.fetch("SELECT extname, extnamespace FROM pg_extension ORDER BY extname")
        await conn.close()
        assert extensions_before == extensions_after
