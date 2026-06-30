"""Tests for thesis extraction and persistence (Phase 4 sub-deliverable 1).

A thesis is a structured testable belief derived from a synthesis. Extraction
runs once on forced completion via a dedicated LLM call; defensive JSON parsing
ensures the cycle never breaks on a malformed model response, and an empty
result is a valid outcome (the synthesis may genuinely lack a testable claim).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain
from core.llm_client import ChatMessage, ChatResponse
from memory.episodic import EpisodicMetadata, QueryMatch
from memory.persistent import PersistentMemory


class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _fake_response(content: str = "") -> ChatResponse:
    return ChatResponse(
        model="test",
        message=ChatMessage(role="assistant", content=content or None, tool_calls=[]),
        done=True,
    )


def _fake_match(content: str, objective_id: str = "obj-1") -> QueryMatch:
    return QueryMatch(
        document_id=str(uuid4()),
        content=content,
        metadata=EpisodicMetadata(
            timestamp="2026-06-29T12:00:00+00:00",
            agent_id="morgoth_autonomous",
            user_id="morgoth_autonomous",
            category="objective_action",
            objective_id=objective_id,
        ),
        distance=0.1,
    )


def _build_brain(llm_client: MagicMock) -> Brain:
    tool_router = MagicMock()
    tool_router.get_schemas.return_value = []
    tool_router.execute_tool = AsyncMock()

    config = MagicMock()
    config.log_level_thought = False
    config.max_cycles_per_objective = 5
    config.autonomous_cycle_minutes = 0

    episodic_memory = MagicMock()
    episodic_memory.add_text = AsyncMock(return_value="doc-1")
    episodic_memory.query = AsyncMock(return_value=[])

    persistent_memory = MagicMock()
    persistent_memory.insert_log = AsyncMock()
    persistent_memory.add_source_used = AsyncMock(return_value=[])
    persistent_memory.get_objectives = AsyncMock(return_value=[])
    persistent_memory.increment_cycle_count = AsyncMock(return_value=0)
    persistent_memory.get_sources_used = AsyncMock(return_value=[])
    persistent_memory.update_objective = AsyncMock(return_value={})
    persistent_memory.add_thesis = AsyncMock(return_value="thesis-1")

    return Brain(
        config=config,
        llm_client=llm_client,
        persistent_memory=persistent_memory,
        episodic_memory=episodic_memory,
        scheduler=MagicMock(),
        tool_router=tool_router,
        agent_manager=MagicMock(),
        notifier=MagicMock(),
        websocket_manager=None,
    )


def _make_short_sleep():
    sleep_calls = [0]

    async def short_sleep(*_args, **_kwargs):
        sleep_calls[0] += 1
        if sleep_calls[0] >= 2:
            raise asyncio.CancelledError()

    return short_sleep


# ---------------------- Persistence: add_thesis / get_theses ----------------------


@pytest.mark.asyncio
async def test_add_thesis_persists_and_returns_id(app_config) -> None:
    """add_thesis inserts into the theses table and returns the new thesis_id."""

    pm = PersistentMemory(app_config)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"thesis_id": "11111111-1111-1111-1111-111111111111"})
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm._pool = pool

    new_id = await pm.add_thesis(
        subject="BTC short-term price",
        claim="bearish",
        confidence="medium",
        evidence=[{"source": "get_crypto_price", "detail": "-1.63% / 24h"}],
        objective_id="obj-1",
    )

    assert new_id == "11111111-1111-1111-1111-111111111111"
    conn.fetchrow.assert_called_once()
    sql = conn.fetchrow.call_args[0][0]
    assert "INSERT INTO theses" in sql
    assert "RETURNING thesis_id" in sql
    args = conn.fetchrow.call_args[0][1:]
    assert args[0] == "BTC short-term price"
    assert args[1] == "bearish"
    assert args[2] == "medium"
    assert json.loads(args[3]) == [{"source": "get_crypto_price", "detail": "-1.63% / 24h"}]
    assert args[4] == "obj-1"


@pytest.mark.asyncio
async def test_get_theses_filters_by_subject_and_status(app_config) -> None:
    """get_theses applies status and subject filters and orders newest first."""

    pm = PersistentMemory(app_config)
    fake_row = {"thesis_id": "t-1", "subject": "BTC short-term price", "claim": "bearish",
                "confidence": "medium", "evidence": [], "status": "active",
                "objective_id": "obj-1", "created_at": "2026-06-29T12:00:00+00:00"}
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[fake_row])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm._pool = pool

    result = await pm.get_theses(status="active", subject="BTC short-term price", limit=10)

    assert result == [fake_row]
    conn.fetch.assert_called_once()
    sql, *params = conn.fetch.call_args[0]
    assert "FROM theses" in sql
    assert "ORDER BY created_at DESC" in sql
    assert "status = $1" in sql and "subject ILIKE '%' || $2 || '%'" in sql
    assert params == ["active", "BTC short-term price", 10]


@pytest.mark.asyncio
async def test_get_theses_no_filters_uses_no_where(app_config) -> None:
    """With no filters, get_theses must omit the WHERE clause and only bind LIMIT."""

    pm = PersistentMemory(app_config)
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm._pool = pool

    await pm.get_theses(limit=25)

    sql, *params = conn.fetch.call_args[0]
    assert "WHERE" not in sql
    assert params == [25]


# ---------------------- Defensive JSON parsing ----------------------


def test_parse_thesis_json_well_formed_array() -> None:
    """A clean JSON array parses into structured thesis dicts."""

    raw = json.dumps([
        {"subject": "BTC short-term price", "claim": "bearish",
         "confidence": "medium",
         "evidence": [{"source": "get_crypto_price", "detail": "-1.6%"}]},
        {"subject": "BTC news sentiment", "claim": "neutral", "confidence": "low",
         "evidence": [{"source": "get_news", "detail": "mixed coverage"}]},
    ])

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 2
    assert result[0]["subject"] == "BTC short-term price"
    assert result[0]["claim"] == "bearish"
    assert result[0]["confidence"] == "medium"
    assert result[1]["confidence"] == "low"


def test_parse_thesis_json_strips_markdown_fences() -> None:
    """A JSON array wrapped in ```json fences must still parse."""

    raw = (
        '```json\n'
        '[{"subject": "X", "claim": "rising", "confidence": "high", '
        '"evidence": [{"source": "get_crypto_price", "detail": "+2%"}]}]\n'
        '```'
    )

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 1
    assert result[0]["subject"] == "X"
    assert result[0]["claim"] == "rising"


