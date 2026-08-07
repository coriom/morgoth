"""Unit tests for the read-only thesis calibration backtest.

Covers:
    - direction polarity parsing (keyword sets + ambiguous→None)
    - subject → asset resolution
    - price-move → direction bucketing
    - hit / miss / skip outcome logic
    - nearest_price lookup (in-window, boundary, out-of-window)
    - end-to-end resolve_theses with mocked prices (no HTTP, no DB)
    - aggregate() correctness (per-bucket counts + hit-rate)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from analysis.thesis_backtest import (
    aggregate,
    bucket_actual,
    evidence_source,
    nearest_price,
    parse_direction,
    resolve_theses,
    score,
    subject_asset,
)


class TestParseDirection:
    @pytest.mark.parametrize(
        "claim, expected",
        [
            ("declining", "down"),
            ("Declining", "down"),
            ("modestly declining", "down"),
            ("bearish", "down"),
            ("correcting", "down"),
            ("increasing", "up"),
            ("mildly increasing", "up"),
            ("bullish", "up"),
            ("growing", "up"),
            ("stable", "flat"),
            ("volatile", None),  # ambiguous
            ("fluctuating", None),  # ambiguous
            ("uncorrelated with high Ethereum hash rate", None),
            ("not reflected in price over last 24 hours", None),
            ("inaccurate", None),
            ("", None),
        ],
    )
    def test_polarity(self, claim, expected):
        assert parse_direction(claim) == expected


class TestParseDirectionHardening:
    """Post-audit hardening cases from the first 91-row live run."""

    def test_unstable_is_not_flat(self):
        # First run parsed "unstable" as flat via substring match on "stable".
        assert parse_direction("unstable") is None

    def test_hedged_or_split_is_unresolvable(self):
        # "stable or slightly increasing" — hedged between two classes.
        assert parse_direction("stable or slightly increasing") is None
        assert parse_direction("bullish or bearish") is None
        assert parse_direction("may rise or fall") is None

    def test_contradictory_up_and_down_words_are_unresolvable(self):
        # "positive (short-term correction)" — up + down in same claim.
        assert parse_direction("positive (short-term correction)") is None

    def test_conditional_is_unresolvable(self):
        assert parse_direction("if BTC breaks 60k then rises") is None
        assert parse_direction("depends on macro conditions") is None
        assert parse_direction("may rise") is None
        assert parse_direction("might decline") is None

    def test_negation_flips_polarity(self):
        assert parse_direction("not rising") == "down"
        assert parse_direction("unlikely to decline") == "up"
        assert parse_direction("will not fall") == "up"
        assert parse_direction("fails to rise") == "down"

    def test_negation_of_flat_is_unresolvable(self):
        # "not stable" isn't cleanly up or down — flip meaning is undefined.
        assert parse_direction("not stable") is None

    def test_bare_polarity_words_still_work(self):
        # Sanity: hardening didn't break the common case.
        assert parse_direction("declining") == "down"
        assert parse_direction("bullish") == "up"
        assert parse_direction("stable") == "flat"


class TestSubjectAsset:
    @pytest.mark.parametrize(
        "subject, expected",
        [
            ("BTC short-term price", "bitcoin"),
            ("Bitcoin short-term price", "bitcoin"),
            ("ETH short-term price", "ethereum"),
            ("Ethereum short-term price", "ethereum"),
            ("Ethereum hash rate", None),  # no 'price' → not scored
            # Hardened: gas prices are gwei, not USD spot — excluded.
            ("Ethereum gas prices", None),
            ("Ethereum gas price", None),
            # Hardened: long-term theses can't be scored at 24h.
            ("BTC long-term price stability", None),
            ("BTC long-term price trends", None),
            # Hardened: non-price crypto metrics.
            ("Bitcoin dominance percentage", None),
            ("Bitcoin futures funding rate", None),
            ("U.S. GDP growth", None),
            ("", None),
        ],
    )
    def test_resolution(self, subject, expected):
        assert subject_asset(subject) == expected


class TestBucketAndScore:
    def test_bucket_flat_within_band(self):
        assert bucket_actual(100.0, 100.5, 0.01) == "flat"
        assert bucket_actual(100.0, 99.5, 0.01) == "flat"

    def test_bucket_up_beyond_band(self):
        assert bucket_actual(100.0, 102.0, 0.01) == "up"

    def test_bucket_down_beyond_band(self):
        assert bucket_actual(100.0, 98.0, 0.01) == "down"

    def test_score_hit(self):
        assert score("down", 100.0, 95.0, 0.01) == "hit"
        assert score("up", 100.0, 105.0, 0.01) == "hit"
        assert score("flat", 100.0, 100.3, 0.01) == "hit"

    def test_score_miss(self):
        assert score("up", 100.0, 95.0, 0.01) == "miss"
        assert score("flat", 100.0, 110.0, 0.01) == "miss"

    def test_score_skip_on_missing_price(self):
        assert score("up", None, 100.0, 0.01) == "skip"
        assert score("up", 100.0, None, 0.01) == "skip"
        assert score("up", 0.0, 100.0, 0.01) == "skip"


class TestNearestPrice:
    def test_empty_series(self):
        assert nearest_price([], [], 1_000) is None

    def test_out_of_window_low(self):
        assert nearest_price([100, 200], [1.0, 2.0], 50) is None

    def test_out_of_window_high(self):
        assert nearest_price([100, 200], [1.0, 2.0], 500) is None

    def test_exact_match(self):
        assert nearest_price([100, 200, 300], [1.0, 2.0, 3.0], 200) == 2.0

    def test_picks_closer(self):
        # 140 → closer to 100 than 200
        assert nearest_price([100, 200], [1.0, 2.0], 140) == 1.0
        # 160 → closer to 200 than 100
        assert nearest_price([100, 200], [1.0, 2.0], 160) == 2.0


class TestEvidenceSource:
    def test_none_or_empty(self):
        assert evidence_source(None) == "(none)"
        assert evidence_source([]) == "(none)"

    def test_first_source(self):
        assert evidence_source([{"source": "get_crypto_price", "detail": "x"}]) == "get_crypto_price"

    def test_missing_source_key(self):
        assert evidence_source([{"detail": "x"}]) == "(none)"


def _mk_series(base_ts_ms: int, points: list[tuple[int, float]]):
    """Build (sorted_ms, prices) from offsets in HOURS."""
    ms = [base_ts_ms + h * 3600_000 for h, _ in points]
    return ms, [p for _, p in points]


class TestResolveTheses:
    def setup_method(self):
        # Anchor everything at 2026-08-01 00:00 UTC — deterministic.
        self.anchor = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        self.anchor_ms = int(self.anchor.timestamp() * 1000)
        # 4-day BTC series: flat at 100 for two days, then +10 %
        self.btc = _mk_series(
            self.anchor_ms,
            [(0, 100.0), (24, 100.5), (48, 110.0), (72, 115.0), (96, 116.0)],
        )
        self.eth = _mk_series(self.anchor_ms, [(0, 5.0), (24, 5.05), (48, 5.10), (72, 5.20)])
        self.series = {"bitcoin": self.btc, "ethereum": self.eth}

    def test_end_to_end_hit_miss_skip(self):
        # Freeze "now" 5 days after anchor so all 24h horizons have elapsed.
        now = self.anchor + timedelta(days=5)
        rows = [
            # 1) BTC declining, created at anchor: actual +0.5% → flat → MISS
            {
                "thesis_id": "t1",
                "subject": "BTC short-term price",
                "claim": "declining",
                "confidence": "high",
                "created_at": self.anchor,
                "evidence": [{"source": "get_crypto_price", "detail": "x"}],
            },
            # 2) BTC increasing, created at anchor+24h: 100.5 → 110 → +9.4% → up → HIT
            {
                "thesis_id": "t2",
                "subject": "BTC short-term price",
                "claim": "increasing",
                "confidence": "medium",
                "created_at": self.anchor + timedelta(hours=24),
                "evidence": [{"source": "get_crypto_price", "detail": "x"}],
            },
            # 3) ETH stable, anchor+24h → 5.05→5.10 = +0.99% → flat → HIT
            {
                "thesis_id": "t3",
                "subject": "Ethereum short-term price",
                "claim": "stable",
                "confidence": "low",
                "created_at": self.anchor + timedelta(hours=24),
                "evidence": [{"source": "get_crypto_history", "detail": "y"}],
            },
            # 4) Non-price subject → skipped (non_price)
            {
                "thesis_id": "t4",
                "subject": "Ethereum hash rate",
                "claim": "high",
                "confidence": "medium",
                "created_at": self.anchor,
                "evidence": [],
            },
            # 5) Ambiguous claim → skipped (no_direction)
            {
                "thesis_id": "t5",
                "subject": "BTC short-term price",
                "claim": "volatile",
                "confidence": "low",
                "created_at": self.anchor,
                "evidence": [],
            },
            # 6) Horizon not elapsed yet (created 12h before "now") → horizon_open
            {
                "thesis_id": "t6",
                "subject": "BTC short-term price",
                "claim": "declining",
                "confidence": "high",
                "created_at": now - timedelta(hours=12),
                "evidence": [],
            },
        ]
        records, counts = resolve_theses(
            rows, self.series, horizon_hours=24, flat_band=0.01, now=now
        )
        assert counts["input"] == 6
        assert counts["non_price"] == 1  # t4
        assert counts["no_direction"] == 1  # t5
        assert counts["horizon_open"] == 1  # t6
        assert counts["scored"] == 3  # t1, t2, t3
        outcomes = {r.thesis_id: r.outcome for r in records}
        assert outcomes == {"t1": "miss", "t2": "hit", "t3": "hit"}


class TestMultiHorizonNearestPrice:
    """Same series, same theses, different horizons — verify the nearest_price
    lookup keeps working correctly when P1 lands deep inside the series."""

    def test_p1_moves_with_horizon(self):
        # Build a 10-day daily-sample series priced at 100 + day_idx * 5.
        anchor = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        anchor_ms = int(anchor.timestamp() * 1000)
        ms = [anchor_ms + d * 86400_000 for d in range(10)]
        prices = [100.0 + d * 5.0 for d in range(10)]
        series = {"bitcoin": (ms, prices)}
        row = {
            "thesis_id": "t",
            "subject": "BTC short-term price",
            "claim": "increasing",
            "confidence": "medium",
            "created_at": anchor,
            "evidence": [{"source": "get_crypto_price"}],
        }
        now = anchor + timedelta(days=15)
        # 24h horizon: P0=100, P1=105 → +5% → up → HIT
        recs24, _ = resolve_theses([row], series, horizon_hours=24, flat_band=0.01, now=now)
        assert recs24[0].outcome == "hit"
        assert recs24[0].p0 == 100.0 and recs24[0].p1 == 105.0
        # 7d horizon: P0=100, P1=135 → +35% → up → HIT
        recs7d, _ = resolve_theses([row], series, horizon_hours=168, flat_band=0.026, now=now)
        assert recs7d[0].outcome == "hit"
        assert recs7d[0].p0 == 100.0 and recs7d[0].p1 == 135.0

    def test_horizon_outside_window_skips(self):
        anchor = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
        anchor_ms = int(anchor.timestamp() * 1000)
        # Only 2 days of data.
        ms = [anchor_ms, anchor_ms + 86400_000]
        series = {"bitcoin": (ms, [100.0, 101.0])}
        row = {
            "thesis_id": "t",
            "subject": "BTC short-term price",
            "claim": "increasing",
            "confidence": "medium",
            "created_at": anchor,
            "evidence": [],
        }
        now = anchor + timedelta(days=30)
        # 7-day horizon → P1 target outside window → no_price bucket, not scored.
        recs, counts = resolve_theses([row], series, horizon_hours=168, flat_band=0.026, now=now)
        assert recs == []
        assert counts["no_price"] == 1


class TestAggregate:
    def _mk(self, thesis_id, subject, claim, confidence, source, outcome):
        # Bypass resolve_theses — construct ScoredThesis directly via kw.
        from analysis.thesis_backtest import ScoredThesis
        return ScoredThesis(
            thesis_id=thesis_id,
            subject=subject,
            claim=claim,
            confidence=confidence,
            asset="bitcoin",
            predicted="up",
            created_at=datetime(2026, 8, 1),
            horizon_hours=24,
            p0=100.0,
            p1=101.0,
            outcome=outcome,
            source=source,
        )

    def test_overall_and_buckets(self):
        recs = [
            self._mk("a", "BTC short-term price", "increasing", "high", "get_crypto_price", "hit"),
            self._mk("b", "BTC short-term price", "increasing", "high", "get_crypto_price", "miss"),
            self._mk("c", "ETH short-term price", "increasing", "low", "get_crypto_price", "hit"),
            self._mk("d", "BTC short-term price", "increasing", "medium", "get_crypto_history", "miss"),
        ]
        agg = aggregate(recs)
        assert agg["overall"] == [("all", 4, 2, 0.5)]
        # by_subject sorted by n desc — BTC (3) before ETH (1)
        by_subj = agg["by_subject"]
        assert by_subj[0][0] == "BTC short-term price"
        assert by_subj[0][1:] == (3, 1, pytest.approx(1 / 3))
        assert by_subj[1] == ("ETH short-term price", 1, 1, 1.0)
        # by_confidence: high=2 (1 hit), low=1 (1 hit), medium=1 (0)
        by_conf = {k: (n, h, r) for k, n, h, r in agg["by_confidence"]}
        assert by_conf["high"] == (2, 1, 0.5)
        assert by_conf["low"] == (1, 1, 1.0)
        assert by_conf["medium"] == (1, 0, 0.0)
