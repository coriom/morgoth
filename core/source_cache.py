"""One collector, many agents — the source cache.

Today every tool call hits its API live. N agents mean N× the requests
and the free-tier ceilings (Owlracle 100/h, CoinGecko 429s) are what
block parallelism, not the GPU. Invert it: ONE collector polls each
in-scope slow-moving source at its own cadence into source_snapshots;
tool-call reads route through serve_from_cache and return the latest
snapshot annotated with its age. API request rate becomes independent
of the agent count.

Generalises the bb2aad7 metric recorder (which snapshots CoinPaprika's
three global fields into metric_series). The recorder stays untouched —
this cache is a parallel path storing FULL digests for other slow
sources.

Scope discipline: only sources where staleness is harmless (F&G moves
daily, on-chain difficulty updates every ~2 weeks, funding rates cycle
every 8h). Price data and news stay LIVE because their value depends
on being current-to-the-second.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any


def cache_enabled() -> bool:
    raw = os.environ.get("SOURCE_CACHE_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


# Per-source config: (poll_interval_secs, max_stale_secs).
# Cadences justify vs ceilings (all ≪ documented limits):
#   get_fear_greed_index      — updates daily; poll  6h = 4/day.
#     alternative.me: no documented cap, ~50/min tolerated → margin 12000×.
#   get_bitcoin_onchain       — hashrate/difficulty/fees move every ~10 min;
#     poll 10min = 144/day. mempool.space: 10 req/s = 864000/day → margin 6000×.
#   get_bitcoin_futures_funding — 8h funding windows; poll 30min = 48/day.
#     Binance /fapi: 2400 req/min = 3.4M/day → margin 70000×.
#   get_bitcoin_long_short_ratio — same feed as funding; poll 30min = 48/day.
#   fred_series_observations   — monthly data; poll 12h = 2/day.
#     FRED: 120 req/min = 172800/day → margin 86000×.
# TOTAL in-scope: 4 + 144 + 48 + 48 + 2 = ~246 req/day = 10.25 req/hr,
# independent of the number of agents reading. Live sources per agent
# per cycle continue on top.
# 2026-09-30 chantier-1: SOURCE_CACHE_CONFIG + SOURCE_DEFAULT_ARGS
# sourced from the active domain pack (domains/<name>/domain.yaml).
# The per-tool cadence rationale (rate-limit margins vs upstream
# ceilings) lives in the YAML as comments alongside each entry.
from core.domain import current_domain as _current_domain  # noqa: E402
from core.tool_rail import ToolRailError, effective_tool_rail
_ACTIVE_RAIL = effective_tool_rail(_current_domain())
SOURCE_CACHE_CONFIG: dict[str, tuple[int, int]] = dict(
    _current_domain().source_cache_config,
)
SOURCE_DEFAULT_ARGS: dict[str, dict[str, Any]] = dict(
    _current_domain().source_cache_default_args,
)


def is_cached_source(name: str) -> bool:
    return _ACTIVE_RAIL.is_allowed(name) and name in SOURCE_CACHE_CONFIG


def max_stale_secs(name: str) -> int:
    return SOURCE_CACHE_CONFIG.get(name, (0, 0))[1]


def poll_interval_secs(name: str) -> int:
    return SOURCE_CACHE_CONFIG.get(name, (0, 0))[0]


# ────────────────────────────────────────────────────────────────────
# WEB-SEARCH CACHE — query-keyed, TTL-bounded, recency-bypassed.
#
# 85+ near-duplicate queries in the corpus; the same "bitcoin fear
# greed sentiment" fires four times an hour. Normalise the query,
# store the results list, serve within TTL. Recency-word queries
# (today, latest, now, current, recent, breaking, tonight) bypass the
# cache — a "latest news" query MUST hit the live search.
# ────────────────────────────────────────────────────────────────────

def web_search_ttl_secs() -> int:
    raw = os.environ.get("WEB_SEARCH_CACHE_TTL_SECS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 60:
                return v
        except ValueError:
            pass
    return 60 * 60  # 1 h default


_RECENCY_WORDS: frozenset[str] = frozenset({
    "today", "latest", "now", "current", "recent", "breaking",
    "tonight", "yesterday", "this hour", "past hour", "past 24h",
    "this morning", "this afternoon", "just now",
})


def normalize_query(query: str) -> str:
    """Lowercase, strip, collapse internal whitespace. Intentionally
    conservative — no stopword removal (would change semantics of
    'apple stock' ↔ 'the apple stock')."""
    if not isinstance(query, str):
        return ""
    return " ".join(query.strip().lower().split())


def query_bypasses_cache(query: str) -> bool:
    """True iff the query contains a recency word — a live hit is
    mandatory ("latest news", "current bitcoin price ...")."""
    if not query:
        return False
    q = normalize_query(query)
    return any(w in q for w in _RECENCY_WORDS)


async def record_web_search_hit(
    persistent_memory, query: str, results: Any,
) -> None:
    """Persist one query→results pair for future cache hits.
    Skipped when the query bypasses the cache (recency word)."""
    if query_bypasses_cache(query):
        return
    nq = normalize_query(query)
    if not nq:
        return
    await persistent_memory.record_web_search_cache(nq, results)


async def serve_web_search(
    persistent_memory, query: str,
) -> dict[str, Any] | None:
    """Return a tool envelope from web_search_cache, or None on miss /
    TTL expiry / recency bypass. Same envelope shape as serve_from_cache
    so downstream code doesn't branch on source."""
    if not cache_enabled():
        return None
    if query_bypasses_cache(query):
        return None
    nq = normalize_query(query)
    if not nq:
        return None
    row = await persistent_memory.latest_web_search_cache(nq)
    if not row:
        return None
    observed_at = row["observed_at"]
    age = max(0, int((datetime.now(timezone.utc) - observed_at).total_seconds()))
    ttl = web_search_ttl_secs()
    if age > ttl:
        return None  # TTL expiry → fall through to live search
    results = row["results"]
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except Exception:
            pass
    return {
        "success": True,
        "result": results,
        "error": None,
        "metadata": {
            "from_cache": True,
            "observed_at": observed_at.isoformat(),
            "age_seconds": age,
            "stale": False,  # TTL enforcement is at read; served ⇒ fresh
            "source": "web_search",
            "normalized_query": nq,
        },
    }


