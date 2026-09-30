"""Dedicated morgoth_test proof of expiry and concurrent campaign exclusion."""
import asyncio
import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from core.project import Project
from core.llm import profiles as P, environment as E


@pytest.mark.integration
@pytest.mark.asyncio
async def test_profile_campaign_guard_real_sql(tmp_path,monkeypatch):
    dsn=os.environ.get('MORGOTH_TEST_POSTGRES_URL','')
    assert urlparse(dsn).path=='/morgoth_test'
    conn=await asyncpg.connect(dsn,timeout=5)
    assert await conn.fetchval('SELECT current_database()')=='morgoth_test'
    schema='profile_test_'+uuid4().hex[:12]
    project=Project(id=schema,name='synthetic',domain='crypto',postgres_schema=schema,
                    chroma_prefix=schema+'_',runtime_dir=tmp_path/'state',vault_dir=tmp_path/'vault')
    cap=E.Capability('ok','synthetic')
    async def environment(): return E.Environment('synthetic',cap,cap,cap,cap,cap)
    monkeypatch.setattr(E,'detect_environment',environment)
    other=None;pending=None
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute('CREATE TABLE campaigns(status text, ended_at timestamptz, ends_at timestamptz)')
        await conn.execute("INSERT INTO campaigns VALUES ('active',NULL,NOW()+interval '1 day')")
        with pytest.raises(P.ProfileError,match='research-live'):
            await P.switch_profile(project,'claude')
        assert not P.state_path(project).exists()
        # An expired row is still persisted active; no lifecycle mutation needed.
        await conn.execute("UPDATE campaigns SET ends_at=NOW()-interval '1 second'")
        await P.switch_profile(project,'claude')
        assert P.current_profile(project).id=='claude'
        assert await conn.fetchval('SELECT status FROM campaigns')=='active'
        other=await asyncpg.connect(dsn,server_settings={'search_path':schema},timeout=5)
        async with P.campaign_guard(project):
            pending=asyncio.create_task(other.execute("INSERT INTO campaigns VALUES ('active',NULL,NOW()+interval '1 day')"))
            await asyncio.sleep(.05)
            assert not pending.done(), 'campaign creation must wait for profile publication'
        await asyncio.wait_for(pending,5)
        await P.switch_profile(project,'claude')  # no-op allowed during live campaign
        with pytest.raises(P.ProfileError,match='research-live'):
            await P.switch_profile(project,'legacy')
        await conn.execute("UPDATE campaigns SET ended_at=NOW(),status='cancelled'")
        await P.switch_profile(project,'legacy')
        assert P.current_profile(project).id=='legacy'
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending,return_exceptions=True)
        if other is not None: await other.close()
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
