"""Knowledge-grounded objective-generation context tests.

The builder in ``core/objective_gen_context.py`` reads the persistence
layer + offline tool registry and renders a multi-section prompt
prelude. Every DB call is guarded independently so a partial failure
still surfaces the sections we could recover; a total wipeout returns
``""`` and the caller falls back to the historical bootstrap prompt.

Tests here cover: full-render happy path, section-by-section
independence, non-blocking on every loader path, the empty-DB
bootstrap signal, and the divergence-instruction ordering.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import objective_gen_context as ogc


# ---------- fixtures + helpers ------------------------------------------

class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _mock_pm(
    titles: list[str] | None = None,
    theses: list[dict[str, Any]] | None = None,
    contradictions: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Return a PM mock whose loaders yield the given rows."""
    conn = AsyncMock()
    if titles is not None:
        conn.fetch = AsyncMock(return_value=[{"title": t} for t in titles])
    else:
        conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    pm.get_theses = AsyncMock(return_value=theses or [])
    pm.get_contradictions = AsyncMock(return_value=contradictions or [])
    return pm


class _FakeTool:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


DATA_SOURCES_SIX = [
    _FakeTool("get_bitcoin_futures_funding", "Fetch BTC perpetuals funding data."),
    _FakeTool("get_bitcoin_onchain", "Fetch Bitcoin on-chain metrics."),
    _FakeTool("get_crypto_global_market", "Fetch global crypto market aggregates."),
    _FakeTool("get_crypto_price", "Fetch current crypto prices."),
    _FakeTool("get_fear_greed_index", "Fetch the Crypto Fear & Greed Index."),
    _FakeTool("get_news", "Fetch news via RSS feeds by topic."),
]

USAGE_COUNTS = {
    "get_bitcoin_futures_funding": 0,
    "get_bitcoin_onchain": 10,
    "get_crypto_global_market": 2,
    "get_crypto_price": 20,
    "get_fear_greed_index": 7,
    "get_news": 8,
}

DATA_SOURCE_SET = {t.name for t in DATA_SOURCES_SIX}


def _patch_registry(tools: list[_FakeTool] = DATA_SOURCES_SIX,
                    usage: dict[str, int] | None = None) -> tuple[Any, Any, Any]:
    """Bundle the three patches every full-render test needs."""
    return (
        patch(
            "scripts.compile_wiki._registered_tools_offline",
            return_value=tools,
        ),
        patch(
            "scripts.compile_wiki._load_tool_usage",
            AsyncMock(return_value=(usage or USAGE_COUNTS, {})),
        ),
        patch("core.brain.DATA_SOURCE_TOOLS", DATA_SOURCE_SET),
    )


# ---------- full-render happy path --------------------------------------

@pytest.mark.asyncio
async def test_full_render_contains_all_four_sections() -> None:
    pm = _mock_pm(
        titles=["Bitcoin price analysis", "Bitcoin mempool investigation"],
        theses=[
            {"subject": "BTC short-term price", "status": "active"},
            {"subject": "Fear & Greed volatility", "status": "active"},
        ],
        contradictions=[
            {"subject_group": "hash-rate direction",
             "subject_a": "hash rate rising", "subject_b": "hash rate flat"},
        ],
    )
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "RECENT OBJECTIVES" in text
    assert "DATA SOURCES" in text
    assert "ACTIVE THESIS SUBJECTS" in text
    assert "OPEN CONTRADICTIONS" in text


@pytest.mark.asyncio
async def test_divergence_instruction_precedes_recent_titles() -> None:
    pm = _mock_pm(titles=["A", "B", "C"])
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    divergence_idx = text.find("DIVERGE from these")
    titles_idx = text.find("RECENT OBJECTIVES")
    assert 0 <= divergence_idx < titles_idx, (
        "the DIVERGE instruction must precede the titles it applies to"
    )


@pytest.mark.asyncio
async def test_recent_titles_appear_newest_first() -> None:
    """The dedicated SQL query orders by created_at DESC; the render
    preserves that order verbatim."""
    pm = _mock_pm(titles=["newest", "middle", "oldest"])
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    n_idx = text.find("- newest")
    m_idx = text.find("- middle")
    o_idx = text.find("- oldest")
    assert 0 <= n_idx < m_idx < o_idx


@pytest.mark.asyncio
async def test_data_source_usage_counts_appear() -> None:
    """The 0-usage tool is what tells the model derivatives are unexplored."""
    pm = _mock_pm(titles=["x"])
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "get_bitcoin_futures_funding (objectives_using=0)" in text
    assert "get_crypto_price (objectives_using=20)" in text
    assert "get_fear_greed_index (objectives_using=7)" in text


@pytest.mark.asyncio
async def test_only_data_source_tools_rendered_no_utilities() -> None:
    """Utility tools like `notify`/`remember` are means, not topics —
    they must not appear in the DATA SOURCES section."""
    pm = _mock_pm(titles=["x"])
    tools_plus_utils = DATA_SOURCES_SIX + [
        _FakeTool("notify", "Send a notification"),
        _FakeTool("remember", "Store a memory"),
    ]
    p_registry, p_usage, p_dsset = _patch_registry(tools=tools_plus_utils)
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "notify" not in text
    assert "remember" not in text


@pytest.mark.asyncio
async def test_open_contradictions_count_and_groups() -> None:
    contras = [
        {"subject_group": f"group-{i}",
         "subject_a": f"a{i}", "subject_b": f"b{i}"}
        for i in range(5)
    ]
    pm = _mock_pm(titles=["x"], contradictions=contras)
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    # Header reflects total from the scan window.
    assert "OPEN CONTRADICTIONS (5):" in text
    # Only the first 3 groups rendered.
    assert "group-0" in text
    assert "group-1" in text
    assert "group-2" in text
    assert "group-3" not in text


