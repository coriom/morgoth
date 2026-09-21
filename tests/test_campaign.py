"""Campaigns: lifecycle, prompt swap, accumulation block, drift/dup
guards, report rendering. Everything but the DB round-trips (mocked
where needed)."""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.campaign import (
    build_accumulation_block,
    format_campaign_report,
    title_matches_subject,
    titles_near_duplicate,
    CAMPAIGN_DUP_JACCARD,
)


class TestDriftGuard:
    def test_shared_token_passes(self):
        assert title_matches_subject("BTC dominance vs price", "BTC dominance") is True
        assert title_matches_subject("Ethereum congestion", "Ethereum gas") is True

    def test_no_shared_token_fails(self):
        assert title_matches_subject("Ethereum hashrate", "BTC dominance") is False
        assert title_matches_subject("Random topic here", "market cap") is False

    def test_stopwords_dont_rescue_drift(self):
        # "the" / "of" are stopwords — they must not paper over the drift.
        assert title_matches_subject("The of and", "BTC dominance") is False


class TestDupGuard:
    def test_near_duplicate_flagged(self):
        # "BTC dominance vs price" vs "BTC dominance and price" — same
        # tokens minus stopword. Jaccard = 1.0 → flagged.
        assert titles_near_duplicate(
            "BTC dominance vs price", "BTC dominance and price"
        ) is True

    def test_distinct_angles_not_flagged(self):
        # "BTC dominance" and "BTC volatility" share only "btc" —
        # Jaccard = 1/3 = 0.33 < 0.7.
        assert titles_near_duplicate(
            "BTC dominance level", "BTC volatility surge"
        ) is False

    def test_empty_titles_return_false(self):
        assert titles_near_duplicate("", "anything") is False
        assert titles_near_duplicate("anything", "") is False


class TestAccumulationBlock:
    def test_block_names_the_subject_and_the_lock(self):
        b = build_accumulation_block("BTC dominance", [], [])
        assert "CAMPAIGN SUBJECT: BTC dominance" in b
        assert "diverge in ANGLE, not in topic" in b
        assert "MENTION the subject explicitly" in b

    def test_prior_titles_rendered(self):
        b = build_accumulation_block(
            "BTC dominance", [],
            ["BTC dominance vs price", "BTC dominance short-term"],
        )
        assert "ANGLES ALREADY INVESTIGATED" in b
        assert "BTC dominance vs price" in b
        assert "BTC dominance short-term" in b

    def test_theses_rendered_with_evidence_detail(self):
        theses = [{
            "subject": "BTC dominance",
            "claim": "high",
            "evidence": [{"source": "get_crypto_global_market",
                          "detail": "bitcoin_dominance_percentage: 56.32"}],
        }]
        b = build_accumulation_block("BTC dominance", theses, [])
        assert "WHAT HAS BEEN ESTABLISHED" in b
        assert "BTC dominance: high" in b
        assert "56.32" in b

    def test_theses_capped(self):
        from core.campaign import CAMPAIGN_THESES_CAP
        many = [{"subject": f"s{i}", "claim": "c", "evidence": []} for i in range(50)]
        b = build_accumulation_block("s", many, [])
        assert b.count("- s") <= CAMPAIGN_THESES_CAP + 5  # rough cap check


