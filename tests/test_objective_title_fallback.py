"""Tests for the ``create_objective`` title fallback + render-side
placeholder derivation, plus the OPEN CONTRADICTIONS render dedup.

Write-side (CreateObjectiveTool): the 8B was measured to drop the
``title`` arg 2/5 dry-runs — those calls used to KeyError silently
and waste the cycle slot. Now they land as writable rows with a
derived title.

Render-side (objective_gen_context._recent_objective_titles): legacy
rows with empty title are surfaced via the same derivation so the
divergence list doesn't lose them silently.

Dedup (objective_gen_context, OPEN CONTRADICTIONS block): three
contradictions on the same subject_group used to render three
duplicated lines; now they collapse to one, header count stays the
truthful total.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import objective_gen_context as ogc
from tools import objectives_tool as ot


# ---------- pure derivation function -------------------------------------

def test_derivation_takes_first_8_words_with_ellipsis_when_longer() -> None:
    desc = "Investigate correlations between Bitcoin on-chain metrics and market capitalization"
    title = ot.derive_title_from_description(desc)
    # 8 words + ellipsis.
    assert title == "Investigate correlations between Bitcoin on-chain metrics and market…"


def test_derivation_no_ellipsis_when_shorter_than_limit() -> None:
    desc = "Analyze BTC funding"
    title = ot.derive_title_from_description(desc)
    assert title == "Analyze BTC funding"
    assert "…" not in title


def test_derivation_exactly_8_words_no_ellipsis() -> None:
    desc = "one two three four five six seven eight"
    title = ot.derive_title_from_description(desc)
    assert title == "one two three four five six seven eight"


def test_derivation_empty_description_returns_sentinel() -> None:
    assert ot.derive_title_from_description("") == "(untitled investigation)"
    assert ot.derive_title_from_description("    ") == "(untitled investigation)"
    assert ot.derive_title_from_description(None) == "(untitled investigation)"  # type: ignore[arg-type]


# ---------- CreateObjectiveTool integration ------------------------------

@pytest.mark.asyncio
async def test_missing_title_derives_from_description() -> None:
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={
        "objective_id": "id1", "title": "x", "description": "y",
    })
    tool = ot.CreateObjectiveTool(pm)
    result = await tool.execute(description="Investigate correlations between BTC and ETH short-term price moves")
    assert result["success"]
    pm.create_objective.assert_awaited_once()
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "Investigate correlations between BTC and ETH short-term price…"


@pytest.mark.asyncio
async def test_empty_title_derives_from_description() -> None:
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={"objective_id": "id1"})
    tool = ot.CreateObjectiveTool(pm)
    await tool.execute(title="", description="Explore BTC perpetual futures")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "Explore BTC perpetual futures"


@pytest.mark.asyncio
async def test_whitespace_only_title_derives_from_description() -> None:
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={"objective_id": "id1"})
    tool = ot.CreateObjectiveTool(pm)
    await tool.execute(title="   \t\n  ", description="Explore BTC perpetual futures")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "Explore BTC perpetual futures"


@pytest.mark.asyncio
async def test_none_title_derives_from_description() -> None:
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={"objective_id": "id1"})
    tool = ot.CreateObjectiveTool(pm)
    await tool.execute(title=None, description="Explore BTC perpetual futures")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "Explore BTC perpetual futures"


@pytest.mark.asyncio
async def test_provided_title_untouched() -> None:
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={"objective_id": "id1"})
    tool = ot.CreateObjectiveTool(pm)
    await tool.execute(title="My specific title", description="body")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "My specific title"


@pytest.mark.asyncio
async def test_missing_title_AND_empty_description_yields_sentinel() -> None:
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={"objective_id": "id1"})
    tool = ot.CreateObjectiveTool(pm)
    await tool.execute(description="")
    kwargs = pm.create_objective.await_args.kwargs
    assert kwargs["title"] == "(untitled investigation)"


@pytest.mark.asyncio
async def test_title_still_truncated_at_100_chars() -> None:
    """Derivation happens BEFORE the 100-char cap, so both paths respect it."""
    pm = MagicMock()
    pm.create_objective = AsyncMock(return_value={"objective_id": "id1"})
    tool = ot.CreateObjectiveTool(pm)
    long_desc = "word " * 40  # 40 words → 8-word head fits well under 100
    await tool.execute(description=long_desc)
    kwargs = pm.create_objective.await_args.kwargs
    assert len(kwargs["title"]) <= 100


# ---------- render-side title fallback -----------------------------------

class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _mock_pm_with_recent_rows(rows: list[dict[str, Any]]) -> MagicMock:
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    pm.get_theses = AsyncMock(return_value=[])
    pm.get_contradictions = AsyncMock(return_value=[])
    return pm


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "Fetch " + name


def _patch_registry() -> tuple[Any, Any, Any]:
    tools = [_FakeTool("get_bitcoin_onchain")]
    return (
        patch(
            "scripts.compile_wiki._registered_tools_offline",
            return_value=tools,
        ),
        patch(
            "scripts.compile_wiki._load_tool_usage",
            AsyncMock(return_value=({}, {})),
        ),
        patch(
            "core.brain.DATA_SOURCE_TOOLS",
            {"get_bitcoin_onchain"},
        ),
    )


@pytest.mark.asyncio
async def test_render_derives_title_from_description_for_legacy_empty_title() -> None:
    """A legacy row with title='' must still surface in RECENT
    OBJECTIVES with a readable string derived from its description."""
    rows = [
        {"title": "",
         "description": "Investigate correlations between Bitcoin on-chain and market cap"},
        {"title": "Real title",
         "description": "Any description"},
    ]
    pm = _mock_pm_with_recent_rows(rows)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "Real title" in text
    assert "Investigate correlations between Bitcoin on-chain and market cap" in text


@pytest.mark.asyncio
async def test_render_derives_title_for_null_title_row() -> None:
    rows = [{"title": None, "description": "Explore BTC perpetual futures"}]
    pm = _mock_pm_with_recent_rows(rows)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "Explore BTC perpetual futures" in text


@pytest.mark.asyncio
async def test_render_uses_sentinel_when_row_has_no_content_at_all() -> None:
    rows = [{"title": None, "description": None}]
    pm = _mock_pm_with_recent_rows(rows)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "(untitled investigation)" in text


# ---------- OPEN CONTRADICTIONS dedup ------------------------------------

@pytest.mark.asyncio
async def test_contradiction_dedup_collapses_duplicate_subject_groups() -> None:
    """Three contradictions on the same subject_group render as ONE line;
    the header count still reflects the TOTAL unresolved."""
    duplicates = [
        {"subject_group": "BTC short-term price",
         "subject_a": "rising", "subject_b": "falling"},
        {"subject_group": "BTC short-term price",
         "subject_a": "stable", "subject_b": "declining"},
        {"subject_group": "BTC short-term price",
         "subject_a": "up", "subject_b": "down"},
    ]
    pm = _mock_pm_with_recent_rows([{"title": "seed", "description": "x"}])
    pm.get_contradictions = AsyncMock(return_value=duplicates)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "OPEN CONTRADICTIONS (3):" in text
    # Only ONE bullet for the deduped group.
    assert text.count("- BTC short-term price") == 1


@pytest.mark.asyncio
async def test_contradiction_dedup_preserves_first_occurrence_order() -> None:
    """Order-preserving dedup: first-seen wins."""
    rows = [
        {"subject_group": "hash-rate", "subject_a": "a", "subject_b": "b"},
        {"subject_group": "BTC short-term price", "subject_a": "c", "subject_b": "d"},
        {"subject_group": "hash-rate", "subject_a": "e", "subject_b": "f"},
        {"subject_group": "F&G divergence", "subject_a": "g", "subject_b": "h"},
        {"subject_group": "BTC short-term price", "subject_a": "i", "subject_b": "j"},
    ]
    pm = _mock_pm_with_recent_rows([{"title": "seed", "description": "x"}])
    pm.get_contradictions = AsyncMock(return_value=rows)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    hr = text.find("- hash-rate")
    btc = text.find("- BTC short-term price")
    fg = text.find("- F&G divergence")
    assert 0 <= hr < btc < fg  # first-occurrence order preserved


@pytest.mark.asyncio
async def test_contradiction_dedup_still_caps_at_three_distinct() -> None:
    """Even with plenty of distinct groups available, only 3 render."""
    rows = [
        {"subject_group": f"group-{i}",
         "subject_a": f"a{i}", "subject_b": f"b{i}"}
        for i in range(6)
    ]
    pm = _mock_pm_with_recent_rows([{"title": "seed", "description": "x"}])
    pm.get_contradictions = AsyncMock(return_value=rows)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "OPEN CONTRADICTIONS (6):" in text
    assert "- group-0" in text
    assert "- group-1" in text
    assert "- group-2" in text
    assert "- group-3" not in text


@pytest.mark.asyncio
async def test_contradiction_header_count_is_total_not_distinct() -> None:
    """Header count must remain the TRUTHFUL unresolved-total, not the
    post-dedup distinct-group count."""
    rows = [
        {"subject_group": "same",
         "subject_a": "a", "subject_b": "b"}
        for _ in range(10)
    ]
    pm = _mock_pm_with_recent_rows([{"title": "seed", "description": "x"}])
    pm.get_contradictions = AsyncMock(return_value=rows)
    p_r, p_u, p_d = _patch_registry()
    with p_r, p_u, p_d:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    # Header count = 10 (total). Body = 1 line (distinct).
    assert "OPEN CONTRADICTIONS (10):" in text
    assert text.count("- same") == 1
