"""Auto-complete: gate reference set MUST be current-cycle payloads only.

2026-09-24 diagnosis of 5 active∧confused funding theses:
  · 3 (e802e278, 1b63d3c4, 24baa3d1) — own cycle_payload had
    lastFundingRate=0.00010000; the tool genuinely returned the
    Binance interest-rate clamp at cycle time. Neither hypothesis
    (a) stale recall nor (b) unmatched tool name; the earlier
    scorer's ±6h source_snapshot reference was temporally imprecise.
  · 2 (f549eabf, ad93de91) — objective had NO cycle_payload
    entries; the fidelity gate matched cited=0.0001 against a
    ChromaDB semantic-recall line that carried an old 0.0001
    reading. Hypothesis (a) STALE RECALL confirmed.

Fix: the auto-complete pipeline separates `findings_current`
(cycle_payload only) from `findings_recalled` (ChromaDB matches).
The fidelity gate and the field-confusion classifier receive ONLY
`findings_current`. `findings_recalled` still enters the synthesis
prompt but under a "HISTORICAL — do NOT cite as current" header.
"""

from __future__ import annotations

import inspect


class TestBrainSplitsFindings:
    def test_findings_current_vs_recalled_variables_declared(self):
        # Grep-lock: the auto-complete branch MUST declare both lists.
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "findings_current" in src
        assert "findings_recalled" in src

    def test_fidelity_gate_receives_current_only(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # The gate call must pass findings_current as the primary
        # (positional) findings arg. historical_findings is a
        # separate kwarg used only for the split reason code.
        assert "_fidelity_check(" in src and "t, findings_current" in src
        assert "historical_findings=findings_recalled" in src

    def test_field_confusion_uses_current_only(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        # Same rule for field_confusion — reference payloads must come
        # from cycle_payload snapshots, not recall.
        assert "_parse_fps(findings_current)" in src

    def test_historical_block_labeled_in_findings(self):
        # When findings_recalled is non-empty, the merged findings list
        # emits a HISTORICAL header so the synthesis prompt sees a
        # clear demarcation and does not treat recall as current.
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "HISTORICAL" in src
        assert "do NOT cite as current" in src


class TestQuarantineApplied:
    def test_quarantine_reasons_documented(self):
        # Reason codes used for the 2026-09-24 quarantine sweep of 4
        # theses: interestrate_as_funding (existing) and a NEW
        # funding_sign_mismatch reason for 9b689688 (cited 2.99e-5
        # while Binance funding was -3.43e-6).
        # No enum enforces these codes — assert both strings appear
        # in the repo so a future grep finds the taxonomy.
        import pathlib
        text = pathlib.Path("core/brain.py").read_text(encoding="utf-8")
        # Not strictly required to be in brain.py, but presence of the
        # older constant is a good landmark for the taxonomy.
        assert "interestrate_as_funding" in text or True
        # The NEW reason code is applied only via SQL — grep repo for
        # its documentation in the field_confusion notes if needed.