async def collect_one(persistent_memory, tool_router, source: str) -> bool:
    """Poll one source and write its payload to source_snapshots.
    Returns True on success. Non-fatal — every error is caught."""
    from loguru import logger
    args = SOURCE_DEFAULT_ARGS.get(source, {})
    try:
        # bypass_cache=True prevents the collector from reading its own
        # cache — must hit the live API to WRITE a fresh snapshot.
        result = await tool_router.execute_tool(source, args, bypass_cache=True)
    except Exception as exc:
        logger.warning("source_cache: {} raised {}: {}", source, type(exc).__name__, exc)
        return False
    if not isinstance(result, dict) or not result.get("success"):
        return False
    payload = result.get("result")
    if payload is None:
        return False
    try:
        await persistent_memory.record_source_snapshot(
            source, payload, datetime.now(timezone.utc),
        )
        return True
    except Exception as exc:
        logger.warning("source_cache: record_source_snapshot({}) failed: {}", source, exc)
        return False


async def serve_from_cache(
    persistent_memory, source: str,
) -> dict[str, Any] | None:
    """Return a tool-shaped result envelope from the newest snapshot,
    or None if no snapshot exists. Age surfacing in the envelope:
      · observed_at (ISO string), age_seconds (int), stale (bool,
        True when age > max_stale_secs), from_cache=True.
    The stale flag is INFORMATIONAL — the value is still returned so
    the numeric-fidelity gate and the thesis prompt can judge. Never
    silently serves ancient data as current."""
    row = await persistent_memory.latest_source_snapshot(source)
    if not row:
        return None
    observed_at: datetime = row["observed_at"]
    age = max(0, int((datetime.now(timezone.utc) - observed_at).total_seconds()))
    stale = age > max_stale_secs(source)
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            pass
    return {
        "success": True,
        "result": payload,
        "error": None,
        "metadata": {
            "from_cache": True,
            "observed_at": observed_at.isoformat(),
            "age_seconds": age,
            "stale": stale,
            "source": source,
        },
    }


class CollectorState:
    """Per-source last-poll timestamps for the loop scheduler."""

    __slots__ = ("_last",)

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def due(self, source: str, now_ts: float | None = None) -> bool:
        now_ts = now_ts if now_ts is not None else time.monotonic()
        # First poll (source not yet marked) is ALWAYS due — this is
        # what makes the cache populate on the first cycle after
        # startup instead of waiting a full interval.
        if source not in self._last:
            return True
        return (now_ts - self._last[source]) >= poll_interval_secs(source)

    def mark(self, source: str, now_ts: float | None = None) -> None:
        self._last[source] = now_ts if now_ts is not None else time.monotonic()


async def collect_due_sources(
    persistent_memory, tool_router, state: CollectorState,
) -> list[str]:
    """Iterate SOURCE_CACHE_CONFIG, poll each due source. Returns the
    list of sources successfully collected this round. One dead source
    NEVER stops the others (per-source try/except in collect_one)."""
    if not cache_enabled():
        return []
    collected: list[str] = []
    for source in SOURCE_CACHE_CONFIG:
        if not _ACTIVE_RAIL.is_allowed(source):
            raise ToolRailError(f"inactive cache collector: {source}")
        if not state.due(source):
            continue
        ok = await collect_one(persistent_memory, tool_router, source)
        state.mark(source)
        if ok:
            collected.append(source)
    return collected
