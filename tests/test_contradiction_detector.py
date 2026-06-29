"""Tests for the cross-thesis contradiction detector (Phase 4 sub-2)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain
from core.contradictions import (
    DIRECTION_LEXICON,
    SUBJECT_SIMILARITY_THRESHOLD,
    claims_oppose,
    group_theses_by_subject,
)


# ---------------------- claims_oppose ----------------------


def test_claims_oppose_declining_vs_increasing() -> None:
    assert claims_oppose("declining", "increasing") is True


def test_claims_oppose_declining_vs_decreasing_same_pole() -> None:
    """Same-direction synonyms must NOT be a contradiction."""
    assert claims_oppose("declining", "decreasing") is False


def test_claims_oppose_bullish_vs_bearish() -> None:
    assert claims_oppose("bullish", "bearish") is True


def test_claims_oppose_unknown_word_is_uncomparable() -> None:
    """A claim that matches no lexicon word yields no contradiction (inert)."""
    assert claims_oppose("stable", "declining") is False
    assert claims_oppose("stable", "rising") is False
    assert claims_oppose("stable", "wobbly") is False


def test_claims_oppose_claim_with_both_poles_is_uncomparable() -> None:
    """A claim that contains words from BOTH poles is uncomparable (returns False)."""
    # "rising but with declining momentum" matches both 'rising' and 'declining'
    assert claims_oppose("rising but with declining momentum", "increasing") is False


def test_claims_oppose_handles_phrases_around_lexicon_words() -> None:
    """Lexicon match is substring-based: 'sharply declining' still maps to down."""
    assert claims_oppose("sharply declining", "strongly increasing") is True


def test_claims_oppose_handles_empty_or_non_str() -> None:
    assert claims_oppose("", "declining") is False
    assert claims_oppose("declining", "") is False
    assert claims_oppose(None, "declining") is False  # type: ignore[arg-type]


# ---------------------- group_theses_by_subject ----------------------


def _mock_embed_factory(subject_vectors: dict[str, list[float]]):
    """Return an embed_fn that looks up each subject in a deterministic dict."""

    def _embed(texts: list[str]) -> list[list[float]]:
        return [subject_vectors[t] for t in texts]

    return _embed


def test_group_theses_similar_subjects_group_together() -> None:
    """Two theses whose subject embeddings exceed the threshold land in one group."""

    # Crafted vectors: A and B nearly identical (cos ≈ 1.0); C orthogonal.
    embed = _mock_embed_factory({
        "BTC transaction volume": [1.0, 0.0, 0.0],
        "Bitcoin tx volume": [0.99, 0.05, 0.0],
        "Apple pie recipes": [0.0, 1.0, 0.0],
    })
    theses = [
        {"thesis_id": "t1", "subject": "BTC transaction volume", "claim": "declining"},
        {"thesis_id": "t2", "subject": "Bitcoin tx volume", "claim": "increasing"},
        {"thesis_id": "t3", "subject": "Apple pie recipes", "claim": "bullish"},
    ]

    groups = group_theses_by_subject(theses, embed_fn=embed)

    # Two groups: {t1, t2} and {t3}
    assert len(groups) == 2
    group_with_two = next(g for g in groups if len(g) == 2)
    ids = {t["thesis_id"] for t in group_with_two}
    assert ids == {"t1", "t2"}


def test_group_theses_unrelated_subjects_in_separate_groups() -> None:
    """Below-threshold pairs do not group."""

    # All pairwise cosines ~0
    embed = _mock_embed_factory({
        "BTC price": [1.0, 0.0, 0.0],
        "Inflation rate": [0.0, 1.0, 0.0],
        "Cat memes": [0.0, 0.0, 1.0],
    })
    theses = [
        {"thesis_id": "t1", "subject": "BTC price", "claim": "rising"},
        {"thesis_id": "t2", "subject": "Inflation rate", "claim": "rising"},
        {"thesis_id": "t3", "subject": "Cat memes", "claim": "rising"},
    ]

    groups = group_theses_by_subject(theses, embed_fn=embed)

    assert len(groups) == 3
    assert all(len(g) == 1 for g in groups)


def test_group_theses_threshold_is_tunable() -> None:
    """A higher threshold breaks loosely-similar subjects into separate groups."""

    embed = _mock_embed_factory({
        "X": [1.0, 0.0],
        "Y": [0.8, 0.6],  # cos with X = 0.8
    })
    theses = [
        {"thesis_id": "a", "subject": "X", "claim": "declining"},
        {"thesis_id": "b", "subject": "Y", "claim": "increasing"},
    ]

    grouped_lenient = group_theses_by_subject(theses, threshold=0.75, embed_fn=embed)
    grouped_strict = group_theses_by_subject(theses, threshold=0.95, embed_fn=embed)

    assert len(grouped_lenient) == 1 and len(grouped_lenient[0]) == 2
    assert len(grouped_strict) == 2 and all(len(g) == 1 for g in grouped_strict)


def test_group_theses_empty_input() -> None:
    assert group_theses_by_subject([], embed_fn=_mock_embed_factory({})) == []


# ---------------------- detect_contradictions ----------------------


def _build_brain() -> Brain:
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
    persistent_memory.get_theses = AsyncMock(return_value=[])
    persistent_memory.update_thesis_status = AsyncMock(return_value=True)
    persistent_memory.record_contradiction = AsyncMock(return_value="contra-1")

    return Brain(
        config=config,
        llm_client=MagicMock(),
        persistent_memory=persistent_memory,
        episodic_memory=episodic_memory,
        scheduler=MagicMock(),
        tool_router=tool_router,
        agent_manager=MagicMock(),
        notifier=MagicMock(),
        websocket_manager=None,
    )


@pytest.mark.asyncio
async def test_detect_contradictions_flips_both_theses_and_records_one_pair() -> None:
    """Two active theses, same subject group, opposite claims -> both flipped, one recorded."""

    brain = _build_brain()
    brain._persistent_memory.get_theses = AsyncMock(return_value=[
        {"thesis_id": "t1", "subject": "BTC volume", "claim": "declining"},
        {"thesis_id": "t2", "subject": "Bitcoin volume", "claim": "increasing"},
    ])
    embed = _mock_embed_factory({
        "BTC volume": [1.0, 0.0],
        "Bitcoin volume": [0.95, 0.05],
    })

    with patch("core.brain.group_theses_by_subject",
               side_effect=lambda theses, **kw: group_theses_by_subject(theses, embed_fn=embed, **kw)):
        contradictions = await brain.detect_contradictions()

    assert len(contradictions) == 1
    pair = contradictions[0]
    assert {pair["thesis_id_a"], pair["thesis_id_b"]} == {"t1", "t2"}
    # both marked contradicted
    flipped_ids = {c.args[0] for c in brain._persistent_memory.update_thesis_status.call_args_list}
    assert flipped_ids == {"t1", "t2"}
    # one contradiction row written
    brain._persistent_memory.record_contradiction.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_contradiction_when_same_direction() -> None:
    """Same subject, same-pole synonyms -> not a contradiction."""

    brain = _build_brain()
    brain._persistent_memory.get_theses = AsyncMock(return_value=[
        {"thesis_id": "t1", "subject": "BTC volume", "claim": "declining"},
        {"thesis_id": "t2", "subject": "Bitcoin volume", "claim": "decreasing"},
    ])
    embed = _mock_embed_factory({
        "BTC volume": [1.0, 0.0],
        "Bitcoin volume": [0.95, 0.05],
    })

    with patch("core.brain.group_theses_by_subject",
               side_effect=lambda theses, **kw: group_theses_by_subject(theses, embed_fn=embed, **kw)):
        contradictions = await brain.detect_contradictions()

    assert contradictions == []
    brain._persistent_memory.update_thesis_status.assert_not_called()
    brain._persistent_memory.record_contradiction.assert_not_called()


@pytest.mark.asyncio
async def test_no_contradiction_when_subjects_differ() -> None:
    """Opposite claims on UNRELATED subjects -> not a contradiction."""

    brain = _build_brain()
    brain._persistent_memory.get_theses = AsyncMock(return_value=[
        {"thesis_id": "t1", "subject": "BTC price", "claim": "declining"},
        {"thesis_id": "t2", "subject": "Inflation rate", "claim": "increasing"},
    ])
    embed = _mock_embed_factory({
        "BTC price": [1.0, 0.0],
        "Inflation rate": [0.0, 1.0],
    })

    with patch("core.brain.group_theses_by_subject",
               side_effect=lambda theses, **kw: group_theses_by_subject(theses, embed_fn=embed, **kw)):
        contradictions = await brain.detect_contradictions()

    assert contradictions == []
    brain._persistent_memory.update_thesis_status.assert_not_called()
    brain._persistent_memory.record_contradiction.assert_not_called()


@pytest.mark.asyncio
async def test_detector_failure_does_not_block_completion() -> None:
    """A detector failure during forced completion must NOT prevent status=done.

    Wire-level check: even if get_theses raises, run_autonomous_cycle's forced-
    completion path still reaches the INTENTIONAL continue.
    """

    brain = _build_brain()
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
    # Synthesis + extraction both succeed; contradiction loader explodes.
    brain._persistent_memory.get_theses = AsyncMock(
        side_effect=RuntimeError("DB down")
    )

    # mock the LLM: 1st call = synthesis, 2nd = extraction (empty)
    from core.llm_client import ChatMessage, ChatResponse

    def _resp(content: str) -> ChatResponse:
        return ChatResponse(
            model="test",
            message=ChatMessage(role="assistant", content=content, tool_calls=[]),
            done=True,
        )

    brain._llm_client.chat = AsyncMock(side_effect=[
        _resp("Some synthesis text."),
        _resp("[]"),
    ])

    sleep_calls = [0]

    async def short_sleep(*_a, **_kw):
        sleep_calls[0] += 1
        if sleep_calls[0] >= 2:
            raise asyncio.CancelledError()

    with (
        patch("asyncio.sleep", new=short_sleep),
        patch.object(brain, "_write_log_file", new=AsyncMock()),
    ):
        await brain.run_autonomous_cycle()

    # objective still reached status=done
    update_calls = brain._persistent_memory.update_objective.call_args_list
    done_updates = [c for c in update_calls if c.kwargs.get("status") == "done"]
    assert len(done_updates) == 1, "objective must still reach status=done"
    # detector errored out, no contradictions recorded
    brain._persistent_memory.record_contradiction.assert_not_called()


# ---------------------- direction lexicon coverage ----------------------


def test_direction_lexicon_has_paired_opposites() -> None:
    """Every word in the lexicon maps to either 'up' or 'down', and both poles are present."""
    poles = set(DIRECTION_LEXICON.values())
    assert poles == {"up", "down"}, f"unexpected poles: {poles}"
    # At least the well-known opposite anchors are present on each pole
    down_words = {w for w, p in DIRECTION_LEXICON.items() if p == "down"}
    up_words = {w for w, p in DIRECTION_LEXICON.items() if p == "up"}
    assert "declining" in down_words and "bearish" in down_words
    assert "increasing" in up_words and "bullish" in up_words


def test_subject_similarity_threshold_is_a_float_constant() -> None:
    """The threshold is exposed as a named constant so it can be tuned."""
    assert isinstance(SUBJECT_SIMILARITY_THRESHOLD, float)
    assert 0.0 < SUBJECT_SIMILARITY_THRESHOLD < 1.0
