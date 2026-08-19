"""Descriptive-thesis backtest — scores NON-directional theses against
the ground truth from the source that generated them.

Directional scorer (thesis_backtest.py) only touches "BTC short-term
price → up/down/flat". This module scores the OTHER classes: claims
about non-price metrics that a data source reports historically —
hashrate, mining difficulty, sentiment (Fear & Greed index).

Design:
    - Triage every non-directional thesis into one of:
        VERIFIABLE-METRIC : subject maps to a reachable historical metric
        VERIFIABLE-RELATION : claim asserts a checkable co-occurrence
                              (kept as a separate bucket; not scored here
                              because most reduce to two metric-lookups
                              and the corpus has almost none)
        UNVERIFIABLE : subjective / no reachable history / conceptually
                       wrong (e.g. "Ethereum hash rate" — ETH is PoS)
    - For each VERIFIABLE-METRIC thesis, extract the asserted class
      (up/down/flat, or sentiment bucket) and compare to the metric's
      observed value nearest the thesis timestamp.
    - Conservative parser: ambiguous → unresolvable, don't force.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from analysis.thesis_backtest import parse_direction  # shared polarity parser

Verifiability = Literal["metric", "relation", "unverifiable"]
Outcome = Literal["hit", "miss", "skip"]
MetricKind = Literal[
    "btc_hashrate", "btc_difficulty", "market_sentiment",
    "btc_market_cap", "eth_market_cap",
    "btc_volume", "eth_volume",
    "eth_gas", "btc_funding",
]

# Subject → metric mapping. Kept explicit (not a regex tangle) because
# the vocabulary is small and stable — grown from the 248-row dump.
_HASHRATE_SUBJECTS = ("btc hash rate", "bitcoin hash rate")
_DIFFICULTY_SUBJECTS = (
    "bitcoin mining difficulty",
    "btc mining difficulty",
    "bitcoin difficulty",
    "btc difficulty",
)
_SENTIMENT_SUBJECTS = ("market sentiment",)
# Volume/mkt-cap subject discrimination: BTC/ETH-specific → CoinGecko
# market_chart is authoritative. "Global"/"Crypto" → the whole-market
# figure needs CoinGecko Pro (paid), so stays UNREACHABLE. A BTC-proxy
# for global would silently produce wrong hit-rates — deliberately not done.
_BTC_MKTCAP_MARKERS = ("btc market cap", "btc's market cap", "bitcoin market cap")
_ETH_MKTCAP_MARKERS = ("ethereum market cap", "eth market cap")
_BTC_VOLUME_MARKERS = (
    "btc 24-hour volume", "btc trading volume", "btc transaction volume",
    "btc short-term trading volume", "bitcoin volume", "btc volume",
    "24-hour trading volume for btc", "24-hour volume for btc",
)
_ETH_VOLUME_MARKERS = (
    "ethereum volume", "ethereum trading volume", "ethereum 24h volume",
    "eth volume", "eth trading volume",
)
_ETH_GAS_MARKERS = ("gas price", "gas prices", "ethereum gas")
_BTC_FUNDING_MARKERS = (
    "bitcoin futures funding", "btc futures funding", "funding rate",
    "funding rates",
)

# Subjects for which the underlying quantity has NO cheap historical
# source (probed 2026-08-19; see docs/BACKTEST_HISTORICAL_SOURCES.md).
# Kept separately from truly-subjective claims — this is triage class
# "verifiable in principle, unreachable in practice".
_UNREACHABLE_SUBJECT_MARKERS = (
    "dominance",       # CoinGecko /global/market_cap_chart is Pro-only (401)
    "long-short",      # no free historical endpoint checked
    "mining profitability",  # composite (revenue × cost) — not one series
    "on-chain metrics",  # too vague to map to a single metric
    # Global/whole-market mkt cap and volume specifically:
    "global market cap", "global market capitalization",
    "global crypto market cap", "global crypto market capitalization",
    "crypto market cap", "crypto market capitalization",
    "market capitalization of all cryptocurrencies",
    "market capitalization of cryptocurrencies",
    "global crypto market volume", "global market capitalization volume",
    "crypto market volume", "crypto 24-hour trading volume",
    "cryptocurrency market",
)

# Sentiment vocabulary → F&G bucket. The F&G index is 0-100:
#   0-24  extreme fear    25-44 fear    45-55 neutral
#   56-74 greed           75-100 extreme greed
# We collapse into low/mid/high (3 buckets) so the granularity matches
# the claim vocabulary (which never gets finer than "cautious"/"greedy").
_SENTIMENT_UP = ("bullish", "positive", "greed", "optim", "risk-on", "euphor")
_SENTIMENT_DOWN = ("bearish", "negative", "fear", "panic", "pessim", "risk-off")
_SENTIMENT_MID = ("cautious", "neutral", "mixed", "uncertain", "balanced")


def classify_subject(subject: str) -> tuple[Verifiability, MetricKind | None, str | None]:
    """Return (verifiability, metric_or_None, unreachable_reason_or_None).

    unreachable_reason is set only when verifiability=='unverifiable' AND
    the subject IS about an objective metric — just one we can't fetch.
    Used by the CLI to separate "we don't have a source" from "the claim
    is genuinely subjective/vague".
    """
    if not subject:
        return "unverifiable", None, "empty subject"
    s = subject.lower()
    # Ethereum "hash rate" is metaphysically wrong post-merge (PoS, no
    # hashrate). Not a data-availability issue; the metric doesn't exist.
    if "ethereum" in s and "hash rate" in s:
        return "unverifiable", None, "eth hash rate does not exist post-merge"
    # Relation keywords in the SUBJECT take precedence over metric mapping:
    # "Bitcoin difficulty adjustments correlate with BTC price" is a
    # relation claim (the correlation itself), not a level claim on
    # difficulty. Metric-only match would score the wrong thing.
    if any(kw in s for kw in (" and ", " while ", "vs", " between ", "correlat")):
        return "relation", None, None
    if any(m in s for m in _HASHRATE_SUBJECTS):
        return "metric", "btc_hashrate", None
    if any(m in s for m in _DIFFICULTY_SUBJECTS) and "adjustment progress" not in s:
        return "metric", "btc_difficulty", None
    if any(m in s for m in _SENTIMENT_SUBJECTS):
        return "metric", "market_sentiment", None
    # Whole-market unreachables checked FIRST (a "global market cap" subject
    # also contains the bare "market cap" substring — order matters).
    for marker in _UNREACHABLE_SUBJECT_MARKERS:
        if marker in s:
            return "unverifiable", None, f"no free historical source for '{marker}'"
    if any(m in s for m in _BTC_MKTCAP_MARKERS):
        return "metric", "btc_market_cap", None
    if any(m in s for m in _ETH_MKTCAP_MARKERS):
        return "metric", "eth_market_cap", None
    if any(m in s for m in _BTC_VOLUME_MARKERS):
        return "metric", "btc_volume", None
    if any(m in s for m in _ETH_VOLUME_MARKERS):
        return "metric", "eth_volume", None
    if any(m in s for m in _ETH_GAS_MARKERS):
        return "metric", "eth_gas", None
    if any(m in s for m in _BTC_FUNDING_MARKERS):
        return "metric", "btc_funding", None
    # Bare "market cap" / "market capitalization" with no BTC/ETH/global
    # qualifier — most likely means "the market", so treat as unreachable.
    if "market cap" in s or "market capitalization" in s:
        return "unverifiable", None, "no free historical source for 'market cap' (bare — likely global)"
    if "market volume" in s or ("volume" in s and "btc" not in s and "ethereum" not in s and "eth " not in s):
        return "unverifiable", None, "no free historical source for 'market volume' (bare — likely global)"
    return "unverifiable", None, "subjective / no metric mapping"


def parse_sentiment(claim: str) -> Literal["up", "down", "mid"] | None:
    """Bucket a sentiment claim into up/mid/down; None if not clearly one."""
    if not claim:
        return None
    c = claim.lower()
    up = any(w in c for w in _SENTIMENT_UP)
    down = any(w in c for w in _SENTIMENT_DOWN)
    mid = any(w in c for w in _SENTIMENT_MID)
    hits = sum([up, down, mid])
    if hits != 1:
        return None
    return "up" if up else ("down" if down else "mid")


def bucket_sentiment_value(fng_index: float) -> Literal["up", "mid", "down"]:
    """Map a 0-100 F&G index to the same 3-way bucket used for claims."""
    if fng_index >= 56:
        return "up"
    if fng_index <= 44:
        return "down"
    return "mid"


def nearest_value(sorted_ms: list[int], values: list[float], ts_ms: int) -> float | None:
    """Same lookup as the directional scorer, returned separately so this
    module doesn't reach into the other's internals."""
    if not sorted_ms:
        return None
    if ts_ms < sorted_ms[0] or ts_ms > sorted_ms[-1]:
        return None
    idx = bisect_left(sorted_ms, ts_ms)
    if idx == 0:
        return values[0]
    if idx == len(sorted_ms):
        return values[-1]
    before, after = sorted_ms[idx - 1], sorted_ms[idx]
    return values[idx - 1] if (ts_ms - before) <= (after - ts_ms) else values[idx]


def most_recent_before(sorted_ms: list[int], values: list[float], ts_ms: int) -> float | None:
    """For epoch-style series (difficulty adjustments): the last observed
    value at or before ts_ms. Different from nearest_value because a
    thesis stamped mid-epoch describes the *just-past* adjustment."""
    if not sorted_ms or ts_ms < sorted_ms[0]:
        return None
    idx = bisect_left(sorted_ms, ts_ms + 1)  # rightmost idx with ms <= ts_ms
    if idx == 0:
        return None
    return values[idx - 1]


def bucket_change(before: float, after: float, flat_pct: float) -> Literal["up", "down", "flat"]:
    """Sign-with-flat-band on a % change (before → after)."""
    if before <= 0:
        return "flat"
    ret = (after - before) / before
    if abs(ret) <= flat_pct:
        return "flat"
    return "up" if ret > 0 else "down"


@dataclass(frozen=True)
class DescScoredThesis:
    thesis_id: str
    subject: str
    claim: str
    confidence: str
    metric: MetricKind
    predicted: str  # up|down|flat|mid
    observed: str
    outcome: Outcome


def triage(
    rows: Iterable[dict],
) -> tuple[
    dict[str, int],
    list[tuple[str, str, MetricKind]],
    list[str],
    list[str],
]:
    """Return (bucket_counts, verifiable_metric_rows, unreachable_reasons, subjective_subjects).

    verifiable_metric_rows carries (subject, claim, metric) so the caller
    doesn't have to re-classify to score.
    """
    counts = {"metric": 0, "relation": 0, "unreachable": 0, "subjective": 0, "input": 0}
    metric_rows: list[tuple[str, str, MetricKind]] = []
    unreachable_reasons: list[str] = []
    subjective_subjects: list[str] = []
    for row in rows:
        counts["input"] += 1
        subj = str(row.get("subject", ""))
        claim = str(row.get("claim", ""))
        verifiability, metric, reason = classify_subject(subj)
        if verifiability == "metric" and metric is not None:
            counts["metric"] += 1
            metric_rows.append((subj, claim, metric))
        elif verifiability == "relation":
            counts["relation"] += 1
        else:
            if reason and "no free historical source" in reason or (reason and "does not exist" in reason):
                counts["unreachable"] += 1
                unreachable_reasons.append(reason)
            else:
                counts["subjective"] += 1
                subjective_subjects.append(subj)
    return counts, metric_rows, unreachable_reasons, subjective_subjects


def score_hashrate(
    row: dict,
    hashrate_series: tuple[list[int], list[float]],
    horizon_hours: int,
    flat_pct: float,
    now: datetime,
) -> DescScoredThesis | None:
    """Score a hashrate direction claim against mempool's hashrate history."""
    predicted = parse_direction(str(row.get("claim", "")))
    if predicted is None:
        return None
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    target = created_at + timedelta(hours=horizon_hours)
    if target > now:
        return None
    sorted_ms, values = hashrate_series
    p0_ms = int(created_at.timestamp() * 1000)
    p1_ms = int(target.timestamp() * 1000)
    v0 = nearest_value(sorted_ms, values, p0_ms)
    v1 = nearest_value(sorted_ms, values, p1_ms)
    if v0 is None or v1 is None:
        return None
    observed = bucket_change(v0, v1, flat_pct)
    return DescScoredThesis(
        thesis_id=str(row.get("thesis_id", "")),
        subject=str(row.get("subject", "")),
        claim=str(row.get("claim", "")),
        confidence=str(row.get("confidence", "")),
        metric="btc_hashrate",
        predicted=predicted,
        observed=observed,
        outcome="hit" if observed == predicted else "miss",
    )