def test_parse_thesis_json_empty_array_yields_empty_list() -> None:
    """An explicit '[]' from the model is a valid 'no testable claim' result."""

    assert Brain._parse_thesis_json("[]") == []


def test_parse_thesis_json_prose_returns_empty_without_raising() -> None:
    """Prose output (model ignored the JSON instruction) is logged + returns []."""

    raw = "I think the price is going down because of the news. No JSON here."

    assert Brain._parse_thesis_json(raw) == []


def test_parse_thesis_json_malformed_json_returns_empty() -> None:
    """Broken JSON syntax returns [] rather than raising."""

    raw = "[ {subject: missing quotes}, "

    assert Brain._parse_thesis_json(raw) == []


def test_parse_thesis_json_substring_stoplist_catches_phrase_hedges() -> None:
    """The non-directional filter is now SUBSTRING-based so phrases like
    'unclear or unrelated to BTC itself' are dropped — previously they slipped
    past the exact-match check.
    """

    raw = json.dumps([
        {"subject": "Reasons behind BTC price drop",
         "claim": "unclear or unrelated to BTC itself",
         "evidence": [{"source": "get_news", "detail": "no direct mention"}]},
        {"subject": "Market state",
         "claim": "very complex picture today",
         "evidence": [{"source": "web_search", "detail": "many factors"}]},
        {"subject": "Future direction",
         "claim": "mostly mixed signals",
         "evidence": [{"source": "get_news", "detail": "split"}]},
        # Survivor: directional + evidence, no stoplist word as substring
        {"subject": "BTC price", "claim": "declining",
         "evidence": [{"source": "get_crypto_price", "detail": "-1.6%"}]},
        # Survivor: 'increasing' must NOT be falsely caught by any stoplist
        # token as a substring (safety invariant for the substring upgrade).
        {"subject": "BTC fees", "claim": "increasing",
         "evidence": [{"source": "get_crypto_price", "detail": "+10 sat/vB"}]},
    ])

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 2
    subjects = {r["subject"] for r in result}
    assert subjects == {"BTC price", "BTC fees"}


