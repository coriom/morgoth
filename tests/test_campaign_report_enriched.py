"""Enriched campaign report: angle divergence, scorability, novelty."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.campaign import (
    format_campaign_report, _jaccard, _pairwise_title_similarity,
)


def _campaign():
    started = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
    ended = started + timedelta(hours=48)
    return {
        "campaign_id": "abc-123", "subject": "BTC dominance",
        "status": "completed", "started_at": started, "ends_at": ended,
        "ended_at": ended,
    }


class TestPairwiseHelpers:
    def test_jaccard_empty_is_zero(self):
        assert _jaccard(set(), set()) == 0.0
        assert _jaccard({"a"}, set()) == 0.0

    def test_jaccard_full_overlap_is_one(self):
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_partial(self):
        # {a,b,c} vs {b,c,d} → intersection 2, union 4 → 0.5
        assert _jaccard({"a", "b", "c"}, {"b", "c", "d"}) == 0.5

    def test_pairwise_returns_none_when_under_two_titles(self):
        mean, top = _pairwise_title_similarity(["one"])
        assert mean == 0.0 and top is None

    def test_pairwise_returns_top_pair_verbatim(self):
        titles = ["BTC dominance vs price",
                   "BTC dominance and price",
                   "Ethereum congestion"]
        mean, top = _pairwise_title_similarity(titles)
        assert 0.0 < mean < 1.0
        # Closest pair is the first two (highest overlap).
        assert top is not None
        assert set([top[0], top[1]]) == {
            "BTC dominance vs price", "BTC dominance and price",
        }
        # Score should be high (near-duplicate).
        assert top[2] >= 0.5


class TestReportAngleDivergence:
    def test_report_shows_mean_and_closest_pair(self):
        campaign = _campaign()
        objs = [
            {"title": "BTC dominance vs price", "status": "done",
             "sources_used": ["get_crypto_global_market"]},
            {"title": "BTC dominance and price", "status": "done",
             "sources_used": ["get_crypto_global_market"]},
            {"title": "BTC dominance macro drivers", "status": "done",
             "sources_used": ["fred_series_observations"]},
        ]
        out = format_campaign_report(campaign, objs, [], [])
        assert "ANGLE DIVERGENCE (pairwise Jaccard on titles):" in out
        assert "mean similarity" in out
        assert "closest pair" in out
        # The two near-duplicates land as the closest pair.
        assert "BTC dominance vs price" in out
        assert "BTC dominance and price" in out

    def test_single_objective_reports_na(self):
        campaign = _campaign()
        objs = [{"title": "BTC dominance solo", "status": "done",
                 "sources_used": []}]
        out = format_campaign_report(campaign, objs, [], [])
        assert "n/a — need ≥2 objectives" in out


class TestReportScorability:
    def test_scorability_uses_triage_classifier_no_reimpl(self):
        # BTC dominance is metric via canonical mapping (post-9fa06de);
        # market cap is metric; a fabricated "vibes" subject is
        # subjective. Triage must produce the expected split.
        campaign = _campaign()
        theses = [
            {"canonical_subject": "btc dominance", "subject": "BTC dominance",
             "claim": "high", "evidence": []},
            {"canonical_subject": "bitcoin futures funding rate",
             "subject": "BTC futures funding rate", "claim": "low",
             "evidence": []},
            {"canonical_subject": "some random topic",
             "subject": "Some random topic", "claim": "unclear",
             "evidence": []},
        ]
        out = format_campaign_report(campaign, [], theses, [])
        assert "SCORABILITY (of the theses this campaign produced):" in out
        assert "verifiable-metric      : 2" in out
        assert "subjective / unmapped  : 1" in out


class TestReportNovelty:
    def test_novelty_counts_when_prior_provided(self):
        campaign = _campaign()
        theses = [
            {"canonical_subject": "btc dominance", "subject": "x", "claim": "high", "evidence": []},
            {"canonical_subject": "market cap", "subject": "x", "claim": "high", "evidence": []},
            {"canonical_subject": "new never-seen subject", "subject": "x", "claim": "high", "evidence": []},
        ]
        prior = {"btc dominance", "market cap"}
        out = format_campaign_report(campaign, [], theses, [],
                                        prior_canonical_subjects=prior)
        assert "NOVELTY (canonical subjects introduced by this campaign):" in out
        assert "novel (not seen before campaign start) : 1" in out
        assert "reused (already in store)              : 2" in out

    def test_novelty_defaults_prior_to_empty(self):
        # When no prior set is supplied, every canonical counts as
        # novel — permissive default so the section renders even in
        # dry runs.
        campaign = _campaign()
        theses = [{"canonical_subject": "x", "subject": "x", "claim": "high",
                    "evidence": []}]
        out = format_campaign_report(campaign, [], theses, [])
        assert "novel (not seen before campaign start) : 1" in out


class TestNoEmptyDataCrash:
    """Report must render cleanly with zero data everywhere."""

    def test_empty_campaign_still_renders_the_three_sections(self):
        campaign = _campaign()
        out = format_campaign_report(campaign, [], [], [])
        # Old sections still present.
        assert "CAMPAIGN REPORT" in out
        # New sections rendered even without data.
        assert "ANGLE DIVERGENCE" in out
        assert "SCORABILITY" in out
        assert "NOVELTY" in out
        # Triage on zero rows → all zeros; render doesn't crash.
        assert "verifiable-metric      : 0" in out
