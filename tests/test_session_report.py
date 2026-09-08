"""Session-report aggregator + rate-limit inventory + abstention accounting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from analysis import session_report as SR


class _AsyncCtx:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


def _fake_pool(fetch_map):
    """fetch_map: {sql_substring: rows-list} matched by presence in first arg."""
    conn = MagicMock()
    async def _fetch(sql, *args):
        for sub, rows in fetch_map.items():
            if sub in sql:
                return rows
        return []
    conn.fetch = AsyncMock(side_effect=_fetch)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    return pool


def _fake_pm(fetch_map):
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=_fake_pool(fetch_map))
    return pm


@pytest.mark.asyncio
async def test_collect_assembles_all_sections_on_mocked_data():
    since = datetime.now(tz=timezone.utc) - timedelta(hours=2)
    fetch_map = {
        "FROM objectives": [{"status": "done", "n": 3}, {"status": "pending", "n": 1}],
        "FROM theses": [{"n": 12}],
        "FROM abstention_events": [{"n": 4}],
        "FROM contradictions": [{"n": 1}],
        "FROM rate_limit_events": [{"tool_name": "get_ethereum_network_stats", "n": 2}],
        "FROM llm_calls": [
            {"task": "thesis", "provider": "ollama", "n": 8, "med": 5200.0},
        ],
        "FROM self_modify_proposals WHERE status": [{"n": 2}],
        "FROM self_modify_proposals WHERE proposed_by": [{"n": 8}],
    }
    pm = _fake_pm(fetch_map)
    r = await SR.collect(pm, since, full=False)
    assert r.objectives_completed == 3
    assert r.theses_total == 12
    assert r.abstentions == 4
    # rough abstention rate: 4 abstentions / 3 completions (extraction attempts proxy)
    assert 0.5 < r.abstention_rate <= 2.0
    assert r.contradictions_new == 1
    assert r.rate_limit_warnings == [("get_ethereum_network_stats", 2)]
    assert r.proposals_pending == 2
    assert r.llm_by_task_provider[0]["provider"] == "ollama"
    assert "auto-approve decisions (need >= 30)" in r.pending_measurements


@pytest.mark.asyncio
async def test_collect_tolerates_missing_tables():
    """Report must render even if the abstention/rate_limit tables don't
    exist yet — the read paths catch the exception and default to zero."""
    since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    conn = MagicMock()

    async def _fetch(sql, *args):
        # abstention/rate_limit/contradictions raise; others return empty
        if "abstention_events" in sql or "rate_limit_events" in sql \
                or "contradictions" in sql or "llm_calls" in sql \
                or "self_modify_proposals" in sql:
            raise RuntimeError("table missing")
        return []
    conn.fetch = AsyncMock(side_effect=_fetch)
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = MagicMock(); pm._require_pool = MagicMock(return_value=pool)
    r = await SR.collect(pm, since, full=False)
    assert r.abstentions == 0
    assert r.contradictions_new == 0
    assert r.rate_limit_warnings == []
    assert r.llm_by_task_provider == []


@pytest.mark.asyncio
async def test_render_handles_zero_data_gracefully():
    since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    r = SR.SessionReport(since=since, now=datetime.now(tz=timezone.utc))
    out = r.render()
    assert "session report" in out.lower()
    assert "CYCLES completed" in out
    assert "RATE-LIMIT WARNINGS      : none" in out


@pytest.mark.asyncio
async def test_render_prints_rate_limit_warnings_when_present():
    since = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    r = SR.SessionReport(since=since, now=datetime.now(tz=timezone.utc))
    r.rate_limit_warnings = [("coingecko", 5), ("owlracle", 1)]
    out = r.render()
    assert "coingecko: 5 hits" in out
    assert "owlracle: 1 hits" in out


def test_module_docstring_carries_rate_limit_inventory():
    """The rate-limit inventory + SAFE FLOOR justification lives in the
    module docstring — grep-lock so a future refactor can't strip it."""
    import inspect
    src = inspect.getsource(SR)
    assert "CoinGecko" in src
    assert "BlockCypher" in src
    assert "Owlracle" in src
    assert "SAFE FLOOR" in src
    # Explicit floor value called out.
    assert "N >= 1.2 min" in src or "SAFE FLOOR\n= 2 min" in src or "SAFE FLOOR = 2 min" in src or "SAFE FLOOR" in src and "2 min" in src


def test_since_parse_reuses_auto_approve_helper():
    """The session-report CLI uses parse_since from auto_approve.
    Grep-lock: a future edit that duplicates the parser breaks the check."""
    from pathlib import Path
    src = Path("self_modify/cli.py").read_text()
    assert "from self_modify.auto_approve import parse_since" in src
