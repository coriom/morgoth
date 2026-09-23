"""Campaign quality scorer — READ-ONLY.

Classifies every thesis in a campaign into six error classes so two
campaigns (llama-synthesis vs claude-cli-synthesis) can be compared on
identical criteria. No writes, no rewrites — the operator reads and acts.

Six classes (each independent; a thesis can hit more than one):

  1. scope_misattribution   — get_crypto_global_market value cited under
     a subject that names a BTC-specific metric that global-market
     doesn't actually publish (e.g. "BTC 24h trading volume" — global
     is the total crypto volume, not BTC's).
  2. field_confusion        — reuses core.field_confusion classifier.
     A cited number matches a DIFFERENT field of the same tool.
  3. direction_error        — claim direction contradicts the metric's
     unambiguous convention (longShortRatio < 1 called "bullish", etc.).
     Only unambiguous rules; ambiguous cases skipped.
  4. cross_subject_value    — cited value matches no field of the named
     source, but matches a field of a DIFFERENT source. Numeric
     collision on wrong tool.
  5. unsourced_subject      — subject names a metric no rail tool
     publishes AND the thesis cites no web_search/news evidence.
  6. subject_fragmentation  — multiple canonical_subjects that describe
     the same underlying metric. Reported per campaign as the count of
     extra variants (variants − 1) summed over metric families.

Also computes:

  · genuine_vs_confused_00010000 — for lastFundingRate=0.0001 citations,
    compares against Binance /fapi/v1/fundingRate at the thesis
    timestamp. 0.0001 is the Binance interest-rate clamp when the
    premium is small — many genuine cases exist.
  · angle_serviceability — for each objective title, is at least one
    rail tool likely to have the data? "Unserviceable" = no rail
    tool fields keyword-match the title.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.field_confusion import (
    FIELD_PHRASES,
    classify_thesis_evidence,
    iter_numbers_with_context,
    parse_findings_payloads,
    phrase_to_field,
)


# Rail tools: what NUMERIC fields they publish. Used for cross-source
# collision detection and scope checks. Non-rail sources (web_search,
# get_news) have no fields — they carry qualitative content only.
RAIL_TOOL_FIELDS: dict[str, frozenset[str]] = {
    "get_bitcoin_futures_funding": frozenset({
        "lastFundingRate", "markPrice", "indexPrice", "nextFundingTime",
    }),
    "get_bitcoin_long_short_ratio": frozenset({
        "longShortRatio", "longAccount", "shortAccount",
    }),
    "get_bitcoin_onchain": frozenset({
        "hash_rate", "difficulty", "mempool_vsize", "mempool_tx_count",
    }),
    "get_ethereum_network_stats": frozenset({
        "height", "unconfirmed_count", "base_fee",
        "high_gas_price", "medium_gas_price", "low_gas_price",
    }),
    "get_crypto_global_market": frozenset({
        "market_cap_usd", "market_cap_change_24h", "volume_24h_usd",
        "volume_24h_change_24h", "bitcoin_dominance_percentage",
        "cryptocurrencies_number",
    }),
    "get_fear_greed_index": frozenset({"value"}),
    "get_coinbase_btc_stats": frozenset({
        "last", "high", "low", "volume", "volume_30day",
    }),
    "fred_series_observations": frozenset({"value"}),
    "get_crypto_price": frozenset({"price", "market_cap", "volume_24h"}),
    "get_stablecoin_market_activity": frozenset(),
    "get_news": frozenset(),
    "web_search": frozenset(),
    "technical_analysis": frozenset({"score", "value", "signal"}),
}

# Direction rules: only UNAMBIGUOUS cases. Any subject/claim/value combo
# where the operator would readily agree the direction is wrong. Skip
# borderline cases (they count as "not-flagged" — no false positive).
_BULLISH_WORDS = frozenset({
    "bullish", "bull", "buyer", "buyers", "more buyers", "buying pressure",
    "upward", "positive sentiment",
})
_BEARISH_WORDS = frozenset({
    "bearish", "bear", "seller", "sellers", "more sellers", "selling pressure",
    "downward", "negative sentiment",
})

# Metric families — variants under one canonical metric. Used only for
# fragmentation counting; the tokens are lower-cased subject substrings.
METRIC_FAMILIES: dict[str, tuple[str, ...]] = {
    "funding_rate":  ("funding rate", "funding_rate", "perpetual funding"),
    "long_short":    ("long-short", "long/short", "long short", "positioning",
                       "long account", "short account"),
    "hashrate":      ("hashrate", "hash rate", "hash-rate", "network hash"),
    "difficulty":    ("difficulty",),
    "dominance":     ("dominance",),
    "mempool":       ("mempool", "unconfirmed"),
    "gas":           ("gas price", "gwei"),
    "fear_greed":    ("fear", "greed", "sentiment index"),
    "market_cap":    ("market cap", "market_cap", "marketcap"),
    "trading_volume":("trading volume", "24h volume", "24-hour volume"),
}


# Rail-tool keyword hints for angle serviceability. A title mentioning any
# of these keywords is "serviceable" — the rail can produce the data.
_SERVICEABLE_KEYWORDS: frozenset[str] = frozenset({
    "funding", "long-short", "long/short", "positioning", "long account",
    "short account", "hashrate", "hash rate", "difficulty", "mempool",
    "unconfirmed", "gas", "gwei", "dominance", "market cap", "trading volume",
    "24h volume", "fear", "greed", "price", "high", "low",
    "block height", "base fee", "premium", "mark price", "index price",
    "network stats", "onchain", "on-chain",
    # FRED macro rail (fred_series_observations):
    "fred", "federal reserve", "cpi", "unemployment", "gdp", "treasury",
    # Coinbase spot rail (get_coinbase_btc_stats):
    "coinbase", "spot",
})
# "sentiment" is intentionally excluded — too broad (fear-greed measures
# MARKET sentiment; "influencer sentiment" is not on the rail). "fear"
# and "greed" narrowly cover the fear_greed_index angle.


# Generic filler that shows up on both sides of the real theme.
# Used ONLY by the phrase-extractor to prune n-grams that contain a
# generic token at either end. Distinct from _TITLE_TEMPLATE_WORDS
# (which is used for the SERVICEABILITY residue). A word can legitimately
# appear here without appearing there: e.g. "trends", "framework" are
# useful markers when detecting whether an angle is on rail, but as the
# leading/trailing word of a candidate theme they add no information.
_PHRASE_STOPWORDS: frozenset[str] = frozenset({
    # generic template
    "the", "and", "or", "of", "in", "on", "at", "to", "for", "with",
    "vs", "versus", "via", "through", "under", "over", "into", "amid",
    "during", "toward", "against", "within", "between",
    "exploring", "explore", "analyzing", "analyze", "analysis",
    "investigating", "investigate", "examining", "examine",
    "unexplored", "emerging", "evolving", "recent", "current",
    "new", "novel", "potential",
    # generic effects
    "impact", "influence", "context", "correlation", "relationship",
    "changes", "change", "trends", "trend", "effects", "effect",
    "adoption", "activities", "activity", "aggregates", "aggregate",
    "response", "responses",
    # generic modifiers
    "global", "major", "recent", "novel", "unspecified",
    "indicators", "indicator", "drivers", "factors", "dynamics",
    "patterns", "level", "levels", "state", "share", "shares",
    "distribution", "distributions", "framework", "frameworks",
    "environment", "series", "metrics", "metric", "variable",
    # already stripped as subject / template
    "btc", "bitcoin", "crypto", "cryptocurrency", "market", "markets",
    "rate", "rates", "ratio", "ratios",
})


def _extract_theme_phrases(
    residue_texts: list[str], top_k: int = 10,
) -> list[tuple[str, int]]:
    """Rank multi-word noun phrases (bigrams + trigrams) by frequency.

    residue_texts is the LIST of already-cleaned title residues (one per
    unservable title). Each residue is split into tokens; every n-gram
    whose first AND last token is non-stopword becomes a candidate.
    Interior tokens may be stopwords — that preserves "stablecoin
    reserve ratios" (interior "reserve" is fine) and "central bank
    digital currency" (long noun compounds).

    Return the top_k phrases with their counts. Single-word phrases
    are ALSO emitted as a fallback so a purely one-word theme
    ("stablecoin") still surfaces if the model wrote no bigram.
    """
    counter: Counter = Counter()
    for text in residue_texts:
        tokens = [t for t in re.findall(r"[a-z][a-z\-]{2,}", (text or "").lower())]
        if not tokens:
            continue
        # bigrams + trigrams
        for n in (2, 3):
            for i in range(len(tokens) - n + 1):
                gram = tokens[i:i + n]
                if gram[0] in _PHRASE_STOPWORDS:
                    continue
                if gram[-1] in _PHRASE_STOPWORDS:
                    continue
                counter[" ".join(gram)] += 1
        # unigrams (fallback for very short residues)
        for t in tokens:
            if t not in _PHRASE_STOPWORDS:
                counter[t] += 1
    # Post-processing: when a shorter phrase is CONTAINED (word-bounded)
    # in a longer phrase at the same count, drop the shorter one — the
    # longer phrase is strictly more informative.
    items = counter.most_common()
    kept: list[tuple[str, int]] = []
    for phrase, n in items:
        contained_pat = f" {phrase} "
        redundant = False
        for other, m in items:
            if other == phrase:
                continue
            if m == n:
                if contained_pat in f" {other} ":
                    redundant = True
                    break
        if not redundant:
            kept.append((phrase, n))
    return kept[:top_k]


@dataclass
class QualityReport:
    campaign_id: str
    subject: str
    n_objectives: int = 0
    n_theses: int = 0
    class_counts: Counter = field(default_factory=Counter)
    class_examples: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: defaultdict(list),
    )
    fragmentation_families: dict[str, list[str]] = field(default_factory=dict)
    fragmentation_extra: int = 0
    zero_zero_zero_one_hits: list[dict[str, Any]] = field(default_factory=list)
    quarantined_interestrate: list[dict[str, Any]] = field(default_factory=list)
    snapshots_with_interest_rate: int = 0
    snapshots_without_interest_rate: int = 0
    unservable_titles: list[str] = field(default_factory=list)
    top_missing_themes: list[tuple[str, int]] = field(default_factory=list)


def _canonicalize_number_re() -> re.Pattern:
    return re.compile(r"(?<!\w)-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")


_NUMBER_RE = _canonicalize_number_re()


def _extract_numbers(detail: str) -> list[float]:
    out: list[float] = []
    for m in _NUMBER_RE.finditer((detail or "").replace(",", "")):
        try:
            out.append(float(m.group(0)))
        except ValueError:
            continue
    return out


def _close(a: float, b: float, tol: float = 0.02) -> bool:
    if a == 0.0 or b == 0.0:
        return abs(a - b) < 1e-9
    return abs(a - b) / abs(b) <= tol


def classify_scope_misattribution(
    thesis: dict[str, Any],
) -> tuple[bool, str]:
    """A get_crypto_global_market citation under a subject that mentions
    'BTC/Bitcoin' + a specific ASSET-LEVEL metric (volume, market cap,
    price) is a scope error. 'BTC dominance' is EXCEPT — that IS a
    global-market field.
    """
    subj = (thesis.get("subject") or "").lower()
    if not ("btc" in subj or "bitcoin" in subj):
        return False, ""
    asset_metric = any(k in subj for k in (
        "volume", "market cap", "trading volume", "market_cap", "price",
    ))
    if not asset_metric or "dominance" in subj:
        return False, ""
    for e in (thesis.get("evidence") or []):
        if not isinstance(e, dict):
            continue
        if e.get("source") == "get_crypto_global_market":
            return True, str(e.get("detail", ""))[:120]
    return False, ""


# Cross-source numeric collision tolerance. TIGHTENED 2026-09-23: the
# earlier 2 % window flagged "mark price 85776.9 matches onchain hash
# rate 8.5e20" — two vastly different scales that only agree on their
# first three significant digits. 0.1 % keeps genuine field-swap
# collisions (values only differ by trailing noise) while rejecting
# order-of-magnitude coincidences.
CROSS_SUBJECT_TOL: float = 0.001


def classify_cross_subject_value(
    thesis: dict[str, Any],
    findings_payloads: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    """Cited value matches a field of a DIFFERENT rail tool than the one
    named — EXACT match preferred, otherwise tolerance CROSS_SUBJECT_TOL.
    Requires findings_payloads.
    """
    if not findings_payloads:
        return False, ""
    for e in (thesis.get("evidence") or []):
        if not isinstance(e, dict):
            continue
        src = e.get("source")
        detail = str(e.get("detail", ""))
        own = findings_payloads.get(src)
        if not isinstance(own, dict):
            continue
        for v in _extract_numbers(detail):
            # skip trivially small ints (they collide everywhere)
            if abs(v) < 1.0:
                continue
            matches_own = any(
                _close(v, float(r), CROSS_SUBJECT_TOL)
                for r in own.values() if isinstance(r, (int, float))
            )
            if matches_own:
                continue
            for other_src, other_pl in findings_payloads.items():
                if other_src == src or not isinstance(other_pl, dict):
                    continue
                for k, r in other_pl.items():
                    if not isinstance(r, (int, float)):
                        continue
                    if _close(v, float(r), CROSS_SUBJECT_TOL):
                        return True, (
                            f"cited={v} src={src} → matches "
                            f"{other_src}.{k}={r}"
                        )
    return False, ""


def classify_direction_error(thesis: dict[str, Any]) -> tuple[bool, str]:
    """Return True when direction is unambiguously wrong.

    Rules (only fire on clear-cut cases):
      · longShortRatio < 1  claimed "bullish"/"more buyers"     → error.
      · longShortRatio > 1  claimed "bearish"/"more sellers"    → error.
      · lastFundingRate > 0 claimed "bearish"/"short-biased"    → error.
      · lastFundingRate < 0 claimed "bullish"/"long-biased"     → error.
    Any subject/claim outside these keywords is not flagged.
    """
    subj = (thesis.get("subject") or "").lower()
    claim = (thesis.get("claim") or "").lower()
    bull = any(w in claim for w in _BULLISH_WORDS)
    bear = any(w in claim for w in _BEARISH_WORDS)
    if not (bull or bear):
        return False, ""
    is_ls = ("long-short" in subj or "long/short" in subj
             or "long short" in subj or "long-short ratio" in subj)
    is_funding = ("funding" in subj and "rate" in subj)
    for e in (thesis.get("evidence") or []):
        if not isinstance(e, dict):
            continue
        detail = str(e.get("detail", ""))
        for v in _extract_numbers(detail):
            if is_ls and v > 0.001 and v < 10.0:
                if v < 1.0 and bull:
                    return True, f"longShortRatio {v} but claim={claim!r}"
                if v > 1.0 and bear:
                    return True, f"longShortRatio {v} but claim={claim!r}"
            if is_funding and abs(v) < 0.01:
                if v > 0 and bear:
                    return True, f"lastFundingRate {v} but claim={claim!r}"
                if v < 0 and bull:
                    return True, f"lastFundingRate {v} but claim={claim!r}"
    return False, ""


def classify_unsourced_subject(thesis: dict[str, Any]) -> tuple[bool, str]:
    """Subject names a metric no rail tool publishes AND no
    web_search/news evidence is cited. That's a phantom claim."""
    rail_srcs = set(RAIL_TOOL_FIELDS.keys())
    qualitative = {"web_search", "get_news"}
    evidence_srcs = set()
    for e in (thesis.get("evidence") or []):
        if isinstance(e, dict):
            evidence_srcs.add(e.get("source"))
    # If any rail data source is cited, subject IS sourced.
    if evidence_srcs & (rail_srcs - qualitative):
        return False, ""
    # If web_search/news is cited, treat it as sourced (qualitative).
    if evidence_srcs & qualitative:
        return False, ""
    return True, str(thesis.get("subject") or "?")[:100]


