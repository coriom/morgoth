"""Report bugs from the first campaign run:
  C1  SOURCES USED iterated characters of an undecoded JSON string.
  C2  status printed None (get_active_campaign didn't SELECT status).
  C3  NOVELTY was 0/0 (get_theses_by_objective didn't SELECT
       canonical_subject).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from core.campaign import format_campaign_report
from memory.persistent import PersistentMemory


def _campaign(status="active"):
    started = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)
    ended = started + timedelta(hours=24)
    return {
        "campaign_id": "8e12a10a", "subject": "BTC dominance",
        "status": status, "started_at": started, "ends_at": ended,
    }


class TestC1_SourcesUsedJsonDecode:
    def test_json_string_payload_decoded_before_counting(self):
        # asyncpg returns JSONB as str in some driver configs. Report
        # must decode before iterating.
        objs = [
            {"title": "obj1", "status": "done",
             "sources_used": '["get_crypto_global_market","get_bitcoin_onchain"]'},
            {"title": "obj2", "status": "done",
             "sources_used": '["get_crypto_global_market"]'},
        ]
        out = format_campaign_report(_campaign(), objs, [], [])
        assert "SOURCES USED (tool → objectives touching it):" in out
        assert "get_crypto_global_market         2" in out
        assert "get_bitcoin_onchain              1" in out
        # NO character-level names should appear.
        for ch in ('  - g   ', '  - c   ', '  - s   '):
            assert ch not in out

    def test_python_list_still_works(self):
        # Backwards-compat: some paths deliver a real list.
        objs = [{"title": "obj1", "status": "done",
                 "sources_used": ["get_crypto_global_market"]}]
        out = format_campaign_report(_campaign(), objs, [], [])
        assert "get_crypto_global_market         1" in out

    def test_empty_json_string_is_unverif(self):
        # '[]' as a string used to read as truthy → 0 unverif. Fix
        # decodes and correctly counts as empty.
        objs = [
            {"title": "obj1", "status": "done", "sources_used": '[]'},
            {"title": "obj2", "status": "done",
             "sources_used": '["get_bitcoin_onchain"]'},
        ]
        out = format_campaign_report(_campaign(), objs, [], [])
        assert "UNVERIFIABLE (objectives with 0 sources): 1" in out


class TestC2_StatusColumnSelected:
    def test_get_active_campaign_selects_status(self):
        src = inspect.getsource(PersistentMemory.get_active_campaign)
        assert "status" in src
        # Structural: the SELECT list mentions status.
        assert "SELECT campaign_id, subject, started_at, ends_at, status" in src

    def test_report_renders_status_when_set(self):
        out = format_campaign_report(_campaign(status="active"), [], [], [])
        assert "status       : active" in out

    def test_report_shows_status_completed(self):
        out = format_campaign_report(_campaign(status="completed"), [], [], [])
        assert "status       : completed" in out


class TestC3_NoveltyWiring:
    def test_get_theses_by_objective_selects_canonical_subject(self):
        src = inspect.getsource(PersistentMemory.get_theses_by_objective)
        assert "canonical_subject" in src
        # Included in the SELECT column list.
        assert "canonical_subject" in src.split("FROM theses")[0]

    def test_novelty_counts_canonicals_from_theses(self):
        theses = [
            {"canonical_subject": "btc dominance", "subject": "x", "claim": "high", "evidence": []},
            {"canonical_subject": "market cap", "subject": "x", "claim": "high", "evidence": []},
            {"canonical_subject": "new subject", "subject": "x", "claim": "high", "evidence": []},
        ]
        prior = {"btc dominance", "market cap"}
        out = format_campaign_report(_campaign(), [], theses, [],
                                        prior_canonical_subjects=prior)
        assert "novel (not seen before campaign start) : 1" in out
        assert "reused (already in store)              : 2" in out

    def test_novelty_ignores_theses_with_empty_canonical(self):
        theses = [
            {"canonical_subject": None, "subject": "x", "claim": "high", "evidence": []},
            {"canonical_subject": "", "subject": "y", "claim": "high", "evidence": []},
            {"canonical_subject": "real", "subject": "z", "claim": "high", "evidence": []},
        ]
        out = format_campaign_report(_campaign(), [], theses, [],
                                        prior_canonical_subjects=set())
        # Only the one with a real canonical counts → novel=1, reused=0.
        assert "novel (not seen before campaign start) : 1" in out


class TestLiveDataShape:
    """The bug-report specifically mentioned the live campaign's
    numbers. Lock the format so a regression is loud."""

    def test_sources_used_line_format(self):
        objs = [{"title": "obj", "status": "done",
                 "sources_used": '["get_crypto_global_market"]'}]
        out = format_campaign_report(_campaign(), objs, [], [])
        # Format: 32-char left-pad tool name + count.
        assert "get_crypto_global_market         1" in out
