"""Per-class contradiction window — price-class subjects get 2h.

Baseline verdict on 19 live pairs: 16/19 were price-direction
subjects with 1–4h gaps (extraction variance). The 6h flat window
treated them as contradictions; the 2h price-class window keeps
them as supersession. The 3 analytical pairs (mining difficulty,
mining profitability) remain contradicted.

The classifier and window_for helper are pure functions — the tests
exercise them directly without spinning up brain.detect_contradictions.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from core import contradictions as C


# ---------- classifier: token match on real live subjects -----------

@pytest.mark.parametrize(
    "subject, expected",
    [
        ("BTC short-term price", True),                    # 8-thesis cluster
        ("Market capitalization change", True),            # 70ec80
        ("BTC 24-hour price change", True),                # 00623b
        ("Bitcoin price", True),                           # 45b9b6
        ("Bitcoin's short-term price trend", True),        # a7157e — trend token
        ("Ethereum short-term price", True),
        ("Bitcoin volume", True),                          # volume token
        # non-price: the 3 analytical subjects from the baseline
        ("Bitcoin mining difficulty adjustment", False),
        ("Mining profitability", False),
        ("Bitcoin difficulty adjustment progress", False),
        ("Ethereum hash rate", False),                     # neighbor cluster
        ("Bitcoin hash rate", False),
        # edge cases
        ("", False),
        ("some/other/subject", False),
    ],
)
def test_subject_is_price_class(subject: str, expected: bool) -> None:
    assert C.subject_is_price_class(subject) is expected


# ---------- window_for: mixed-pair takes the tighter window -------

def test_window_for_both_price_class_uses_price_window() -> None:
    w = C.window_for("BTC short-term price", "Ethereum short-term price")
    assert w == C.CONTRADICTION_WINDOW_HOURS_PRICE


def test_window_for_both_non_price_uses_default() -> None:
    w = C.window_for("Bitcoin mining difficulty adjustment", "Mining profitability")
    assert w == C.CONTRADICTION_WINDOW_HOURS


def test_window_for_mixed_pair_takes_tighter_window() -> None:
    """Conservative — a price-side extraction-variance flip shouldn't
    leak through the wider window just because the OTHER side of the
    (unlikely) match was non-price."""
    w = C.window_for("BTC short-term price", "Mining profitability")
    assert w == C.CONTRADICTION_WINDOW_HOURS_PRICE


def test_window_for_defaults_are_2_and_6() -> None:
    assert C.CONTRADICTION_WINDOW_HOURS_PRICE == 2.0
    assert C.CONTRADICTION_WINDOW_HOURS == 6.0


# ---------- env override on the price constant --------------------

def test_price_window_env_override() -> None:
    """Same pattern as CONTRADICTION_WINDOW_HOURS — env-driven, no
    code change. Restores the default at the end so downstream tests
    that read C.CONTRADICTION_WINDOW_HOURS_PRICE keep seeing 2.0."""
    import importlib
    try:
        with patch.dict(os.environ, {"CONTRADICTION_WINDOW_HOURS_PRICE": "3.5"}):
            mod = importlib.reload(C)
            assert mod.CONTRADICTION_WINDOW_HOURS_PRICE == 3.5
    finally:
        os.environ.pop("CONTRADICTION_WINDOW_HOURS_PRICE", None)
        mod = importlib.reload(C)
        assert mod.CONTRADICTION_WINDOW_HOURS_PRICE == 2.0


# ---------- separation-proof from the live baseline ---------------

def test_separation_matches_A3_verdict() -> None:
    """A3 recommendation was: the 16 price-class pairs go through the
    tight window, the 3 analytical pairs keep the wide window. Locks
    the classifier so a future edit that broadens PRICE_CLASS_TOKENS
    (e.g. adds "mining") would trip here and require an operator
    decision."""
    price_class_subjects = [
        "BTC 24-hour price change",
        "BTC short-term price",
        "Bitcoin price",
        "Bitcoin's short-term price trend",
        "Ethereum short-term price",
        "Market capitalization change",
    ]
    analytical_subjects = [
        "Bitcoin difficulty adjustment progress",
        "Bitcoin mining difficulty adjustment",
        "Mining profitability",
    ]
    for s in price_class_subjects:
        assert C.subject_is_price_class(s), f"expected price-class: {s!r}"
    for s in analytical_subjects:
        assert not C.subject_is_price_class(s), (
            f"analytical subject wrongly flagged price-class: {s!r}"
        )


# ---------- window matrix (integration-shape) ---------------------

@pytest.mark.parametrize(
    "subject_a, subject_b, gap_h, expected_over_window",
    [
        # price-class boundary
        ("BTC short-term price", "BTC short-term price", 1.9, False),
        ("BTC short-term price", "BTC short-term price", 2.1, True),
        # non-price boundary
        ("Mining profitability", "Mining profitability", 5.9, False),
        ("Mining profitability", "Mining profitability", 6.1, True),
        # mixed pair takes the tight window
        ("BTC short-term price", "Mining profitability", 1.9, False),
        ("BTC short-term price", "Mining profitability", 2.1, True),
    ],
)
def test_gap_vs_window_matrix(
    subject_a: str, subject_b: str, gap_h: float, expected_over_window: bool,
) -> None:
    w = C.window_for(subject_a, subject_b)
    assert (gap_h > w) is expected_over_window
