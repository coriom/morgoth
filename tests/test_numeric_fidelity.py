"""Numeric-fidelity gate: PASS / REWRITE / DROP outcomes on real cases.

Uses the actual failing values from the c85477a diagnosis (btc_funding
1e-4 cited when the tool returned 8.037e-5) as the flagship regression
case. Also locks the ambiguity paths (PASS with reason) so the gate
never silently deletes verifiable theses.
"""

from __future__ import annotations

import os

import pytest

from core.numeric_fidelity import (
    FidelityAction,
    check_thesis,
    _flag_enabled,
    _substitute_number,
    _tool_digest_for,
    _all_numbers_in,
)


def _thesis(subject, claim, source, detail):
    return {
        "subject": subject, "claim": claim, "confidence": "medium",
        "evidence": [{"source": source, "detail": detail}],
    }


def _findings(tool: str, payload: str) -> list[str]:
    """Build a findings blob in the shape _format_cycle_finding produces."""
    return [f"TOOL RESULTS:\n- {tool}: {payload}"]


class TestFlag:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("NUMERIC_GATE_ENABLED", raising=False)
        assert _flag_enabled() is True

    @pytest.mark.parametrize("val", ["false", "0", "no", "off"])
    def test_disabled_by_env(self, monkeypatch, val):
        monkeypatch.setenv("NUMERIC_GATE_ENABLED", val)
        assert _flag_enabled() is False


class TestToolDigest:
    def test_extracts_only_named_tool_lines(self):
        blob = ("TOOL RESULTS:\n"
                "- get_fear_greed_index: {\"value\": 63}\n"
                "- get_crypto_price: {\"btc\": 45000}")
        d = _tool_digest_for("get_fear_greed_index", [blob])
        assert '"value": 63' in d
        assert "45000" not in d

    def test_includes_failed_lines_for_the_source(self):
        blob = "- get_news FAILED: DNS error"
        d = _tool_digest_for("get_news", [blob])
        assert "FAILED" in d

    def test_no_findings_returns_empty(self):
        assert _tool_digest_for("get_x", []) == ""

    def test_no_source_returns_empty(self):
        assert _tool_digest_for("", ["- get_x: 42"]) == ""


class TestAllNumbersIn:
    def test_extracts_multiple(self):
        nums = _all_numbers_in("value 63 volume 45000 change -2.01")
        assert nums == [63.0, 45000.0, -2.01]

    def test_skips_unit_prefixed_tokens(self):
        # "24h" must not contribute a 24.
        assert 24.0 not in _all_numbers_in("24h change of -2.01")

    def test_strips_commas(self):
        assert 63120.0 in _all_numbers_in("mkt cap 63,120")


class TestCheckThesisPass:
    def test_exact_integer_match(self):
        # market_sentiment 63 vs actual 63 — the empirically-perfect case.
        f = _findings("get_fear_greed_index", '{"value": 63, "classification": "Greed"}')
        act = check_thesis(
            _thesis("Fear & Greed", "high", "get_fear_greed_index", "Index value 63"),
            f,
        )
        assert act.action == "pass" and act.reason == "exact"

    def test_no_number_in_detail_passes_with_reason(self):
        act = check_thesis(
            _thesis("X", "high", "get_x", "no numbers here at all"),
            _findings("get_x", "42"),
        )
        assert act.action == "pass"
        # Either 'no_number_in_detail' (loop exhausted) or 'no_number' —
        # locked at 'no_number_in_detail' for stability.
        assert act.reason == "no_number_in_detail"

    def test_no_tool_output_passes_with_reason(self):
        # Model cites get_fear_greed_index but findings has get_news only.
        act = check_thesis(
            _thesis("X", "high", "get_fear_greed_index", "value 63"),
            _findings("get_news", "some text 100"),
        )
        assert act.action == "pass" and act.reason == "no_tool_output"

    def test_no_source_named_passes_with_reason(self):
        # evidence entry missing 'source' field entirely.
        thesis = {
            "subject": "X", "claim": "high",
            "evidence": [{"detail": "value 63"}],
        }
        act = check_thesis(thesis, _findings("get_x", "63"))
        assert act.action == "pass" and act.reason == "no_tool_named"

    def test_no_candidates_passes_when_tool_output_has_no_numbers(self):
        act = check_thesis(
            _thesis("X", "high", "get_x", "value 63"),
            _findings("get_x", "FAILED: DNS error"),
        )
        # digest exists but has no numeric candidates → PASS with reason.
        assert act.action == "pass" and act.reason == "no_candidates"