def score_difficulty(
    row: dict,
    difficulty_epochs: list[dict],
    flat_pct: float,
    now: datetime,
) -> DescScoredThesis | None:
    """Score a difficulty direction claim using mempool's difficulty epochs.

    Each epoch carries an `adjustment` field: signed % change vs previous
    epoch. We pick the epoch whose adjustment took effect at or before
    the thesis timestamp — that's what Morgoth *observed*, so that's
    what the claim is describing.
    """
    predicted = parse_direction(str(row.get("claim", "")))
    if predicted is None:
        return None
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at > now:
        return None
    ts_ms = int(created_at.timestamp() * 1000)
    epochs = sorted(difficulty_epochs, key=lambda e: e["time"])
    sorted_ms = [int(e["time"]) * 1000 for e in epochs]
    adjustments = [float(e["adjustment"]) / 100.0 for e in epochs]  # % → fraction
    ret = most_recent_before(sorted_ms, adjustments, ts_ms)
    if ret is None:
        return None
    if abs(ret) <= flat_pct:
        observed = "flat"
    else:
        observed = "up" if ret > 0 else "down"
    return DescScoredThesis(
        thesis_id=str(row.get("thesis_id", "")),
        subject=str(row.get("subject", "")),
        claim=str(row.get("claim", "")),
        confidence=str(row.get("confidence", "")),
        metric="btc_difficulty",
        predicted=predicted,
        observed=observed,
        outcome="hit" if observed == predicted else "miss",
    )


