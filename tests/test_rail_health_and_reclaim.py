"""Rail-health classifier + orphan reclaim + env.example completeness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from analysis import rail_health as RH


# ═════════════════════════════════════════════════════════════════════
# RAIL HEALTH classifier — pure logic, no I/O
# ═════════════════════════════════════════════════════════════════════


class TestRailHealthClassifier:
    def test_ok_when_success_and_all_digest_fields_present(self):
        result = {"success": True, "result": {"symbol": "btc", "price": 62000,
                                              "change_24h": 1.2, "volume_24h": 3e10}}
        r = RH.classify("get_crypto_price", result,
                        declared_digest_fields=("symbol", "price", "change_24h", "volume_24h"),
                        prior_digest=None, latency_ms=120)
        assert r.status == "OK"
        assert r.digest  # non-empty
        assert r.digest_fields_missing == []

    def test_degraded_when_declared_field_null(self):
        result = {"success": True, "result": {"symbol": "btc", "price": 62000,
                                              "change_24h": None, "volume_24h": 3e10}}
        r = RH.classify("get_crypto_price", result,
                        declared_digest_fields=("symbol", "price", "change_24h", "volume_24h"),
                        prior_digest=None)
        assert r.status == "DEGRADED"
        assert "change_24h" in r.digest_fields_missing

    def test_frozen_when_digest_matches_prior_and_no_missing(self):
        result = {"success": True, "result": {"a": 1, "b": 2}}
        first = RH.classify("t", result, ("a", "b"), prior_digest=None)
        assert first.status == "OK"
        second = RH.classify("t", result, ("a", "b"), prior_digest=first.digest)
        assert second.status == "FROZEN"
        assert second.digest == first.digest

    def test_frozen_beats_degraded_when_digest_matches_but_field_null(self):
        """Order of precedence: FROZEN wins over DEGRADED only if the
        digest matches AND every declared field is present. If a field
        is null the payload is still DEGRADED (the data-shape signal
        is more actionable than 'nothing changed since last run')."""
        result = {"success": True, "result": {"a": 1, "b": None}}
        prior_digest = RH.digest_of_result(result)
        r = RH.classify("t", result, ("a", "b"), prior_digest=prior_digest)
        assert r.status == "DEGRADED"

    def test_dead_on_exception(self):
        r = RH.classify("t", RuntimeError("timeout"), ("a",), prior_digest=None)
        assert r.status == "DEAD"
        assert "RuntimeError" in r.detail

    def test_dead_on_success_false(self):
        result = {"success": False, "error": "rate-limited 429"}
        r = RH.classify("t", result, ("a",), prior_digest=None)
        assert r.status == "DEAD"
        assert "429" in r.detail

    def test_dead_on_non_dict_result(self):
        r = RH.classify("t", "hello", ("a",), prior_digest=None)  # type: ignore[arg-type]
        assert r.status == "DEAD"

    def test_digest_is_deterministic_and_ignores_key_order(self):
        d1 = RH.digest_of_result({"success": True, "result": {"a": 1, "b": 2}})
        d2 = RH.digest_of_result({"success": True, "result": {"b": 2, "a": 1}})
        assert d1 == d2 and d1

    def test_one_line_summary_format(self):
        results = [
            RH.RailResult("t1", "OK", "d1"),
            RH.RailResult("t2", "OK", "d2"),
            RH.RailResult("t3", "FROZEN", "d3", detail="static"),
        ]
        s = RH.one_line_summary(results)
        assert "2 OK" in s and "1 FROZEN" in s
        assert "t3=FROZEN" in s

    def test_default_inter_tool_delay_respects_owlracle(self):
        """Grep-lock: the inter-tool spacing MUST be ≥ 6 s so a full sweep
        of 11 tools takes ≥ 60 s. That keeps Owlracle (100 req/hr = one
        every 36 s) safely covered by exactly one request per sweep."""
        assert RH.DEFAULT_INTER_TOOL_DELAY_SECS >= 6.0


# ═════════════════════════════════════════════════════════════════════
# ORPHAN reclaim — persistent-memory unit tests
# ═════════════════════════════════════════════════════════════════════


class _AsyncCtx:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


@pytest.mark.asyncio
async def test_reclaim_query_filters_only_in_progress_and_stale():
    """The RETURNING contract: query MUST select on status='in_progress'
    AND updated_at < NOW() - $1::int * INTERVAL '1 minute'. Grep-lock on
    the SQL guarantees a future edit can't quietly widen the scope to
    done/failed rows."""
    from memory.persistent import PersistentMemory
    src = Path("memory/persistent.py").read_text()
    assert "status = 'in_progress'" in src
    assert "updated_at < NOW() - ($1::int * INTERVAL '1 minute')" in src
    # RETURNING columns include the ones the caller logs.
    assert "RETURNING objective_id, title, cycle_count, updated_at" in src


@pytest.mark.asyncio
async def test_reclaim_excludes_current_objective_id_when_provided():
    """Belt-and-braces: even if the active objective's updated_at somehow
    drifts backward (clock skew, missed update), the current_objective_id
    exclusion prevents reclaiming what the process is actively cycling."""
    from memory.persistent import PersistentMemory
    from core.config import AppConfig

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = pool

    await pm.reclaim_orphan_objectives(
        30, current_objective_id="11111111-2222-3333-4444-555555555555",
    )
    sql, *args = conn.fetch.call_args[0]
    assert "AND objective_id != $2" in sql
    assert args[0] == 30  # threshold
    # UUID was parsed and passed as $2.
    assert str(args[1]) == "11111111-2222-3333-4444-555555555555"


@pytest.mark.asyncio
async def test_reclaim_omits_exclusion_when_no_active_objective():
    """Startup path: no active objective → exclusion clause absent so
    the query still runs across all in_progress rows."""
    from memory.persistent import PersistentMemory

    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = pool

    await pm.reclaim_orphan_objectives(30, current_objective_id=None)
    sql, *args = conn.fetch.call_args[0]
    assert "AND objective_id != $2" not in sql
    assert args == [30]


@pytest.mark.asyncio
async def test_increment_cycle_count_bumps_updated_at():
    """Grep-lock: the SQL for increment_cycle_count MUST bump updated_at
    so the reclaim path sees the row as active."""
    src = Path("memory/persistent.py").read_text()
    assert "cycle_count = cycle_count + 1, " in src
    assert "updated_at = NOW() WHERE objective_id = $1" in src


def test_env_example_lists_every_touched_var():
    """The .env.example must enumerate every env var the runtime reads.
    Add-a-var-forget-to-document is the classic drift — grep-lock the ones
    the codebase actively consumes."""
    example = Path(".env.example").read_text()
    required = (
        "POSTGRES_URL", "OLLAMA_HOST", "OLLAMA_PRIMARY_MODEL",
        "AUTONOMOUS_CYCLE_MINUTES", "MAX_CYCLES_PER_OBJECTIVE",
        "FRED_API_KEY", "SHADOW_DELEGATION", "AUTO_APPROVE_ENABLED",
        "TRACK_RECORD_ENABLED", "ORPHAN_RECLAIM_MINUTES",
        "REFLECT_LLM_TIMEOUT_SECONDS", "ANTHROPIC_API_KEY",
        "MORGOTH_LLM_THESIS", "THESIS_GENERATOR",
        "NEXT_PUBLIC_LLM_TIMEOUT_SECONDS", "LOG_RETENTION_DAYS",
        "SECRET_KEY",
    )
    for name in required:
        assert name in example, f".env.example missing {name!r}"


def test_gitignore_still_excludes_env_but_not_env_example():
    """.env stays ignored (secret values). .env.example is committed."""
    gi = Path(".gitignore").read_text()
    assert ".env\n" in gi or ".env" in gi.splitlines()
    # And example is NOT ignored — a stray '.env*' pattern would ignore both.
    assert ".env.example" not in gi or gi.count(".env.example") == 0 or "!.env.example" in gi


def test_log_retention_days_wired_into_main():
    """LOG_RETENTION_DAYS was declared in config but never consumed.
    Grep-lock the wiring in main.py so a future refactor can't drop it."""
    src = Path("main.py").read_text()
    assert "LOG_RETENTION_DAYS" in src
    assert 'retention=f"{retention_days} days"' in src
