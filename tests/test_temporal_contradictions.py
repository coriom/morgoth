"""Temporal-semantics tests for the contradiction detector.

Cover the three-way branch introduced to make the detector aware that
opposing readings of a rolling metric on different days are REVISIONS,
not contradictions, and that subjects with mismatched timeframe
qualifiers are non-comparable.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import brain as brain_mod
from core.contradictions import (
    CONTRADICTION_WINDOW_HOURS,
    subjects_timeframe_conflict,
)


# ---------- timeframe guard ------------------------------------------------

def test_timeframe_guard_matrix() -> None:
    """short vs long → conflict; short vs short / long vs long / neither → not."""
    # Conflict (blocks pairing)
    assert subjects_timeframe_conflict(
        "BTC long-term price trends", "Bitcoin's short-term price trend"
    )
    assert subjects_timeframe_conflict(
        "24-hour change rate of BTC price", "monthly BTC returns"
    )
    # No conflict — same-side or timeframe-free
    assert not subjects_timeframe_conflict(
        "BTC short-term price", "BTC 24-hour price change"
    )  # both short
    assert not subjects_timeframe_conflict(
        "BTC long-term price trends", "weekly returns"
    )  # both long
    assert not subjects_timeframe_conflict("BTC price", "Bitcoin price")  # neither


def test_timeframe_guard_non_string_returns_false() -> None:
    """Defensive: don't raise on None/int subjects — treat as no conflict."""
    assert subjects_timeframe_conflict(None, "short-term x") is False  # type: ignore[arg-type]
    assert subjects_timeframe_conflict(42, "long-term x") is False  # type: ignore[arg-type]


# ---------- detector helpers -----------------------------------------------

def _thesis(
    subject: str,
    claim: str,
    created_at: datetime,
    status: str = "active",
    thesis_id: str | None = None,
) -> dict[str, Any]:
    return {
        "thesis_id": thesis_id or str(uuid.uuid4()),
        "subject": subject,
        "claim": claim,
        "status": status,
        "confidence": "medium",
        "evidence": [{"source": "get_crypto_price", "detail": "test"}],
        "created_at": created_at,
    }


def _make_brain(active_theses: list[dict[str, Any]]) -> tuple[Any, MagicMock]:
    """Build a Brain stub whose only wiring is what detect_contradictions uses.

    Returns (brain, pm_mock) — the pm_mock exposes:
      - record_contradiction / update_thesis_status / mark_thesis_superseded
    for assertions.
    """
    pm = MagicMock()
    pm.get_theses = AsyncMock(return_value=active_theses)
    pm.record_contradiction = AsyncMock(return_value=str(uuid.uuid4()))
    pm.update_thesis_status = AsyncMock(return_value=True)
    pm.mark_thesis_superseded = AsyncMock(return_value=True)

    # Real Brain has many collaborators; only wire what detect_contradictions
    # touches. Fresh instance without going through __init__.
    brain = brain_mod.Brain.__new__(brain_mod.Brain)
    brain._persistent_memory = pm
    brain._feed_append = MagicMock()
    return brain, pm


# ---------- boundary tests -------------------------------------------------

@pytest.mark.asyncio
async def test_gap_below_window_records_contradiction() -> None:
    """5.9h gap → contradiction path (both flipped, row recorded)."""
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    ta = _thesis("BTC short-term price", "declining", now)
    tb = _thesis("BTC short-term price", "increasing", now - timedelta(hours=5, minutes=54))
    brain, pm = _make_brain([ta, tb])

    found = await brain.detect_contradictions()
    assert len(found) == 1
    pm.record_contradiction.assert_awaited_once()
    assert pm.update_thesis_status.await_count == 2
    pm.mark_thesis_superseded.assert_not_awaited()


@pytest.mark.asyncio
async def test_gap_above_window_marks_supersession() -> None:
    """6.1h gap → supersession path (older flipped, no contradiction row)."""
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    ta = _thesis(
        "BTC short-term price", "declining", now - timedelta(hours=6, minutes=6),
        thesis_id="00000000-0000-0000-0000-00000000000a",
    )
    tb = _thesis(
        "BTC short-term price", "increasing", now,
        thesis_id="00000000-0000-0000-0000-00000000000b",
    )
    brain, pm = _make_brain([ta, tb])

    found = await brain.detect_contradictions()
    assert found == []  # nothing recorded as contradiction
    pm.record_contradiction.assert_not_awaited()
    pm.update_thesis_status.assert_not_awaited()
    pm.mark_thesis_superseded.assert_awaited_once()
    # ta is older → superseded_by = tb
    args = pm.mark_thesis_superseded.await_args.kwargs
    assert args["older_thesis_id"] == "00000000-0000-0000-0000-00000000000a"
    assert args["newer_thesis_id"] == "00000000-0000-0000-0000-00000000000b"