def test_parse_thesis_json_drops_non_directional_stoplist_claims() -> None:
    """Hedge-word claims ('unclear', 'mixed', 'unknown', 'complex', 'uncertain', 'n/a')
    are dropped — they cannot be contradiction-checked even though the JSON is well-formed.
    Case-insensitive.
    """

    raw = json.dumps([
        {"subject": "Reasons behind BTC price drop", "claim": "unclear",
         "evidence": [{"source": "get_news", "detail": "no direct mention"}]},
        {"subject": "Market sentiment", "claim": "Mixed",
         "evidence": [{"source": "web_search", "detail": "both bullish and bearish"}]},
        {"subject": "Future direction", "claim": "UNKNOWN",
         "evidence": [{"source": "get_news", "detail": "no signal"}]},
        {"subject": "Dynamics", "claim": "complex",
         "evidence": [{"source": "web_search", "detail": "many factors"}]},
        {"subject": "Outlook", "claim": "uncertain",
         "evidence": [{"source": "get_news", "detail": "no consensus"}]},
        {"subject": "Status", "claim": "n/a",
         "evidence": [{"source": "x", "detail": "y"}]},
        # Survivor: directional + evidence
        {"subject": "BTC price", "claim": "declining",
         "evidence": [{"source": "get_crypto_price", "detail": "-1.6%"}]},
    ])

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 1
    assert result[0]["subject"] == "BTC price"
    assert result[0]["claim"] == "declining"


def test_parse_thesis_json_drops_theses_with_empty_evidence() -> None:
    """A thesis with no evidence is dropped — the prompt requires sourcing, the
    parse filter enforces it deterministically as a backstop.
    """

    raw = json.dumps([
        {"subject": "BTC fees", "claim": "increasing",
         "evidence": []},  # empty evidence -> dropped
        {"subject": "BTC volume", "claim": "declining"},  # no evidence key -> dropped
        {"subject": "BTC price", "claim": "declining",
         "evidence": [{"source": "get_crypto_price", "detail": "-1.6%"}]},
    ])

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 1
    assert result[0]["subject"] == "BTC price"


def test_parse_thesis_json_passes_through_directional_with_evidence() -> None:
    """A thesis whose claim is directional AND has at least one evidence item
    passes through the hardening filters unchanged.
    """

    raw = json.dumps([
        {"subject": "BTC transaction volume", "claim": "declining",
         "confidence": "high",
         "evidence": [{"source": "get_crypto_price", "detail": "down 24h"}]},
    ])

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 1
    t = result[0]
    assert t["subject"] == "BTC transaction volume"
    assert t["claim"] == "declining"
    assert t["confidence"] == "high"
    assert t["evidence"] == [{"source": "get_crypto_price", "detail": "down 24h"}]