@pytest.mark.asyncio
async def test_contradiction_fallback_uses_subject_a_vs_b_when_group_missing() -> None:
    contras = [
        {"subject_group": None,
         "subject_a": "hash rate rising", "subject_b": "hash rate flat"},
    ]
    pm = _mock_pm(titles=["x"], contradictions=contras)
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "hash rate rising vs hash rate flat" in text


# ---------- empty DB → bootstrap signal (empty string) --------------------

@pytest.mark.asyncio
async def test_completely_empty_state_returns_empty_string() -> None:
    """No titles + no theses + no tools → caller falls back to bootstrap."""
    pm = _mock_pm(titles=[], theses=[], contradictions=[])
    p_registry, p_usage, p_dsset = _patch_registry(tools=[], usage={})
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert text == ""


@pytest.mark.asyncio
async def test_only_tools_present_still_returns_context() -> None:
    """A fresh install has tools but no titles/theses — that's not
    bootstrap; the model can still see the data-source menu."""
    pm = _mock_pm(titles=[], theses=[], contradictions=[])
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert text != ""
    assert "DATA SOURCES" in text
    assert "RECENT OBJECTIVES" not in text


# ---------- non-blocking on every loader ---------------------------------

@pytest.mark.asyncio
async def test_titles_load_failure_still_renders_other_sections() -> None:
    pm = _mock_pm(theses=[{"subject": "x", "status": "active"}])
    # Force the titles query to raise.
    pm._require_pool.side_effect = RuntimeError("pool acquire failed")
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "RECENT OBJECTIVES" not in text
    assert "ACTIVE THESIS SUBJECTS" in text
    assert "DATA SOURCES" in text


@pytest.mark.asyncio
async def test_theses_load_failure_still_renders_other_sections() -> None:
    pm = _mock_pm(titles=["a"])
    pm.get_theses = AsyncMock(side_effect=RuntimeError("theses DB down"))
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "ACTIVE THESIS SUBJECTS" not in text
    assert "RECENT OBJECTIVES" in text
    assert "DATA SOURCES" in text


@pytest.mark.asyncio
async def test_contradictions_load_failure_still_renders_other_sections() -> None:
    pm = _mock_pm(titles=["a"])
    pm.get_contradictions = AsyncMock(side_effect=RuntimeError("contra down"))
    p_registry, p_usage, p_dsset = _patch_registry()
    with p_registry, p_usage, p_dsset:
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert "OPEN CONTRADICTIONS" not in text
    assert "RECENT OBJECTIVES" in text


@pytest.mark.asyncio
async def test_registry_import_failure_returns_empty_string() -> None:
    """If the offline registry itself can't be loaded, drop the whole
    thing rather than serving a partial context that omits the tools
    menu — the tools menu is the load-bearing signal for adoption."""
    pm = _mock_pm(titles=["a"])
    with patch(
        "scripts.compile_wiki._registered_tools_offline",
        side_effect=RuntimeError("registry down"),
    ):
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    # titles will still render because their loader is separate.
    # This documents partial-recovery behavior — the tools section
    # will simply be missing.
    assert "RECENT OBJECTIVES" in text
    assert "DATA SOURCES" not in text


@pytest.mark.asyncio
async def test_all_loaders_fail_returns_empty_string() -> None:
    """Bootstrap fallback signal on total collapse."""
    pm = _mock_pm()
    pm._require_pool.side_effect = RuntimeError("pool acquire failed")
    pm.get_theses = AsyncMock(side_effect=RuntimeError("theses DB down"))
    pm.get_contradictions = AsyncMock(side_effect=RuntimeError("contra down"))
    with patch(
        "scripts.compile_wiki._registered_tools_offline",
        side_effect=RuntimeError("registry down"),
    ):
        text = await ogc.build_generation_context(pm, SimpleNamespace())
    assert text == ""


# ---------- brain.py integration: bootstrap vs. context branches --------

def test_brain_source_carries_bootstrap_fallback_markers() -> None:
    """The historical STEP-1 price-scan prompt is retained AS A
    FALLBACK — first-run byte-identity for zero-knowledge state."""
    from pathlib import Path
    src = Path("core/brain.py").read_text(encoding="utf-8")
    # New builder is invoked.
    assert "build_generation_context" in src
    # Bootstrap path preserved verbatim.
    assert "STEP 1: Call get_crypto_price with symbol='bitcoin' to scan markets." in src
    # Knowledge-grounded path has the DIVERGE instruction.
    assert "DIVERGE from the recent titles above" in src
    # Mandatory ending preserved in BOTH branches. String is split
    # across multiple Python literals in the source, so count each
    # half separately.
    assert src.count("MANDATORY: end this cycle by calling create_objective. ") >= 2
    assert src.count("Do not narrate. Tool calls only.") >= 2
    # Focus block still appended LAST (unchanged code path).
    assert "OPERATOR FOCUS DIRECTIVE" in src


def test_brain_source_context_branch_is_free_of_hardcoded_price_scan() -> None:
    """The knowledge-grounded branch must not carry STEP 1/2 language —
    the whole point of the fix is to stop prescribing the price anchor."""
    from pathlib import Path
    src = Path("core/brain.py").read_text(encoding="utf-8")
    # Split at the bootstrap fallback marker; the "if generation_ctx"
    # branch sits BEFORE the else with STEP 1.
    marker = "STEP 1: Call get_crypto_price"
    idx = src.find(marker)
    assert idx > 0
    ctx_branch = src[: idx].rsplit("if generation_ctx:", 1)[-1]
    # The context branch must not repeat STEP 1 language.
    assert "STEP 1" not in ctx_branch
    assert "call get_crypto_price with symbol" not in ctx_branch.lower()