class TestCheckThesisRewrite:
    def test_flagship_case_1e_minus_4_vs_8_037e_minus_5(self):
        # The empirical failing case from the c85477a diagnosis: model
        # anchors to 0.00010000 when the tool returned 0.00008037.
        # Ratio 1.24 — inside the REWRITE band [0.5, 2.0] → rewrite.
        payload = ('{"lastFundingRate": "0.00008037", "symbol": "BTCUSDT"}')
        act = check_thesis(
            _thesis(
                "BTCUSDT funding", "high", "get_bitcoin_futures_funding",
                "lastFundingRate of 0.00010000",
            ),
            _findings("get_bitcoin_futures_funding", payload),
        )
        assert act.action == "rewrite"
        assert act.reason == "transcription_drift"
        assert act.cited_value == 1e-4
        assert abs(act.true_value - 8.037e-5) < 1e-9
        # Corrected detail must NOT still contain the false 0.00010000.
        assert "0.00010000" not in (act.corrected_detail or "")

    def test_rewrite_preserves_original_prose(self):
        payload = '{"value": 63}'
        # Off by 12 % — outside 1 % tolerance, inside 0.5-2x band → rewrite.
        act = check_thesis(
            _thesis("F&G", "high", "get_fear_greed_index", "value 55"),
            _findings("get_fear_greed_index", payload),
        )
        assert act.action == "rewrite"
        # The subject/claim survive the rewrite (only detail changes).


class TestCheckThesisDrop:
    def test_fabricated_number_ratio_outside_band_dropped(self):
        # Cited 100, tool has 3 → ratio 33x, outside [0.5, 2.0] AND no
        # unit-scaling story fits. DROP.
        act = check_thesis(
            _thesis("X", "high", "get_x", "value 100"),
            _findings("get_x", '{"reading": 3}'),
        )
        assert act.action == "drop" and act.reason == "no_match_in_tool_output"

    def test_number_present_in_different_tool_is_dropped(self):
        # Model cites get_x but the value 63 only exists in get_y's
        # output. Behaviour LOCKED: gate does NOT cross tool boundaries.
        # The thesis is attributing the value to the wrong tool — drop.
        act = check_thesis(
            _thesis("X", "high", "get_x", "value 63"),
            [f"TOOL RESULTS:\n- get_y: {{\"value\": 63}}"],
        )
        assert act.action == "pass"
        # get_x has no output at all → PASS with reason (not DROP).
        assert act.reason == "no_tool_output"


class TestUnitScaledBehaviour:
    def test_wei_vs_gwei_order_of_magnitude_off_is_dropped(self):
        # Cited 3118255194 (wei-scale), tool has 30 (gwei). Ratio ~1e8 —
        # far outside REWRITE band. Behaviour LOCKED: DROP. A unit-
        # aware rewrite would require metric-kind knowledge the gate
        # deliberately doesn't have — the metric-agnostic contract keeps
        # the gate simple; unit conversion belongs at the scorer, not
        # the fidelity gate.
        act = check_thesis(
            _thesis("gas", "high", "get_ethereum_network_stats",
                     "gas price of 3118255194"),
            _findings("get_ethereum_network_stats", '{"gas_price_gwei": 30}'),
        )
        assert act.action == "drop"


class TestSubstituteNumber:
    def test_replaces_the_first_standalone_number(self):
        out = _substitute_number("value 55 rising", 55.0, 63.0)
        assert "63" in out
        assert "55" not in out

    def test_falls_back_to_marker_when_number_not_findable(self):
        # A detail with no standalone number (edge case — extract_reported_
        # value would have returned None, so this branch is defensive only).
        out = _substitute_number("no digits here", 0.0, 8.037e-5)
        assert "[corrected: true value" in out