def test_parse_thesis_json_drops_items_missing_required_fields() -> None:
    """Items without subject+claim are dropped; valid ones survive."""

    raw = json.dumps([
        {"subject": "Valid", "claim": "rising",
         "evidence": [{"source": "get_crypto_price", "detail": "+2%"}]},
        {"subject": "", "claim": "still has empty subject",
         "evidence": [{"source": "x", "detail": "y"}]},
        {"claim": "missing subject",
         "evidence": [{"source": "x", "detail": "y"}]},
        {"subject": "missing claim",
         "evidence": [{"source": "x", "detail": "y"}]},
        {"subject": "Other valid", "claim": "falling", "confidence": "weird-value",
         "evidence": [{"source": "get_news", "detail": "bearish"}]},
    ])

    result = Brain._parse_thesis_json(raw)

    assert len(result) == 2
    subjects = [r["subject"] for r in result]
    assert "Valid" in subjects and "Other valid" in subjects
    # invalid confidence falls back to medium
    other = next(r for r in result if r["subject"] == "Other valid")
    assert other["confidence"] == "medium"


# ---------------------- _extract_theses (Ollama call wrapper) ----------------------


@pytest.mark.asyncio
async def test_extract_theses_skips_when_synthesis_empty() -> None:
    """No synthesis text -> no LLM call, returns []."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock()
    brain = _build_brain(llm_client)

    result = await brain._extract_theses(
        obj={"objective_id": "obj-1", "title": "X"},
        synthesis_text="",
        sources=["get_crypto_price"],
    )

    assert result == []
    llm_client.chat.assert_not_called()


@pytest.mark.asyncio
async def test_extract_theses_parses_well_formed_response() -> None:
    """A mocked LLM returning a valid JSON array yields parsed theses."""

    payload = json.dumps([
        {"subject": "BTC short-term price", "claim": "bearish",
         "confidence": "medium",
         "evidence": [{"source": "get_crypto_price", "detail": "-1.63%"}]},
    ])
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=_fake_response(content=payload))
    brain = _build_brain(llm_client)

    result = await brain._extract_theses(
        obj={"objective_id": "obj-1", "title": "BTC"},
        synthesis_text="The price dropped 1.63% in 24h.",
        sources=["get_crypto_price", "get_news"],
    )

    assert len(result) == 1
    assert result[0]["subject"] == "BTC short-term price"
    assert result[0]["claim"] == "bearish"


@pytest.mark.asyncio
async def test_extract_theses_returns_empty_for_no_testable_claim() -> None:
    """When the model emits '[]', no thesis is extracted."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=_fake_response(content="[]"))
    brain = _build_brain(llm_client)

    result = await brain._extract_theses(
        obj={"objective_id": "obj-1", "title": "X"},
        synthesis_text="No clear signal in any source.",
        sources=["get_crypto_price"],
    )

    assert result == []


@pytest.mark.asyncio
async def test_extract_theses_returns_empty_on_prose_response() -> None:
    """Prose despite the JSON instruction is parsed defensively to []."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=_fake_response(
        content="Sure! I think the price is bearish."
    ))
    brain = _build_brain(llm_client)

    result = await brain._extract_theses(
        obj={"objective_id": "obj-1", "title": "X"},
        synthesis_text="some synthesis text",
        sources=["get_crypto_price"],
    )

    assert result == []


@pytest.mark.asyncio
async def test_extract_theses_returns_empty_on_chat_failure() -> None:
    """A non-transient chat error is caught and yields [] without raising."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=RuntimeError("LLM down"))
    brain = _build_brain(llm_client)

    result = await brain._extract_theses(
        obj={"objective_id": "obj-1", "title": "X"},
        synthesis_text="some synthesis text",
        sources=["get_crypto_price"],
    )

    assert result == []


# ---------------------- Wiring into forced completion ----------------------