def score_sentiment(
    row: dict,
    fng_series: tuple[list[int], list[float]],
    now: datetime,
) -> DescScoredThesis | None:
    """Score a sentiment claim against the F&G index at the thesis timestamp."""
    predicted = parse_sentiment(str(row.get("claim", "")))
    if predicted is None:
        return None
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at > now:
        return None
    sorted_ms, values = fng_series
    v = nearest_value(sorted_ms, values, int(created_at.timestamp() * 1000))
    if v is None:
        return None
    observed = bucket_sentiment_value(v)
    return DescScoredThesis(
        thesis_id=str(row.get("thesis_id", "")),
        subject=str(row.get("subject", "")),
        claim=str(row.get("claim", "")),
        confidence=str(row.get("confidence", "")),
        metric="market_sentiment",
        predicted=predicted,
        observed=observed,
        outcome="hit" if observed == predicted else "miss",
    )


# ─────────────────────────────────────────────────────────────────────────
# LEVEL-CLAIM SCORER
#
# Rationale: claude-cli generates ~74 % LEVEL claims ("dominance is high")
# following the grounding prompt's "observational over predictive" rule.
# The 8B still emits ~24 level claims total across the whole corpus.
# Directional-only scoring returns None for all of these — a coverage
# gap, not a model failure. This scorer fills it.
#
# Window: 30 days ending AT (not after) the thesis timestamp. A claim
# "high" implicitly means "vs recent normal", not "vs all-time" — 30d
# captures a recent regime and gives enough samples for stable
# percentiles (30 daily samples / 90 8h-funding samples / 720 hourly
# mkt-cap samples). Sensitivity: a claim borderline on a 30d percentile
# is likely borderline on 60d too; extreme claims are stable.
#
# NO-LOOKAHEAD GUARANTEE (critical): the percentile distribution is
# computed EXCLUSIVELY from values whose timestamp is STRICTLY BEFORE
# the thesis's created_at. Using post-thesis data would let the scorer
# "know" what happened next — invalid for backtest scoring. Enforced
# by the strict `< ts_ms` slice below; grep-locked in tests.
# ─────────────────────────────────────────────────────────────────────────

