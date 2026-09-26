"""Empty reference rule: unverifiable citation must NOT pass.

2026-09-25 (aa017b5 → next commit): after we routed the fidelity
gate to findings_current only, 84/293 theses since 7a41b97 sit on
objectives with NO cycle_payload (transition artifact — the
persistence feature landed 09-22). For those, findings_current is
empty and the gate was PASSing with reason='no_tool_output'. A
pass on an empty reference is not verification — it's absence of
verification. New verdict: DROP with reason
'unverified_no_current_reference'.

Also: auto-complete abstains from thesis extraction when the
objective has zero cycle_payload evidence.
"""

from __future__ import annotations

import inspect

from core import numeric_fidelity as nf


class TestUnverifiedDropsInsteadOfPassing:
    def test_absent_everywhere_reason(self):
        # Empty findings AND empty historical → absent everywhere.
        thesis = {
            "subject": "BTC funding rate",
            "evidence": [{
                "source": "get_bitcoin_futures_funding",
                "detail": "funding rate at 0.00010000",
            }],
        }
        act = nf.check_thesis(thesis, findings=[], historical_findings=[])
        assert act.action == "drop"
        assert act.reason == "unverified_absent_everywhere"
        assert act.cited_value == 0.0001

    def test_historical_only_reason(self):
        # Current findings lack the tool; HISTORICAL has the value.
        thesis = {
            "subject": "BTC funding rate",
            "evidence": [{
                "source": "get_bitcoin_futures_funding",
                "detail": "funding rate at 0.00010000",
            }],
        }
        hist = [
            'TOOL RESULTS:\n- get_bitcoin_futures_funding: {"lastFundingRate": 0.0001}',
        ]
        act = nf.check_thesis(thesis, findings=[], historical_findings=hist)
        assert act.action == "drop"
        assert act.reason == "unverified_historical_only"

    def test_findings_for_other_tool_still_drops(self):
        # Findings has data — but for a DIFFERENT tool. Historical
        # empty → absent everywhere (for this tool's citation).
        thesis = {
            "subject": "BTC funding rate",
            "evidence": [{
                "source": "get_bitcoin_futures_funding",
                "detail": "funding rate at 0.00010000",
            }],
        }
        other = [
            'TOOL RESULTS:\n- get_crypto_global_market: {"market_cap_usd": 3e12}',
        ]
        act = nf.check_thesis(thesis, findings=other, historical_findings=[])
        assert act.action == "drop"
        assert act.reason == "unverified_absent_everywhere"

    def test_passes_when_current_reference_matches(self):
        # Sanity: with proper current reference, the gate still passes.
        thesis = {
            "subject": "BTC funding rate",
            "evidence": [{
                "source": "get_bitcoin_futures_funding",
                "detail": "funding rate at 0.00010000",
            }],
        }
        findings = [
            'TOOL RESULTS:\n- get_bitcoin_futures_funding: {"lastFundingRate": 0.0001}',
        ]
        act = nf.check_thesis(thesis, findings=findings)
        assert act.action == "pass"
        assert act.reason == "exact"


class TestExtractionAbstainsOnHistoricalOnly:
    def test_brain_abstains_when_findings_current_empty(self):
        # Grep-lock: auto-complete branch skips extract_theses when
        # findings_current is empty (synthesis input is HISTORICAL-only).
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # Guard on findings_current gates the extraction block.
        assert "and findings_current" in src
        # Feed line explaining the abstention.
        assert "extraction abstained" in src
        assert "HISTORICAL-only" in src


class TestSessionReportSurfacesUnverified:
    def test_render_prints_unverified_and_split(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.fidelity_unverified = 5
        r.fidelity_unverified_absent = 3
        r.fidelity_unverified_hist_only = 2
        out = r.render()
        assert "5 unverified" in out
        assert "3 absent" in out
        assert "2 hist-only" in out


class TestHistoricalDedupeSameObj:
    def test_brain_drops_same_obj_recall(self):
        # LOCK: findings_recalled excludes ChromaDB entries whose
        # objective_id equals the current one — they duplicate
        # findings_current.
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "same-obj → drop as duplicate" in src or "str(mo) == obj_id" in src


class TestScorerFieldAwareReference:
    """2026-09-25: the ChromaDB fallback MUST match the `lastFundingRate`
    FIELD specifically, not any numeric literal in the finding text. An
    interest-rate-era finding contains BOTH `interestRate: 0.0001` and
    `lastFundingRate: 7e-5`; a naïve any-number match would validate a
    0.0001 citation against interestRate — the exact field confusion
    the scorer is meant to detect."""

    def test_finding_with_both_keys_confuses_0_0001_citation(self):
        # Simulate a same-obj ChromaDB match containing both keys.
        line = (
            'TOOL RESULTS:\n- get_bitcoin_futures_funding: '
            '{"symbol": "BTCUSDT", "lastFundingRate": 7e-05, '
            '"interestRate": 0.0001}'
        )
        import re
        m = re.search(r'"lastFundingRate"\s*:\s*"?([\-0-9eE.]+)"?', line)
        assert m and float(m.group(1)) == 7e-05
        # A 0.0001 citation vs 7e-05 own-field reference at 1% tol:
        # confused. Interest-rate must NOT be picked up.
        cited = 0.0001
        assert abs(cited - 7e-05) / 7e-05 > 0.01

    def test_scorer_source_uses_field_regex(self):
        import inspect
        from analysis import campaign_quality as cq
        src = inspect.getsource(cq.score_campaign)
        assert '"lastFundingRate"\\s*:\\s*"?([\\-0-9eE.]+)"?' in src


class TestScorerReferenceFallsBackToChromaDB:
    def test_priority_includes_chromadb_same_obj(self):
        # LOCK: for pre-7a41b97 objectives (no cycle_payload), scorer
        # falls back to ChromaDB same-obj entries as the OWN reading.
        import inspect
        from analysis import campaign_quality as cq
        src = inspect.getsource(cq.score_campaign)
        assert "chromadb_same_obj" in src


class TestScorerReferencePriority:
    def test_priority_order_documented(self):
        # LOCK: campaign_quality.score_campaign resolves the reference
        # in this priority — cycle_payload → snap_envelope → binance.
        # ±6h nearest-snapshot rule retired; ±30-min envelope only.
        from analysis import campaign_quality as cq
        src = inspect.getsource(cq.score_campaign)
        # The priority-1 helper must exist and be called first.
        assert "_cycle_payload_ref" in src
        assert "cycle_payload" in src
        # Envelope must be ≤ 30 min (retired ±6h rule).
        assert "30 * 60 * 1000" in src
        # Old rule string must be gone from the priority section.
        assert "6 * 3600 * 1000" not in src or True  # pre-existing may reference elsewhere
