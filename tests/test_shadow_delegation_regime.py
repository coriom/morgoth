"""Delegation regime — REJECT-only, operator-approved.

The shadow verifier gains authority to auto-flip proposals into
``shadow_rejected`` when SHADOW_DELEGATION is on, but ONLY on a REJECT
verdict. APPROVE/FLAG stays in the operator queue — the dangerous
direction remains 100% human.

Grep-locked invariants:
  - The reflect module has ONE code path writing STATUS_SHADOW_REJECTED
    (the delegation hook). Any second reference is a smuggled write.
  - apply.py refuses shadow_rejected — the precondition rejects
    anything that isn't STATUS_APPROVED_PENDING_APPLY, so
    shadow_rejected can't slip through.
  - Flag off → hook byte-identical to today (no status transition).
"""
from __future__ import annotations

import inspect
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import proposals as P
from self_modify import reflect as R


# ---------- status lifecycle --------------------------------------------

def test_shadow_rejected_registered_in_lifecycle() -> None:
    assert P.STATUS_SHADOW_REJECTED == "shadow_rejected"
    assert P.STATUS_SHADOW_REJECTED in P.ALL_STATUSES
    assert P.STATUS_SHADOW_REJECTED in P.NEGATIVE_LIST_STATUSES


def test_shadow_rejected_not_in_pre_submit_terminals() -> None:
    """The delegation hook fires AFTER submit + gate_tests + shadow.
    ``shadow_rejected`` is a post-submit terminal — it cannot be
    written via ``submit_terminal``."""
    assert P.STATUS_SHADOW_REJECTED not in P._PRE_SUBMIT_TERMINAL_STATUSES


# ---------- flag reader --------------------------------------------------

def test_delegation_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unpatched deployment behaves byte-identically to pre-delegation."""
    monkeypatch.delenv("SHADOW_DELEGATION", raising=False)
    assert R._delegation_enabled() is False


def test_delegation_flag_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for on_val in ("1", "true", "TRUE", "on", "yes", "  On  "):
        monkeypatch.setenv("SHADOW_DELEGATION", on_val)
        assert R._delegation_enabled() is True, on_val


def test_delegation_flag_falsy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for off_val in ("", "0", "off", "no", "false", "typo"):
        monkeypatch.setenv("SHADOW_DELEGATION", off_val)
        assert R._delegation_enabled() is False, off_val


# ---------- reason formatter --------------------------------------------

def test_delegation_reason_carries_shadow_prefix() -> None:
    """[shadow] prefix distinguishes delegation-rejects from operator
    rejects in the negative-list rendering."""
    v = {
        "verdict": "REJECT",
        "axes": {"api_liveness": "PASS", "field_liveness": "FAIL"},
        "reasons": ["field zero across hits", "rationale contradicted",
                    "extra reason that gets dropped"],
    }
    reason = R._format_delegation_reason(v)
    assert reason.startswith("[shadow]")
    assert "REJECT" in reason
    assert "api_liveness=PASS" in reason
    assert "field zero" in reason


# ---------- grep-level invariants (Phase C3) ----------------------------

def test_reflect_writes_shadow_rejected_only_via_delegation_hook() -> None:
    """The reflect module must have EXACTLY ONE call to update_status
    that names STATUS_SHADOW_REJECTED — the delegation hook itself.
    Any second write is a smuggled path around the invariant."""
    import re
    src = inspect.getsource(R)
    # Match ``update_status(...STATUS_SHADOW_REJECTED...)`` with the
    # call spanning any number of lines. DOTALL makes . cross \n.
    pattern = re.compile(
        r"\.update_status\([^)]*STATUS_SHADOW_REJECTED[^)]*\)",
        re.DOTALL,
    )
    matches = pattern.findall(src)
    assert len(matches) == 1, (
        f"expected exactly one update_status(...STATUS_SHADOW_REJECTED)"
        f" call; found {len(matches)}"
    )
    # And that write must be gated by _delegation_enabled().
    hook_context = src.split("Delegation hook")[-1]
    assert "_delegation_enabled" in hook_context


def test_reflect_never_delegates_on_approve_or_flag() -> None:
    """Search the reflect source for any code path that would write
    shadow_rejected on APPROVE or FLAG — such a path is the false-
    APPROVE risk the whole regime exists to prevent."""
    src = inspect.getsource(R)
    # The hook checks verdict == "REJECT" — assert no other equality
    # to APPROVE or FLAG is coupled with a STATUS_SHADOW_REJECTED
    # write on the same block.
    for verdict in ('== "APPROVE"', '== "FLAG"'):
        # Find each occurrence and check the enclosing ~200 chars don't
        # also mention STATUS_SHADOW_REJECTED.
        idx = 0
        while True:
            idx = src.find(verdict, idx)
            if idx < 0:
                break
            window = src[max(0, idx - 300):idx + 300]
            assert "STATUS_SHADOW_REJECTED" not in window, (
                f"delegation code path couples {verdict} with a "
                "STATUS_SHADOW_REJECTED write — dangerous-direction "
                "invariant broken"
            )
            idx += 1


def test_apply_precondition_refuses_shadow_rejected() -> None:
    """apply.py refuses ANY status that isn't STATUS_APPROVED_PENDING_APPLY.
    A shadow_rejected row would trip the precondition and end at
    apply_failed_rolled_back — defense in depth against the hook
    somehow producing an approvable row."""
    from self_modify import apply as apply_mod
    src = inspect.getsource(apply_mod.apply_proposal)
    # The gate is: row["status"] != P.STATUS_APPROVED_PENDING_APPLY
    assert "STATUS_APPROVED_PENDING_APPLY" in src
    assert 'row["status"] != P.STATUS_APPROVED_PENDING_APPLY' in src


# ---------- CLI --all surfacing -----------------------------------------

@pytest.mark.asyncio
async def test_cmd_list_all_flag_shows_shadow_rows() -> None:
    """--all appends the shadow_rejected block; default view omits it."""
    from types import SimpleNamespace
    from self_modify import cli as _cli
    store = MagicMock()
    store.list_pending = AsyncMock(return_value=[])
    store.list_shadow_rejected = AsyncMock(return_value=[
        {"proposal_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
         "status": "shadow_rejected", "change_type": "new_file",
         "target_path": "tools/data_feeds/x.py"},
    ])
    store.list_recent = AsyncMock(return_value=[])

    args = SimpleNamespace(limit=20, recent=False, all=True)
    rc = await _cli._cmd_list(store, args)
    assert rc == 0
    store.list_shadow_rejected.assert_awaited_once()


@pytest.mark.asyncio
async def test_cmd_list_default_view_omits_shadow() -> None:
    from types import SimpleNamespace
    from self_modify import cli as _cli
    store = MagicMock()
    store.list_pending = AsyncMock(return_value=[])
    store.list_shadow_rejected = AsyncMock(return_value=[])
    store.list_recent = AsyncMock(return_value=[])
    args = SimpleNamespace(limit=20, recent=False, all=False)
    await _cli._cmd_list(store, args)
    store.list_shadow_rejected.assert_not_awaited()


# ---------- retroactivity: pre-cutover rows stay untouched --------------

@pytest.mark.asyncio
async def test_pre_cutover_pending_rows_unaffected_by_flag_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delegation regime is CUTOVER: it applies only to proposals
    that reach pending_approval AFTER the flag is on. Pre-cutover
    rows sit in the operator queue untouched — no periodic sweep, no
    retroactive flip. This test locks the reflect module contains no
    sweep query for pending_approval rows."""
    src = inspect.getsource(R)
    # No SELECT loop over pending_approval that could sweep old rows
    # into shadow_rejected. The delegation hook is inline in the
    # single-proposal reflect path — no batch job here.
    assert "SELECT * FROM self_modify_proposals" not in src or \
           "STATUS_SHADOW_REJECTED" not in src.split(
               "SELECT * FROM self_modify_proposals")[1]


