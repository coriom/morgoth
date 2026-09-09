"""SKIP-LOCKED objective claim + code_version provenance."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import version as V
from memory.persistent import PersistentMemory


# ═════════════════════════════════════════════════════════════════════
# CLAIM — SQL grep-lock (concurrency exclusion primitive)
# ═════════════════════════════════════════════════════════════════════


def test_claim_uses_for_update_skip_locked():
    """The SQL must literally contain FOR UPDATE SKIP LOCKED. A future
    edit that drops it silently reintroduces the double-claim race."""
    src = inspect.getsource(PersistentMemory.claim_next_objective)
    assert "FOR UPDATE SKIP LOCKED" in src, (
        "claim_next_objective MUST use FOR UPDATE SKIP LOCKED; the "
        "concurrency exclusion primitive is not optional"
    )


def test_claim_transitions_to_in_progress_in_same_transaction():
    """Same-transaction UPDATE ensures no window where a concurrent
    claimer sees the row still as 'pending'."""
    src = inspect.getsource(PersistentMemory.claim_next_objective)
    assert "async with conn.transaction()" in src
    assert "UPDATE objectives SET status = 'in_progress'" in src


def test_claim_includes_active_in_progress_within_threshold():
    """Multi-cycle work on the same objective: subsequent cycles claim
    the SAME row (still status='in_progress', updated_at fresh). If the
    query only picked 'pending', the cycle-loop would abandon its own
    work after one iteration."""
    src = inspect.getsource(PersistentMemory.claim_next_objective)
    assert "status = 'in_progress'" in src
    assert "updated_at > NOW() -" in src


class _AsyncCtx:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


class _TxCtx:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


@pytest.mark.asyncio
async def test_claim_returns_empty_when_no_pending():
    """No rows to claim → empty list, no error."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxCtx())
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = PersistentMemory.__new__(PersistentMemory); pm._pool = pool
    result = await pm.claim_next_objective(limit=1)
    assert result == []
    # Should have called SELECT but NOT the UPDATE (nothing to update).
    conn.fetch.assert_awaited_once()
    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_marks_in_progress_and_returns_updated_shape():
    """On claim, the returned row's status is 'in_progress' (matches
    what's now in the DB); a subsequent iteration of the cycle can
    proceed against that shape."""
    from uuid import uuid4
    uid = uuid4()
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"objective_id": uid, "title": "X", "status": "pending", "cycle_count": 0}
    ])
    conn.execute = AsyncMock()
    conn.transaction = MagicMock(return_value=_TxCtx())
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = PersistentMemory.__new__(PersistentMemory); pm._pool = pool
    result = await pm.claim_next_objective(limit=1)
    assert len(result) == 1
    assert result[0]["status"] == "in_progress"
    # UPDATE was executed within the transaction with the row's UUID.
    conn.execute.assert_awaited_once()
    upd_sql = conn.execute.await_args.args[0]
    assert "SET status = 'in_progress'" in upd_sql
    assert "updated_at = NOW()" in upd_sql
    # UUID is passed as a parameter — not string-interpolated.
    assert conn.execute.await_args.args[1] == [uid]


def test_reclaim_still_works_after_claim_wired():
    """Grep-lock: the reclaim path (in_progress + old updated_at →
    pending) is orthogonal to claim. Both share updated_at as the
    activity marker; a future edit that removes reclaim would let
    orphaned rows accumulate."""
    src = Path("memory/persistent.py").read_text()
    assert "def reclaim_orphan_objectives" in src
    assert "status = 'pending'" in src  # reclaim's UPDATE target


# ═════════════════════════════════════════════════════════════════════
# CODE_VERSION — captured once, cached, 'unknown' fallback
# ═════════════════════════════════════════════════════════════════════


class TestCodeVersion:
    def setup_method(self):
        V._reset_cache_for_tests()

    def test_returns_a_string(self):
        v = V.get_code_version()
        assert isinstance(v, str)
        assert v  # non-empty; either a sha or 'unknown'

    def test_caches_after_first_call(self):
        """Subsequent calls must NOT re-shell — one subprocess per
        process. Verified by patching after warm-up."""
        first = V.get_code_version()
        # Patch subprocess to blow up if called again.
        with patch("core.version.subprocess.run",
                   side_effect=AssertionError("subprocess called again — cache broken")):
            second = V.get_code_version()
        assert first == second

    def test_unknown_fallback_when_git_absent(self, monkeypatch):
        """A machine without git in PATH → 'unknown', never a crash."""
        V._reset_cache_for_tests()

        def raise_filenotfound(*a, **kw):
            raise FileNotFoundError("git: not on PATH")

        monkeypatch.setattr("core.version.subprocess.run", raise_filenotfound)
        assert V.get_code_version() == "unknown"

    def test_unknown_fallback_on_git_error(self, monkeypatch):
        """Git present but repo has no HEAD (fresh clone with no
        commits) → 'unknown'."""
        V._reset_cache_for_tests()
        result = MagicMock()
        result.returncode = 128
        result.stdout = ""
        monkeypatch.setattr("core.version.subprocess.run", lambda *a, **kw: result)
        assert V.get_code_version() == "unknown"


def test_add_thesis_accepts_code_version_kwarg():
    """Grep-lock: the code_version parameter is present on add_thesis
    and the INSERT includes it. A future edit that drops the column
    reference would silently write NULL for every new thesis."""
    src = inspect.getsource(PersistentMemory.add_thesis)
    assert "code_version" in src
    assert "code_version" in src.split("INSERT INTO theses")[1].split("RETURNING")[0]


def test_brain_passes_code_version_on_thesis_write():
    """Grep-lock on brain.py — add_thesis must be called with a
    code_version kwarg, and the value must come from get_code_version
    (not a hard-coded literal)."""
    src = Path("core/brain.py").read_text()
    assert "from core.version import get_code_version" in src
    assert "code_version=_cv" in src


def test_backtest_cli_accepts_code_version_filter():
    """The --code-version flag must exist on the CLI, and NULL rows
    must be excluded rather than crashing."""
    src = Path("scripts/backtest_theses_descriptive.py").read_text()
    assert "--code-version" in src
    # Filter excludes NULL cleanly (t.get returns None which != the arg)
    assert 't.get("code_version") == args.code_version' in src
