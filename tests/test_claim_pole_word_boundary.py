"""Word-boundary claim pole tests.

Regression-locks the fix for the substring-collision bug that let
``upcoming`` inherit the ``up`` pole (via ``"up" in "upcoming"``) and
produced 2 false ``upcoming vs decreasing`` contradiction pairs that
survived the temporal remediation as "genuine".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core.contradictions import _claim_pole, claims_oppose


# ---------- pole matrix ----------------------------------------------------

@pytest.mark.parametrize(
    "claim, expected",
    [
        # The bug that motivated this fix.
        ("upcoming", None),
        # Other substring-collision candidates that must now return None.
        ("update", None),
        ("upgrade", None),
        ("upper", None),
        ("downside", None),
        ("upside", None),
        ("positively", None),
        ("negatively", None),
        # Bare pole words still work.
        ("up", "up"),
        ("down", "down"),
        # Standard directional claims.
        ("declining", "down"),
        ("increasing", "up"),
        ("bearish", "down"),
        ("bullish", "up"),
        # Multi-token claims still resolve to a pole.
        ("bearish high", "down"),
        ("mostly bullish", "up"),
        # Punctuation must not defeat the tokenizer.
        ("positive (short-term change rate)", "up"),
        ("negative — sustained", "down"),
        # Newly-added lexicon entries: uptrend / downtrend must have a pole
        # now that word-boundary matching would otherwise miss them.
        ("uptrend", "up"),
        ("downtrend", "down"),
        # Mixed-pole claims stay uncomparable.
        ("rising but with declining momentum", None),
        # Empty / non-string inputs.
        ("", None),
    ],
)
def test_claim_pole_matrix(claim: str, expected: str | None) -> None:
    assert _claim_pole(claim) == expected


def test_claim_pole_ignores_non_string() -> None:
    assert _claim_pole(None) is None  # type: ignore[arg-type]
    assert _claim_pole(42) is None  # type: ignore[arg-type]


# ---------- claims_oppose behaviour ---------------------------------------

def test_upcoming_no_longer_opposes_decreasing() -> None:
    """The exact bug: ``upcoming vs decreasing`` produced 2 false pairs."""
    assert claims_oppose("upcoming", "decreasing") is False
    assert claims_oppose("upcoming", "declining") is False


def test_uptrend_downtrend_still_oppose() -> None:
    """Compensation entries in DIRECTION_LEXICON preserve this pairing."""
    assert claims_oppose("uptrend", "downtrend") is True


def test_directional_pairs_still_oppose() -> None:
    """Nothing else regressed."""
    assert claims_oppose("declining", "increasing") is True
    assert claims_oppose("bearish", "bullish") is True
    assert claims_oppose("bearish high", "increasing") is True


# ---------- remediation classifier under the fix --------------------------

def test_remediation_voids_pole_fix_pair() -> None:
    """A ``upcoming vs decreasing`` pair is now voided_pole_fix, ahead of
    every other rule (a pair that no longer opposes was a false positive
    from the substring era and cannot be a genuine contradiction).
    """
    from scripts.remediate_contradictions import _classify

    window_seconds = 6.0 * 3600.0
    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    pair = {
        "subject_a": "Bitcoin difficulty adjustment",
        "subject_b": "Bitcoin mining difficulty adjustments",
        "claim_a": "upcoming",          # no pole under the fix
        "claim_b": "decreasing",
        "created_at_a": now,
        "created_at_b": now - timedelta(hours=3),
    }
    assert _classify(pair, window_seconds) == "voided_pole_fix"


def test_remediation_keeps_genuine_opposing_pair() -> None:
    """A same-window pair with real opposition is still ``kept``.

    Uses "Mining profitability" (non-price-class) with a 3h gap so
    the 6h default window governs — this test exercises the pole-
    detection classifier path, not the per-class window boundary
    (see tests/test_contradiction_priceclass for that)."""
    from scripts.remediate_contradictions import _classify

    now = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    pair = {
        "subject_a": "Mining profitability",
        "subject_b": "Mining profitability",
        "claim_a": "declining",
        "claim_b": "increasing",
        "created_at_a": now,
        "created_at_b": now - timedelta(hours=3),
    }
    assert _classify(pair, 0.0) == "kept"