def measure_fragmentation(
    raw_subjects: list[str | None],
) -> tuple[dict[str, list[str]], int]:
    """Group RAW subjects (the model's own naming — NOT canonical_subject)
    by METRIC_FAMILY keyword; return (family → variants) and the count of
    extra variants beyond the first. n=1 contributes 0; n=k contributes k-1.

    Uses raw subjects intentionally so a future change to the
    canonicalisation function cannot fake an improvement here — the
    metric measures what the model wrote, not what a normaliser reduces
    it to. Grep-locked in tests/test_campaign_quality.py.
    """
    families: dict[str, set[str]] = defaultdict(set)
    for cs in raw_subjects:
        if not cs:
            continue
        low = cs.lower()
        for fam, keywords in METRIC_FAMILIES.items():
            if any(k in low for k in keywords):
                families[fam].add(cs)
                break
    extra = sum(max(0, len(v) - 1) for v in families.values())
    return {k: sorted(v) for k, v in families.items() if len(v) > 1}, extra


# Template / narrative words that objective titles use as filler around the
# actual angle. Strip these plus the campaign-subject tokens before matching
# the residue against the rail. Otherwise every title contains the subject
# ("BTC Funding Rate and Positioning" → "funding" always matches) and the
# check reports 0 unservable.
_TITLE_TEMPLATE_WORDS: frozenset[str] = frozenset({
    "exploring", "explore", "analyzing", "analyze", "analysis",
    "investigating", "investigate", "examining", "examine",
    "impact", "influence", "context", "correlation", "relationship",
    "between", "via", "through", "under", "over", "into", "amid",
    "during", "toward", "against", "within",
    "unexplored", "emerging", "evolving", "recent", "current",
    "new", "novel", "potential", "the", "and", "or", "of", "in",
    "on", "at", "to", "for", "with", "vs", "versus",
    "level", "levels", "state", "trend", "trends", "crypto",
    "cryptocurrency", "btc", "bitcoin",
})
# NOT in the template set (kept in residue on purpose):
#   · "market" — collides with the "market cap" rail keyword; stripping it
#     would strand "cap" alone. The angle "via Market Cap Changes" is
#     servable and must not be false-flagged.