@pytest.mark.asyncio
async def test_forced_completion_stores_extracted_theses() -> None:
    """On forced completion, each extracted thesis is persisted via add_thesis."""

    # First chat = synthesis; second chat = thesis extraction returning two theses
    thesis_payload = json.dumps([
        {"subject": "BTC short-term price", "claim": "bearish",
         "confidence": "medium",
         "evidence": [{"source": "get_crypto_price", "detail": "-1.6%"}]},
        {"subject": "BTC news sentiment", "claim": "neutral", "confidence": "low",
         "evidence": [{"source": "get_news", "detail": "mixed"}]},
    ])
    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        _fake_response(content="Cross-source: price down, news mixed."),
        _fake_response(content=thesis_payload),
    ])
    brain = _build_brain(llm_client)
    obj_row = {
        "objective_id": "obj-1",
        "title": "BTC analysis",
        "description": "Investigate BTC price",
        "status": "pending",
    }
    brain._persistent_memory.get_objectives = AsyncMock(return_value=[obj_row])
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=5)
    brain._persistent_memory.get_sources_used = AsyncMock(
        return_value=["get_crypto_price", "get_news"]
    )
    brain._episodic_memory.query = AsyncMock(return_value=[
        _fake_match("price -1.6%"),
        _fake_match("news mixed"),
    ])

    with (
        patch("asyncio.sleep", new=_make_short_sleep()),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        await brain.run_autonomous_cycle()

    add_thesis_calls = brain._persistent_memory.add_thesis.call_args_list
    assert len(add_thesis_calls) == 2
    subjects = {c.kwargs["subject"] for c in add_thesis_calls}
    assert subjects == {"BTC short-term price", "BTC news sentiment"}
    for c in add_thesis_calls:
        assert c.kwargs["objective_id"] == "obj-1"


@pytest.mark.asyncio
async def test_extraction_failure_does_not_block_completion() -> None:
    """If extraction's chat blows up, the objective must still reach status=done."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(side_effect=[
        # synthesis succeeds
        _fake_response(content="Some real synthesis text."),
        # extraction raises a non-transient error
        RuntimeError("extraction LLM is down"),
    ])
    brain = _build_brain(llm_client)
    obj_row = {
        "objective_id": "obj-1",
        "title": "X",
        "description": "Y",
        "status": "pending",
    }
    brain._persistent_memory.get_objectives = AsyncMock(return_value=[obj_row])
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=5)
    brain._persistent_memory.get_sources_used = AsyncMock(
        return_value=["get_crypto_price", "get_news"]
    )
    brain._episodic_memory.query = AsyncMock(return_value=[])

    with (
        patch("asyncio.sleep", new=_make_short_sleep()),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        await brain.run_autonomous_cycle()

    # Completion still happened
    update_calls = brain._persistent_memory.update_objective.call_args_list
    done_updates = [c for c in update_calls if c.kwargs.get("status") == "done"]
    assert len(done_updates) == 1, "objective must still reach status=done"
    # No theses were stored (extraction failed)
    brain._persistent_memory.add_thesis.assert_not_called()


@pytest.mark.asyncio
async def test_extraction_skipped_when_synthesis_skipped() -> None:
    """With <2 distinct sources, synthesis is None -> extraction is skipped too."""

    llm_client = MagicMock()
    llm_client.chat = AsyncMock(return_value=_fake_response(content="should not be called"))
    brain = _build_brain(llm_client)
    obj_row = {
        "objective_id": "obj-1",
        "title": "X",
        "description": "Y",
        "status": "pending",
    }
    brain._persistent_memory.get_objectives = AsyncMock(return_value=[obj_row])
    brain._persistent_memory.increment_cycle_count = AsyncMock(return_value=5)
    brain._persistent_memory.get_sources_used = AsyncMock(return_value=["get_crypto_price"])
    brain._episodic_memory.query = AsyncMock(return_value=[])

    with (
        patch("asyncio.sleep", new=_make_short_sleep()),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        await brain.run_autonomous_cycle()

    llm_client.chat.assert_not_called()
    brain._persistent_memory.add_thesis.assert_not_called()
