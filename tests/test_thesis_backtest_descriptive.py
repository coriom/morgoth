"""Unit tests for the descriptive-thesis backtest (non-directional scoring).

Covers:
    - subject classifier: metric / relation / unverifiable / unreachable
    - sentiment vocabulary → up/mid/down parser
    - F&G index → 3-bucket mapping
    - most_recent_before lookup (epoch-style series)
    - end-to-end score_hashrate / score_difficulty / score_sentiment on
      mocked source series (no HTTP)
    - aggregate() shape parity with the directional aggregator
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.thesis_backtest_descriptive import (
    aggregate,
    bucket_change,
    bucket_sentiment_value,
    classify_subject,
    most_recent_before,
    parse_sentiment,
    score_difficulty,
    score_funding,
    score_gas,
    score_hashrate,
    score_market_cap,
    score_sentiment,
    score_volume,
    triage,
)


class TestClassifySubject:
    @pytest.mark.parametrize(
        "subject, expected_verifiability, expected_metric",
        [
            ("BTC hash rate", "metric", "btc_hashrate"),
            ("Bitcoin hash rate", "metric", "btc_hashrate"),
            ("Bitcoin mining difficulty", "metric", "btc_difficulty"),
            ("BTC mining difficulty adjustment", "metric", "btc_difficulty"),
            ("Market sentiment", "metric", "market_sentiment"),
            ("Ethereum hash rate", "unverifiable", None),  # PoS post-merge
            ("Bitcoin dominance percentage", "unverifiable", None),
            # Post-source-wiring these ARE reachable (Binance funding, Owlracle gas).
            ("Bitcoin futures funding rate", "metric", "btc_funding"),
            ("Ethereum gas prices", "metric", "eth_gas"),
            ("Bitcoin difficulty adjustment progress", "unverifiable", None),
            ("Bitcoin difficulty adjustments correlate with BTC price", "relation", None),
            ("some random subject", "unverifiable", None),
            ("", "unverifiable", None),
        ],
    )
    def test_classify(self, subject, expected_verifiability, expected_metric):
        v, m, _ = classify_subject(subject)
        assert v == expected_verifiability
        assert m == expected_metric


class TestParseSentiment:
    @pytest.mark.parametrize(
        "claim, expected",
        [
            ("bullish", "up"),
            ("increasingly bullish", "up"),
            ("positive", "up"),
            ("greedy", "up"),
            ("optimistic", "up"),
            ("bearish", "down"),
            ("fearful", "down"),
            ("panic", "down"),
            ("cautious", "mid"),
            ("neutral", "mid"),
            ("mixed", "mid"),
            ("uncertain", "mid"),
            # Ambiguous / multi-class → None (conservative).
            ("cautiously bullish", None),
            ("bullish but fearful", None),
            ("", None),
        ],
    )
    def test_parse(self, claim, expected):
        assert parse_sentiment(claim) == expected


class TestBucketSentimentValue:
    def test_extremes(self):
        assert bucket_sentiment_value(80) == "up"
        assert bucket_sentiment_value(20) == "down"
        assert bucket_sentiment_value(50) == "mid"

    def test_boundaries(self):
        # 56 → up; 55 → mid; 45 → mid; 44 → down
        assert bucket_sentiment_value(56) == "up"
        assert bucket_sentiment_value(55) == "mid"
        assert bucket_sentiment_value(45) == "mid"
        assert bucket_sentiment_value(44) == "down"


class TestBucketChange:
    def test_flat(self):
        assert bucket_change(100.0, 100.3, 0.005) == "flat"

    def test_up(self):
        assert bucket_change(100.0, 101.0, 0.005) == "up"

    def test_down(self):
        assert bucket_change(100.0, 99.0, 0.005) == "down"

    def test_nonpositive_before_is_flat(self):
        assert bucket_change(0.0, 5.0, 0.005) == "flat"


class TestMostRecentBefore:
    def test_before_window(self):
        assert most_recent_before([100, 200], [1.0, 2.0], 50) is None

    def test_exact(self):
        assert most_recent_before([100, 200, 300], [1.0, 2.0, 3.0], 200) == 2.0

    def test_between(self):
        # 250 → most recent BEFORE = idx 1 = 2.0
        assert most_recent_before([100, 200, 300], [1.0, 2.0, 3.0], 250) == 2.0

    def test_after_last(self):
        # 500 > last (300) → return last value 3.0
        assert most_recent_before([100, 200, 300], [1.0, 2.0, 3.0], 500) == 3.0


class TestTriage:
    def test_bucket_counts(self):
        rows = [
            {"subject": "BTC hash rate", "claim": "increasing"},
            {"subject": "Bitcoin mining difficulty", "claim": "decreasing"},
            {"subject": "Market sentiment", "claim": "bullish"},
            {"subject": "Ethereum hash rate", "claim": "stable"},
            {"subject": "Bitcoin dominance percentage", "claim": "increasing"},
            {"subject": "some subjective take", "claim": "vibes"},
        ]
        counts, metric_rows, unreachable_reasons, subjective_subjects = triage(rows)
        assert counts["input"] == 6
        assert counts["metric"] == 3
        # dominance = unreachable (no free source), eth hash rate = unreachable (metric doesn't exist)
        assert counts["unreachable"] == 2
        assert counts["subjective"] == 1
        assert len(metric_rows) == 3


class TestClassifySubjectNewMetrics:
    @pytest.mark.parametrize(
        "subject, expected_metric",
        [
            ("BTC market capitalization", "btc_market_cap"),
            ("BTC's market cap", "btc_market_cap"),
            ("Ethereum market capitalization", "eth_market_cap"),
            ("BTC 24-hour volume", "btc_volume"),
            ("BTC transaction volume", "btc_volume"),
            ("Bitcoin volume traded", "btc_volume"),
            ("Ethereum volume", "eth_volume"),
            ("Ethereum trading volume", "eth_volume"),
            ("Ethereum gas price", "eth_gas"),
            ("Ethereum Gas Price", "eth_gas"),
            ("Bitcoin futures funding rate", "btc_funding"),
            ("BTC futures funding rate", "btc_funding"),
            ("Funding rates on Bitcoin futures", "btc_funding"),
        ],
    )
    def test_new_metrics_reachable(self, subject, expected_metric):
        v, m, _ = classify_subject(subject)
        assert v == "metric" and m == expected_metric

    @pytest.mark.parametrize(
        "subject",
        [
            "Global market capitalization",
            "Crypto market capitalization",
            "Global crypto market volume",
            "Crypto market volume",
            "Market cap",  # bare — likely global
            "Market capitalization of cryptocurrencies",
            "Bitcoin dominance percentage",  # still paid-only
        ],
    )
    def test_global_or_bare_stays_unreachable(self, subject):
        v, m, reason = classify_subject(subject)
        assert v == "unverifiable" and m is None
        assert reason is not None and "no free historical source" in reason


class TestScoreNewMetrics:
    """Directional scoring on the four new metrics, with mocked series."""

    def setup_method(self):
        self.anchor = datetime(2026, 6, 1, tzinfo=timezone.utc)
        anchor_ms = int(self.anchor.timestamp() * 1000)
        # 30 daily points, values rising 2 %/day.
        self.ms = [anchor_ms + d * 86400_000 for d in range(30)]
        self.up_series = (self.ms, [100.0 * (1.02 ** d) for d in range(30)])
        # 30 daily points, flat.
        self.flat_series = (self.ms, [100.0 for _ in range(30)])
        self.now = self.anchor + timedelta(days=25)

    def test_market_cap_up_prediction_hits(self):
        row = {
            "thesis_id": "m1", "subject": "BTC market capitalization",
            "claim": "increasing", "confidence": "medium",
            "created_at": self.anchor,
        }
        r = score_market_cap(row, self.up_series, 168, 0.01, self.now, "bitcoin")
        assert r is not None and r.outcome == "hit" and r.metric == "btc_market_cap"

    def test_market_cap_wrong_prediction_misses(self):
        row = {
            "thesis_id": "m2", "subject": "BTC market capitalization",
            "claim": "declining", "confidence": "medium",
            "created_at": self.anchor,
        }
        r = score_market_cap(row, self.up_series, 168, 0.01, self.now, "bitcoin")
        assert r is not None and r.outcome == "miss"

    def test_volume_stable_hits_on_flat_series(self):
        row = {
            "thesis_id": "v1", "subject": "BTC trading volume",
            "claim": "stable", "confidence": "low",
            "created_at": self.anchor,
        }
        r = score_volume(row, self.flat_series, 168, 0.15, self.now, "bitcoin")
        assert r is not None and r.outcome == "hit" and r.metric == "btc_volume"

    def test_gas_increasing_hit(self):
        row = {
            "thesis_id": "g1", "subject": "Ethereum gas price",
            "claim": "increasing", "confidence": "high",
            "created_at": self.anchor,
        }
        r = score_gas(row, self.up_series, 168, 0.10, self.now)
        assert r is not None and r.outcome == "hit" and r.metric == "eth_gas"

    def test_funding_directional_hit(self):
        row = {
            "thesis_id": "f1", "subject": "Bitcoin futures funding rate",
            "claim": "increasing", "confidence": "medium",
            "created_at": self.anchor,
        }
        r = score_funding(row, self.up_series, 168, 0.0005, self.now)
        assert r is not None and r.outcome == "hit"

    def test_funding_level_claim_skips(self):
        """'low'/'high' level claims can't be scored against a % change —
        must return None (SKIP), not fabricate a match."""
        row = {
            "thesis_id": "f2", "subject": "Bitcoin futures funding rate",
            "claim": "low", "confidence": "medium",
            "created_at": self.anchor,
        }
        r = score_funding(row, self.up_series, 168, 0.0005, self.now)
        assert r is None

    def test_graceful_degrade_series_none(self):
        """Source outage → series=None → SKIP, never a crash, never a hit."""
        row = {
            "thesis_id": "d1", "subject": "BTC market capitalization",
            "claim": "increasing", "confidence": "medium",
            "created_at": self.anchor,
        }
        assert score_market_cap(row, None, 168, 0.01, self.now, "bitcoin") is None
        assert score_volume(row, None, 168, 0.15, self.now, "bitcoin") is None
        assert score_gas(row, None, 168, 0.10, self.now) is None
        assert score_funding(row, None, 168, 0.0005, self.now) is None

    def test_no_fabrication_when_target_before_series(self):
        """Thesis timestamp outside the series window → None, not 0/fake value."""
        pre_anchor = self.anchor - timedelta(days=90)
        row = {
            "thesis_id": "d2", "subject": "BTC market capitalization",
            "claim": "increasing", "confidence": "medium",
            "created_at": pre_anchor,
        }
        r = score_market_cap(row, self.up_series, 168, 0.01, self.now, "bitcoin")
        assert r is None


class TestScoreHashrate:
    def setup_method(self):
        anchor = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        self.anchor = anchor
        anchor_ms = int(anchor.timestamp() * 1000)
        # 30 daily points, hashrate rises 1% per day.
        self.ms = [anchor_ms + d * 86400_000 for d in range(30)]
        self.values = [500.0 * (1.01 ** d) for d in range(30)]
        self.now = anchor + timedelta(days=25)

    def test_correct_up_prediction_hits(self):
        row = {
            "thesis_id": "h1", "subject": "BTC hash rate", "claim": "increasing",
            "confidence": "high", "created_at": self.anchor,
        }
        r = score_hashrate(row, (self.ms, self.values), horizon_hours=168, flat_pct=0.02, now=self.now)
        # 7 days at +1%/day = ~+7.2% > 2% band → up → HIT
        assert r is not None and r.outcome == "hit"

    def test_wrong_down_prediction_misses(self):
        row = {
            "thesis_id": "h2", "subject": "BTC hash rate", "claim": "decreasing",
            "confidence": "medium", "created_at": self.anchor,
        }
        r = score_hashrate(row, (self.ms, self.values), horizon_hours=168, flat_pct=0.02, now=self.now)
        assert r is not None and r.outcome == "miss"

    def test_horizon_open_returns_none(self):
        row = {
            "thesis_id": "h3", "subject": "BTC hash rate", "claim": "increasing",
            "confidence": "medium", "created_at": self.now - timedelta(hours=1),
        }
        r = score_hashrate(row, (self.ms, self.values), horizon_hours=168, flat_pct=0.02, now=self.now)
        assert r is None


class TestScoreDifficulty:
    def setup_method(self):
        self.now = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
        # Three difficulty adjustment epochs.
        anchor = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        self.epochs = [
            {"time": int((anchor + timedelta(days=0)).timestamp()), "adjustment": 2.5},   # +2.5%
            {"time": int((anchor + timedelta(days=14)).timestamp()), "adjustment": -3.0}, # -3%
            {"time": int((anchor + timedelta(days=28)).timestamp()), "adjustment": 0.2},  # +0.2%
        ]

    def test_thesis_after_positive_adjustment(self):
        row = {
            "thesis_id": "d1", "subject": "Bitcoin mining difficulty", "claim": "increasing",
            "confidence": "high", "created_at": datetime(2026, 6, 5, tzinfo=timezone.utc),
        }
        r = score_difficulty(row, self.epochs, flat_pct=0.005, now=self.now)
        assert r is not None and r.observed == "up" and r.outcome == "hit"

    def test_thesis_after_negative_adjustment(self):
        row = {
            "thesis_id": "d2", "subject": "Bitcoin mining difficulty", "claim": "declining",
            "confidence": "medium", "created_at": datetime(2026, 6, 20, tzinfo=timezone.utc),
        }
        r = score_difficulty(row, self.epochs, flat_pct=0.005, now=self.now)
        assert r is not None and r.observed == "down" and r.outcome == "hit"

    def test_thesis_after_small_positive_is_flat(self):
        row = {
            "thesis_id": "d3", "subject": "Bitcoin mining difficulty", "claim": "stable",
            "confidence": "low", "created_at": datetime(2026, 6, 30, tzinfo=timezone.utc),
        }
        r = score_difficulty(row, self.epochs, flat_pct=0.005, now=self.now)
        # +0.2% within ±0.5% band → flat → matches "stable"
        assert r is not None and r.observed == "flat" and r.outcome == "hit"


class TestScoreSentiment:
    def setup_method(self):
        self.anchor = datetime(2026, 6, 1, tzinfo=timezone.utc)
        anchor_ms = int(self.anchor.timestamp() * 1000)
        # Alternating F&G values across 10 daily points.
        self.ms = [anchor_ms + d * 86400_000 for d in range(10)]
        self.values = [30.0, 65.0, 50.0, 20.0, 80.0, 45.0, 55.0, 70.0, 40.0, 50.0]
        self.now = self.anchor + timedelta(days=15)

    def test_bullish_hits_on_high_index(self):
        row = {
            "thesis_id": "s1", "subject": "Market sentiment", "claim": "bullish",
            "confidence": "medium", "created_at": self.anchor + timedelta(days=1),
        }
        r = score_sentiment(row, (self.ms, self.values), now=self.now)
        # day 1 = 65 → up → matches bullish → HIT
        assert r is not None and r.outcome == "hit"

    def test_bullish_misses_on_low_index(self):
        row = {
            "thesis_id": "s2", "subject": "Market sentiment", "claim": "bullish",
            "confidence": "high", "created_at": self.anchor + timedelta(days=3),
        }
        # day 3 = 20 → down → predicted up → MISS
        r = score_sentiment(row, (self.ms, self.values), now=self.now)
        assert r is not None and r.outcome == "miss"

    def test_cautious_hits_on_mid_range(self):
        row = {
            "thesis_id": "s3", "subject": "Market sentiment", "claim": "cautious",
            "confidence": "low", "created_at": self.anchor + timedelta(days=2),
        }
        # day 2 = 50 → mid → matches cautious → HIT
        r = score_sentiment(row, (self.ms, self.values), now=self.now)
        assert r is not None and r.outcome == "hit"


class TestAggregate:
    def _mk(self, tid, metric, conf, outcome, predicted="up"):
        from analysis.thesis_backtest_descriptive import DescScoredThesis
        return DescScoredThesis(
            thesis_id=tid, subject="s", claim="c", confidence=conf,
            metric=metric, predicted=predicted, observed="up",
            outcome=outcome,
        )

    def test_overall_and_buckets(self):
        recs = [
            self._mk("a", "btc_hashrate", "high", "hit"),
            self._mk("b", "btc_hashrate", "high", "miss"),
            self._mk("c", "btc_difficulty", "medium", "hit"),
            self._mk("d", "market_sentiment", "low", "hit"),
        ]
        agg = aggregate(recs)
        assert agg["overall"] == [("all", 4, 3, 0.75)]
        by_metric = {k: (n, h) for k, n, h, _ in agg["by_metric"]}
        assert by_metric["btc_hashrate"] == (2, 1)
        assert by_metric["btc_difficulty"] == (1, 1)
        by_conf = {k: (n, h) for k, n, h, _ in agg["by_confidence"]}
        assert by_conf["high"] == (2, 1)
        assert by_conf["medium"] == (1, 1)
        assert by_conf["low"] == (1, 1)
