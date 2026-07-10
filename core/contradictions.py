"""Cross-thesis contradiction detection.

A contradiction = two active theses whose SUBJECTS refer to the same object
AND whose CLAIMS point in opposite directional poles.

Subject matching is SEMANTIC, via the same ChromaDB default embedding
(MiniLM, 384-d) used by EpisodicMemory — the 8B does not normalize subjects
("BTC transaction volume" vs "Bitcoin tx volume") so exact-string match would
miss most real contradictions.

Claim opposition uses a small closed DIRECTION_LEXICON (rules beat vectors on
a finite vocabulary). Same-pole synonyms (declining ≈ decreasing) are NOT
contradictions; cross-pole pairs are. Claims that map to NO pole, or BOTH
poles, are uncomparable — no contradiction is signaled (inert, not a false
positive).
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Callable

from chromadb.utils import embedding_functions
from loguru import logger


# Tunable. Calibrated against real Morgoth subjects: "BTC X" vs "Bitcoin X"
# pairs land around 0.85-0.91; distinct concepts ("price" vs "volume") land
# around 0.48. 0.75 separates the two cleanly. Raise to be stricter, lower
# to be more lenient about subject variants.
SUBJECT_SIMILARITY_THRESHOLD: float = 0.75


# Temporal window for the CONTRADICTION classification.
#
# Rationale: subjects like "BTC price" are point-in-time readings of a rolling
# metric. Two opposite readings made ≥6 hours apart are almost certainly
# capturing different windows of the same time-series, not a genuine
# disagreement. This is REVISION (the belief was updated), not
# CONTRADICTION. Within-window opposites are kept — they signal a real
# reasoning or extraction conflict inside a single reading of the world.
#
# Env override lets the runtime tune without a code change:
#   CONTRADICTION_WINDOW_HOURS=6.0
CONTRADICTION_WINDOW_HOURS: float = float(
    os.environ.get("CONTRADICTION_WINDOW_HOURS", "6.0")
)


# Per-class window for price-direction subjects.
#
# Baseline verdict on 19 live pairs: 16/19 (84%) were price-direction
# subjects with 1–4h gaps — both readings TRUE at their moment, not a
# real disagreement. The 6h window is wider than that subject class's
# own volatility, so it treats extraction variance as contradiction.
# Tighter window (2h) matches the class's actual timescale; the 3
# analytical pairs (mining difficulty adjustment, mining profitability)
# stay open because their subjects are not price-class.
#
# Same env-override pattern as the flat constant:
#   CONTRADICTION_WINDOW_HOURS_PRICE=2.0
CONTRADICTION_WINDOW_HOURS_PRICE: float = float(
    os.environ.get("CONTRADICTION_WINDOW_HOURS_PRICE", "2.0")
)


# Price-class subject tokens — same shape as SHORT_TERM_TOKENS, same
# substring/case-insensitive matching. "price", "change", "trend",
# "volume" are the recurring intraday-metric keywords in the live
# subject distribution. If a subject carries a long-term qualifier
# (weekly/monthly/yearly), the long-timeframe path already keeps it
# in its own bucket via subjects_timeframe_conflict — this classifier
# does not need to re-check that.
PRICE_CLASS_TOKENS: frozenset[str] = frozenset({
    "price",
    "change",
    "trend",
    "volume",
})


def subject_is_price_class(subject: str) -> bool:
    """True iff ``subject`` contains a price-class token.

    Substring match, case-insensitive — mirrors the timeframe-guard
    normalization so the two classifiers behave the same way on
    hyphenated / compound wording.
    """
    if not isinstance(subject, str) or not subject:
        return False
    low = subject.lower()
    return any(tok in low for tok in PRICE_CLASS_TOKENS)


def window_for(subject_a: str, subject_b: str) -> float:
    """Return the contradiction window (hours) for a subject pair.

    Conservative: a MIXED pair (one price-class, one not) takes the
    TIGHTER window (2h). Extraction variance on the price-class side
    would otherwise leak through the wider window.
    """
    if subject_is_price_class(subject_a) or subject_is_price_class(subject_b):
        return CONTRADICTION_WINDOW_HOURS_PRICE
    return CONTRADICTION_WINDOW_HOURS


# Timeframe qualifiers that make two subjects NON-COMPARABLE regardless of
# what the embedding says. "long-term price trends" vs "short-term price
# trend" are semantically similar in embedding space (both mention "price
# trend") but describe different horizons — comparing their claims across
# horizons is a category mistake, not a contradiction.
#
# Substring match, case-insensitive. Kept tight — no gray-zone words.
SHORT_TERM_TOKENS: frozenset[str] = frozenset({
    "short-term",
    "24-hour",
    "24h",
    "daily",
    "intraday",
})
LONG_TERM_TOKENS: frozenset[str] = frozenset({
    "long-term",
    "weekly",
    "monthly",
    "yearly",
})


def subjects_timeframe_conflict(subject_a: str, subject_b: str) -> bool:
    """True iff one subject carries a SHORT-term token and the other a LONG.

    Pure function. Same-side (both short, both long, neither) → False.
    """
    if not (isinstance(subject_a, str) and isinstance(subject_b, str)):
        return False
    a_low = subject_a.lower()
    b_low = subject_b.lower()
    a_short = any(tok in a_low for tok in SHORT_TERM_TOKENS)
    a_long = any(tok in a_low for tok in LONG_TERM_TOKENS)
    b_short = any(tok in b_low for tok in SHORT_TERM_TOKENS)
    b_long = any(tok in b_low for tok in LONG_TERM_TOKENS)
    return (a_short and b_long) or (a_long and b_short)


# Closed directional vocabulary. Two claims are opposed iff they map to
# different poles. Matching is WORD-BOUNDARY (whole-token), case-insensitive
# — a substring match would pull "up" out of "upcoming" and falsely pair
# temporal claims against directional ones (a live bug found after e95abd5
# left 2 "upcoming vs decreasing" pairs in the belief base).
#
# ``uptrend``/``downtrend`` are enumerated explicitly: substring matching
# used to catch them via ``up``/``down``, but whole-token matching does
# not, so they must be listed to preserve the directional signal.
# ``positively``/``negatively`` similarly lose their old substring hit and
# now produce no pole — an acceptable trade since those adverbs are rare
# as extraction claims.
DIRECTION_LEXICON: dict[str, str] = {
    # down pole
    "declining": "down",
    "decreasing": "down",
    "falling": "down",
    "down": "down",
    "downtrend": "down",
    "weakening": "down",
    "negative": "down",
    "contracting": "down",
    "bearish": "down",
    # up pole
    "increasing": "up",
    "rising": "up",
    "up": "up",
    "uptrend": "up",
    "strengthening": "up",
    "positive": "up",
    "expanding": "up",
    "bullish": "up",
}


# Pre-compiled tokenizer: pull runs of ASCII letters. Any non-letter (space,
# punctuation, digit) acts as a boundary — so ``positive (short-term change
# rate)`` tokenizes to ``[positive, short, term, change, rate]`` and picks
# up the ``positive`` pole cleanly.
_WORD_RE = re.compile(r"[a-z]+")


def _claim_pole(claim: str) -> str | None:
    """Return 'up' / 'down' / None for a claim string.

    Tokenizes the claim into whole words (``[a-z]+``) and matches DIRECTION_LEXICON
    entries as WHOLE TOKENS only. If the tokens include words from both poles
    ("rising but with declining momentum") or none, the claim is uncomparable.
    """
    if not isinstance(claim, str) or not claim:
        return None
    tokens = set(_WORD_RE.findall(claim.lower()))
    if not tokens:
        return None
    poles = {DIRECTION_LEXICON[t] for t in tokens if t in DIRECTION_LEXICON}
    if len(poles) != 1:
        return None
    return next(iter(poles))


def claims_oppose(claim_a: str, claim_b: str) -> bool:
    """True iff the two claims map to opposite directional poles."""
    pa = _claim_pole(claim_a)
    pb = _claim_pole(claim_b)
    if pa is None or pb is None:
        return False
    return pa != pb


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return s / (na * nb)


_embedding_fn: Any = None


def _get_embedding_fn() -> Callable[[list[str]], list[list[float]]]:
    """Return a cached Chroma default embedding function (MiniLM, 384-d)."""
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = embedding_functions.DefaultEmbeddingFunction()
    return _embedding_fn


def group_theses_by_subject(
    theses: list[dict[str, Any]],
    threshold: float = SUBJECT_SIMILARITY_THRESHOLD,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> list[list[dict[str, Any]]]:
    """Group theses by semantic subject similarity.

    Greedy single-link clustering: each thesis joins the first existing group
    whose representative subject cosine-similarity meets the threshold; else
    opens a new group. embed_fn is injectable for tests.
    """
    if not theses:
        return []
    fn = embed_fn or _get_embedding_fn()
    subjects = [(t.get("subject") or "").strip() for t in theses]
    embeddings = fn(subjects)
    groups_idx: list[list[int]] = []
    group_reps: list[list[float]] = []
    for i, emb in enumerate(embeddings):
        emb_list = list(emb)
        placed = False
        for g_idx, rep in enumerate(group_reps):
            if _cosine(emb_list, rep) >= threshold:
                groups_idx[g_idx].append(i)
                placed = True
                break
        if not placed:
            groups_idx.append([i])
            group_reps.append(emb_list)
    return [[theses[i] for i in g] for g in groups_idx]
