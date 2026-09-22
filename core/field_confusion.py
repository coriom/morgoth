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
    """
    if not source or not context:
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


def classify_number(
    value: float, expected_field: str | None,
    tool_payload: dict[str, Any] | None,
    tolerance: float = 0.02,
) -> tuple[str, str | None]:
    """Return (verdict, matched_field_or_None). Verdicts:
      · 'true_pass'         — matches the expected field's value.
      · 'field_confusion'   — matches a DIFFERENT field of the same tool.
      · 'no_mapping'        — expected_field is None (phrase ambiguous
                                or unrecognised) OR value matches no field.
    """
    if not isinstance(tool_payload, dict):
        return "no_mapping", None
    def _close(a: float, b: float) -> bool:
        if a == 0.0 or b == 0.0:
            return abs(a - b) < 1e-9
        return abs(a - b) / abs(b) <= tolerance
    matched: list[str] = []
    for key, ref in tool_payload.items():
        try:
            r = float(ref)
        except (TypeError, ValueError):
            continue
        if _close(value, r):
            matched.append(key)
    if not matched:
        return "no_mapping", None
    if expected_field is None:
        return "no_mapping", matched[0]
    if expected_field in matched:
        return "true_pass", expected_field
    return "field_confusion", matched[0]