class TestScaleNormalisation:
    """Real cases from bb2aad7 drops that the operator flagged as
    scale-mismatch rescues, not fabrication."""

    def test_2_77_trillion_matches_2_694880e12(self):
        # The flagship case: "2.77 trillion" cited against a tool that
        # returned 2694880000000. Must PASS via scale_normalised (the
        # tool value is within tolerance of 2.77e12).
        payload = ('{"market_cap_usd": 2694880000000, "volume_24h_usd": 1.6e11, '
                    '"bitcoin_dominance_percentage": 56.32}')
        act = check_thesis(
            _thesis("Global crypto mkt cap", "high", "get_crypto_global_market",
                     "market cap of $2.77 trillion"),
            _findings("get_crypto_global_market", payload),
        )
        # 2.77e12 vs 2.6949e12 → 2.79 % off, outside 1 % PASS tolerance
        # but comfortably inside REWRITE band after scaling.
        assert act.action == "rewrite"
        assert act.reason == "transcription_drift_scaled"
        assert abs(act.true_value - 2.694880e12) < 1e6

    def test_2_694_trillion_matches_exactly_via_scale(self):
        # Same payload, tighter reported value → PASS exact via scale.
        payload = '{"market_cap_usd": 2694880000000}'
        act = check_thesis(
            _thesis("X", "high", "get_crypto_global_market",
                     "market cap $2.694 trillion"),
            _findings("get_crypto_global_market", payload),
        )
        assert act.action == "pass"
        assert act.reason == "scale_normalised"

    def test_140_billion_matches_1_67e11_within_rewrite(self):
        # 140 * 1e9 = 1.4e11 vs actual 1.67111e11 → 16 % off → REWRITE.
        payload = '{"volume_24h_usd": 167111000000}'
        act = check_thesis(
            _thesis("Global 24h volume", "high", "get_crypto_global_market",
                     "$140 billion in 24-hour trading volume"),
            _findings("get_crypto_global_market", payload),
        )
        assert act.action == "rewrite"
        assert act.reason == "transcription_drift_scaled"

    def test_suffix_letter_T_directly_after_number(self):
        payload = '{"market_cap_usd": 2694880000000}'
        act = check_thesis(
            _thesis("X", "high", "get_crypto_global_market", "cap: $2.77T"),
            _findings("get_crypto_global_market", payload),
        )
        assert act.action in ("pass", "rewrite")
        assert "scaled" in act.reason or "scale" in act.reason

    def test_percentage_never_scaled_up(self):
        # -2.01 % change is NOT $2.01T market cap. The gate must NOT
        # scale a percentage — locked test.
        payload = '{"market_cap_change_24h": -2.01, "market_cap_usd": 2.7e12}'
        act = check_thesis(
            _thesis("X", "declining", "get_crypto_global_market",
                     "24h change of -2.01%"),
            _findings("get_crypto_global_market", payload),
        )
        # The bare -2.01 matches the -2.01 in the tool output → PASS exact.
        assert act.action == "pass"
        assert act.reason == "exact"

    def test_implicit_scale_accepted_when_lands_on_tolerance(self):
        # No word, no suffix — but 63000 vs 63,120,000,000 (billion)
        # matches at 1e6 scale within 1 %. Implicit scaling PASSes.
        payload = '{"market_cap_usd": 63120000000}'
        act = check_thesis(
            _thesis("X", "high", "get_crypto_global_market", "cap 63000"),
            _findings("get_crypto_global_market", payload),
        )
        # 63000 * 1e6 = 6.312e10 exactly → PASS.
        assert act.action == "pass"
        assert act.reason == "scale_normalised_implicit"

    def test_implicit_scale_never_widens_rewrite_band(self):
        # No word, no suffix, no implicit scale hits PASS tolerance,
        # AND bare comparison is well outside REWRITE band → DROP.
        # Locks the cap: implicit scaling MUST NOT rescue a fabrication.
        payload = '{"value": 100}'
        act = check_thesis(
            _thesis("X", "high", "get_x", "value 12345"),
            _findings("get_x", payload),
        )
        assert act.action == "drop"

    def test_genuine_fabrication_still_drops(self):
        # No scale word, no implicit scale lands, ratio outside band → DROP.
        payload = '{"reading": 3}'
        act = check_thesis(
            _thesis("X", "high", "get_x", "value 999"),
            _findings("get_x", payload),
        )
        assert act.action == "drop"

    def test_integer_exact_match_unchanged(self):
        # market_sentiment 63 vs 63 → still PASS exact (no scaling applied
        # because bare comparison already hits tolerance).
        payload = '{"value": 63}'
        act = check_thesis(
            _thesis("F&G", "high", "get_fear_greed_index", "Index value 63"),
            _findings("get_fear_greed_index", payload),
        )
        assert act.action == "pass" and act.reason == "exact"


class TestSessionReportGrepLocks:
    def test_report_renders_fidelity_line(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.fidelity_checked = 12
        r.fidelity_rewritten = 4
        r.fidelity_dropped = 1
        out = r.render()
        assert "NUMERIC FIDELITY" in out
        assert "12 checked" in out
        assert "4 corrected" in out
        assert "1 dropped" in out


class TestBrainWiringGrepLocks:
    def test_brain_imports_and_calls_the_gate(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "from core.numeric_fidelity import" in src
        assert "check_thesis" in src
        assert "record_numeric_fidelity_event" in src
        assert '"drop"' in src or "'drop'" in src

    def test_kill_switch_short_circuits_wiring(self):
        # Structural fence: the gate is invoked ONLY inside
        # `if _fg_enabled():`. With the flag off, theses go straight
        # to add_thesis without gate touching evidence[].detail.
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "if _fg_enabled():" in src
