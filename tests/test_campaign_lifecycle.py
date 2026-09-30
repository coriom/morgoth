"""Canonical eligibility guard: preserve recovery for live/unbound objectives."""
import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.campaign_lifecycle import CAMPAIGN_LIVE_SQL, OBJECTIVE_RESEARCH_SQL
from memory.persistent import PersistentMemory


class Checkout:
    def __init__(self, connection): self.connection = connection
    async def __aenter__(self): return self.connection
    async def __aexit__(self, *args): return False


@pytest.mark.parametrize('method,args', [('claim_next_objective', ()), ('reclaim_orphan_objectives', (30,)), ('timeout_stale_objectives', (7,))])
@pytest.mark.asyncio
async def test_all_recovery_selectors_use_one_guard(method, args):
    connection = MagicMock(); connection.fetch = AsyncMock(return_value=[])
    connection.transaction.return_value = Checkout(connection)
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = MagicMock(); pm._pool.acquire.return_value = Checkout(connection)
    await getattr(pm, method)(*args)
    sql = connection.fetch.await_args.args[0]
    assert OBJECTIVE_RESEARCH_SQL in sql
    assert CAMPAIGN_LIVE_SQL in sql
    assert 'public.' not in sql
    assert 'campaign_id IS NULL OR EXISTS' in sql
    if method == 'claim_next_objective': assert 'FOR UPDATE SKIP LOCKED' in sql
    else: assert 'AND ' + OBJECTIVE_RESEARCH_SQL in sql


@pytest.mark.asyncio
async def test_generation_and_expiration_share_live_definition():
    connection = MagicMock(); connection.fetchrow = AsyncMock(return_value=None)
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = MagicMock(); pm._pool.acquire.return_value = Checkout(connection)
    await pm.get_active_campaign()
    assert CAMPAIGN_LIVE_SQL in connection.fetchrow.await_args.args[0]
    await pm.expire_active_campaign_if_due()
    assert f'NOT ({CAMPAIGN_LIVE_SQL})' in connection.fetchrow.await_args.args[0]


def test_startup_order_proves_guard_needed_even_before_expiry():
    from core.brain import Brain
    initialize = inspect.getsource(Brain.initialize)
    cycle = inspect.getsource(Brain.run_autonomous_cycle)
    assert initialize.index('timeout_stale_objectives') < initialize.index('reclaim_orphan_objectives') < initialize.index('create_task')
    assert cycle.index('expire_active_campaign_if_due') < cycle.index('claim_next_objective')
    # Real SQL scenarios exercise this exact worst-case ordering in the dedicated
    # test DB. Neither recovery timeout nor env override is changed here.
    assert 'ORPHAN_RECLAIM_MINUTES' not in inspect.getsource(__import__('core.campaign_lifecycle', fromlist=['']))
