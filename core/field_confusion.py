"""Field-confusion audit: does a cited number come from the RIGHT field
of the source tool's output?

The numeric-fidelity gate (a6e8f19+) checks that a cited number EXISTS
somewhere in the source tool's payload. It does NOT check that the
number comes from the field the model NAMED. That's how "funding rate
0.00010000" (really Binance's constant interestRate) passed for weeks.

Class of bug, not incident. Example still in the corpus:
  "BTC dominance: declining ← 55.92% with 24-hour change -2.17%"
  55.92 is bitcoin_dominance_percentage (correct), but -2.17 is
  market_cap_change_24h — a different field of the same tool. The
  DIRECTION ("declining") rests on the wrong field.

This module is READ-ONLY infrastructure: a per-tool field map, phrase
resolvers, and a classify_number() helper. Downstream:
  scripts/audit_field_confusion.py runs it over the store and reports.
  A future commit may persist events + rewrite/drop — this one does not.
"""

from __future__ import annotations

import re
from typing import Any


# Phrases that unambiguously designate a specific field. Case-insensitive
# substring match. A phrase that could name two fields is deliberately
# ABSENT — better to classify a number as "no confident mapping" than
# to force-fit it into a bucket. Keep this table conservative.
FIELD_PHRASES: dict[str, dict[str, tuple[str, ...]]] = {
    "get_crypto_global_market": {
        "market_cap_usd": (
            "market_cap_usd",
        ),
        "market_cap_change_24h": (
            "market_cap_change_24h", "market cap change", "market-cap change",
            "24-hour change", "24h change", "market cap 24h", "24h market cap change",
        ),
        "volume_24h_usd": (
            "volume_24h_usd", "24h volume", "24-hour volume", "trading volume",
        ),
        "volume_24h_change_24h": (
            "volume_24h_change_24h", "volume 24h change", "volume change",
        ),
        "bitcoin_dominance_percentage": (
            "bitcoin_dominance_percentage", "btc dominance", "bitcoin dominance",
            "dominance",
        ),
        "cryptocurrencies_number": (
            "cryptocurrencies_number", "number of cryptocurrencies",
        ),
    },
    "get_bitcoin_futures_funding": {
        "lastFundingRate": (
            "lastFundingRate", "last funding rate", "funding rate", "funding_rate",
        ),
        "markPrice": ("markPrice", "mark price", "mark_price"),
        "indexPrice": ("indexPrice", "index price", "index_price"),
        "nextFundingTime": ("nextFundingTime", "next funding time"),
    },
    "get_bitcoin_onchain": {
        # Payload keys are snake_case (verified against live snapshot).
        "hash_rate": ("hash rate", "hashrate", "hash_rate", "hashRate"),
        "difficulty": ("mining difficulty", "difficulty"),
        "mempool_vsize": ("mempool vsize", "mempool size"),
        "mempool_tx_count": ("mempool tx count", "unconfirmed transactions"),
    },
    "get_ethereum_network_stats": {
        "height": ("block height", "block_height", "blockHeight", "current block height"),
        "unconfirmed_count": ("unconfirmed", "mempool"),
        "base_fee": ("base fee", "base_fee", "baseFee"),
        "high_gas_price": ("high gas", "high gwei", "high gas price"),
        "medium_gas_price": ("medium gas", "medium gwei", "medium gas price"),
        "low_gas_price": ("low gas", "low gwei", "low gas price"),
    },
    "get_fear_greed_index": {
        "value": ("index value", "fear & greed value", "fear greed value",
                   "f&g value", "sentiment value"),
    },
    "get_bitcoin_long_short_ratio": {
        "longShortRatio": ("long/short ratio", "long-short ratio", "longShortRatio",
                             "long short ratio"),
        "longAccount": ("long account", "longAccount"),
        "shortAccount": ("short account", "shortAccount"),
    },
    "get_coinbase_btc_stats": {
        "last": ("last price",),
        "high": ("24h high",),
        "low": ("24h low",),
        "volume": ("24h volume", "coinbase volume"),
        "volume_30day": ("30-day volume", "30d volume", "volume_30day"),
    },
}

