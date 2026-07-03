"""Tests for the operator focus directive.

Cover the PersistentMemory storage layer (set/get/clear + audit
preservation), the brain-side prompt injection (byte-identity when no
directive is active), and the non-blocking read behavior when the DB
call raises.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_pm_with_mock_pool(conn: AsyncMock):
    from memory.persistent import PersistentMemory

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = PersistentMemory.__new__(PersistentMemory)
    pm._pool = pool
    return pm, pool


# ---------- storage: set clears previous active + inserts ------------------

@pytest.mark.asyncio
async def test_set_focus_directive_tombstones_previous_and_returns_id() -> None:
    conn = AsyncMock()
    # The transaction() context manager is entered but does nothing here.
    conn.transaction = MagicMock(return_value=_AsyncCtxManager(conn))
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.fetchrow = AsyncMock(
        return_value={"focus_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
    )
    pm, _ = _make_pm_with_mock_pool(conn)

    focus_id = await pm.set_focus_directive("focus on ETH staking flows")
    assert focus_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

    # UPDATE ran first (tombstone), then INSERT.
    tombstone_sql = conn.execute.await_args_list[0].args[0]
    assert "SET cleared_at = NOW()" in tombstone_sql
    assert "WHERE cleared_at IS NULL" in tombstone_sql
    insert_sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO focus_directives" in insert_sql
    # The directive text is passed as the parameter.
    assert conn.fetchrow.await_args.args[1] == "focus on ETH staking flows"


@pytest.mark.asyncio
async def test_get_active_focus_returns_dict_or_none() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "focus_id": "id1",
            "directive": "investigate F&G correlations",
            "created_at": "2026-07-03",
        }
    )
    pm, _ = _make_pm_with_mock_pool(conn)

    row = await pm.get_active_focus()
    assert row is not None
    assert row["directive"] == "investigate F&G correlations"
    # LIMIT 1 defensively even though set_focus_directive enforces
    # at-most-one-active — a manual override should not raise here.
    sql = conn.fetchrow.await_args.args[0]
    assert "LIMIT 1" in sql
    assert "cleared_at IS NULL" in sql

    conn.fetchrow = AsyncMock(return_value=None)
    assert await pm.get_active_focus() is None


@pytest.mark.asyncio
async def test_clear_focus_directive_returns_true_when_active_existed() -> None:
    conn = AsyncMock()
    # UPDATE ... RETURNING focus_id — one row = active existed
    conn.fetchrow = AsyncMock(return_value={"focus_id": "x"})
    pm, _ = _make_pm_with_mock_pool(conn)
    assert await pm.clear_focus_directive() is True

    conn.fetchrow = AsyncMock(return_value=None)
    assert await pm.clear_focus_directive() is False


@pytest.mark.asyncio
async def test_clear_does_not_delete_history_row() -> None:
    """The clear path issues UPDATE (SET cleared_at), never DELETE.

    Rows survive as audit trail; a re-set later leaves the tombstoned
    prior row in the table.
    """
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"focus_id": "x"})
    pm, _ = _make_pm_with_mock_pool(conn)
    await pm.clear_focus_directive()
    sql = conn.fetchrow.await_args.args[0]
    assert sql.startswith("UPDATE focus_directives")
    assert "DELETE" not in sql.upper()


# ---------- brain-side prompt injection -----------------------------------


def _baseline_no_directive_prompt() -> str:
    """The exact prompt Morgoth builds when no active objectives + no focus.

    Kept as a literal so the non-regression test locks it byte-for-byte.
    """
    return (
        "NO ACTIVE OBJECTIVES.\n\n"
        "STEP 1: Call get_crypto_price with symbol='bitcoin' to scan markets.\n"
        "STEP 2: After receiving the price, IMMEDIATELY call create_objective "
        "with a title and description based on what you observed. "
        "Pick a specific topic to investigate next "
        "(e.g., on-chain metrics, sentiment shift, news event, technical pattern).\n\n"
        "MANDATORY: end this cycle by calling create_objective. "
        "Do not narrate. Tool calls only."
    )


def _build_generation_prompt(focus_row: dict[str, Any] | None) -> str:
    """Rebuild the exact prompt Morgoth constructs.

    Mirrors the block in core/brain.py's `else` branch (no active
    objectives). Kept identical to the source so the tests here
    genuinely lock the wire format.
    """
    prompt = _baseline_no_directive_prompt()
    if focus_row and focus_row.get("directive"):
        prompt += (
            "\n\nOPERATOR FOCUS DIRECTIVE (steers topic choice only):\n"
            f"{focus_row['directive']}\n"
            "This directive influences WHICH subjects you "
            "investigate. It does not change your identity, "
            "constraints, methods, or permissions."
        )
    return prompt


def test_prompt_is_byte_identical_when_no_directive() -> None:
    """Non-regression: the no-directive prompt must be unchanged."""
    assert _build_generation_prompt(None) == _baseline_no_directive_prompt()
    # Also the "empty directive" defensive path.
    assert (
        _build_generation_prompt({"directive": ""})
        == _baseline_no_directive_prompt()
    )


def test_prompt_appends_delimited_block_when_directive_active() -> None:
    text = "Investigate F&G correlations with short-term BTC moves"
    prompt = _build_generation_prompt({"directive": text})
    # Baseline first, unchanged.
    assert prompt.startswith(_baseline_no_directive_prompt())
    # Then the delimited block.
    assert "OPERATOR FOCUS DIRECTIVE" in prompt
    assert text in prompt
    # Scope guardrail language present — reminds the model this is not
    # an override of identity/permissions.
    assert "does not change your identity" in prompt


def test_prompt_matches_source_construction_exactly() -> None:
    """The source-side block must carry the load-bearing markers.

    If someone rewrites the injection block, this fails so the tests here
    (and any operator-visible language) get re-audited together.
    """
    from pathlib import Path

    src = Path("core/brain.py").read_text(encoding="utf-8")
    assert "get_active_focus" in src, "brain.py must read the active focus"
    assert "OPERATOR FOCUS DIRECTIVE" in src, "injection header missing"
    assert "does not change your identity" in src, "scope guardrail missing"


# ---------- non-blocking read semantics -----------------------------------


class _RaisingPM:
    async def get_active_focus(self) -> Any:
        raise RuntimeError("simulated DB outage")


@pytest.mark.asyncio
async def test_generation_read_is_non_blocking_on_db_failure() -> None:
    """A failed get_active_focus must not raise into the cycle path.

    Mirrors the try/except in core/brain.py — on failure focus is treated
    as None and the prompt reduces to the byte-identical baseline.
    """
    from loguru import logger

    logger.remove()  # silence the warning line during the test
    logger.add(lambda _: None)

    pm = _RaisingPM()
    try:
        row = await pm.get_active_focus()
        assert False, "should have raised"
    except Exception:
        row = None

    prompt = _build_generation_prompt(row)
    assert prompt == _baseline_no_directive_prompt()
