"""Field-confusion detection: does a cited number come from the RIGHT
field of the source tool's output?"""

from __future__ import annotations

from core.field_confusion import (
    FIELD_PHRASES, AMBIGUOUS_PHRASES,
    phrase_to_field, iter_numbers_with_context, classify_number,
)


class TestFieldMap:
    def test_all_configured_tools_have_at_least_one_phrase(self):
        for source, mapping in FIELD_PHRASES.items():
            assert mapping, f"{source} has empty field map"
            for field, phrases in mapping.items():
                assert phrases, f"{source}.{field} has no phrases"

    def test_ambiguous_phrases_are_reserved(self):
        # "value" / "rate" / "change" alone map to no field.
        for source in FIELD_PHRASES:
            for word in AMBIGUOUS_PHRASES:
                # A phrase that IS exactly the ambiguous word should
                # not be listed as a distinctive-phrase for any field.
                for field, phrases in FIELD_PHRASES[source].items():
                    assert word not in phrases, (
                        f"{source}.{field} lists ambiguous phrase {word!r}"
                    )


class TestPhraseResolution:
    def test_dominance_phrase_resolves(self):
        assert phrase_to_field(
            "get_crypto_global_market", "BTC dominance at",
        ) == "bitcoin_dominance_percentage"

    def test_market_cap_change_beats_market_cap(self):
        # Longest phrase wins.
        assert phrase_to_field(
            "get_crypto_global_market", "24-hour change of",
        ) == "market_cap_change_24h"
        assert phrase_to_field(
            "get_crypto_global_market", "market_cap_usd",
        ) == "market_cap_usd"

    def test_no_phrase_returns_none(self):
        assert phrase_to_field("get_crypto_global_market", "") is None
        assert phrase_to_field("get_crypto_global_market", "unrelated text") is None

    def test_unknown_tool_returns_none(self):
        assert phrase_to_field("not_a_tool", "dominance") is None


class TestIterNumbersWithContext:
    def test_yields_all_numbers(self):
        pairs = list(iter_numbers_with_context(
            "market cap 2.7e12 with 24h change -2.17%"
        ))
        vals = [v for v, _ in pairs]
        assert 2.7e12 in vals
        assert -2.17 in vals

    def test_prefix_carries_local_context(self):
        pairs = list(iter_numbers_with_context(
            "market cap 2.7e12 with 24h change -2.17%"
        ))
        # The number 2.17 is preceded by "24h change" — enough for the
        # phrase resolver to map to market_cap_change_24h.
        _, prefix_2 = pairs[-1]
        assert "change" in prefix_2

    def test_empty_detail_yields_nothing(self):
        assert list(iter_numbers_with_context("")) == []


class TestClassifyNumber:
    def _payload(self):
        return {
            "bitcoin_dominance_percentage": 55.92,
            "market_cap_change_24h": -2.17,
            "market_cap_usd": 2.7e12,
            "volume_24h_usd": 1.67e11,
        }

    def test_matches_expected_field_true_pass(self):
        v, m = classify_number(-2.17, "market_cap_change_24h", self._payload())
        assert v == "true_pass" and m == "market_cap_change_24h"

    def test_matches_different_field_flags_confusion(self):
        # A -2.17 attributed to bitcoin_dominance_percentage would be
        # a genuine field confusion — dominance is not -2.17.
        v, m = classify_number(-2.17, "bitcoin_dominance_percentage", self._payload())
        assert v == "field_confusion"
        assert m == "market_cap_change_24h"

    def test_no_expected_field_no_mapping(self):
        v, m = classify_number(-2.17, None, self._payload())
        assert v == "no_mapping"
        # We DID identify which field it matches; the phrase just
        # didn't name it. Useful diagnostic.
        assert m == "market_cap_change_24h"

    def test_value_matches_nothing(self):
        v, m = classify_number(999.999, "bitcoin_dominance_percentage", self._payload())
        assert v == "no_mapping" and m is None

    def test_multi_number_detail_scanned(self):
        # Scan every number in the detail. The dominance one is a
        # true_pass; -2.17 without a phrase-context that names its
        # field is a no_mapping (not a field_confusion because the
        # phrase didn't ASSERT anything).
        detail = "55.92% dominance with 24-hour change of -2.17%"
        payload = TestClassifyNumber._payload(TestClassifyNumber())
        outcomes = []
        for v, prefix in iter_numbers_with_context(detail):
            expected = phrase_to_field("get_crypto_global_market", prefix)
            verdict, matched = classify_number(v, expected, payload)
            outcomes.append((v, verdict, matched))
        # Dominance matches — the phrase precedes the number.
        # -2.17 preceded by "24-hour change" → expected=market_cap_change_24h → true_pass.
        assert any(o for o in outcomes if o[1] == "true_pass")
        assert any(o[2] == "market_cap_change_24h" for o in outcomes)


class TestSessionReportWiring:
    def test_report_renders_field_confused_count(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.fidelity_checked = 100
        r.fidelity_rewritten = 5
        r.fidelity_dropped = 3
        r.field_confusions = 7
        out = r.render()
        assert "field-confused" in out
        assert "7 field-confused" in out