# Phrases that name TWO or more fields (or "value" / "rate" / "change"
# in isolation) — always resolve to None. Anything on this list guarantees
# no confident mapping is attempted.
AMBIGUOUS_PHRASES: frozenset[str] = frozenset({
    "value", "rate", "change", "price", "volume",
})

# Tools whose numeric output is effectively ONE scalar. Any number
# extracted from a citation of such a tool is by construction the
# single numeric field — no phrase needed to disambiguate.
# get_fear_greed_index: value ∈ [0,100]; the other keys (timestamp,
# value_classification) are non-scalar for this purpose.
SINGLE_NUMERIC_FIELD: dict[str, str] = {
    "get_fear_greed_index": "value",
}


# Fields that are METADATA (unix epochs, next-window times) and must
# NEVER count as a candidate for a cited-value match. A model that
# writes "at timestamp 1789689600" is quoting metadata, not a value.
# 2026-09-22: without this exclusion, F&G "value" trivial-mapping
# treated the unix timestamp as a genuine value confusion.
TIMESTAMP_LIKE_FIELDS: frozenset[str] = frozenset({
    "timestamp", "nextFundingTime", "next_funding_time",
    "next_difficulty_adjustment", "observed_at", "occurred_at",
    "created_at", "updated_at",
})


_NUMBER_RE = re.compile(
    r"(?<!\w)-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    r"(?=[TBMKtbmk](?![a-zA-Z])|(?!\w))"
)


def phrase_to_field(source: str, context: str) -> str | None:
    """Return the field name the context text designates for this source,
    or None if no phrase / ambiguous / no mapping.

    context is the local text around the number (e.g. the 40 characters
    preceding it). Longest-first match so "market cap change" wins over
    "market cap".

    Trivial-mapping short-circuit: for tools in SINGLE_NUMERIC_FIELD,
    the phrase context is irrelevant — the tool has only one scalar
    field to name.
    """
    if not source:
        return None
    if source in SINGLE_NUMERIC_FIELD:
        return SINGLE_NUMERIC_FIELD[source]
    if not context:
        return None
    mapping = FIELD_PHRASES.get(source)
    if not mapping:
        return None
    text = context.lower()
    # Reject the whole context if it starts / ends with a bare ambiguous
    # phrase and nothing more distinctive is nearby.
    matches: list[tuple[int, str]] = []
    for field, phrases in mapping.items():
        for phrase in phrases:
            idx = text.rfind(phrase.lower())
            if idx >= 0:
                matches.append((len(phrase), field))
    if not matches:
        return None
    # Longest phrase wins; ties → None (ambiguous).
    matches.sort(key=lambda kv: -kv[0])
    if len(matches) >= 2 and matches[0][0] == matches[1][0] and matches[0][1] != matches[1][1]:
        return None
    return matches[0][1]


def iter_numbers_with_context(detail: str, window: int = 40):
    """Yield (number: float, prefix_context: str) for every number in
    detail. Prefix is the `window` characters preceding the number
    (lowercased); enough to catch "market cap change of -2.17%"."""
    if not detail:
        return
    cleaned = detail.replace(",", "")
    for m in _NUMBER_RE.finditer(cleaned):
        try:
            v = float(m.group(0))
        except ValueError:
            continue
        prefix_start = max(0, m.start() - window)
        prefix = cleaned[prefix_start:m.start()]
        yield v, prefix


_TOOL_LINE_RE = re.compile(
    r"^-\s+([a-z_][a-z0-9_]*)\s*:\s*(\{.*\})\s*$", re.MULTILINE
)
# Truncated JSON — the finding text got cut mid-payload (300-char cap).
# Detect open-brace with no matching close on the tail.
_TRUNCATION_MARK = "\n- TOOL RESULTS:\n"


