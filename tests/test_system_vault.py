"""System-vault compilation tests.

Cover the deterministic system section: writing every tool page,
provenance for self-modify-born tools, source wikilinks in entity pages,
counts dict shape, and — crucially — that the LLM client is NEVER
invoked on the system path (Phase C3's design invariant).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import scripts.compile_wiki as cw


class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


# ---------- helpers ---------------------------------------------------------

class _FakeTool:
    """Minimal BaseTool stand-in for compiler tests."""

    def __init__(
        self,
        name: str,
        description: str = "",
        is_data_source: bool = False,
        is_chat_tool: bool = True,
        src_file: Path | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.is_data_source = is_data_source
        self.is_chat_tool = is_chat_tool
        if src_file is not None:
            self.__class__ = type(
                f"_FakeTool_{name}",
                (_FakeTool,),
                {"__module__": name},
            )


# ---------- deterministic building blocks ----------------------------------

def test_wikilink_source_wraps_known_and_leaves_unknown_plain() -> None:
    assert cw._wikilink_source("get_news", {"get_news"}) == "[[system/tools/get_news|get_news]]"
    assert cw._wikilink_source("get_news", set()) == "get_news"
    # Empty source is left untouched.
    assert cw._wikilink_source("", {"get_news"}) == ""


def test_claims_table_wraps_source_names_with_known_tools() -> None:
    theses = [
        {
            "claim": "declining",
            "confidence": "medium",
            "status": "active",
            "objective_id": "abcdef1234",
            "evidence": [{"source": "get_crypto_price", "detail": "-1%"}],
        }
    ]
    md = cw._claims_table(theses, known_tools={"get_crypto_price"})
    assert "[[system/tools/get_crypto_price|get_crypto_price]]" in md
    assert "abcdef12" in md  # objective short id


def test_entity_page_wraps_evidence_source_when_known() -> None:
    theses = [
        {
            "subject": "BTC price",
            "claim": "declining",
            "confidence": "medium",
            "status": "active",
            "evidence": [{"source": "get_news", "detail": "bearish headline"}],
        }
    ]
    md = cw._entity_page("BTC price", theses, "summary text", known_tools={"get_news"})
    assert "[[system/tools/get_news|get_news]]" in md
    # Unknown source stays plain (no dangling link).
    md2 = cw._entity_page("BTC price", theses, "summary text", known_tools=set())
    assert "[[system/tools/get_news" not in md2
    assert "get_news" in md2


def test_tool_page_hand_built_no_provenance() -> None:
    tool = _FakeTool(
        "get_crypto_price",
        description="Fetch crypto prices.",
        is_data_source=True,
    )
    # is_data_source now passed IN by the caller (from DATA_SOURCE_TOOLS),
    # not read from the tool's class attribute.
    page = cw._tool_page(
        tool, provenance=None, objectives_count=5, theses_fed=[],
        is_data_source=True,
    )
    assert "# get_crypto_price" in page
    assert "Fetch crypto prices." in page
    assert "data_source: **True**" in page
    assert "hand-built" in page
    # No proposal id leaked (the phrase "self-modify pipeline" appears in
    # the hand-built copy too — assert on the id instead).
    assert "proposal `" not in page


def test_tool_page_self_modify_born_includes_provenance() -> None:
    tool = _FakeTool(
        "get_fear_greed_index",
        description="Fetch F&G index.",
        is_data_source=True,
    )
    provenance = {
        "proposal_id": "8ef18ce9-6acc-4908-9cbb-eceb5bafc2bb",
        "rationale": "Add sentiment source.",
        "updated_at": "2026-07-03 09:35:22+00",
    }
    page = cw._tool_page(tool, provenance=provenance, objectives_count=0, theses_fed=[])
    # Proposal id (short form) is present — the audit link.
    assert "8ef18ce9" in page
    assert "self-modify pipeline" in page
    assert "Add sentiment source." in page


def test_tool_page_theses_fed_lists_entity_wikilinks() -> None:
    tool = _FakeTool("get_bitcoin_onchain", description="on-chain")
    page = cw._tool_page(
        tool,
        provenance=None,
        objectives_count=1,
        theses_fed=[
            ("BTC hash rate", "btc-hash-rate"),
            ("BTC hash rate", "btc-hash-rate"),  # duplicate → deduped
            ("BTC fees", "btc-fees"),
        ],
    )
    # List is deduped and sorted; count reflects RAW theses fed (3), not the
    # number of unique subjects listed (2). "theses fed" is a
    # thesis-citation count, not an entity-page count.
    assert page.count("[[entities/btc-hash-rate|BTC hash rate]]") == 1
    assert "[[entities/btc-fees|BTC fees]]" in page
    assert "theses fed: **3**" in page


def test_system_index_page_lists_all_tools_with_flags_and_origin() -> None:
    rows = [
        {
            "name": "get_fear_greed_index",
            "is_data_source": True,
            "is_chat_tool": True,
            "origin": "self-modify `#8ef18ce9`",
            "objectives_count": 0,
            "theses_fed": 0,
        },
        {
            "name": "web_search",
            "is_data_source": True,
            "is_chat_tool": True,
            "origin": "hand-built",
            "objectives_count": 25,
            "theses_fed": 3,
        },
    ]
    md = cw._system_index_page(rows)
    assert "Tools documented: **2**" in md
    assert "[[system/tools/get_fear_greed_index|get_fear_greed_index]]" in md
    assert "self-modify" in md
    assert "hand-built" in md


def test_index_page_includes_system_section_link() -> None:
    md = cw._index_page([], total_theses=0, total_contradictions=0)
    assert "## System" in md
    assert "[[system/_index" in md


# ---------- provenance loader ----------------------------------------------

@pytest.mark.asyncio
async def test_load_applied_provenance_filters_missing_files(tmp_path: Path) -> None:
    """A row whose target_path no longer exists on disk must be skipped."""
    # A file that DOES exist and one that DOES NOT.
    (tmp_path / "existing.py").write_text("# real\n")

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "proposal_id": "11111111-1111-1111-1111-111111111111",
                "target_path": "existing.py",
                "rationale": "kept",
                "updated_at": "2026-07-03",
            },
            {
                "proposal_id": "22222222-2222-2222-2222-222222222222",
                "target_path": "vanished.py",
                "rationale": "was reverted",
                "updated_at": "2026-07-02",
            },
        ]
    )
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)

    # Patch PROJECT_ROOT for the resolver.
    with patch.object(cw, "PROJECT_ROOT", tmp_path):
        result = await cw._load_applied_provenance(pm)

    assert "existing.py" in result
    assert "vanished.py" not in result
    assert result["existing.py"]["proposal_id"].startswith("11111111")


# ---------- usage loader ---------------------------------------------------

@pytest.mark.asyncio
async def test_load_tool_usage_aggregates_sources() -> None:
    conn = AsyncMock()
    obj_rows = [
        {"sources_used": ["get_news", "get_crypto_price"]},
        {"sources_used": ["get_news"]},
        {"sources_used": json.dumps(["web_search"])},  # str JSONB path
    ]
    thesis_rows = [
        {
            "subject": "BTC",
            "evidence": [{"source": "get_news"}, {"source": "web_search"}],
        },
        {
            "subject": "ETH",
            "evidence": json.dumps([{"source": "web_search"}]),
        },
    ]
    conn.fetch = AsyncMock(side_effect=[obj_rows, thesis_rows])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)

    objs, theses = await cw._load_tool_usage(pm)
    assert objs["get_news"] == 2
    assert objs["get_crypto_price"] == 1
    assert objs["web_search"] == 1
    # Theses map subject → dedup at the (subject, slug) level per tool.
    assert ("BTC", "btc") in theses["get_news"]
    assert ("BTC", "btc") in theses["web_search"]
    assert ("ETH", "eth") in theses["web_search"]


# ---------- system path never calls the LLM --------------------------------

@pytest.mark.asyncio
async def test_system_path_makes_no_llm_calls_when_no_theses(tmp_path: Path) -> None:
    """Design invariant (Phase C3): the system section is FULLY DETERMINISTIC.

    Compile a vault with zero theses so the entity-summary path never fires
    either. Assert the OllamaLLMClient's chat is not called.
    """
    from unittest.mock import AsyncMock as _AM

    llm = MagicMock()
    llm.chat = _AM()

    pm = MagicMock()
    pm.get_theses = _AM(return_value=[])
    pm.get_contradictions = _AM(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(AsyncMock(fetch=_AM(return_value=[]))))
    pm._require_pool = MagicMock(return_value=pool)

    # Stub the offline registry to a small fake list.
    fake_tools = [_FakeTool("web_search", description="d", is_data_source=True)]
    with patch.object(cw, "VAULT_DIR", tmp_path / "vault"), \
         patch.object(cw, "ENTITIES_DIR", tmp_path / "vault" / "entities"), \
         patch.object(cw, "SYSTEM_DIR", tmp_path / "vault" / "system"), \
         patch.object(cw, "SYSTEM_TOOLS_DIR", tmp_path / "vault" / "system" / "tools"), \
         patch.object(cw, "_registered_tools_offline", return_value=fake_tools):
        counts = await cw.compile_wiki(pm, llm)

    assert counts["tools_documented"] == 1
    # No LLM calls at all — no theses, no summaries required.
    assert llm.chat.await_count == 0
    # System files present.
    assert (tmp_path / "vault" / "system" / "_index.md").exists()
    assert (tmp_path / "vault" / "system" / "tools" / "web_search.md").exists()


@pytest.mark.asyncio
async def test_counts_dict_includes_tools_documented(tmp_path: Path) -> None:
    """The counts dict returned by compile_wiki must carry tools_documented."""
    from unittest.mock import AsyncMock as _AM

    llm = MagicMock()
    llm.chat = _AM()

    pm = MagicMock()
    pm.get_theses = _AM(return_value=[])
    pm.get_contradictions = _AM(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(AsyncMock(fetch=_AM(return_value=[]))))
    pm._require_pool = MagicMock(return_value=pool)

    fake_tools = [
        _FakeTool("a", description="d"),
        _FakeTool("b", description="d"),
        _FakeTool("c", description="d"),
    ]
    with patch.object(cw, "VAULT_DIR", tmp_path / "vault"), \
         patch.object(cw, "ENTITIES_DIR", tmp_path / "vault" / "entities"), \
         patch.object(cw, "SYSTEM_DIR", tmp_path / "vault" / "system"), \
         patch.object(cw, "SYSTEM_TOOLS_DIR", tmp_path / "vault" / "system" / "tools"), \
         patch.object(cw, "_registered_tools_offline", return_value=fake_tools):
        counts = await cw.compile_wiki(pm, llm)

    assert set(counts.keys()) >= {
        "theses_read",
        "entities_written",
        "contradictions",
        "tools_documented",
        "vault_path",
    }
    assert counts["tools_documented"] == 3