@pytest.mark.asyncio
async def test_timeframe_conflict_blocks_pair_entirely() -> None:
    """long vs short → nothing recorded, nothing flipped, nothing superseded."""
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    ta = _thesis("BTC long-term price trends", "bearish", now)
    tb = _thesis("Bitcoin's short-term price trend", "bullish", now - timedelta(hours=1))
    brain, pm = _make_brain([ta, tb])

    found = await brain.detect_contradictions()
    assert found == []
    pm.record_contradiction.assert_not_awaited()
    pm.update_thesis_status.assert_not_awaited()
    pm.mark_thesis_superseded.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_timeframe_short_short_still_pairs() -> None:
    """The timeframe guard only fires on short-vs-long, not short-vs-short.

    Uses identical subject strings so the semantic grouper (real embedding)
    definitely places them in the same group — the branch under test is
    the timeframe guard, not the grouping threshold.
    """
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    ta = _thesis("BTC short-term price", "declining", now)
    tb = _thesis("BTC short-term price", "increasing", now - timedelta(hours=1))
    brain, _ = _make_brain([ta, tb])

    found = await brain.detect_contradictions()
    # Same-window (1h < 6h), same timeframe class → normal contradiction path.
    assert len(found) == 1


# ---------- remediation logic (isolated from the DB) -----------------------

def test_remediation_classify_matrix() -> None:
    """Direct test of the classifier used by the remediation script."""
    from scripts.remediate_contradictions import _classify

    window_seconds = 6.0 * 3600.0
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)

    # kept: same timeframe, gap < window, claims genuinely oppose
    same_window = {
        "subject_a": "BTC short-term price",
        "subject_b": "BTC short-term price",
        "claim_a": "declining",
        "claim_b": "increasing",
        "created_at_a": now,
        "created_at_b": now - timedelta(hours=3),
    }
    assert _classify(same_window, window_seconds) == "kept"

    # reclassified_supersession: same timeframe, gap ≥ window
    cross_window = {
        "subject_a": "BTC short-term price",
        "subject_b": "BTC short-term price",
        "claim_a": "declining",
        "claim_b": "increasing",
        "created_at_a": now,
        "created_at_b": now - timedelta(hours=7),
    }
    assert _classify(cross_window, window_seconds) == "reclassified_supersession"

    # voided_timeframe_guard: long vs short (regardless of gap)
    timeframe_conflict = {
        "subject_a": "BTC long-term price trends",
        "subject_b": "Bitcoin's short-term price trend",
        "claim_a": "bearish",
        "claim_b": "bullish",
        "created_at_a": now,
        "created_at_b": now - timedelta(hours=1),
    }
    assert _classify(timeframe_conflict, window_seconds) == "voided_timeframe_guard"


def test_remediation_older_newer_selection() -> None:
    from scripts.remediate_contradictions import _newer_id, _older_id

    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    pair = {
        "thesis_id_a": "aaaa",
        "thesis_id_b": "bbbb",
        "created_at_a": now,
        "created_at_b": now - timedelta(hours=10),
    }
    # b was created earlier → older = b, newer = a
    assert _older_id(pair) == "bbbb"
    assert _newer_id(pair) == "aaaa"

    pair2 = {
        "thesis_id_a": "aaaa",
        "thesis_id_b": "bbbb",
        "created_at_a": now - timedelta(hours=10),
        "created_at_b": now,
    }
    assert _older_id(pair2) == "aaaa"
    assert _newer_id(pair2) == "bbbb"


# ---------- persistence hooks (new methods, mocked pool) -------------------

class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_mark_thesis_superseded_writes_status_and_link() -> None:
    from memory.persistent import PersistentMemory

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"thesis_id": "x"})
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = pool

    ok = await pm.mark_thesis_superseded(
        older_thesis_id="00000000-0000-0000-0000-00000000000a",
        newer_thesis_id="00000000-0000-0000-0000-00000000000b",
    )
    assert ok is True
    call = conn.fetchrow.await_args
    query = call.args[0]
    assert "SET status = 'superseded'" in query
    assert "superseded_by = $1" in query


@pytest.mark.asyncio
async def test_set_contradiction_resolution_updates_column() -> None:
    from memory.persistent import PersistentMemory

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"contradiction_id": "x"})
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = pool

    ok = await pm.set_contradiction_resolution(
        contradiction_id="00000000-0000-0000-0000-000000000001",
        resolution="voided_timeframe_guard",
    )
    assert ok is True
    query = conn.fetchrow.await_args.args[0]
    assert "UPDATE contradictions SET resolution = $1" in query
