"""Real SQL proof, opt-in dedicated test DB; never production configuration."""
from datetime import datetime, timedelta, timezone
import json
import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from analysis.campaign_archive import read_campaign_archive
from memory.persistent import PersistentMemory
from test_campaign_lifecycle import Checkout


@pytest.mark.integration
@pytest.mark.asyncio
async def test_expired_campaign_restart_and_readonly_archive():
    dsn = os.environ.get('MORGOTH_TEST_POSTGRES_URL', '')
    assert urlparse(dsn).path == '/morgoth_test'
    conn = await asyncpg.connect(dsn, timeout=5)
    assert await conn.fetchval('SELECT current_database()') == 'morgoth_test'
    schema = 'campaign_probe_' + uuid4().hex[:12]
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}"')
        await conn.execute('''CREATE TABLE campaigns (campaign_id uuid PRIMARY KEY, status text, started_at timestamptz, ends_at timestamptz, ended_at timestamptz, subject text);
        CREATE TABLE objectives (objective_id uuid PRIMARY KEY, campaign_id uuid, status text, created_at timestamptz, updated_at timestamptz, cycle_count int, priority int, title text, evidence jsonb);
        CREATE TABLE theses (thesis_id uuid PRIMARY KEY, objective_id text, created_at timestamptz, code_version text);''')
        now = datetime.now(timezone.utc)
        ids = {}
        for name, status, end, ended in [('live', 'active', now+timedelta(days=1), None), ('expired', 'active', now-timedelta(seconds=1), None), ('closed', 'cancelled', now+timedelta(days=1), now), ('inconsistent', 'active', now+timedelta(days=1), now)]:
            ids[name] = uuid4()
            await conn.execute('INSERT INTO campaigns VALUES($1,$2,$3,$4,$5,$6)', ids[name], status, now-timedelta(days=1), end, ended, 'synthetic')
        objectives = {}
        for name in ('live', 'expired', 'closed', 'ordinary', 'inconsistent', 'dangling'):
            objectives[name] = uuid4()
            cid = ids.get(name) if name != 'dangling' else uuid4()
            await conn.execute('INSERT INTO objectives VALUES($1,$2,$3,$4,$5,3,1,$6,$7)', objectives[name], cid, 'in_progress', now-timedelta(hours=3), now-timedelta(hours=2), name, json.dumps([{'type':'cycle_payload','cycle':3,'tool_results':[]}]))
        pm = PersistentMemory.__new__(PersistentMemory)
        class Pool:
            def acquire(self): return Checkout(conn)
        pm._pool = Pool()
        original = {r['objective_id']: dict(r) for r in await conn.fetch('SELECT * FROM objectives')}
        # Startup sweep precedes reclaim; forensic rows must not be touched even
        # when old enough for the ordinary stale terminal status.
        swept = await pm.timeout_stale_objectives(1)
        assert swept == []
        reclaimed = await pm.reclaim_orphan_objectives(30)
        assert {r['objective_id'] for r in reclaimed} == {objectives['live'], objectives['ordinary']}
        # Before expiry has run, only live + unbound can be claimed.
        claimed = await pm.claim_next_objective(limit=20)
        assert {r['objective_id'] for r in claimed} == {objectives['live'], objectives['ordinary']}
        await pm.expire_active_campaign_if_due()
        assert await conn.fetchval('SELECT status FROM campaigns WHERE campaign_id=$1', ids['expired']) == 'completed'
        for name in ('expired', 'closed', 'inconsistent', 'dangling'):
            assert dict(await conn.fetchrow('SELECT * FROM objectives WHERE objective_id=$1', objectives[name])) == original[objectives[name]]
        # Pending and freshly in-progress objectives of closed campaigns are
        # equally nonclaimable (not just abandoned old rows).
        await conn.execute("UPDATE objectives SET status='pending' WHERE objective_id=$1", objectives['closed'])
        await conn.execute('UPDATE objectives SET updated_at=NOW() WHERE objective_id=$1', objectives['expired'])
        claimed = await pm.claim_next_objective(limit=20)
        assert {r['objective_id'] for r in claimed} == {objectives['live'], objectives['ordinary']}
        # Restore original forensic rows for comparison/export fixture.
        await conn.execute("UPDATE objectives SET status='in_progress' WHERE objective_id=$1", objectives['closed'])
        await conn.execute('UPDATE objectives SET updated_at=$2 WHERE objective_id=$1', objectives['expired'], original[objectives['expired']]['updated_at'])
        thesis_id = uuid4()
        await conn.execute('INSERT INTO theses VALUES($1,$2,$3,$4)', thesis_id, str(objectives['expired']), now, 'synthetic-sha')
        await conn.execute('INSERT INTO theses VALUES($1,$2,$3,$4)', uuid4(), str(objectives['live']), now, 'unrelated-sha')
        before = await conn.fetch('SELECT * FROM objectives ORDER BY objective_id')
        campaign_before = await conn.fetch('SELECT * FROM campaigns ORDER BY campaign_id')
        result = await read_campaign_archive(conn, str(ids['expired']), schema=schema, project_id='synthetic', exporter_version='exporter', validity='PARTIAL')
        assert result['record_counts']['objectives'] == 1 and result['record_counts']['theses'] == 1
        assert result['code_versions_in_records'] == ['synthetic-sha']
        assert result['campaign']['status'] == 'completed'
        assert before == await conn.fetch('SELECT * FROM objectives ORDER BY objective_id')
        assert campaign_before == await conn.fetch('SELECT * FROM campaigns ORDER BY campaign_id')
        # Verify DB itself enforces the same read-only transaction primitive.
        with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
            async with conn.transaction(isolation='repeatable_read', readonly=True):
                await conn.execute("UPDATE campaigns SET subject='forbidden'")
        # General stale timeout still terminalizes eligible ordinary work only.
        swept = await pm.timeout_stale_objectives(.01)
        assert {r['objective_id'] for r in swept} == {objectives['live'], objectives['ordinary']}
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await conn.close()