_LEVEL_WINDOW_DAYS = 30
_LEVEL_HIGH_PCT = 0.75
_LEVEL_LOW_PCT = 0.25

# Vocabulary → band. Grown from ACTUAL corpus frequencies (8B + claude-cli
# experiment): "high" 28, "low" 17, "elevated"/"depressed"/"strong"/"weak"
# 0 (unused by either model). Included the synonyms for future-proofing
# but they contribute nothing at current sample sizes.
_LEVEL_HIGH_WORDS = ("high", "elevated", "strong")
_LEVEL_LOW_WORDS = ("low", "depressed", "weak")


def parse_level(claim: str) -> Literal["high", "low"] | None:
    """Extract level from a claim. Returns None if not cleanly high/low
    or if both words appear (contradictory)."""
    if not claim:
        return None
    c = claim.lower()
    is_high = any(w in c for w in _LEVEL_HIGH_WORDS)
    is_low = any(w in c for w in _LEVEL_LOW_WORDS)
    if is_high and is_low:
        return None  # "low high"? contradictory / hedge
    if is_high:
        return "high"
    if is_low:
        return "low"
    return None


def _percentile(sorted_values: list[float], p: float) -> float:
    """Simple linear-interp percentile on a pre-sorted list. p ∈ [0, 1]."""
    n = len(sorted_values)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_values[0]
    idx = p * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def score_level(
    row: dict,
    series: tuple[list[int], list[float]] | None,
    metric: MetricKind,
    now: datetime,
    window_days: int = _LEVEL_WINDOW_DAYS,
) -> DescScoredThesis | None:
    """Score a level claim ('high'/'low') against the metric's percentile
    distribution over the `window_days` PRIOR to the thesis timestamp.

    Contract:
      · NO LOOKAHEAD — distribution uses only samples strictly before ts.
      · No fabrication — missing series / too-few-samples / unmapped
        vocabulary → return None (SKIP).
      · HIT iff observed percentile is in the claimed band
        (high = ≥ 75th, low = ≤ 25th).
    """
    predicted = parse_level(str(row.get("claim", "")))
    if predicted is None:
        return None
    if not series:
        return None
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if created_at > now:
        return None
    ts_ms = int(created_at.timestamp() * 1000)
    window_start_ms = int((created_at - timedelta(days=window_days)).timestamp() * 1000)
    sorted_ms, values = series
    # STRICT < ts_ms: no data at or after the thesis. This is the
    # no-lookahead guarantee — the scorer can only "see" what Morgoth
    # could have seen at the moment of the claim.
    window_vals = [
        v for m, v in zip(sorted_ms, values)
        if window_start_ms <= m < ts_ms
    ]
    if len(window_vals) < 5:  # too few samples for a stable percentile
        return None
    observed_now = nearest_value(sorted_ms, values, ts_ms)
    if observed_now is None:
        return None
    sw = sorted(window_vals)
    high_thr = _percentile(sw, _LEVEL_HIGH_PCT)
    low_thr = _percentile(sw, _LEVEL_LOW_PCT)
    if observed_now >= high_thr:
        observed_band: Literal["high", "low", "mid"] = "high"
    elif observed_now <= low_thr:
        observed_band = "low"
    else:
        observed_band = "mid"
    return DescScoredThesis(
        thesis_id=str(row.get("thesis_id", "")),
        subject=str(row.get("subject", "")),
        claim=str(row.get("claim", "")),
        confidence=str(row.get("confidence", "")),
        metric=metric,
        predicted=predicted,
        observed=observed_band,
        outcome="hit" if observed_band == predicted else "miss",
    )


