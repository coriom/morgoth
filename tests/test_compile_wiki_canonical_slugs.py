"""Ghost-target fix in compile_wiki._load_tool_usage.

Pre-fix bug: theses_fed emitted (subject, slugify(subject)) so every
surface variant of a merged canonical produced a wikilink to a
non-existent entity page (122 dangling out of 193 nodes; 63 %).

Fix: pass subject_to_canonical into _load_tool_usage. Surface subjects
are resolved through the map; the slug is computed from the canonical
form the entity page was actually written under.

Grep-locked at the emission site so a future refactor that drops the
canonical resolution breaks the build.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import compile_wiki


class _AsyncCtx:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


def _fake_pm(obj_rows, thesis_rows):
    conn = MagicMock()
    async def _fetch(sql, *args):
        if "FROM objectives" in sql: return obj_rows
        if "FROM theses" in sql: return thesis_rows
        return []
    conn.fetch = AsyncMock(side_effect=_fetch)
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = MagicMock(); pm._require_pool = MagicMock(return_value=pool)
    return pm


@pytest.mark.asyncio
async def test_theses_fed_slugs_use_canonical_when_map_provided():
    """Two surface variants ('BTC mining difficulty' + 'Bitcoin mining
    difficulty') that merged into canonical 'Bitcoin mining difficulty'
    must BOTH emit slug 'bitcoin-mining-difficulty' — the page that
    actually exists — not 'btc-mining-difficulty' (dangling)."""
    pm = _fake_pm(
        obj_rows=[],
        thesis_rows=[
            {"subject": "BTC mining difficulty",
             "evidence": '[{"source": "get_bitcoin_onchain", "detail": "x"}]'},
            {"subject": "Bitcoin mining difficulty",
             "evidence": '[{"source": "get_bitcoin_onchain", "detail": "y"}]'},
        ],
    )
    canonical_map = {
        "BTC mining difficulty": "Bitcoin mining difficulty",
        "Bitcoin mining difficulty": "Bitcoin mining difficulty",
    }
    _, theses_fed = await compile_wiki._load_tool_usage(pm, canonical_map)
    fed = theses_fed.get("get_bitcoin_onchain", [])
    # Both entries resolved to the same canonical slug.
    slugs = {s for _, s in fed}
    assert slugs == {"bitcoin-mining-difficulty"}, (
        f"expected canonical slug only, got {slugs}"
    )
    displays = {d for d, _ in fed}
    assert displays == {"Bitcoin mining difficulty"}, (
        f"display must show canonical, got {displays}"
    )


@pytest.mark.asyncio
async def test_absent_canonical_map_falls_back_to_surface_slug():
    """Backward compat: if no canonical map is provided (or a specific
    subject is missing from it), fall back to surface-slugified subject.
    Missing entries are honest — they render as 'missing' section on the
    graph, not dangling due to slug drift."""
    pm = _fake_pm(
        obj_rows=[],
        thesis_rows=[
            {"subject": "Some novel subject",
             "evidence": '[{"source": "web_search", "detail": "x"}]'},
        ],
    )
    _, theses_fed = await compile_wiki._load_tool_usage(pm)  # no map
    fed = theses_fed["web_search"]
    assert fed == [("Some novel subject", "some-novel-subject")]


@pytest.mark.asyncio
async def test_objectives_count_unaffected_by_map():
    """The objectives_count return path is unchanged by the fix; regression
    guard so a future refactor doesn't accidentally touch that value."""
    obj_rows = [
        {"sources_used": '["get_crypto_price", "get_news"]'},
        {"sources_used": '["get_crypto_price"]'},
    ]
    pm = _fake_pm(obj_rows=obj_rows, thesis_rows=[])
    obj_count, _ = await compile_wiki._load_tool_usage(pm, {"any": "map"})
    assert obj_count == {"get_crypto_price": 2, "get_news": 1}


def test_load_tool_usage_signature_carries_canonical_map():
    """Grep-lock: the function must accept subject_to_canonical. A future
    refactor that drops the parameter would silently reintroduce the ghost
    bug — this test fails first."""
    sig = inspect.signature(compile_wiki._load_tool_usage)
    assert "subject_to_canonical" in sig.parameters


def test_compile_flow_passes_canonical_map():
    """Grep-lock the call site: the main compile flow MUST pass
    subject_to_canonical into _load_tool_usage."""
    src = Path("scripts/compile_wiki.py").read_text()
    assert "_load_tool_usage(pm, subject_to_canonical)" in src


def test_theses_fed_docstring_documents_the_fix():
    """Grep-lock the docstring so the 'why' anchor stays visible to any
    future maintainer reading _load_tool_usage."""
    doc = compile_wiki._load_tool_usage.__doc__ or ""
    assert "canonical map" in doc.lower()
    assert "dangling" in doc.lower()
