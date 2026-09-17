"""Time-window guard in the number extractor + subject canonicalisation
for contradiction grouping. Reproduces the 047dfde-era failure modes
observed in the corpus."""

from __future__ import annotations

import pytest

from analysis.thesis_backtest_descriptive import extract_reported_value
from core.contradictions import canonicalize_subject_for_grouping as _canon
from core.numeric_fidelity import _all_numbers_in


class TestTimeWindowGuardOnExtract:
    """Real detail-shape strings from the last 12 drops (all cited=24)."""

    @pytest.mark.parametrize("detail, expected", [
        # The four real dropped-thesis shapes — "24" is a WINDOW, not a value.
        ("24-hour trading volume shows increase", None),
        ("Crypto 24h trading volume increased", None),
        ("over the past 24 hours the market cap declined", None),
        ("24-hour change is +65.59%", 65.59),
        # Windows in various forms → skipped.
        ("5 min funding rate cycle", None),
        ("7 days of consolidation", None),
        ("30 minutes into the session", None),
        # Numbers PAST a window phrase must be extracted.
        ("over the past 24 hours BTC gained 3.4%", 3.4),
        ("in the last 7 days market cap rose $80 billion", 80.0),
        # Scientific notation regression — used to collapse to bare "1".
        ("$1.531e10 volume 24h", 1.531e10),
        # Non-window numbers keep their behaviour.
        ("2970 gwei gas price", 2970.0),
        ("$21.26 trillion market", 21.26),
    ])
    def test_extract_skips_time_windows(self, detail, expected):
        got = extract_reported_value([{"detail": detail}])
        assert got == expected, f"detail={detail!r}"

    def test_only_window_number_returns_none_not_drop(self):
        # A detail whose ONLY number is a timeframe returns None so the
        # gate PASSes with reason=no_number_in_detail — never a DROP.
        assert extract_reported_value([{"detail": "trading volume over 24 hours"}]) is None
        assert extract_reported_value([{"detail": "24h window"}]) is None


class TestAllNumbersInSkipsWindows:
    """The tool-digest search regex must also skip windows so a value
    that happens to equal a duration doesn't spuriously match."""

    def test_windows_excluded_from_candidate_list(self):
        # "market_cap_change_24h" contains "24" then "h" — window; skip.
        digest = ('{"market_cap_change_24h": -2.01, "market_cap_usd": 2694880000000, '
                    '"volume_24h_usd": 167111000000}')
        nums = _all_numbers_in(digest)
        assert 24.0 not in nums or nums.count(24.0) == 0
        assert -2.01 in nums
        assert 2694880000000 in nums

    def test_free_prose_windows_also_skipped(self):
        nums = _all_numbers_in("24-hour change: -2.01% over 5 days")
        assert 24.0 not in nums and 5.0 not in nums
        assert -2.01 in nums


class TestSubjectCanonicalisation:
    """Grouping-key transform ONLY — stored thesis subjects untouched."""

    def test_lowercase_and_prefix_strip(self):
        assert _canon("Global crypto market cap 24h change") == "market cap 24h change"
        assert _canon("crypto market cap 24h change") == "market cap 24h change"
        assert _canon("global crypto market cap 24h change") == "market cap 24h change"

    def test_market_cap_vs_volume_stay_distinct(self):
        # Genuinely different subjects MUST NOT merge.
        assert _canon("Global crypto market cap") != _canon("Global crypto trading volume")
        assert _canon("crypto trading volume") == "trading volume"
        assert _canon("crypto market cap") == "market cap"

    def test_whitespace_collapsed(self):
        assert _canon("  Global   Crypto   Market Cap  ") == "market cap"

    def test_no_leading_prefix_left_alone(self):
        assert _canon("Ethereum gas price") == "ethereum gas price"
        assert _canon("BTC funding rate") == "btc funding rate"
        assert _canon("Fear & Greed Index") == "fear & greed index"

    def test_multiple_prefix_stopwords_all_stripped(self):
        assert _canon("The global crypto market cap") == "market cap"

    def test_non_string_returns_empty(self):
        assert _canon(None) == ""  # type: ignore[arg-type]

    def test_ethereum_stays_distinct_from_crypto(self):
        # "Ethereum ..." must not lose the asset qualifier.
        assert _canon("Ethereum 24h trading volume") == "ethereum 24h trading volume"


class TestBrainWiresCanonicalizationForGrouping:
    def test_detect_contradictions_uses_canonicalize(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.detect_contradictions)
        assert "canonicalize_subject_for_grouping" in src
        # It must be called on pair_subject build.
        assert "_canon(str(ta.get(\"subject\") or \"\"))" in src
