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

    def test_no_expected_field_returns_no_phrase_mapping(self):
        # SPLIT: expected_field=None → 'no_phrase_mapping', not 'no_mapping'.
        v, m = classify_number(-2.17, None, self._payload())
        assert v == "no_phrase_mapping"
        # We DID identify which field it matches; useful diagnostic.
        assert m == "market_cap_change_24h"

    def test_expected_but_value_not_in_reference(self):
        # SPLIT: expected_field set but value matches no field →
        # 'value_not_in_reference' (distinct from no_phrase_mapping).
        v, m = classify_number(999.999, "bitcoin_dominance_percentage", self._payload())
        assert v == "value_not_in_reference" and m is None

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


class TestTimestampExclusion:
    """Unix timestamps and next-window epochs must NEVER count as a
    cited-value candidate. The F&G "1789689600" case surfaced when
    trivial-mapping treated the timestamp as a value confusion."""

    def test_timestamp_field_never_matches(self):
        payload = {"value": 63, "timestamp": 1789689600}
        # Cited number 1789689600 must NOT resolve to any field — it's
        # metadata, not data. Even under trivial-mapping to F&G value.
        v, m = classify_number(1789689600, "value", payload)
        assert v == "value_not_in_reference" and m is None

    def test_next_funding_time_excluded(self):
        payload = {"lastFundingRate": 0.0001, "nextFundingTime": 1789488000000}
        v, m = classify_number(1789488000000, "lastFundingRate", payload)
        # 1789488000000 is nextFundingTime (excluded) → value_not_in_reference.
        assert v == "value_not_in_reference"

    def test_data_field_still_matches(self):
        # Regression: excluding timestamp doesn't break normal data values.
        payload = {"value": 63, "timestamp": 1789689600}
        v, m = classify_number(63, "value", payload)
        assert v == "true_pass" and m == "value"


class TestCyclePayloadPersistWiring:
    def test_brain_writes_cycle_payload_entry(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # Structural grep-lock: after cycle work the objective's
        # evidence gains a cycle_payload entry with the RAW tool_results
        # list (no [:300] truncation on this path).
        assert '"type": "cycle_payload"' in src
        assert "tool_results" in src
        # Untruncated: no [:300] between the tool_results build and
        # the update_objective call.

    def test_auto_complete_merges_cycle_payload_into_findings(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # Cycle_payload entries are turned back into TOOL RESULTS
        # strings and appended to findings before extract_theses runs.
        assert 'entry.get("type") != "cycle_payload"' in src
        assert "TOOL RESULTS:" in src


class TestSingleFieldTools:
    def test_fear_greed_trivial_mapping(self):
        # get_fear_greed_index has ONE scalar field. Any citation
        # from that tool trivially resolves to 'value' regardless of
        # phrase context.
        assert phrase_to_field("get_fear_greed_index", "") == "value"
        assert phrase_to_field("get_fear_greed_index", "the value") == "value"

    def test_trivial_mapping_enables_true_pass(self):
        # Combined with classify_number: a bare F&G value citation
        # is scored true_pass, not no_phrase_mapping.
        payload = {"value": 63}
        expected = phrase_to_field("get_fear_greed_index", "")
        v, m = classify_number(63, expected, payload)
        assert v == "true_pass" and m == "value"


class TestParseFindingsAndClassify:
    def test_parse_findings_extracts_per_source_payload(self):
        from core.field_confusion import parse_findings_payloads
        finding = (
            'TOOL RESULTS:\n'
            '- get_fear_greed_index: {"value": 63, "value_classification": "Greed"}\n'
            '- get_crypto_global_market: {"bitcoin_dominance_percentage": 55.92, '
            '"market_cap_change_24h": -2.17, "volume_24h_usd": 1.67e11}\n'
        )
        payloads, truncated = parse_findings_payloads([finding])
        assert truncated == 0
        assert payloads["get_fear_greed_index"]["value"] == 63
        assert payloads["get_crypto_global_market"]["market_cap_change_24h"] == -2.17

    def test_truncated_json_counted_separately(self):
        # 300-char cap mid-payload → open brace, no close. Line skipped
        # but truncation counter increments.
        from core.field_confusion import parse_findings_payloads
        truncated_line = (
            '- get_crypto_global_market: {"market_cap_usd": 27000000'
        )
        _, trunc = parse_findings_payloads([truncated_line])
        assert trunc == 1

    def test_write_time_flags_dominance_case_from_real_findings(self):
        # Reproduce the operator's flagship case using real finding text.
        from core.field_confusion import (
            parse_findings_payloads, classify_thesis_evidence,
        )
        finding = (
            'TOOL RESULTS:\n'
            '- get_crypto_global_market: {"bitcoin_dominance_percentage": 55.92, '
            '"market_cap_change_24h": -2.17, "market_cap_usd": 2.7e12}\n'
        )
        payloads, _ = parse_findings_payloads([finding])
        thesis_evidence = [{
            "source": "get_crypto_global_market",
            "detail": "BTC dominance at -2.17% (24h)",
        }]
        recs = classify_thesis_evidence(thesis_evidence, payloads)
        # The number -2.17 was attributed to a dominance-shaped detail;
        # phrase "dominance" resolves to bitcoin_dominance_percentage but
        # the value matches market_cap_change_24h → field_confusion.
        confusions = [r for r in recs if r["verdict"] == "field_confusion"]
        assert len(confusions) == 1
        assert confusions[0]["expected_field"] == "bitcoin_dominance_percentage"
        assert confusions[0]["matched_field"] == "market_cap_change_24h"


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
