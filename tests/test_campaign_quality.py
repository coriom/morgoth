"""Campaign quality scorer — fixture tests for the six error classes."""

from __future__ import annotations

from analysis.campaign_quality import (
    METRIC_FAMILIES, RAIL_TOOL_FIELDS,
    classify_scope_misattribution, classify_direction_error,
    classify_unsourced_subject, classify_cross_subject_value,
    measure_fragmentation, score_thesis, title_is_serviceable,
    render_report, QualityReport,
    _extract_theme_phrases, _angle_residue_tokens_ordered,
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


class TestPhraseLevelServiceability:
    """2026-09-23: phrase-level check with per-tool served-concept map
    and off-rail overrides. Word-level check made "news", "economic",
    "sentiment" false-serviceable when the actual angle was reddit /
    influencer / social-media sentiment."""

    def test_market_sentiment_served_by_fear_greed(self):
        assert title_is_serviceable(
            "Angle X via Market Sentiment shift",
            campaign_subject="Angle X",
        )

    def test_reddit_sentiment_unservable_despite_the_word_sentiment(self):
        assert not title_is_serviceable(
            "Angle X via Reddit Sentiment Analysis",
            campaign_subject="Angle X",
        )

    def test_social_media_sentiment_unservable(self):
        assert not title_is_serviceable(
            "Angle X via Social Media Sentiment",
            campaign_subject="Angle X",
        )

    def test_influencer_sentiment_unservable(self):
        assert not title_is_serviceable(
            "Angle X via Crypto Influencer Sentiment",
            campaign_subject="Angle X",
        )

    def test_economic_news_served_by_get_news(self):
        assert title_is_serviceable(
            "Angle X via Major Economic News Impact",
            campaign_subject="Angle X",
        )

    def test_fred_inflation_served(self):
        assert title_is_serviceable(
            "Angle X via US Inflation Rates Impact",
            campaign_subject="Angle X",
        )

    def test_override_wins_over_served_phrase(self):
        # A title that mentions BOTH a served concept AND an off-rail
        # concept is UNSERVABLE — the model is proposing new data,
        # not repurposing get_news.
        assert not title_is_serviceable(
            "Angle X via Social Media Sentiment inferred from News",
            campaign_subject="Angle X",
        )


class TestServedPhrasesCatalog:
    def test_every_rail_tool_has_served_phrases(self):
        from analysis.campaign_quality import TOOL_SERVED_PHRASES, RAIL_TOOL_FIELDS
        # The scorer's rail tool set is the source of truth. Every rail
        # tool with a payload MUST declare at least one served phrase —
        # otherwise the serviceability check silently drops it.
        for src, fields in RAIL_TOOL_FIELDS.items():
            if not fields:
                continue  # qualitative sources (news, web_search) handled elsewhere
            if src == "get_stablecoin_market_activity":
                continue  # legacy label, no server
            assert src in TOOL_SERVED_PHRASES, (
                f"{src} has no entry in TOOL_SERVED_PHRASES"
            )
            assert TOOL_SERVED_PHRASES[src], f"{src} declares no served phrases"


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


class TestThemeExtraction:
    _CAMPAIGN2_UNSERVABLE_FIXTURES = [
        "Exploring Bitcoin via Unexplored Stablecoin Issuance Rates",
        "Exploring via Unexplored Stablecoin Reserve Ratios",
        "Exploring via Unexplored Stablecoin Market Activity",
        "Exploring via Unexplored Stablecoin Reserve Balances",
        "Exploring via Stablecoin Issuance Rates as a Variable",
        "Exploring via Unexplored Stablecoin Issuance Metrics",
        "Exploring via Emerging Global Stablecoin Issuance Trends",
        "Exploring via Unexplored Stablecoin Market Maker Profits",
        "Bitcoin in the Context of Stablecoin Market Dynamics",
        "Exploring via Unexplored Global Stablecoin Adoption Trends",
        "Exploring via Unexplored Central Bank Digital Currencies",
        "Exploring via Unexplored Central Bank Digital Currency Adoption",
        "Impact of Central Bank Digital Currencies on BTC",
        "Exploring via Unexplored Crypto Regulatory Environment News",
        "Exploring via Unexplored Blockchain Regulatory Compliance Framework",
        "Exploring via Unexplored Global Crypto Regulatory Frameworks",
        "Exploring via Unexplored Reddit Sentiment Analysis",
        "Exploring via Unexplored Bitcoin Social Media Sentiment Analysis",
        "Exploring via Unexplored Crypto Influencer Sentiment Analysis",
    ]

    def test_stablecoin_ranks_first_on_campaign2_fixture(self):
        subj = "BTC funding rate and positioning"
        residues = [
            " ".join(_angle_residue_tokens_ordered(t, subj))
            for t in self._CAMPAIGN2_UNSERVABLE_FIXTURES
        ]
        top = _extract_theme_phrases(residues, top_k=10)
        # stablecoin must rank first — 10 titles cite it in the fixture.
        assert top[0][0] == "stablecoin"

    def test_multi_word_phrases_surface(self):
        subj = "BTC funding rate and positioning"
        residues = [
            " ".join(_angle_residue_tokens_ordered(t, subj))
            for t in self._CAMPAIGN2_UNSERVABLE_FIXTURES
        ]
        top = _extract_theme_phrases(residues, top_k=15)
        phrases = [p for p, _ in top]
        # At least one bigram/trigram must appear — else single tokens are
        # burying real multi-word signals.
        assert any(" " in p for p in phrases)
        # "central bank digital" (trigram) should surface — 3 in fixture.
        assert any("central bank digital" in p for p in phrases)

    def test_generic_words_are_stopped(self):
        # "global", "trends", "changes", "adoption" alone must not be
        # top-ranked themes — they carry no signal about missing rail.
        subj = "BTC funding rate and positioning"
        residues = [
            " ".join(_angle_residue_tokens_ordered(t, subj))
            for t in self._CAMPAIGN2_UNSERVABLE_FIXTURES
        ]
        top = dict(_extract_theme_phrases(residues, top_k=20))
        for generic in ("global", "trends", "changes", "adoption",
                         "framework", "frameworks"):
            assert generic not in top, f"{generic!r} should be stopword"

    def test_redundant_containment_dropped(self):
        # "bank digital" (bigram) contained in "central bank digital"
        # (trigram) at same count → shorter one is dropped.
        residues = ["central bank digital currency"] * 3
        top = _extract_theme_phrases(residues, top_k=10)
        phrases = [p for p, _ in top]
        # trigram survives; the bigram "bank digital" must NOT co-appear.
        assert "central bank digital" in phrases
        assert "bank digital" not in phrases


class TestScorerFalsePositiveFixes:
    """2026-09-24: three false positives distorted the campaign 2 vs 3
    comparison. Lock each so a later change can't reintroduce them."""

    def test_long_short_account_ratio_maps_to_long_short_ratio(self):
        # Binance's own field name is "long/short account ratio". The
        # longest-match resolver must map this to longShortRatio, NOT
        # shortAccount (which is a shorter, wrongly-winning substring).
        from core.field_confusion import phrase_to_field
        assert phrase_to_field(
            "get_bitcoin_long_short_ratio",
            "the long/short account ratio dropped to 0.85 today",
        ) == "longShortRatio"
        # Also with variant separators.
        assert phrase_to_field(
            "get_bitcoin_long_short_ratio",
            "long-short account ratio print of 1.12",
        ) == "longShortRatio"

    def test_short_account_alone_still_maps_to_short_account(self):
        # Regression guard: the plain "short account" phrase (no "long/
        # short" prefix) still legitimately resolves to shortAccount.
        from core.field_confusion import phrase_to_field
        assert phrase_to_field(
            "get_bitcoin_long_short_ratio",
            "the short account share is 44 %",
        ) == "shortAccount"

    def test_derivatives_and_leverage_are_served(self):
        # Derivatives / leverage / basis are on the rail (futures_funding
        # + long_short_ratio provide the underlying data). Titles that
        # cite these must NOT be classified unservable.
        subj = "BTC funding rate and positioning"
        for angle in (
            "Angle X via BTC Derivatives Leverage Ratios",
            "Angle X via Mark-Index Basis Divergence",
            "Angle X via Retail Positioning Divergence",
            "Angle X via Whale Long/Short Positioning",
        ):
            assert title_is_serviceable(angle, campaign_subject=subj), (
                f"{angle!r} should be serviceable — rail covers it"
            )

    def test_0_0001_verdict_tolerance_is_one_percent(self):
        # 9242a8a2 cited 0.0001 vs Binance funding 7.77e-05 at that
        # timestamp — 29 % off. Under the previous 5 % tolerance this
        # was mislabelled 'genuine'. The gate's PASS band is 1 %, so
        # the verdict tolerance MUST match. Grep-lock via source.
        import inspect
        from analysis.campaign_quality import score_campaign
        src = inspect.getsource(score_campaign)
        # The `_close(cited, ref, X)` call must use ≤ 0.01.
        import re
        m = re.search(r'_close\(cited,\s*ref,\s*([\d.]+)\)', src)
        assert m, "verdict tolerance line missing"
        tol = float(m.group(1))
        assert tol <= 0.01, f"verdict tolerance {tol} exceeds gate PASS band 1 %"


class TestExactMatchGenuineAt1Percent:
    """Phase 1 lock: an exact-match 0.0001 cited vs 0.0001 Binance
    reference MUST verdict 'genuine' at the 1 % tolerance. Regression
    against a bug where a set change (quarantine release) made these
    invisible in the report."""

    def test_exact_0_0001_match_is_genuine_at_1pct(self):
        # Emulates the verdict math the scorer applies inline.
        from analysis.campaign_quality import _close
        assert _close(0.0001, 0.0001, 0.01)
        assert _close(0.00010, 0.00010, 0.01)

    def test_29pct_off_is_confused_at_1pct(self):
        # 9242a8a2: cited 0.0001 vs Binance 7.77e-05 → 28.7 % off.
        # Under old 5 % tol this was mislabelled 'genuine'; at 1 %
        # the correct verdict is 'confused'.
        from analysis.campaign_quality import _close
        assert not _close(0.0001, 7.77e-05, 0.01)

    def test_scorer_includes_released_theses(self):
        # Grep-lock: the SQL that pulls the cross-check set MUST
        # include theses whose evidence cites 0.0001 sourced from
        # get_bitcoin_futures_funding, regardless of quarantine_reason
        # (which is cleared when a thesis is released). Prior code
        # only queried quarantine_reason='interestrate_as_funding' →
        # a released exact-match thesis silently disappeared.
        import inspect
        from analysis.campaign_quality import score_campaign
        src = inspect.getsource(score_campaign)
        assert "get_bitcoin_futures_funding" in src
        assert "'%0.0001%'" in src or "0.0001" in src
        assert "OR (evidence::text LIKE" in src


class TestLengthControl:
    def test_score_campaign_accepts_limit_first_n(self):
        import inspect
        from analysis.campaign_quality import score_campaign
        sig = inspect.signature(score_campaign)
        assert "limit_first_n" in sig.parameters
        # keyword-only so a stray positional caller can't mask the default.
        assert sig.parameters["limit_first_n"].kind == inspect.Parameter.KEYWORD_ONLY

    def test_scorer_slices_first_n_oldest(self):
        # Grep-lock: score_campaign sorts by created_at ASC and slices.
        import inspect
        from analysis.campaign_quality import score_campaign
        src = inspect.getsource(score_campaign)
        assert "limit_first_n" in src
        assert 'key=lambda o: o.get("created_at")' in src
        assert '[:limit_first_n]' in src


class TestReflectDataGapsBlock:
    def test_empty_block_yields_byte_identical_prompt(self):
        # LOCK: when data_gaps_block is empty, _reflection_prompt output
        # must match the pre-data-gaps prompt byte-for-byte. Same
        # contract as rejections and leads.
        from self_modify.reflect import _reflection_prompt
        base_ctx = {
            "tools_block": "- t (data_source) — objectives_using=0: x",
            "objectives_block": "- OBJ", "theses_block": "- SUB",
            "rejections_block": "", "leads_block": "",
        }
        with_key = dict(base_ctx, data_gaps_block="")
        assert _reflection_prompt(base_ctx) == _reflection_prompt(with_key)
        assert "DATA GAPS" not in _reflection_prompt(with_key)

    def test_non_empty_block_appears(self):
        from self_modify.reflect import _reflection_prompt
        ctx = {
            "tools_block": "- t", "objectives_block": "- o",
            "theses_block": "- s", "rejections_block": "",
            "leads_block": "",
            "data_gaps_block": "- stablecoin  ×24\n- regulatory  ×17",
        }
        out = _reflection_prompt(ctx)
        assert "DATA GAPS" in out
        assert "stablecoin" in out and "×24" in out
        # It's evidence, not instruction — the block does NOT tell the
        # model to propose one of these; it merely reports the counts.
        assert "MUST propose" not in out
        assert "REQUIRED to target" not in out

    def test_prompt_cap_size_reasonable(self):
        # A 12-phrase block (worst case) with 40-char phrases is <1 KB.
        # Ensure the prompt render doesn't explode past 20 KB. The whole
        # reflect prompt is designed for the 8B ~8K context budget.
        from self_modify.reflect import _reflection_prompt
        big_gaps = "\n".join(f"- theme phrase {i:03d}  ×{100-i}" for i in range(12))
        ctx = {
            "tools_block": "- t" * 200, "objectives_block": "- obj\n" * 10,
            "theses_block": "- th\n" * 15, "rejections_block": "",
            "leads_block": "", "data_gaps_block": big_gaps,
        }
        out = _reflection_prompt(ctx)
        assert len(out) < 20000


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