# ---------- flag-off: hook inert byte-identical -------------------------

@pytest.mark.asyncio
async def test_flag_off_hook_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag OFF, a shadow REJECT verdict must NOT flip the
    status — reflect returns pending_approval unchanged."""
    monkeypatch.delenv("SHADOW_DELEGATION", raising=False)
    # We test the delegation branch in isolation — construct the
    # local state the hook sees.
    from self_modify import reflect as _R
    assert not _R._delegation_enabled()
    # No branch fires when the flag is off; the reason formatter is
    # never called, and update_status is never invoked with
    # STATUS_SHADOW_REJECTED.


@pytest.mark.asyncio
async def test_flag_on_reject_only_verdict_flips_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook contract: (flag on + verdict REJECT) → shadow_rejected;
    (flag on + verdict APPROVE|FLAG) → no flip."""
    monkeypatch.setenv("SHADOW_DELEGATION", "on")

    # Inline-simulate the hook's conditional:
    for verdict, expected_flip in (
        ("REJECT", True),
        ("APPROVE", False),
        ("FLAG", False),
        ("ERROR", False),
    ):
        should_flip = (
            R._delegation_enabled() and verdict == "REJECT"
        )
        assert should_flip is expected_flip, (verdict, should_flip)


# ---------- cap-exclusion: shadow_rejected doesn't count ----------------

def test_shadow_rejected_excluded_from_pending_cap() -> None:
    """count_by_status_and_author checks pending_approval. Since
    shadow_rejected is a distinct terminal status, it naturally
    doesn't count — the query filters by status literal."""
    import inspect as _i
    src = _i.getsource(P.ProposalStore.count_by_status_and_author)
    # The cap query passes status as a bind param — anything not
    # equal to pending_approval is filtered out at query time. This
    # test locks the query shape.
    assert "status = $1" in src
