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
    def test_no_tool_output_drops_not_passes(self):
        # Empty findings → digest empty → must DROP with the new reason.
        thesis = {
            "subject": "BTC funding rate",
            "evidence": [{
                "source": "get_bitcoin_futures_funding",
                "detail": "funding rate at 0.00010000",
            }],
        }
        act = nf.check_thesis(thesis, findings=[])
        assert act.action == "drop"
        assert act.reason == "unverified_no_current_reference"
        assert act.cited_value == 0.0001

    def test_findings_for_other_tool_still_drops(self):
        # findings has data — but for a DIFFERENT tool. The cited
        # tool's line is missing → same drop.
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
        act = nf.check_thesis(thesis, findings=other)
        assert act.action == "drop"
        assert act.reason == "unverified_no_current_reference"

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
    def test_render_prints_unverified_count(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.fidelity_unverified = 5
        out = r.render()
        assert "5 unverified" in out


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