def parse_findings_payloads(findings: list[str] | str) -> tuple[
    dict[str, dict[str, Any]], int
]:
    """Extract per-source payloads from the TOOL RESULTS blocks that live
    in each cycle's finding text. Returns (payloads, truncated_count).

    truncated_count is the number of `- <source>: {…` lines that could
    NOT be parsed as JSON because the 300-char finding cap cut the
    payload mid-object. Such sources yield NO reference payload for
    that thesis — the audit reports them separately from clean misses.
    """
    import json
    if isinstance(findings, str):
        findings = [findings]
    out: dict[str, dict[str, Any]] = {}
    truncated = 0
    for blob in findings or []:
        if not blob:
            continue
        for m in _TOOL_LINE_RE.finditer(blob):
            source, raw = m.group(1), m.group(2)
            try:
                payload = json.loads(raw)
            except Exception:
                truncated += 1
                continue
            if not isinstance(payload, dict):
                continue
            # Merge — later payloads (newer cycles) overwrite; the freshest
            # value for each key wins.
            merged = out.get(source, {})
            merged.update(payload)
            out[source] = merged
        # Also count `- source: {` lines whose JSON did NOT close before
        # end-of-blob (300-char truncation).
        for m in re.finditer(r"^-\s+([a-z_][a-z0-9_]*)\s*:\s*\{[^}]*$",
                              blob, re.MULTILINE):
            truncated += 1
    return out, truncated


def classify_thesis_evidence(
    evidence: list[dict[str, Any]],
    findings_payloads: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Run the classifier over every number in every evidence entry
    of a thesis. Returns one record per number:
      {source, cited_value, expected_field, verdict, matched_field,
       detail_snippet}
    """
    out: list[dict[str, Any]] = []
    for e in evidence or []:
        if not isinstance(e, dict):
            continue
        source = str(e.get("source") or "")
        detail = str(e.get("detail") or "")
        if not detail:
            continue
        payload = findings_payloads.get(source)
        for value, prefix in iter_numbers_with_context(detail):
            expected = phrase_to_field(source, prefix)
            verdict, matched = classify_number(value, expected, payload)
            out.append({
                "source": source,
                "cited_value": value,
                "expected_field": expected,
                "matched_field": matched,
                "verdict": verdict,
                "detail_snippet": detail[:180],
            })
    return out


def classify_number(
    value: float, expected_field: str | None,
    tool_payload: dict[str, Any] | None,
    tolerance: float = 0.02,
) -> tuple[str, str | None]:
    """Return (verdict, matched_field_or_None). Verdicts:
      · 'true_pass'              — matches the expected field's value.
      · 'field_confusion'        — matches a DIFFERENT field of the same tool.
      · 'no_phrase_mapping'      — expected_field is None (phrase absent
                                    or ambiguous); value may still match
                                    a field (returned as matched_field).
      · 'value_not_in_reference' — expected_field is set but the value
                                    doesn't match any field in the payload.
    """
    if not isinstance(tool_payload, dict):
        return ("no_phrase_mapping" if expected_field is None
                else "value_not_in_reference"), None
    def _close(a: float, b: float) -> bool:
        if a == 0.0 or b == 0.0:
            return abs(a - b) < 1e-9
        return abs(a - b) / abs(b) <= tolerance
    matched: list[str] = []
    for key, ref in tool_payload.items():
        if key in TIMESTAMP_LIKE_FIELDS:
            continue  # metadata epochs never count as data values
        try:
            r = float(ref)
        except (TypeError, ValueError):
            continue
        if _close(value, r):
            matched.append(key)
    if expected_field is None:
        # No phrase mapping. Still report which field(s) the value
        # happens to match, useful diagnostic.
        return "no_phrase_mapping", (matched[0] if matched else None)
    if not matched:
        return "value_not_in_reference", None
    if expected_field in matched:
        return "true_pass", expected_field
    return "field_confusion", matched[0]
