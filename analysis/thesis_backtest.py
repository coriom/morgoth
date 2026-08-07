"""Thesis calibration backtest — measure hit-rate of directional price theses.

Empirical scorer that compares Morgoth's directional price theses against
what BTC/ETH prices ACTUALLY did over the thesis's horizon. Read-only:
never mutates the thesis row, never touches the cycle loop, gates, or
apply — this is measurement, not intervention.

Scope:
    Only subjects that look like "BTC short-term price", "Ethereum
    short-term price", etc. — where the thesis is a bet on price
    direction, so success/failure is checkable against tape.

Not scope:
    Qualitative theses ("gas prices high", "network congestion
    increasing on Ethereum") — those have no cheap ground truth.
    Skipped, not scored.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

Direction = Literal["up", "down", "flat"]
Outcome = Literal["hit", "miss", "skip"]
Asset = Literal["bitcoin", "ethereum"]

# Keyword polarity map — grown from the observed distinct-claim vocabulary
# of the actual theses table. If a claim contains no polarity keyword the
# thesis is treated as unresolvable (returns None) rather than force-fit.
_UP_WORDS = ("increas", "gain", "rise", "rising", "bullish", "growing", "grow", "rally", "positive")
_DOWN_WORDS = ("declin", "decreas", "drop", "fall", "bearish", "correct", "sell-off", "dump", "negative")
_FLAT_WORDS = ("stable", "flat ", "unchanged", "sideways", "range-bound")
# Explicit non-directional — treat as unresolvable even if a substring
# scan would otherwise catch a keyword. Includes:
#   - genuine "no direction" hedges: volatile, fluctuating, uncorrelated
#   - measurement-notes ("not reflected", "inaccurate")
#   - polarity inverters that partially overlap real polarity words
#     ("unstable" contains "stable"; "unchanged" is flat but "changed"
#     alone is not a direction; matched via _CONTRADICTORY_WORDS).
_AMBIGUOUS_WORDS = ("volatile", "fluctuat", "uncorrelated", "inaccurate", "not reflected")
# Explicit polarity inverters we've observed but that don't cleanly map:
# "unstable" is the OPPOSITE of stable but not clearly up or down.
_CONTRADICTORY_WORDS = ("unstable",)
# Words that mean the claim asserts a conditional / hedged relationship
# rather than a direction: "if", "when", "depends", "may", "or slightly",
# "or" between polarity words. When these appear alongside polarity words
# we return None — a wrong direction is worse than a skip.
_HEDGE_MARKERS = (" if ", " when ", " depend", " may ", " might ", " could ", " unless ")
# Negation window: if any of these appears within N tokens BEFORE a
# polarity word, we FLIP the polarity. Deliberately narrow: only the
# obvious constructions. "not"/"unlikely to"/"fails to" cover ~all real
# negations in the LLM's vocabulary; broader nets over-fire.
_NEGATION_WORDS = ("not", "no", "unlikely", "fails", "cannot", "won't", "will not", "isn't", "aren't", "never")
_NEGATION_WINDOW = 3  # tokens


def _tokens(s: str) -> list[str]:
    """Rough tokenizer: strip punctuation, lowercase, split on whitespace."""
    out: list[str] = []
    buf: list[str] = []
    for ch in s.lower():
        if ch.isalnum() or ch == "-":
            buf.append(ch)
        else:
            if buf:
                out.append("".join(buf))
                buf = []
    if buf:
        out.append("".join(buf))
    return out


def _has_negation_before(tokens: list[str], idx: int) -> bool:
    """True if a negation marker sits within _NEGATION_WINDOW tokens before idx."""
    lo = max(0, idx - _NEGATION_WINDOW)
    window = tokens[lo:idx]
    return any(w in _NEGATION_WORDS for w in window)


def parse_direction(claim: str) -> Direction | None:
    """Extract polarity from a claim. Returns None if not cleanly directional.

    Hardening pass (post first-run audit of 91 scored claims):
      - "unstable" no longer parses as flat (was: substring match on "stable")
      - hedged / conditional claims ("if X", "depends on", "may rise or
        fall", "stable or slightly increasing") → None
      - claim containing BOTH up and down polarity words → None
      - negation within 3 tokens before a polarity word FLIPS polarity
    Conservative: when in doubt, unresolvable.
    """
    if not claim:
        return None
    c = claim.lower()
    for w in _AMBIGUOUS_WORDS:
        if w in c:
            return None
    for w in _CONTRADICTORY_WORDS:
        if w in c:
            return None
    for m in _HEDGE_MARKERS:
        if m in f" {c} ":
            return None
    # Detect "X or Y" between polarity classes ("stable or slightly increasing")
    if " or " in f" {c} ":
        halves = c.split(" or ")
        if len(halves) >= 2:
            classes = set()
            for h in halves:
                if any(w in h for w in _DOWN_WORDS):
                    classes.add("down")
                if any(w in h for w in _UP_WORDS):
                    classes.add("up")
                if any(w in h for w in _FLAT_WORDS):
                    classes.add("flat")
            if len(classes) > 1:
                return None

    tokens = _tokens(claim)

    def _find_polarity(words: tuple[str, ...]) -> int | None:
        for i, tok in enumerate(tokens):
            for w in words:
                if w in tok:
                    return i
        return None

    down_i = _find_polarity(_DOWN_WORDS)
    up_i = _find_polarity(_UP_WORDS)
    flat_i = _find_polarity(_FLAT_WORDS)

    # Both up-word and down-word present → contradictory → unresolvable.
    # ("positive (short-term correction)" — the audit's canonical case.)
    if down_i is not None and up_i is not None:
        return None

    # Apply the polarity that's present, flipping if negated.
    for direction, idx in (("down", down_i), ("up", up_i), ("flat", flat_i)):
        if idx is None:
            continue
        negated = _has_negation_before(tokens, idx)
        if not negated:
            return direction
        if direction == "up":
            return "down"
        if direction == "down":
            return "up"
        return None  # negated flat has no cleanly-inverse meaning
    return None


def subject_asset(subject: str) -> Asset | None:
    """Map a subject to a CoinGecko asset id, or None if not a price subject.

    Post-audit exclusions (from the 91-scored dump):
      - "gas price(s)": denominated in gwei, NOT spot ETH — was being
        scored against ETH/USD by mistake (7 rows in the first run).
      - "long-term ...": a 24h horizon can't score a long-term thesis
        (3 rows). At 3d/7d we STILL can't, because "long-term" in
        Morgoth's vocabulary means weeks-to-months.
      - "trend" without a direction word in the subject is fine —
        the direction still has to come from the claim.
    """
    if not subject:
        return None
    s = subject.lower()
    if "gas" in s:  # gas price is in gwei, not USD spot
        return None
    if "long-term" in s or "long term" in s:
        return None
    if "hash rate" in s or "network congestion" in s or "funding" in s or "dominance" in s:
        return None
    if "price" not in s:
        return None
    if "btc" in s or "bitcoin" in s:
        return "bitcoin"
    if "eth" in s or "ethereum" in s:
        return "ethereum"
    return None


def bucket_actual(p0: float, p1: float, flat_band: float) -> Direction:
    """Bucket an observed price move into up/down/flat by a symmetric band."""
    ret = (p1 - p0) / p0
    if abs(ret) <= flat_band:
        return "flat"
    return "up" if ret > 0 else "down"


def score(predicted: Direction, p0: float | None, p1: float | None, flat_band: float) -> Outcome:
    """Hit iff bucketed actual direction matches predicted. Skip if price missing."""
    if p0 is None or p1 is None or p0 <= 0:
        return "skip"
    return "hit" if bucket_actual(p0, p1, flat_band) == predicted else "miss"


def nearest_price(sorted_ms: list[int], prices: list[float], ts_ms: int) -> float | None:
    """Return the price at the closest sample to ts_ms, or None if series empty.

    CoinGecko returns hourly/daily samples; picking the nearest tolerates
    small gaps (a thesis stamped at 14:37 lands on the 14:00 or 15:00
    bucket). If the target is outside the covered window we return None
    (SKIP), because extrapolating price is worse than admitting no data.
    """
    if not sorted_ms:
        return None
    if ts_ms < sorted_ms[0] or ts_ms > sorted_ms[-1]:
        return None
    idx = bisect_left(sorted_ms, ts_ms)
    if idx == 0:
        return prices[0]
    if idx == len(sorted_ms):
        return prices[-1]
    before, after = sorted_ms[idx - 1], sorted_ms[idx]
    return prices[idx - 1] if (ts_ms - before) <= (after - ts_ms) else prices[idx]


@dataclass(frozen=True)
class ScoredThesis:
    thesis_id: str
    subject: str
    claim: str
    confidence: str
    asset: Asset
    predicted: Direction
    created_at: datetime
    horizon_hours: int
    p0: float | None
    p1: float | None
    outcome: Outcome
    source: str


def evidence_source(evidence: list[dict] | None) -> str:
    """Best-effort primary source for the thesis; '(none)' if evidence empty."""
    if not evidence:
        return "(none)"
    first = evidence[0]
    if isinstance(first, dict) and first.get("source"):
        return str(first["source"])
    return "(none)"


def resolve_theses(
    rows: Iterable[dict],
    price_series: dict[Asset, tuple[list[int], list[float]]],
    *,
    horizon_hours: int,
    flat_band: float,
    now: datetime | None = None,
) -> tuple[list[ScoredThesis], dict[str, int]]:
    """Score every resolvable thesis. Returns (records, funnel-counts).

    Funnel:
        input        - all rows fed in
        non_price    - subject is not a price subject
        no_direction - claim carries no polarity keyword
        horizon_open - horizon hasn't elapsed yet (skip: not resolvable YET)
        no_price     - price series doesn't cover P0 or P1 timestamp
        scored       - hit + miss
    """
    now = now or datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    horizon = timedelta(hours=horizon_hours)
    records: list[ScoredThesis] = []
    counts = {"input": 0, "non_price": 0, "no_direction": 0, "horizon_open": 0, "no_price": 0, "scored": 0}
    for row in rows:
        counts["input"] += 1
        asset = subject_asset(str(row.get("subject", "")))
        if asset is None:
            counts["non_price"] += 1
            continue
        direction = parse_direction(str(row.get("claim", "")))
        if direction is None:
            counts["no_direction"] += 1
            continue
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if created_at.tzinfo is None:
            # Theses table is TIMESTAMPTZ, but if a naive slips in, treat as UTC.
            created_at = created_at.replace(tzinfo=timezone.utc)
        target_ts = created_at + horizon
        if target_ts > now:
            counts["horizon_open"] += 1
            continue
        sorted_ms, prices = price_series.get(asset, ([], []))
        # CoinGecko epoch ms is UTC — use the aware datetime's UTC timestamp
        # directly so we don't mis-interpret naive datetimes as local time.
        p0_ms = int(created_at.astimezone(timezone.utc).timestamp() * 1000)
        p1_ms = int(target_ts.astimezone(timezone.utc).timestamp() * 1000)
        p0 = nearest_price(sorted_ms, prices, p0_ms)
        p1 = nearest_price(sorted_ms, prices, p1_ms)
        outcome = score(direction, p0, p1, flat_band)
        if outcome == "skip":
            counts["no_price"] += 1
            continue
        counts["scored"] += 1
        records.append(
            ScoredThesis(
                thesis_id=str(row.get("thesis_id", "")),
                subject=str(row.get("subject", "")),
                claim=str(row.get("claim", "")),
                confidence=str(row.get("confidence", "")),
                asset=asset,
                predicted=direction,
                created_at=created_at,
                horizon_hours=horizon_hours,
                p0=p0,
                p1=p1,
                outcome=outcome,
                source=evidence_source(row.get("evidence")),
            )
        )
    return records, counts


def aggregate(records: list[ScoredThesis]) -> dict[str, list[tuple]]:
    """Bucketed hit-rate tables. Sample sizes are exposed so the caller
    can flag noise-level rows (n<10) when rendering."""

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

    overall_n = len(records)
    overall_hits = sum(1 for r in records if r.outcome == "hit")
    overall_rate = overall_hits / overall_n if overall_n else 0.0
    return {
        "overall": [("all", overall_n, overall_hits, overall_rate)],
        "by_subject": _tally(lambda r: r.subject),
        "by_confidence": _tally(lambda r: r.confidence or "(unset)"),
        "by_source": _tally(lambda r: r.source),
        "by_predicted": _tally(lambda r: r.predicted),
    }
