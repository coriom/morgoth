"""Campaign quality scorer — fixture tests for the six error classes."""

from __future__ import annotations

from analysis.campaign_quality import (
    METRIC_FAMILIES, RAIL_TOOL_FIELDS,
    classify_scope_misattribution, classify_direction_error,
    classify_unsourced_subject, classify_cross_subject_value,
    measure_fragmentation, score_thesis, title_is_serviceable,
    render_report, QualityReport,
)


class TestScopeMisattribution:
    def test_btc_volume_attributed_to_global_market_flags(self):
        t = {
            "subject": "BTC 24h trading volume",
            "evidence": [{
                "source": "get_crypto_global_market",
                "detail": "trading volume 262836585791",
            }],
        }
        hit, _ = classify_scope_misattribution(t)
        assert hit

    def test_btc_dominance_from_global_market_is_ok(self):
        # dominance IS a global-market field — not a scope error.
        t = {
            "subject": "BTC dominance",
            "evidence": [{
                "source": "get_crypto_global_market",
                "detail": "dominance at 55.92%",
            }],
        }
        hit, _ = classify_scope_misattribution(t)
        assert not hit

    def test_non_btc_subject_never_flags(self):
        t = {
            "subject": "global market cap",
            "evidence": [{"source": "get_crypto_global_market",
                          "detail": "market cap 2.7e12"}],
        }
        hit, _ = classify_scope_misattribution(t)
        assert not hit


class TestDirectionError:
    def test_long_short_ratio_below_one_bullish_flags(self):
        t = {
            "subject": "BTC long-short ratio",
            "claim": "bullish - more buyers than sellers",
            "evidence": [{"source": "get_bitcoin_long_short_ratio",
                          "detail": "longShortRatio 0.5269"}],
        }
        hit, _ = classify_direction_error(t)
        assert hit

    def test_long_short_ratio_above_one_bullish_is_ok(self):
        t = {
            "subject": "BTC long-short ratio",
            "claim": "bullish - crowd is long",
            "evidence": [{"source": "get_bitcoin_long_short_ratio",
                          "detail": "longShortRatio 1.45"}],
        }
        hit, _ = classify_direction_error(t)
        assert not hit

    def test_positive_funding_bearish_flags(self):
        t = {
            "subject": "BTC futures funding rate",
            "claim": "bearish sentiment - selling pressure",
            "evidence": [{"source": "get_bitcoin_futures_funding",
                          "detail": "lastFundingRate 0.0001"}],
        }
        hit, _ = classify_direction_error(t)
        assert hit

    def test_neutral_claim_never_flags(self):
        t = {"subject": "BTC funding rate", "claim": "stable",
             "evidence": [{"source": "get_bitcoin_futures_funding",
                            "detail": "0.0001"}]}
        hit, _ = classify_direction_error(t)
        assert not hit


class TestUnsourcedSubject:
    def test_no_evidence_at_all_flags(self):
        t = {"subject": "CBDC adoption pressure", "claim": "increasing",
             "evidence": []}
        hit, _ = classify_unsourced_subject(t)
        assert hit

    def test_web_search_evidence_is_sourced(self):
        t = {"subject": "regulatory framework EU", "claim": "tightening",
             "evidence": [{"source": "web_search", "detail": "MiCA…"}]}
        hit, _ = classify_unsourced_subject(t)
        assert not hit

    def test_rail_source_is_sourced(self):
        t = {"subject": "BTC hashrate", "claim": "rising",
             "evidence": [{"source": "get_bitcoin_onchain",
                            "detail": "hash_rate 6.2e20"}]}
        hit, _ = classify_unsourced_subject(t)
        assert not hit


class TestCrossSubjectValue:
    def test_value_matches_other_tool_flags(self):
        # 332.813 belongs to fred_series_observations (CPIAUCSL), not USDT.
        payloads = {
            "get_crypto_price":         {"price": 1.0},
            "fred_series_observations": {"value": 332.813},
        }
        t = {"subject": "USDT price", "evidence": [
            {"source": "get_crypto_price", "detail": "price 332.813"},
        ]}
        hit, _ = classify_cross_subject_value(t, payloads)
        assert hit

    def test_value_matches_own_tool_does_not_flag(self):
        payloads = {"get_crypto_price": {"price": 332.813}}
        t = {"subject": "USDT price", "evidence": [
            {"source": "get_crypto_price", "detail": "price 332.813"},
        ]}
        hit, _ = classify_cross_subject_value(t, payloads)
        assert not hit


class TestFragmentation:
    def test_funding_variants_counted(self):
        canonical_subs = [
            "btc futures funding rate",
            "btc funding rate",
            "btcusdt funding rate",
            "btc perpetual funding rate",
            "btc long-short ratio",
        ]
        families, extra = measure_fragmentation(canonical_subs)
        assert "funding_rate" in families
        assert len(families["funding_rate"]) == 4
        # 4 funding variants → 3 extra; long-short is a single → 0.
        assert extra == 3


class TestServiceability:
    def test_funding_title_serviceable(self):
        assert title_is_serviceable("BTC funding rate premium at 8h")

    def test_cbdc_title_unservable(self):
        assert not title_is_serviceable("Central bank digital currency adoption pace")


class TestScoreThesis:
    def test_field_confusion_uses_classifier(self):
        # -2.17 attributed to bitcoin_dominance is a field-confusion.
        payloads = {"get_crypto_global_market": {
            "bitcoin_dominance_percentage": 55.92,
            "market_cap_change_24h": -2.17,
        }}
        t = {
            "subject": "BTC dominance",
            "evidence": [{
                "source": "get_crypto_global_market",
                "detail": "BTC dominance at -2.17%",
            }],
        }
        v = score_thesis(t, findings_payloads=payloads)
        assert v["field_confusion"][0]


class TestRailFieldsCatalog:
    def test_every_rail_tool_has_a_field_map(self):
        assert "get_bitcoin_futures_funding" in RAIL_TOOL_FIELDS
        # Every rail tool the field_confusion table declares MUST also
        # be in RAIL_TOOL_FIELDS — otherwise cross-source checks silently
        # skip it.
        from core.field_confusion import FIELD_PHRASES
        for src in FIELD_PHRASES:
            assert src in RAIL_TOOL_FIELDS, f"{src} missing from RAIL_TOOL_FIELDS"


class TestRender:
    def test_render_prints_all_sections(self):
        rep = QualityReport(campaign_id="12345678-abcd", subject="test",
                            n_objectives=1, n_theses=2)
        rep.class_counts["scope_misattribution"] = 1
        rep.class_counts["field_confusion"] = 0
        rep.unservable_titles = ["something unservable"]
        rep.top_missing_themes = [("cbdc", 3)]
        out = render_report(rep)
        assert "A · ERROR CLASSES" in out
        assert "B · lastFundingRate=0.0001" in out
        assert "C · ANGLE SERVICEABILITY" in out
        assert "scope_misattribution" in out