def _angle_residue_tokens(
    title: str, campaign_subject: str,
) -> set[str]:
    """Return the set of tokens that remain after stripping the campaign
    subject's tokens and generic template words. This is the ANGLE — the
    part of the title that names what the objective actually explores.

    NOTE: returns a SET (order-free) for serviceability checks. For
    phrase extraction, use _angle_residue_tokens_ordered() which
    preserves title order — bigrams/trigrams need adjacency.
    """
    subject_tokens = {
        t for t in re.findall(r"[a-z][a-z\-]+", (campaign_subject or "").lower())
    }
    title_tokens = {
        t for t in re.findall(r"[a-z][a-z\-]+", (title or "").lower())
    }
    return title_tokens - subject_tokens - _TITLE_TEMPLATE_WORDS


def _angle_residue_tokens_ordered(
    title: str, campaign_subject: str,
) -> list[str]:
    """Ordered residue: same strip logic as the set version but preserves
    the original title's word order so downstream bigram/trigram
    extraction sees real phrases (e.g. "stablecoin reserve ratios")
    rather than an alphabetically-sorted permutation of the same tokens.
    """
    subject_tokens = {
        t for t in re.findall(r"[a-z][a-z\-]+", (campaign_subject or "").lower())
    }
    out: list[str] = []
    for t in re.findall(r"[a-z][a-z\-]+", (title or "").lower()):
        if t in subject_tokens or t in _TITLE_TEMPLATE_WORDS:
            continue
        out.append(t)
    return out


