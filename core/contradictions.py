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
# different poles. Substring matching against a claim, case-insensitive.
DIRECTION_LEXICON: dict[str, str] = {
    # down pole
    "declining": "down",
    "decreasing": "down",
    "falling": "down",
    "down": "down",
    "weakening": "down",
    "negative": "down",
    "contracting": "down",
    "bearish": "down",
    # up pole
    "increasing": "up",
    "rising": "up",
    "up": "up",
    "strengthening": "up",
    "positive": "up",
    "expanding": "up",
    "bullish": "up",
}


def _claim_pole(claim: str) -> str | None:
    """Return 'up' / 'down' / None for a claim string.

    If a claim contains lexicon words from both poles ("rising but with
    declining momentum") or none, it is uncomparable.
    """
    if not isinstance(claim, str) or not claim:
        return None
    lower = claim.lower()
    poles = {pole for word, pole in DIRECTION_LEXICON.items() if word in lower}
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
