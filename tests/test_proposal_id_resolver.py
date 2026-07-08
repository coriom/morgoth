"""Short-ID resolver for morgoth CLI commands.

``morgoth proposals`` prints 8-char short IDs; every ID-consuming
subcommand used to pass the operator's short input verbatim to
``uuid.UUID()``, raising ``ValueError`` — six sessions of stalled
verdicts trace to this bug. The resolver accepts any hex prefix ≥4
chars, disambiguates against the DB, and returns the full UUID.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from self_modify.proposals import ProposalStore


class _FakeConn:
    """asyncpg-conn stub whose ``fetch`` returns pre-programmed rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return self._rows

    async def __aenter__(self) -> "_FakeConn":
        return self

    async def __aexit__(self, *a: Any) -> None:
        return None


def _pm_with_rows(rows: list[dict[str, Any]]) -> tuple[MagicMock, _FakeConn]:
    conn = _FakeConn(rows)

    class _CtxMgr:
        async def __aenter__(self) -> _FakeConn:
            return conn

        async def __aexit__(self, *a: Any) -> None:
            return None

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_CtxMgr())
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    return pm, conn


# ---------- happy paths ------------------------------------------------

@pytest.mark.asyncio
async def test_resolves_8char_prefix_to_full_uuid() -> None:
    full = "42c43533-71f8-436d-ad5c-bc4175b78fae"
    pm, conn = _pm_with_rows([{"pid": full}])
    store = ProposalStore(pm)
    assert await store.resolve_id("42c43533") == full
    # SQL used LIKE with the '%' wildcard.
    sql, args = conn.calls[-1]
    assert "LIKE" in sql
    assert args == ("42c43533%",)


@pytest.mark.asyncio
async def test_full_uuid_passthrough_skips_db() -> None:
    """A full UUID must not roundtrip — cheap fast-path."""
    full = "42c43533-71f8-436d-ad5c-bc4175b78fae"
    pm, conn = _pm_with_rows([])
    store = ProposalStore(pm)
    resolved = await store.resolve_id(full)
    assert resolved == full
    # No DB call — the passthrough is what saves the roundtrip.
    assert conn.calls == []


@pytest.mark.asyncio
async def test_uppercase_and_whitespace_normalized() -> None:
    """Operator paste from the terminal may carry surrounding
    whitespace or mixed case — normalize both."""
    full = "42c43533-71f8-436d-ad5c-bc4175b78fae"
    pm, conn = _pm_with_rows([{"pid": full}])
    store = ProposalStore(pm)
    assert await store.resolve_id("  42C43533  ") == full


@pytest.mark.asyncio
async def test_4char_prefix_is_the_minimum() -> None:
    """4 hex chars is the floor — enough entropy for a modest DB."""
    full = "abcd1234-71f8-436d-ad5c-bc4175b78fae"
    pm, _ = _pm_with_rows([{"pid": full}])
    store = ProposalStore(pm)
    assert await store.resolve_id("abcd") == full


# ---------- error paths ------------------------------------------------

@pytest.mark.asyncio
async def test_empty_reference_raises_clean() -> None:
    pm, _ = _pm_with_rows([])
    store = ProposalStore(pm)
    with pytest.raises(ValueError, match="empty"):
        await store.resolve_id("")


@pytest.mark.asyncio
async def test_prefix_shorter_than_4_refused() -> None:
    pm, _ = _pm_with_rows([])
    store = ProposalStore(pm)
    with pytest.raises(ValueError, match="≥4"):
        await store.resolve_id("42c")


@pytest.mark.asyncio
async def test_non_hex_input_refused() -> None:
    """A non-hex character in the prefix is a typo — refuse rather
    than issue a query that guarantees no match."""
    pm, _ = _pm_with_rows([])
    store = ProposalStore(pm)
    with pytest.raises(ValueError, match="hex"):
        await store.resolve_id("42c4zzzz")


@pytest.mark.asyncio
async def test_no_match_raises_clean_lookup_error() -> None:
    pm, _ = _pm_with_rows([])
    store = ProposalStore(pm)
    with pytest.raises(LookupError, match="no proposal matching"):
        await store.resolve_id("deadbeef")


@pytest.mark.asyncio
async def test_ambiguous_prefix_lists_candidates() -> None:
    pm, _ = _pm_with_rows([
        {"pid": "42c43533-71f8-436d-ad5c-bc4175b78fae"},
        {"pid": "42c40001-aaaa-bbbb-cccc-dddddddddddd"},
    ])
    store = ProposalStore(pm)
    with pytest.raises(LookupError, match="ambiguous") as exc:
        await store.resolve_id("42c4")
    msg = str(exc.value)
    assert "42c43533" in msg and "42c40001" in msg


# ---------- CLI integration: reject path uses the resolver --------------

@pytest.mark.asyncio
async def test_cli_reject_command_calls_resolver_and_updates_status() -> None:
    """End-to-end shape — reject with short ID resolves + updates."""
    from types import SimpleNamespace
    from self_modify import cli as _cli, proposals as _P

    full = "42c43533-71f8-436d-ad5c-bc4175b78fae"
    store = MagicMock()
    store.resolve_id = AsyncMock(return_value=full)
    store.get = AsyncMock(return_value={
        "proposal_id": full, "status": _P.STATUS_PENDING_APPROVAL,
    })
    store.update_status = AsyncMock(return_value=True)

    args = SimpleNamespace(proposal_id="42c43533", reason="test reject")
    rc = await _cli._cmd_reject(store, args)
    assert rc == 0
    store.resolve_id.assert_awaited_once_with("42c43533")
    call = store.update_status.await_args
    assert call.args[0] == full
    assert call.args[1] == _P.STATUS_REJECTED


@pytest.mark.asyncio
async def test_cli_reject_command_prints_clean_error_on_bad_id() -> None:
    """Ambiguity → exit 2 + one-line stderr (no traceback)."""
    from types import SimpleNamespace
    from self_modify import cli as _cli

    store = MagicMock()
    store.resolve_id = AsyncMock(side_effect=LookupError("ambiguous: 42c43533, 42c40001"))

    args = SimpleNamespace(proposal_id="42c4", reason=None)
    rc = await _cli._cmd_reject(store, args)
    assert rc == 2