def title_is_serviceable(
    title: str, campaign_subject: str | None = None,
) -> bool:
    """Serviceable ⇔ at least one rail-keyword appears in the ANGLE
    (title residue after subject/template strip). If campaign_subject
    is None, fall back to the legacy whole-title match — preserves
    behavior for callers who don't have a subject on hand."""
    if campaign_subject is None:
        low = (title or "").lower()
        return any(kw in low for kw in _SERVICEABLE_KEYWORDS)
    residue = _angle_residue_tokens(title, campaign_subject)
    residue_text = " ".join(residue)
    return any(kw in residue_text for kw in _SERVICEABLE_KEYWORDS)


def score_thesis(
    thesis: dict[str, Any],
    findings_payloads: dict[str, dict[str, Any]] | None = None,
) -> dict[str, tuple[bool, str]]:
    """Run every classifier on one thesis. Returns
    {class_name: (hit, example_snippet)}."""
    out: dict[str, tuple[bool, str]] = {}
    hit, ex = classify_scope_misattribution(thesis)
    out["scope_misattribution"] = (hit, ex)
    # field_confusion: reuse the write-time classifier over evidence.
    # findings_payloads is required — without it we can't classify.
    fc_hit = False
    fc_ex = ""
    if findings_payloads:
        try:
            recs = classify_thesis_evidence(
                thesis.get("evidence") or [], findings_payloads,
            )
            for r in recs:
                if r.get("verdict") == "field_confusion":
                    fc_hit = True
                    fc_ex = f"cited {r.get('cited_value')} expected={r.get('expected_field')} matched={r.get('matched_field')}"
                    break
        except Exception:
            pass
    out["field_confusion"] = (fc_hit, fc_ex)
    out["direction_error"] = classify_direction_error(thesis)
    out["cross_subject_value"] = classify_cross_subject_value(
        thesis, findings_payloads,
    )
    out["unsourced_subject"] = classify_unsourced_subject(thesis)
    return out