def _score_series_directional(
    row: dict,
    series: tuple[list[int], list[float]] | None,
    horizon_hours: int,
    flat_pct: float,
    now: datetime,
    metric: MetricKind,
) -> DescScoredThesis | None:
    """Generic timeseries-directional scorer used by all new metrics.

    Graceful degrade: series=None (fetcher failed) → return None (SKIP).
    Missing data at t0 or t1 → None (SKIP). NEVER fabricates a zero.
    """
    predicted = parse_direction(str(row.get("claim", "")))
    if predicted is None:
        return None
    if not series:
        return None
    created_at = row["created_at"]
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    target = created_at + timedelta(hours=horizon_hours)
    if target > now:
        return None
    sorted_ms, values = series
    v0 = nearest_value(sorted_ms, values, int(created_at.timestamp() * 1000))
    v1 = nearest_value(sorted_ms, values, int(target.timestamp() * 1000))
    if v0 is None or v1 is None:
        return None
    observed = bucket_change(v0, v1, flat_pct)
    return DescScoredThesis(
        thesis_id=str(row.get("thesis_id", "")),
        subject=str(row.get("subject", "")),
        claim=str(row.get("claim", "")),
        confidence=str(row.get("confidence", "")),
        metric=metric,
        predicted=predicted,
        observed=observed,
        outcome="hit" if observed == predicted else "miss",
    )