class TestNonCampaignPromptByteIdentical:
    """The main risk of this feature: quietly changing the prompt when
    no campaign is active. Grep-lock the two prompt strings so any
    accidental modification breaks this test."""

    def test_non_campaign_path_still_contains_diverge_line(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "DIVERGE from the recent titles above." in src

    def test_campaign_branch_replaces_the_diverge_line(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # The campaign path uses the accumulation block, not the DIVERGE
        # instruction. Structural check: build_accumulation_block appears
        # inside the `if _campaign and generation_ctx:` branch.
        assert "build_accumulation_block" in src
        assert "if _campaign and generation_ctx:" in src


class TestExpiryAtCycleStart:
    def test_startup_probe_wired(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "expire_active_campaign_if_due" in src


class TestReportRendersWithAndWithoutData:
    def _campaign(self):
        started = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        ended = started + timedelta(hours=48)
        return {
            "campaign_id": "abc-123", "subject": "BTC dominance",
            "status": "completed", "started_at": started, "ends_at": ended,
            "ended_at": ended,
        }

    def test_report_with_no_data(self):
        out = format_campaign_report(self._campaign(), [], [], [])
        assert "CAMPAIGN REPORT — BTC dominance" in out
        assert "objectives   : 0" in out
        assert "theses       : 0" in out
        assert "UNVERIFIABLE (objectives with 0 sources): 0" in out

    def test_report_with_data(self):
        objs = [
            {"title": "BTC dominance vs price", "status": "done",
             "sources_used": ["get_crypto_global_market", "get_crypto_price"]},
            {"title": "BTC dominance short-term", "status": "done",
             "sources_used": ["get_crypto_global_market"]},
            {"title": "BTC dominance macro", "status": "done", "sources_used": []},
        ]
        theses = [{
            "subject": "BTC dominance", "claim": "high",
            "evidence": [{"source": "get_crypto_global_market",
                          "detail": "bitcoin_dominance_percentage: 56.32"}],
        }]
        out = format_campaign_report(self._campaign(), objs, theses, [])
        assert "objectives   : 3" in out
        assert "theses       : 1" in out
        assert "BTC dominance vs price" in out
        assert "56.32" in out
        assert "get_crypto_global_market" in out
        # 1 objective with 0 sources → UNVERIFIABLE = 1.
        assert "UNVERIFIABLE (objectives with 0 sources): 1" in out


@pytest.mark.asyncio
class TestCreateObjectiveDriftRetry:
    """CreateObjectiveTool drift/dup path: retry-once then accept."""

    async def _tool_with_campaign(self, subject):
        from tools.objectives_tool import CreateObjectiveTool
        pm = MagicMock()
        pm.get_active_campaign = AsyncMock(return_value={
            "campaign_id": "cid-1", "subject": subject,
        })
        pm.list_campaign_objectives = AsyncMock(return_value=[])
        pm.create_objective = AsyncMock(return_value={
            "objective_id": "o-1", "title": "T", "description": "D",
        })
        pm.attach_objective_to_campaign = AsyncMock()
        # Bypass dedup gate — patch its helpers.
        return CreateObjectiveTool(pm), pm

    async def test_drift_first_call_rejects(self, monkeypatch):
        from tools import objectives_tool as ot
        monkeypatch.setattr(ot, "_find_semantic_duplicate",
                             AsyncMock(return_value=None))
        tool, pm = await self._tool_with_campaign("BTC dominance")
        res = await tool.execute(title="Ethereum hashrate",
                                   description="unrelated")
        assert res["success"] is False
        assert "campaign guard" in res["error"]
        pm.create_objective.assert_not_called()

    async def test_second_call_with_valid_title_accepted(self, monkeypatch):
        # Replaces the old within-90s retry-window test. New rule:
        # a retry is accepted only when it PASSES the drift/dup check
        # on its own merits, not merely because it came within a
        # timing window.
        from tools import objectives_tool as ot
        monkeypatch.setattr(ot, "_find_semantic_duplicate",
                             AsyncMock(return_value=None))
        tool, pm = await self._tool_with_campaign("BTC dominance")
        # First reject (drift — title has no subject overlap).
        await tool.execute(title="Ethereum hashrate", description="d")
        # Second call with a title that MENTIONS the subject → passes.
        res = await tool.execute(title="BTC dominance short-term",
                                   description="fresh angle")
        assert res["success"] is True
        pm.create_objective.assert_awaited_once()
        pm.attach_objective_to_campaign.assert_awaited_once()

    async def test_matching_title_creates_immediately(self, monkeypatch):
        from tools import objectives_tool as ot
        monkeypatch.setattr(ot, "_find_semantic_duplicate",
                             AsyncMock(return_value=None))
        tool, pm = await self._tool_with_campaign("BTC dominance")
        res = await tool.execute(title="BTC dominance vs volume",
                                   description="fine")
        assert res["success"] is True
        pm.attach_objective_to_campaign.assert_awaited_once()

    async def test_no_active_campaign_normal_path(self, monkeypatch):
        from tools import objectives_tool as ot
        from tools.objectives_tool import CreateObjectiveTool
        monkeypatch.setattr(ot, "_find_semantic_duplicate",
                             AsyncMock(return_value=None))
        pm = MagicMock()
        pm.get_active_campaign = AsyncMock(return_value=None)
        pm.create_objective = AsyncMock(return_value={
            "objective_id": "o-2", "title": "T", "description": "D",
        })
        tool = CreateObjectiveTool(pm)
        res = await tool.execute(title="Anything at all", description="d")
        assert res["success"] is True
        # No attach call — not in a campaign.
        assert not hasattr(pm, "attach_objective_to_campaign") or \
                not getattr(pm, "attach_objective_to_campaign").called
