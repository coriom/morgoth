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
        subs = [
            "BTC futures funding rate",
            "BTC funding rate",
            "BTCUSDT funding rate",
            "BTC perpetual funding rate",
            "BTC long-short ratio",
        ]
        families, extra = measure_fragmentation(subs)
        assert "funding_rate" in families
        assert len(families["funding_rate"]) == 4
        assert extra == 3

    def test_uses_raw_subject_not_canonical(self):
        # LOCK: measure_fragmentation MUST operate on raw model-written
        # subjects, not the canonicalised form. A later change to the
        # canonicalisation function must not be able to fake an
        # improvement by collapsing variants.
        import inspect
        from analysis.campaign_quality import score_campaign
        src = inspect.getsource(score_campaign)
        # scorer feeds raw subjects into the fragmentation call.
        assert 'raw_subs = [t.get("subject") for t in theses]' in src
        # scorer does NOT read canonical_subject on the fragmentation path.
        for line in src.splitlines():
            if "measure_fragmentation" in line:
                # the call itself uses raw_subs — assert so.
                assert "raw_subs" in line
        # no `canonical_subject` reference in the scorer body at all.
        assert 't.get("canonical_subject")' not in src


class TestServiceability:
    def test_funding_title_serviceable_legacy_signature(self):
        # Legacy no-subject signature: match against the whole title.
        assert title_is_serviceable("BTC funding rate premium at 8h")

    def test_cbdc_title_unservable(self):
        assert not title_is_serviceable("Central bank digital currency adoption pace")

    def test_angle_serviceability_strips_campaign_subject(self):
        # Campaign subject appears in every title — must be stripped
        # before the rail-keyword match. Otherwise every title trivially
        # matches "funding" and the check reports 0 unservable.
        subj = "BTC Funding Rate and Positioning"
        # Serviceable ANGLE (mentions hashrate → rail keyword):
        assert title_is_serviceable(
            "BTC Funding Rate and Positioning via hashrate divergence",
            campaign_subject=subj,
        )
        # Unservable ANGLE (only mentions stablecoin reserve ratios):
        assert not title_is_serviceable(
            "Stablecoin Reserve Ratios impact on BTC Funding Rate and Positioning",
            campaign_subject=subj,
        )
        # Unservable ANGLE (CBDC):
        assert not title_is_serviceable(
            "Central Bank Digital Currency Adoption vs BTC Funding Rate and Positioning",
            campaign_subject=subj,
        )
        # Unservable ANGLE (influencer sentiment):
        assert not title_is_serviceable(
            "Crypto Influencer Sentiment Analysis and BTC Funding Rate and Positioning",
            campaign_subject=subj,
        )


class TestCrossSubjectTightening:
    def test_cross_subject_tolerance_is_narrow(self):
        # LOCK: tolerance stays ≤ 0.1 %. A wider window flags order-of-
        # magnitude coincidences (mark price 85776.9 "matching" onchain
        # hash rate 8.5e20). Narrow tolerance is the whole point of B's
        # tightening.
        from analysis.campaign_quality import CROSS_SUBJECT_TOL
        assert CROSS_SUBJECT_TOL <= 0.001

    def test_wide_scale_no_longer_flags(self):
        # 85776.9 (mark price) vs 6.2e20 (hash rate) — same first digits
        # but 15 orders of magnitude apart. Must NOT flag.
        payloads = {
            "get_bitcoin_futures_funding": {"markPrice": 85776.9},
            "get_bitcoin_onchain": {"hash_rate": 6.2e20},
        }
        t = {"subject": "BTC mark vs index", "evidence": [
            {"source": "get_bitcoin_futures_funding",
             "detail": "mark 85776.9 vs index 85777.0"},
        ]}
        hit, _ = classify_cross_subject_value(t, payloads)
        # markPrice=85776.9 is in own payload → not cross-subject.
        assert not hit

    def test_exact_cross_source_still_flags(self):
        # A cross-source EXACT-value match still flags.
        payloads = {
            "get_crypto_price": {"price": 1.0},
            "fred_series_observations": {"value": 332.813},
        }
        t = {"subject": "USDT price", "evidence": [
            {"source": "get_crypto_price", "detail": "cited price 332.813"},
        ]}
        hit, ex = classify_cross_subject_value(t, payloads)
        assert hit
        assert "fred_series_observations" in ex
        assert "332.813" in ex  # payload value made visible


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
