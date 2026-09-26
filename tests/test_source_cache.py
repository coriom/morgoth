


"""Source cache: config, scheduler, collector, read path with age
surfacing, staleness policy, kill-switch, req/h arithmetic lock."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import source_cache as sc

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("SOURCE_CACHE_ENABLED", raising=False)


class TestConfig:
    def test_scope_lists_slow_moving_sources(self):
        # In-scope (d647524 + phase-A extension): 5 originals + 3 new.
        assert set(sc.SOURCE_CACHE_CONFIG.keys()) == {
            "get_fear_greed_index",
            "get_bitcoin_onchain",
            "get_bitcoin_futures_funding",
            "get_bitcoin_long_short_ratio",
            "fred_series_observations",
            "get_coinbase_btc_stats",
            "get_ethereum_network_stats",
            "get_news",
        }

    def test_live_sources_not_cached(self):
        # get_crypto_price stays LIVE (per-second value).
        # web_search has its own query-keyed cache path — not in
        # SOURCE_CACHE_CONFIG (which is source-name-keyed).
        for name in ("get_crypto_price", "web_search"):
            assert not sc.is_cached_source(name)

    def test_all_intervals_have_room_below_stale_threshold(self):
        # max_stale must be at least ~2× the poll interval so a single
        # missed poll doesn't flip everything to stale=True.
        for name, (interval, stale) in sc.SOURCE_CACHE_CONFIG.items():
            assert stale > 2 * interval, name

    def test_total_req_per_hour_stays_under_ceilings(self):
        """Ceiling arithmetic lock — 246/day, ~10.25/h aggregate,
        independent of agent count. Locked so a rate change here
        triggers a review."""
        total_per_day = sum(
            (24 * 3600) / interval
            for interval, _ in sc.SOURCE_CACHE_CONFIG.values()
        )
        # Documented margins (see SOURCE_CACHE_CONFIG comments):
        #   alternative.me ≥ 72000/day, mempool.space 864000/day,
        #   Binance /fapi 3.4M/day, FRED 172800/day.
        # Aggregate MUST fit under the tightest sub-scope:
        # F&G  = 4/day     — margin > 12000×
        # onchain = 144/day — margin > 6000×
        # funding = 48/day  — margin > 70000×
        # long/short = 48/day
        # FRED = 2/day      — margin > 86000×
        # Post-extension: 5 originals + 3 new = ~702/day.
        # BlockCypher is the tightest ceiling at 4800/day → margin >6×.
        assert total_per_day < 1000, f"aggregate {total_per_day}/day too high"
        assert total_per_day > 200, "aggregate looks suspiciously low"


class TestSchedulerState:
    def test_first_poll_is_due(self):
        s = sc.CollectorState()
        assert s.due("get_fear_greed_index", now_ts=1000.0) is True

    def test_not_due_within_interval(self):
        s = sc.CollectorState()
        s.mark("get_bitcoin_onchain", now_ts=1000.0)
        # 10 min = 600 s → still under at 599.
        assert s.due("get_bitcoin_onchain", now_ts=1000.0 + 599) is False

    def test_due_after_interval(self):
        s = sc.CollectorState()
        s.mark("get_bitcoin_onchain", now_ts=1000.0)
        assert s.due("get_bitcoin_onchain", now_ts=1000.0 + 601) is True


class TestKillSwitch:
    def test_cache_enabled_default(self):
        assert sc.cache_enabled() is True

    @pytest.mark.parametrize("v", ["false", "0", "no", "off"])
    def test_cache_disabled_by_env(self, monkeypatch, v):
        monkeypatch.setenv("SOURCE_CACHE_ENABLED", v)
        assert sc.cache_enabled() is False


@pytest.mark.asyncio
class TestCollectOne:
    async def _pm(self):
        pm = MagicMock()
        pm.record_source_snapshot = AsyncMock()
        return pm

    async def test_success_writes_snapshot(self):
        router = MagicMock()
        router.execute_tool = AsyncMock(return_value={
            "success": True, "result": {"value": 42, "class": "Greed"},
        })
        pm = await self._pm()
        ok = await sc.collect_one(pm, router, "get_fear_greed_index")
        assert ok is True
        pm.record_source_snapshot.assert_awaited_once()
        # bypass_cache=True must be passed so the collector reaches the
        # LIVE tool (chicken/egg guard).
        router.execute_tool.assert_awaited_once()
        assert router.execute_tool.await_args.kwargs.get("bypass_cache") is True

    async def test_tool_failure_is_non_fatal(self):
        router = MagicMock()
        router.execute_tool = AsyncMock(return_value={"success": False, "error": "x"})
        pm = await self._pm()
        assert await sc.collect_one(pm, router, "get_fear_greed_index") is False
        pm.record_source_snapshot.assert_not_called()

    async def test_tool_raises_is_non_fatal(self):
        router = MagicMock()
        router.execute_tool = AsyncMock(side_effect=RuntimeError("boom"))
        pm = await self._pm()
        assert await sc.collect_one(pm, router, "get_fear_greed_index") is False


@pytest.mark.asyncio
class TestCollectDueSources:
    async def test_one_dead_source_does_not_block_others(self):
        router = MagicMock()
        # F&G raises; every other source returns success. Collector
        # must move past the raise and still write the survivors.
        def _exec_sync(*args, **kwargs):
            name = args[0]
            if name == "get_fear_greed_index":
                raise RuntimeError("dead")
            return {"success": True, "result": {name: "ok"}}
        router.execute_tool = AsyncMock(side_effect=_exec_sync)
        pm = MagicMock(); pm.record_source_snapshot = AsyncMock()
        state = sc.CollectorState()  # all due
        collected = await sc.collect_due_sources(pm, router, state)
        # Every source in scope was polled; F&G failed, others succeeded.
        assert "get_fear_greed_index" not in collected
        # All four surviving sources land in the collected list.
        assert "get_bitcoin_onchain" in collected
        assert "get_bitcoin_futures_funding" in collected
        assert "get_bitcoin_long_short_ratio" in collected
        assert "fred_series_observations" in collected

    async def test_kill_switch_short_circuits(self, monkeypatch):
        monkeypatch.setenv("SOURCE_CACHE_ENABLED", "false")
        router = MagicMock(); pm = MagicMock()
        router.execute_tool = AsyncMock(
            side_effect=AssertionError("must not be called when disabled")
        )
        state = sc.CollectorState()
        assert await sc.collect_due_sources(pm, router, state) == []


@pytest.mark.asyncio
class TestServeFromCache:
    async def test_serves_stored_payload_with_age(self):
        now = datetime.now(timezone.utc)
        observed = now - timedelta(minutes=5)
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_fear_greed_index",
            "payload": {"value": 63, "value_classification": "Greed"},
            "observed_at": observed,
        })
        env = await sc.serve_from_cache(pm, "get_fear_greed_index")
        assert env is not None
        assert env["success"] is True
        assert env["result"] == {"value": 63, "value_classification": "Greed"}
        md = env["metadata"]
        assert md["from_cache"] is True
        assert md["source"] == "get_fear_greed_index"
        assert md["age_seconds"] == pytest.approx(300, abs=5)
        # F&G max_stale is 26h → 5 min ≪ threshold → not stale.
        assert md["stale"] is False
        assert md["observed_at"] == observed.isoformat()

    async def test_stale_flag_flips_past_threshold(self):
        # Funding max_stale is 2h. A 3h-old snapshot must be stale.
        now = datetime.now(timezone.utc)
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_bitcoin_futures_funding",
            "payload": {"lastFundingRate": "0.0001"},
            "observed_at": now - timedelta(hours=3),
        })
        env = await sc.serve_from_cache(pm, "get_bitcoin_futures_funding")
        assert env["metadata"]["stale"] is True
        # But the value IS still returned — never silently withheld.
        assert env["result"] == {"lastFundingRate": "0.0001"}

    async def test_no_snapshot_returns_none(self):
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value=None)
        assert await sc.serve_from_cache(pm, "get_fear_greed_index") is None

    async def test_json_string_payload_parsed(self):
        # asyncpg sometimes returns JSONB as str — verify decode.
        now = datetime.now(timezone.utc)
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_fear_greed_index",
            "payload": json.dumps({"value": 63}),
            "observed_at": now,
        })
        env = await sc.serve_from_cache(pm, "get_fear_greed_index")
        assert env["result"] == {"value": 63}


@pytest.mark.asyncio
class TestRouterInterception:
    async def test_execute_tool_serves_from_cache_when_available(self):
        from core.tool_router import ToolRouter
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_fear_greed_index",
            "payload": {"value": 63},
            "observed_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        })
        tool = MagicMock(); tool.execute = AsyncMock(
            side_effect=AssertionError("live tool must not be called when cache hits"),
        )
        r = ToolRouter(persistent_memory=pm)
        r._tools["get_fear_greed_index"] = tool
        env = await r.execute_tool("get_fear_greed_index", {})
        assert env["metadata"]["from_cache"] is True
        assert env["result"] == {"value": 63}

    async def test_bypass_cache_kwarg_forces_live_call(self):
        from core.tool_router import ToolRouter
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_fear_greed_index",
            "payload": {"value": 63},
            "observed_at": datetime.now(timezone.utc),
        })
        tool = MagicMock()
        tool.execute = AsyncMock(return_value={"success": True, "result": {"live": True}})
        r = ToolRouter(persistent_memory=pm)
        r._tools["get_fear_greed_index"] = tool
        env = await r.execute_tool("get_fear_greed_index", {}, bypass_cache=True)
        assert env["result"] == {"live": True}

    async def test_non_cached_source_hits_live_tool(self):
        from core.tool_router import ToolRouter
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(
            side_effect=AssertionError("must not read cache for live-scope sources"),
        )
        tool = MagicMock()
        tool.execute = AsyncMock(return_value={"success": True, "result": {"live": True}})
        r = ToolRouter(persistent_memory=pm)
        r._tools["get_crypto_price"] = tool
        env = await r.execute_tool("get_crypto_price", {"symbol": "btc"})
        assert env["result"] == {"live": True}

    async def test_no_pm_means_no_cache_lookup(self):
        # Backward compat: existing code paths constructing ToolRouter()
        # without persistent_memory must behave exactly as before.
        from core.tool_router import ToolRouter
        tool = MagicMock()
        tool.execute = AsyncMock(return_value={"success": True, "result": "ok"})
        r = ToolRouter()
        r._tools["get_fear_greed_index"] = tool
        env = await r.execute_tool("get_fear_greed_index", {})
        assert env["result"] == "ok"


class TestBrainWiresCollector:
    def test_brain_imports_and_calls_collector(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "from core.source_cache import" in src
        assert "collect_due_sources" in src
        # Gated on connectivity + kill-switch.
        assert "_sc_enabled()" in src
        assert "_connectivity.is_online" in src
