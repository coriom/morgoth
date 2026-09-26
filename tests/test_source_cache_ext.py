
"""Phase-A extensions: 3 new cached sources + web_search cache path.



Locks:
  · get_coinbase_btc_stats, get_ethereum_network_stats, get_news are
    in SOURCE_CACHE_CONFIG with sane intervals and stale thresholds.
  · get_crypto_price stays LIVE.
  · web_search: normalise + recency bypass + TTL expiry + MISS falls
    through to live + list-payload round-trip for news.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import source_cache as sc

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("SOURCE_CACHE_ENABLED", "WEB_SEARCH_CACHE_TTL_SECS"):
        monkeypatch.delenv(k, raising=False)


class TestExtendedScope:
    def test_three_new_sources_in_scope(self):
        for s in ("get_coinbase_btc_stats", "get_ethereum_network_stats",
                   "get_news"):
            assert sc.is_cached_source(s), s

    def test_crypto_price_stays_live(self):
        assert not sc.is_cached_source("get_crypto_price")

    def test_new_intervals_have_room_below_stale(self):
        for s in ("get_coinbase_btc_stats", "get_ethereum_network_stats",
                   "get_news"):
            interval, stale = sc.SOURCE_CACHE_CONFIG[s]
            assert stale > 2 * interval, s

    def test_aggregate_req_per_day_still_negligible(self):
        # BEFORE ext: 246/day. Adding coinbase 96 + eth_network 288 +
        # news 72 = 456 additional → aggregate ~700/day = 29/h.
        # Still << smallest ceiling (BlockCypher 4800/day → margin >6×).
        total_per_day = sum(
            (24 * 3600) / interval
            for interval, _ in sc.SOURCE_CACHE_CONFIG.values()
        )
        assert 200 < total_per_day < 1000


@pytest.mark.asyncio
class TestNewsListPayload:
    """News tool returns a LIST — not a scalar digest. serve_from_cache
    must round-trip it as-is; the envelope metadata carries the age."""

    async def test_list_payload_round_trip(self):
        now = datetime.now(timezone.utc)
        payload = [
            {"title": "BTC bounces on Fed talk", "link": "http://x"},
            {"title": "Ethereum gas up 20%", "link": "http://y"},
        ]
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_news", "payload": payload,
            "observed_at": now - timedelta(minutes=10),
        })
        env = await sc.serve_from_cache(pm, "get_news")
        assert env["success"] is True
        assert env["result"] == payload
        assert env["result"][0]["title"] == "BTC bounces on Fed talk"
        assert env["metadata"]["from_cache"] is True

    async def test_list_payload_json_string_decoded(self):
        # asyncpg sometimes hands back JSONB as str.
        payload = [{"title": "x"}]
        pm = MagicMock()
        pm.latest_source_snapshot = AsyncMock(return_value={
            "source": "get_news", "payload": json.dumps(payload),
            "observed_at": datetime.now(timezone.utc),
        })
        env = await sc.serve_from_cache(pm, "get_news")
        assert env["result"] == payload


class TestQueryNormalisation:
    def test_lowercase_strip_collapse(self):
        assert sc.normalize_query("  Bitcoin  Fear   Greed  ") == "bitcoin fear greed"

    def test_no_stopword_removal(self):
        # Conservative — semantics of "the" / "a" preserved.
        assert sc.normalize_query("the bitcoin") == "the bitcoin"

    def test_non_string_returns_empty(self):
        assert sc.normalize_query(None) == ""  # type: ignore[arg-type]


class TestRecencyBypass:
    @pytest.mark.parametrize("q", [
        "latest bitcoin news",
        "current fear greed index",
        "today's crypto market",
        "breaking crypto news",
        "bitcoin now",
        "recent fed announcements",
        "tonight's macroeconomic outlook",
    ])
    def test_recency_words_bypass(self, q):
        assert sc.query_bypasses_cache(q) is True

    @pytest.mark.parametrize("q", [
        "bitcoin fear greed sentiment",
        "ethereum gas price history",
        "cpi and crypto",
        "fed funds rate impact on btc",
    ])
    def test_non_recency_queries_pass(self, q):
        assert sc.query_bypasses_cache(q) is False


@pytest.mark.asyncio
class TestServeWebSearch:
    async def test_hit_within_ttl_returns_envelope(self):
        payload = [{"title": "cached hit"}]
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(return_value={
            "results": payload,
            "observed_at": datetime.now(timezone.utc) - timedelta(minutes=30),
        })
        env = await sc.serve_web_search(pm, "bitcoin fear greed sentiment")
        assert env is not None
        assert env["result"] == payload
        md = env["metadata"]
        assert md["from_cache"] is True
        assert md["source"] == "web_search"
        assert md["normalized_query"] == "bitcoin fear greed sentiment"

    async def test_ttl_expiry_returns_none(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_CACHE_TTL_SECS", "1800")  # 30 min
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(return_value={
            "results": [{"title": "old"}],
            "observed_at": datetime.now(timezone.utc) - timedelta(hours=1),
        })
        assert await sc.serve_web_search(pm, "some query") is None

    async def test_recency_query_bypasses(self):
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(
            side_effect=AssertionError("must not read cache on recency query")
        )
        assert await sc.serve_web_search(pm, "latest bitcoin news") is None

    async def test_miss_returns_none(self):
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(return_value=None)
        assert await sc.serve_web_search(pm, "unheard-of query") is None

    async def test_kill_switch_short_circuits(self, monkeypatch):
        monkeypatch.setenv("SOURCE_CACHE_ENABLED", "false")
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(
            side_effect=AssertionError("must not read cache when disabled")
        )
        assert await sc.serve_web_search(pm, "anything") is None

    async def test_empty_query_returns_none(self):
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock()
        assert await sc.serve_web_search(pm, "") is None
        pm.latest_web_search_cache.assert_not_called()


@pytest.mark.asyncio
class TestRouterWebSearchHook:
    async def test_hit_short_circuits_live_call(self):
        from core.tool_router import ToolRouter
        payload = [{"title": "cached"}]
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(return_value={
            "results": payload,
            "observed_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        })
        pm.record_web_search_cache = AsyncMock()
        tool = MagicMock()
        tool.name = "web_search"
        tool.parameters = {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}
        tool.execute = AsyncMock(
            side_effect=AssertionError("live must not run on cache hit"),
        )
        r = ToolRouter(persistent_memory=pm)
        r.register(tool)
        env = await r.execute_tool("web_search", {"query": "bitcoin fear greed"})
        assert env["result"] == payload
        pm.record_web_search_cache.assert_not_called()

    async def test_miss_persists_after_live_call(self):
        from core.tool_router import ToolRouter
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(return_value=None)
        pm.record_web_search_cache = AsyncMock()
        tool = MagicMock()
        tool.name = "web_search"
        tool.parameters = {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}
        tool.execute = AsyncMock(
            return_value={"success": True, "result": [{"title": "live"}]},
        )
        r = ToolRouter(persistent_memory=pm)
        r.register(tool)
        env = await r.execute_tool("web_search", {"query": "eth gas price"})
        assert env["success"] is True
        pm.record_web_search_cache.assert_awaited_once()

    async def test_recency_query_does_not_persist(self):
        from core.tool_router import ToolRouter
        pm = MagicMock()
        pm.latest_web_search_cache = AsyncMock(return_value=None)
        pm.record_web_search_cache = AsyncMock()
        tool = MagicMock()
        tool.name = "web_search"
        tool.parameters = {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}
        tool.execute = AsyncMock(
            return_value={"success": True, "result": [{"title": "live"}]},
        )
        r = ToolRouter(persistent_memory=pm)
        r.register(tool)
        env = await r.execute_tool("web_search", {"query": "latest crypto news"})
        pm.record_web_search_cache.assert_not_called()
