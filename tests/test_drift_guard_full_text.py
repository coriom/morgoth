"""Drift guard evaluates title + description together (not truncated title).

Reproduction: 3 real objectives (156f2aa8, 435d31f5, 68676388) had
"Dominance" cut off the 100-char title but preserved in the description.
Guard on title-only rejected them; guard on full-text accepts them.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from tools.objectives_tool import CreateObjectiveTool


@pytest.fixture(autouse=True)
def _no_semantic_dedup(monkeypatch):
    from tools import objectives_tool as ot
    monkeypatch.setattr(ot, "_find_semantic_duplicate", AsyncMock(return_value=None))


def _tool_with_prior(subject, prior):
    pm = MagicMock()
    pm.get_active_campaign = AsyncMock(return_value={
        "campaign_id": "cid-1", "subject": subject,
    })
    pm.list_campaign_objectives = AsyncMock(return_value=prior)
    pm.create_objective = AsyncMock(return_value={
        "objective_id": "o-new", "title": "T", "description": "D",
    })
    pm.attach_objective_to_campaign = AsyncMock()
    return CreateObjectiveTool(pm), pm


@pytest.mark.asyncio
class TestTruncatedTitleAccepted:
    async def test_distinctive_token_in_description_saves_the_title(self):
        # Real production case: title truncated at 100 chars ends "on BTC…"
        # but description preserves "Dominance". Full-text guard accepts.
        tool, pm = _tool_with_prior("BTC dominance", [])
        r = await tool.execute(
            title="Impact of Major Economic News Events on BTC…",
            description=("Impact of Major Economic News Events on BTC "
                          "Dominance Fluctuations via Bitcoin On-Chain "
                          "Transaction Volume"),
        )
        assert r["success"] is True
        pm.create_objective.assert_awaited_once()

    async def test_dominance_in_neither_title_nor_description_rejected(self):
        # Genuine drift: description also lacks the distinctive token.
        tool, pm = _tool_with_prior("BTC dominance", [])
        r = await tool.execute(
            title="Impact of Major Economic News Events on Short-Term BTC…",
            description=("Impact of Major Economic News Events on Short-Term "
                          "BTC Price Moves via Ethereum Gas Price"),
        )
        assert r["success"] is False
        assert "campaign guard" in r["error"]
        pm.create_objective.assert_not_called()


@pytest.mark.asyncio
class TestDupCheckUsesFullText:
    async def test_dup_matches_across_truncated_titles(self):
        # Two objectives, same truncated title, same description → the
        # second must reject on dup (full-text Jaccard on identical
        # strings = 1.0, well past the 0.7 threshold).
        prior = [{
            "title": "Impact of Major Economic News Events on BTC…",
            "description": ("Impact of Major Economic News Events on BTC "
                             "Dominance Fluctuations"),
        }]
        tool, pm = _tool_with_prior("BTC dominance", prior)
        r = await tool.execute(
            title="Impact of Major Economic News Events on BTC…",
            description=("Impact of Major Economic News Events on BTC "
                          "Dominance Fluctuations"),
        )
        assert r["success"] is False
        assert "near-duplicate" in r["error"]


class TestSourceUsesFullText:
    def test_guard_reads_title_and_description_together(self):
        src = inspect.getsource(CreateObjectiveTool.execute)
        # Structural grep-lock: the guard concatenates title + description.
        assert 'full_text = title + " " + (description or "")' in src
        # The call to the drift helper uses full_text, not title alone.
        assert "_tms(full_text, subject)" in src


class TestReplayFullText:
    def test_full_text_replay_of_23_real_titles_accepts_more(self):
        # Simulates the live BTC-dominance campaign with the corpus
        # of (title, description) pairs. Distinctive-token guard on
        # full-text should accept the 3 truncated triplet plus the
        # other title-only survivors.
        from core.campaign import title_matches_subject
        pairs = [
            # 3 truncated → all had "Dominance" in description
            ("Impact of Major Economic News Events on BTC…",
             "Impact of Major Economic News Events on BTC Dominance Fluctuations via Bitcoin On-Chain Transaction Volume"),
            ("Impact of Major Economic News Events on BTC…",
             "Impact of Major Economic News Events on BTC Dominance Fluctuations via Bitcoin On-Chain Activity"),
            ("Impact of Major Economic News Events on BTC…",
             "Impact of Major Economic News Events on BTC Dominance Fluctuations via Bitcoin Derivatives"),
            # 2 short-term-price drifts (no dominance anywhere)
            ("Impact of Major Economic News Events on Short-Term BTC Price Moves via",
             "Impact of Major Economic News Events on Short-Term BTC Price Moves via Ethereum Gas Price"),
            ("Impact of Major Economic News Events on Short-Term Crypto Price Moves",
             "Impact of Major Economic News Events on Short-Term Crypto Price Moves via Some Framework"),
        ]
        subj = "BTC dominance"
        results = [title_matches_subject(t + " " + d, subj) for t, d in pairs]
        # First 3 (truncated) → accepted now that we look at description.
        assert results[:3] == [True, True, True]
        # Last 2 (real drift) → still rejected.
        assert results[3:] == [False, False]
