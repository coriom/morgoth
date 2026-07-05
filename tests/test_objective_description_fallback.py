"""Description fallback + embed-text normalization.

The first production else-branch generation (cefaeb94) landed with a
real title and an EMPTY description — the inverse of the 2/5 dry-run
title-drop quirk fixed in 59db351. Empty descriptions degrade the
dedup gate's ``"title. description"`` embedding and leave the
operator staring at a blank detail field. Symmetric write-side
fallback: empty description → copy title.

Degenerate case (both title and description missing): title
derives from the empty description to ``"(untitled investigation)"``;
description then copies that same sentinel. The row remains
writable, queryable, and readable — the contract the title
fallback established.

Embed-text normalization: legacy rows (created before this fix
lands) with empty descriptions still round-trip through the dedup
gate. The explicit-branch composer preserves legitimate trailing
periods in titles that the old ``.strip(". ")`` used to eat.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import objectives_tool as ot


# ---------- _compose_for_embedding: normalization edge cases -------------

def test_compose_both_present_uses_period_space_separator() -> None:
    assert ot._compose_for_embedding("t", "d") == "t. d"
    assert ot._compose_for_embedding("Fetch BTC", "get current price") == (
        "Fetch BTC. get current price"
    )


def test_compose_empty_description_returns_title_alone() -> None:
    """No trailing ``". "`` when description is empty — regression:
    legacy rows without a description-fallback pass round-trip cleanly."""
    assert ot._compose_for_embedding("just a title", "") == "just a title"
    assert ot._compose_for_embedding("just a title", "   ") == "just a title"
    assert ot._compose_for_embedding("just a title", None) == "just a title"  # type: ignore[arg-type]


def test_compose_preserves_trailing_period_in_title_when_description_empty() -> None:
    """The old ``.strip(". ")`` ate legitimate title punctuation.
    The explicit-branch form keeps it."""
    assert ot._compose_for_embedding("Title with period.", "") == "Title with period."


def test_compose_empty_both_returns_sentinel() -> None:
    assert ot._compose_for_embedding("", "") == "(empty)"
    assert ot._compose_for_embedding(None, None) == "(empty)"  # type: ignore[arg-type]


def test_compose_empty_title_uses_description() -> None:
    """The dedup helper never calls with empty title in practice (the
    fallback fills it), but keep the composer robust."""
    assert ot._compose_for_embedding("", "the body") == ". the body"


# ---------- CreateObjectiveTool: description fallback --------------------

def _mock_pm() -> MagicMock:
    class _Ctx:
        def __init__(self, c: Any) -> None: self._c = c
        async def __aenter__(self) -> Any: return self._c
        async def __aexit__(self, *a: Any) -> None: pass

    pm = MagicMock()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_Ctx(conn))
    pm._require_pool = MagicMock(return_value=pool)
    pm.create_objective = AsyncMock(return_value={
        "objective_id": "new-uuid-abcd1234", "title": "x", "description": "y",
    })
    return pm


@pytest.mark.asyncio
async def test_missing_description_copies_title() -> None:
    """The cefaeb94 case: title=<real>, description=(missing)."""
    pm = _mock_pm()
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(title="Investigate BTC mining difficulty")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["description"] == "Investigate BTC mining difficulty"


@pytest.mark.asyncio
async def test_empty_description_copies_title() -> None:
    pm = _mock_pm()
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(title="T", description="")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["description"] == "T"


@pytest.mark.asyncio
async def test_whitespace_only_description_copies_title() -> None:
    pm = _mock_pm()
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(title="T", description="   \t\n  ")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["description"] == "T"


@pytest.mark.asyncio
async def test_provided_description_untouched() -> None:
    pm = _mock_pm()
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(title="T", description="Real body")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["description"] == "Real body"


@pytest.mark.asyncio
async def test_both_missing_yields_sentinel_pair() -> None:
    """Degenerate case: no title, no description. Title fallback
    derives to ``(untitled investigation)`` from the empty
    description; description then copies that sentinel. Both
    fields equal the same placeholder — the row remains writable
    and human-readable."""
    pm = _mock_pm()
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute()
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "(untitled investigation)"
    assert kwargs["description"] == "(untitled investigation)"


@pytest.mark.asyncio
async def test_missing_title_but_description_present_still_works() -> None:
    """Regression: 59db351 title-fallback path untouched. Description
    with content → title derives from it; description untouched."""
    pm = _mock_pm()
    with patch(
        "tools.objectives_tool._find_semantic_duplicate",
        AsyncMock(return_value=None),
    ):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(description="Analyze BTC funding rates")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "Analyze BTC funding rates"
    # Description NOT copied over itself — it was already present.
    assert kwargs["description"] == "Analyze BTC funding rates"


# ---------- dedup gate still sees the fallback description ---------------

@pytest.mark.asyncio
async def test_dedup_gate_receives_copied_description() -> None:
    """The dedup gate embeds (title, description). When the model
    omits description, the copied-from-title value must reach the
    gate — otherwise embeddings degrade and near-dups slip past."""
    pm = _mock_pm()
    dedup_spy = AsyncMock(return_value=None)
    with patch("tools.objectives_tool._find_semantic_duplicate", dedup_spy):
        tool = ot.CreateObjectiveTool(pm)
        await tool.execute(title="Investigate BTC funding rates")
    args = dedup_spy.await_args.args
    # positional: (pm, title, description, threshold)
    assert args[1] == "Investigate BTC funding rates"
    assert args[2] == "Investigate BTC funding rates"