def score_market_cap(row, series, horizon_hours, flat_pct, now, asset):
    return _score_series_directional(
        row, series, horizon_hours, flat_pct, now,
        metric="btc_market_cap" if asset == "bitcoin" else "eth_market_cap",
    )


def score_volume(row, series, horizon_hours, flat_pct, now, asset):
    return _score_series_directional(
        row, series, horizon_hours, flat_pct, now,
        metric="btc_volume" if asset == "bitcoin" else "eth_volume",
    )


def score_gas(row, series, horizon_hours, flat_pct, now):
    return _score_series_directional(
        row, series, horizon_hours, flat_pct, now, metric="eth_gas",
    )


def score_funding(row, series, horizon_hours, flat_pct, now):
    """Funding claims can be directional ('increasing') OR level ('low', 'high').
    We only score directional here; level claims — 'low' / 'high' — fall
    through to None (skip) rather than force a hit against an arbitrary
    threshold. Prevents fabricated hit-rate on level claims that don't map
    cleanly to a % change."""
    claim = str(row.get("claim", "")).lower()
    if any(w in claim for w in ("low", "high")) and not any(
        w in claim for w in ("increas", "decreas", "declin", "rising", "falling")
    ):
        return None
    return _score_series_directional(
        row, series, horizon_hours, flat_pct, now, metric="btc_funding",
    )


def aggregate(records: list[DescScoredThesis]) -> dict[str, list[tuple]]:
    """Same shape as the directional aggregator so the CLI can reuse render."""

    def _tally(key_fn) -> list[tuple]:
        buckets: dict[str, list[int]] = {}
        for r in records:
            key = key_fn(r)
            b = buckets.setdefault(key, [0, 0])
            b[0] += 1
            if r.outcome == "hit":
                b[1] += 1
        out = []
        for key, (n, hits) in sorted(buckets.items(), key=lambda kv: (-kv[1][0], kv[0])):
            rate = hits / n if n else 0.0
            out.append((key, n, hits, rate))
        return out

    n = len(records)
    hits = sum(1 for r in records if r.outcome == "hit")
    return {
        "overall": [("all", n, hits, hits / n if n else 0.0)],
        "by_metric": _tally(lambda r: r.metric),
        "by_confidence": _tally(lambda r: r.confidence or "(unset)"),
        "by_predicted": _tally(lambda r: r.predicted),
    }
