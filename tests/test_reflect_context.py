"""Reflect-context integrity tests.

The reflect prompt is what the LLM (any engine) reads to decide which
gap to fill. If a description is truncated in the middle of a phrase
(e.g. "recommended fees (sat/vB), " cut off before "and mempool size"),
the model faithfully proposes a duplicate — the input lied, not the
model. These tests pin down that the tools_block preserves every
registered tool's description whole.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import reflect


class _FakeTool:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


# A curated fixture: the important cases (long description, tiny, empty)
# plus the exact real string that regressed. Real registry is exercised
# by the integration probe test at the bottom.
_FIXTURE_TOOLS = [
    _FakeTool(
        "get_bitcoin_onchain",
        # This is the ACTUAL description from tools/data_feeds/onchain.py.
        # It is 285 chars. The old [:120] cap cut it at "recommended fees "
        # "(sat/vB), " — right before "and mempool size" — which caused
        # cec526ec (duplicate mempool proposal). Keep this literal in
        # sync with tools/data_feeds/onchain.py.
        "Fetch real Bitcoin on-chain metrics: current hash rate, mining "
        "difficulty + next adjustment, recommended fees (sat/vB), and "
        "mempool size. Use this for any Bitcoin on-chain investigation "
        "(hash rate, difficulty, fees, transaction volume) instead of "
        "asking for a tool that does not exist.",
    ),
    _FakeTool("get_news", "Search news headlines via NewsAPI."),
    _FakeTool("web_search", ""),  # empty description — edge case
]


async def _build_ctx_with_tools(tools: list[_FakeTool]) -> dict[str, Any]:
    """Invoke _build_context with the registry & pm collaborators mocked."""
    pm = MagicMock()
    pm.get_objectives = AsyncMock(return_value=[])
    pm.get_theses = AsyncMock(return_value=[])
    config = SimpleNamespace()
    with patch(
        "scripts.compile_wiki._registered_tools_offline",
        return_value=tools,
    ), patch(
        "scripts.compile_wiki._load_tool_usage",
        AsyncMock(return_value=({}, {})),
    ):
        return await reflect._build_context(pm, config)


# ---------- the regression: mempool tail must be present ------------------

@pytest.mark.asyncio
async def test_get_bitcoin_onchain_description_reaches_mempool_tail() -> None:
    """The word 'mempool' MUST appear in the built tools_block.

    This is the direct regression for cec526ec — proposal to add a
    mempool-stats tool that duplicated get_bitcoin_onchain's coverage,
    because the reflect model never saw the "and mempool size" tail.
    """
    ctx = await _build_ctx_with_tools(_FIXTURE_TOOLS)
    assert "mempool" in ctx["tools_block"], (
        "get_bitcoin_onchain's 'mempool' coverage was truncated out of "
        "the reflect prompt — the model cannot avoid duplicating it"
    )


# ---------- parametrized whole-description survival -----------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _FIXTURE_TOOLS, ids=lambda t: t.name)
async def test_each_tool_description_appears_whole_in_context(
    tool: _FakeTool,
) -> None:
    ctx = await _build_ctx_with_tools(_FIXTURE_TOOLS)
    tools_block = ctx["tools_block"]
    stripped = tool.description.strip()
    if stripped:
        assert stripped in tools_block, (
            f"description for {tool.name!r} was truncated or altered "
            f"in the built context"
        )
    # And the tool name itself is present on its own line.
    assert f"- {tool.name} " in tools_block


# ---------- INTEGRATION: real offline registry, real descriptions ---------
# No mocking of the registry — this asserts the property against every
# tool that reflect will actually see at runtime. If a new tool with a
# very long description is added later, this test surfaces the size
# implication.

@pytest.mark.asyncio
async def test_real_registry_descriptions_survive_context_build() -> None:
    """Every real registered tool's full description appears in the
    built tools_block; the total block stays within a sane budget."""
    from core.config import load_config
    from memory.persistent import PersistentMemory
    from scripts.compile_wiki import _registered_tools_offline

    config = await load_config()
    pm = PersistentMemory(config)
    # Don't initialize pm — _build_context only reads objectives/theses
    # from it, which we mock at method level below.
    real_tools = _registered_tools_offline(config, pm)

    pm.get_objectives = AsyncMock(return_value=[])
    pm.get_theses = AsyncMock(return_value=[])
    with patch(
        "scripts.compile_wiki._registered_tools_offline",
        return_value=real_tools,
    ), patch(
        "scripts.compile_wiki._load_tool_usage",
        AsyncMock(return_value=({}, {})),
    ):
        ctx = await reflect._build_context(pm, config)

    for t in real_tools:
        desc = (getattr(t, "description", "") or "").strip()
        if not desc:
            continue
        assert desc in ctx["tools_block"], (
            f"description for {t.name!r} does not appear whole in the "
            f"reflect context — a truncation regressed"
        )
    # Sanity: at 18 tools with max description of ~285 chars, total
    # should sit under 5 KB. If someone adds a huge description, the
    # ollama num_ctx budget could get squeezed — surface it here.
    assert len(ctx["tools_block"]) < 5000, (
        f"tools_block grew to {len(ctx['tools_block'])} chars — review "
        f"tool descriptions or reintroduce a per-tool cap that keeps "
        f"every current description whole"
    )