def render_report(report: QualityReport) -> str:
    """Screenshot-friendly plain-text render."""
    lines = [
        f"═══ CAMPAIGN QUALITY: {report.campaign_id[:8]} ═══",
        f"subject       : {report.subject}",
        f"objectives    : {report.n_objectives}",
        f"theses        : {report.n_theses}",
        "",
        "A · ERROR CLASSES (theses hitting each class):",
    ]
    for name in ("scope_misattribution", "field_confusion", "direction_error",
                  "cross_subject_value", "unsourced_subject"):
        n = report.class_counts.get(name, 0)
        share = (n / report.n_theses * 100) if report.n_theses else 0.0
        lines.append(f"  {name:<25} {n:>4}  ({share:.0f}%)")
        # cross_subject_value: print ALL examples so each collision is
        # visible with both payload values. Other classes: keep 2.
        cap = None if name == "cross_subject_value" else 2
        seq = report.class_examples.get(name, [])
        for ex in (seq if cap is None else seq[:cap]):
            lines.append(f"    · {ex.get('example','')[:140]}")
    lines.append(
        f"  subject_fragmentation    {report.fragmentation_extra:>4} extra variants "
        f"across {len(report.fragmentation_families)} families"
    )
    for fam, variants in list(report.fragmentation_families.items())[:3]:
        lines.append(f"    · {fam}: {len(variants)} variants — {variants[:4]}")
    lines.append("")
    lines.append("B · lastFundingRate=0.0001 CITATIONS:")
    lines.append(
        f"  in-campaign hits         : {len(report.zero_zero_zero_one_hits)}"
    )
    lines.append(
        f"  quarantined 'interestrate_as_funding' cross-check: "
        f"{len(report.quarantined_interestrate)} theses"
    )
    genuine = sum(1 for r in report.quarantined_interestrate
                  if r.get("verdict") == "genuine")
    confused = sum(1 for r in report.quarantined_interestrate
                   if r.get("verdict") == "confused")
    unknown = sum(1 for r in report.quarantined_interestrate
                  if r.get("verdict") == "unknown")
    lines.append(
        f"    genuine={genuine}  confused={confused}  unknown={unknown}"
    )
    lines.append(
        f"  funding snapshots WITH interestRate : "
        f"{report.snapshots_with_interest_rate}"
    )
    lines.append(
        f"  funding snapshots WITHOUT interestRate: "
        f"{report.snapshots_without_interest_rate}"
    )
    for r in report.quarantined_interestrate:
        if r.get("verdict") == "genuine":
            lines.append(
                f"    GENUINE {r.get('thesis_id','')[:8]} @ {r.get('ts','')} "
                f"— binance={r.get('reference_rate')} "
            )
    lines.append("")
    n_titles = report.n_objectives
    unservable = len(report.unservable_titles)
    share = (unservable / n_titles * 100) if n_titles else 0.0
    lines.append("C · ANGLE SERVICEABILITY:")
    lines.append(f"  unservable titles       : {unservable}/{n_titles}  ({share:.0f}%)")
    if report.top_missing_themes:
        lines.append("  top missing themes:")
        for theme, n in report.top_missing_themes[:10]:
            lines.append(f"    · {theme:<40} {n}")
    return "\n".join(lines)


async def score_campaign(
    pm,
    campaign_id: str,
    *,
    fetch_binance_funding=None,
) -> QualityReport:
    """Assemble a QualityReport by querying the store.

    `fetch_binance_funding`, if provided, is an async callable returning
    `(sorted_ms, values)` — used for B's genuine/confused verdict. When
    None (unit tests), each quarantined thesis is reported as 'unknown'.
    """
    row = None
    for r in await pm.list_campaigns(limit=200):
        if str(r["campaign_id"]).startswith(campaign_id) or str(r["campaign_id"]) == campaign_id:
            row = r
            break
    if not row:
        raise ValueError(f"campaign {campaign_id!r} not found")
    cid = str(row["campaign_id"])
    objs = await pm.list_campaign_objectives(cid)
    theses: list[dict[str, Any]] = []
    for o in objs:
        for t in await pm.get_theses_by_objective(o["objective_id"]):
            t["_obj_title"] = o.get("title")
            theses.append(t)
    # Payload references — pull the LATEST source_snapshot per source. This
    # is the closest thing to "the tool's actual output" for numeric-collision
    # and field-confusion checks. Best-effort: any snapshot at all is
    # informative; we don't need per-cycle alignment for a scorer.
    findings_payloads: dict[str, dict[str, Any]] = {}
    pool = pm._require_pool()
    async with pool.acquire() as conn:
        rs = await conn.fetch(
            "SELECT DISTINCT ON (source) source, payload FROM source_snapshots "
            "ORDER BY source, observed_at DESC"
        )
        for r in rs:
            import json as _json
            pl = r["payload"] if isinstance(r["payload"], dict) else _json.loads(r["payload"] or "{}")
            findings_payloads[r["source"]] = pl
        # interestRate presence check on post-7e3f8a9 snapshots.
        ir_rows = await conn.fetch(
            "SELECT payload FROM source_snapshots "
            "WHERE source='get_bitcoin_futures_funding' "
            "  AND observed_at >= '2026-09-19'"
        )
        w = 0; wo = 0
        for irow in ir_rows:
            import json as _json
            pl = irow["payload"] if isinstance(irow["payload"], dict) else _json.loads(irow["payload"] or "{}")
            if "interestRate" in pl:
                w += 1
            else:
                wo += 1

    rep = QualityReport(
        campaign_id=cid,
        subject=str(row.get("subject") or "?"),
        n_objectives=len(objs),
        n_theses=len(theses),
        snapshots_with_interest_rate=w,
        snapshots_without_interest_rate=wo,
    )

    # A: six classes
    for t in theses:
        verdicts = score_thesis(t, findings_payloads=findings_payloads)
        for cls, (hit, ex) in verdicts.items():
            if hit:
                rep.class_counts[cls] += 1
                # Cap examples per class at 2 — EXCEPT cross_subject_value,
                # where the operator asked for the full list so each match
                # is visible with both payload values.
                cap = None if cls == "cross_subject_value" else 2
                if cap is None or len(rep.class_examples[cls]) < cap:
                    rep.class_examples[cls].append({
                        "thesis_id": str(t.get("thesis_id"))[:8],
                        "example": (
                            f"{t.get('subject','?')} :: {ex}"
                        ),
                    })
    # FRAGMENTATION: intentionally use RAW subjects (the model's naming),
    # not canonical_subject. A canonicalisation change must not be able to
    # improve this metric by folding variants together.
    raw_subs = [t.get("subject") for t in theses]
    rep.fragmentation_families, rep.fragmentation_extra = measure_fragmentation(raw_subs)

    # B: 0.0001 hits + quarantined cross-check
    for t in theses:
        for e in (t.get("evidence") or []):
            if not isinstance(e, dict):
                continue
            if e.get("source") != "get_bitcoin_futures_funding":
                continue
            if "0.0001" in str(e.get("detail", "")):
                rep.zero_zero_zero_one_hits.append({
                    "thesis_id": str(t.get("thesis_id"))[:8],
                    "ts": str(t.get("created_at")),
                    "detail": str(e.get("detail", ""))[:80],
                })
                break
    # Quarantined interestrate_as_funding — global lookup, not campaign-scoped
    # (per chantier B: check ALL 24 quarantined theses).
    async with pool.acquire() as conn:
        qrows = await conn.fetch(
            "SELECT thesis_id, subject, evidence, created_at, quarantine_reason "
            "FROM theses WHERE quarantine_reason = 'interestrate_as_funding' "
            "ORDER BY created_at"
        )
    for q in qrows:
        entry = {
            "thesis_id": str(q["thesis_id"])[:8],
            "ts": q["created_at"].isoformat(timespec="minutes"),
            "subject": q["subject"],
            "verdict": "unknown",
        }
        if fetch_binance_funding is not None:
            try:
                ms, vals = await fetch_binance_funding()
                # Find funding rate at the thesis creation timestamp.
                t_ms = int(q["created_at"].timestamp() * 1000)
                # Nearest funding event
                closest = None
                min_dt = None
                for i, m in enumerate(ms):
                    dt = abs(m - t_ms)
                    if min_dt is None or dt < min_dt:
                        min_dt = dt
                        closest = (m, vals[i])
                if closest is not None:
                    m, ref = closest
                    entry["reference_rate"] = ref
                    entry["reference_ts_ms"] = m
                    # Genuine iff the CITED value equals the reference within 5 %.
                    import json as _json
                    ev = q["evidence"] if isinstance(q["evidence"], list) else _json.loads(q["evidence"] or "[]")
                    cited = None
                    for e in ev:
                        if isinstance(e, dict):
                            for v in _extract_numbers(str(e.get("detail",""))):
                                if abs(v) < 0.01:
                                    cited = v; break
                        if cited is not None: break
                    entry["cited"] = cited
                    if cited is not None:
                        entry["verdict"] = "genuine" if _close(cited, ref, 0.05) else "confused"
            except Exception as exc:
                entry["verdict"] = "unknown"
                entry["error"] = str(exc)[:80]
        rep.quarantined_interestrate.append(entry)

    # C: angle serviceability — extract the ANGLE first (title residue
    # after subject / template strip). The campaign subject shows up in
    # every title in a campaign; matching against the whole title
    # trivially returns 0 unservable.
    residues: list[str] = []
    subj = str(row.get("subject") or "")
    for o in objs:
        title = o.get("title") or ""
        if not title_is_serviceable(title, campaign_subject=subj):
            rep.unservable_titles.append(title)
            # ORDERED residue preserves adjacency so bigrams like
            # "stablecoin reserve" and "central bank digital" survive.
            residues.append(" ".join(
                _angle_residue_tokens_ordered(title, subj)
            ))
    # Multi-word noun-phrase ranking (was single-token; single tokens
    # produced generics like "economic 8, changes 7" and buried the real
    # signal — "stablecoin" appeared in ~24 titles and never surfaced).
    rep.top_missing_themes = _extract_theme_phrases(residues, top_k=10)
    return rep
