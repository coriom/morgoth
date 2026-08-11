"""Gate-3 auto-approve classifier — separation proof + invariants.

Ships alongside self_modify/auto_approve.py in the SAME commit that
introduces the module INERT. These tests are the safety spine: they
lock the classifier's rule against the ledger's 8 v2 shadow decisions
and grep-lock the invariants that prevent auto-approve from bypassing
a deterministic gate or forking the apply path.

The classifier itself does not fire because AUTO_APPROVE_ENABLED
defaults to False AND the data criteria (n<30, rollback 50 %>20 %)
aren't met — but these tests must pass before either can flip.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from self_modify import auto_approve
from self_modify.auto_approve import (
    AUTO_APPROVE_ENV,
    CriteriaSnapshot,
    RULE_VERSION,
    TierDecision,
    auto_approve_enabled,
    classify_tier,
    evaluate_criteria,
    should_auto_apply,
)

MODULE_PATH = Path(auto_approve.__file__)


# ────────────────────────────────────────────────────────────────────────
# The 8-ledger separation — the actual data drives this. Every operator-
# REJECT under v2 shadow has ≥1 WARN axis; every operator-APPROVE at
# pending_approval that reached the operator has all-PASS. This test
# asserts the classifier reproduces that split exactly. Fixture axes
# transcribed from the audit dump (docs/AUTO_APPROVE_DESIGN.md §A1).
# ────────────────────────────────────────────────────────────────────────


def _mk_v2_verdict(axes: dict[str, str], verdict: str = "APPROVE") -> dict:
    return {
        "verdict": verdict,
        "axes": axes,
        "prompt_version": "v2",
        "reasons": [],
    }


LEDGER_ROWS = [
    # (id, status, axes, expected_tier, operator's actual decision)
    (
        "fd8056a3", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "WARN", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "HUMAN", "reject",  # frozen endpoint — the shadow false-APPROVE
    ),
    (
        "1182ee96", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "WARN", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "HUMAN", "approve",  # operator judged safe despite WARN
    ),
    (
        "54a37dc1", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "WARN", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "HUMAN", "approve",
    ),
    (
        "096533da", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "PASS", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "AUTO", "approve",
    ),
    (
        "1735f617", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "PASS", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "AUTO", "approve",  # retry edge — classifier still AUTO on axes
    ),
    (
        "580d247c", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "PASS", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "AUTO", "approve",
    ),
    (
        "99aa44ea", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "PASS", "rationale_truth": "PASS",
         "semantic_duplication": "WARN", "name_content_coherence": "PASS"},
        "HUMAN", "reject",  # the phase-D E2E test — WARN axis catches it
    ),
    (
        "c158c348", "pending_approval",
        {"api_liveness": "PASS", "field_liveness": "PASS", "rationale_truth": "PASS",
         "semantic_duplication": "PASS", "name_content_coherence": "PASS"},
        "AUTO", "approve",
    ),
]


@pytest.mark.parametrize("pid,status,axes,expected_tier,op", LEDGER_ROWS)
def test_classify_tier_matches_ledger(pid, status, axes, expected_tier, op):
    proposal = {"proposal_id": pid, "status": status}
    verdict = _mk_v2_verdict(axes)
    decision = classify_tier(proposal, verdict)
    assert decision.tier == expected_tier, (
        f"{pid} classified {decision.tier}, expected {expected_tier}; "
        f"operator decided {op}; axes={axes}"
    )


def test_ledger_separation_zero_false_approve():
    """The critical safety cell: no AUTO classification lands on an
    operator-REJECT row. If this breaks, the classifier just started
    allowing a wrong auto-approve."""
    for pid, status, axes, expected, op in LEDGER_ROWS:
        proposal = {"proposal_id": pid, "status": status}
        d = classify_tier(proposal, _mk_v2_verdict(axes))
        if d.tier == "AUTO":
            assert op == "approve", f"AUTO on operator-REJECT row {pid}"


# ────────────────────────────────────────────────────────────────────────
# Property test — any WARN/FAIL axis forces HUMAN (invariant #4).
# Fuzzes across the five known axes; if any single one is non-PASS the
# classifier must return HUMAN regardless of verdict/status.
# ────────────────────────────────────────────────────────────────────────

AXES = ("api_liveness", "field_liveness", "rationale_truth",
        "semantic_duplication", "name_content_coherence")


@pytest.mark.parametrize("bad_axis", AXES)
@pytest.mark.parametrize("bad_val", ["WARN", "FAIL", "SKIP", "unknown", ""])
def test_any_non_pass_axis_forces_human(bad_axis, bad_val):
    axes = {a: "PASS" for a in AXES}
    axes[bad_axis] = bad_val
    proposal = {"status": "pending_approval"}
    d = classify_tier(proposal, _mk_v2_verdict(axes))
    assert d.tier == "HUMAN", f"expected HUMAN on axes={axes}, got {d.tier}"


def test_synthetic_overlap_stays_human_via_gate_precondition():
    """A proposal that would have had field-overlap>0 would have been
    auto-rejected by the endpoint-dedup gate BEFORE reaching
    pending_approval. So the invariant is proven by construction:
    status=pending_approval implies field_overlap==0.

    Simulate a would-be overlap: status='rejected_endpoint'. The
    classifier must return HUMAN with reason mentioning status."""
    proposal = {"status": "rejected_endpoint"}
    axes = {a: "PASS" for a in AXES}
    d = classify_tier(proposal, _mk_v2_verdict(axes))
    assert d.tier == "HUMAN"
    assert "pending_approval" in d.reason  # explains why


def test_shadow_flag_verdict_forces_human():
    proposal = {"status": "pending_approval"}
    axes = {a: "PASS" for a in AXES}
    d = classify_tier(proposal, _mk_v2_verdict(axes, verdict="FLAG"))
    assert d.tier == "HUMAN"


def test_no_shadow_verdict_forces_human():
    proposal = {"status": "pending_approval"}
    d = classify_tier(proposal, None)
    assert d.tier == "HUMAN"


# ────────────────────────────────────────────────────────────────────────
# Data-criteria guard (B2)
# ────────────────────────────────────────────────────────────────────────


def test_criteria_fail_when_below_n_min():
    # 20 clean decisions (< 30 threshold)
    ops = [{"matched_signature_R": True, "op_decision": "approve"}] * 20
    outs = [{"outcome": "applied"}] * 20
    snap = evaluate_criteria(ops, outs)
    assert not snap.ok
    assert any("< required" in r for r in snap.reasons)


def test_criteria_fail_on_any_false_approve():
    # 30 decisions but one is a false-approve
    ops = [{"matched_signature_R": True, "op_decision": "approve"}] * 29 + [
        {"matched_signature_R": True, "op_decision": "reject"},
    ]
    outs = [{"outcome": "applied"}] * 29
    snap = evaluate_criteria(ops, outs)
    assert not snap.ok
    assert any("false-approve" in r for r in snap.reasons)
    assert snap.n_false_approves == 1


def test_criteria_fail_on_rollback_rate_over_cap():
    # 30 clean decisions but rollback rate 50 % (> 20 % cap)
    ops = [{"matched_signature_R": True, "op_decision": "approve"}] * 30
    outs = [{"outcome": "applied"}] * 15 + [{"outcome": "apply_failed_rolled_back"}] * 15
    snap = evaluate_criteria(ops, outs)
    assert not snap.ok
    assert any("rollback rate" in r for r in snap.reasons)


def test_criteria_pass_when_all_conditions_met():
    ops = [{"matched_signature_R": True, "op_decision": "approve"}] * 30
    outs = [{"outcome": "applied"}] * 30
    snap = evaluate_criteria(ops, outs)
    assert snap.ok
    assert snap.reasons == []


# ────────────────────────────────────────────────────────────────────────
# Double-gate guard: classifier AUTO + flag ON + criteria OK ⇒ may_apply.
# Any single miss → refuse.
# ────────────────────────────────────────────────────────────────────────


def _auto_decision():
    return TierDecision(tier="AUTO", reason="ok", signature={})


def _ok_snap():
    return CriteriaSnapshot(ok=True, n_decisions=30, n_false_approves=0,
                            rollback_rate=0.1, reasons=[])


def _bad_snap():
    return CriteriaSnapshot(ok=False, n_decisions=8, n_false_approves=0,
                            rollback_rate=0.5, reasons=["n=8 < 30", "rollback 50 %"])


def test_should_auto_apply_refuses_when_flag_off(monkeypatch):
    monkeypatch.delenv(AUTO_APPROVE_ENV, raising=False)
    may, why = should_auto_apply(_auto_decision(), _ok_snap())
    assert not may
    assert "off" in why


def test_should_auto_apply_refuses_when_criteria_unmet(monkeypatch):
    monkeypatch.setenv(AUTO_APPROVE_ENV, "on")
    may, why = should_auto_apply(_auto_decision(), _bad_snap())
    assert not may
    assert "criteria unmet" in why


def test_should_auto_apply_refuses_when_classifier_human(monkeypatch):
    monkeypatch.setenv(AUTO_APPROVE_ENV, "on")
    d = TierDecision(tier="HUMAN", reason="axis WARN", signature={})
    may, why = should_auto_apply(d, _ok_snap())
    assert not may
    assert "HUMAN" in why


def test_should_auto_apply_true_only_when_all_three_align(monkeypatch):
    monkeypatch.setenv(AUTO_APPROVE_ENV, "on")
    may, why = should_auto_apply(_auto_decision(), _ok_snap())
    assert may
    assert "double gate cleared" in why


# ────────────────────────────────────────────────────────────────────────
# GREP-LOCK INVARIANTS — the safety spine. Structural asserts on the
# module source; break the source → break the build.
# ────────────────────────────────────────────────────────────────────────


def test_grep_lock_default_off():
    """Invariant #3: AUTO_APPROVE_ENABLED default is False. The module
    reads the env with .get(...) or "" (falls through to falsy). Assert
    the fallback pattern is present so a future edit can't quietly flip
    the default by removing the empty-string arm."""
    src = MODULE_PATH.read_text()
    # The env-read pattern from _delegation_enabled: os.environ.get(...) or ""
    assert re.search(
        r'os\.environ\.get\(\s*AUTO_APPROVE_ENV\s*\)\s*or\s*""', src
    ), "auto_approve_enabled must fall back to empty string (default off)"
    # And the truthy set must exclude "" — so empty (default) is off.
    assert '"1", "true", "on", "yes"' in src


def test_grep_lock_no_mutation_of_gate_or_shadow_rows():
    """Invariant #1: the module never mutates gate verdicts or shadow rows.
    Grep-negative on obvious mutation patterns."""
    src = MODULE_PATH.read_text()
    forbidden = (
        "UPDATE shadow_verdicts",
        "UPDATE self_modify_proposals",
        "DELETE FROM shadow_verdicts",
        "DELETE FROM self_modify_proposals",
        "submit_terminal(",  # the write path for proposal status
    )
    for pat in forbidden:
        assert pat not in src, f"forbidden mutation pattern present: {pat!r}"


def test_grep_lock_no_second_apply_implementation():
    """Invariant #2: the module does NOT reimplement apply. If a live path
    is ever wired here, it must call self_modify.apply.apply_proposal —
    not fork it. Assert no local git/systemctl calls (the apply's own
    surface) live here."""
    src = MODULE_PATH.read_text()
    forbidden_forks = (
        "subprocess.run(",
        '"git"',
        "'git'",
        "systemctl",
        "def apply_proposal",  # would-be shadow implementation
        "_run_live_pytest",
    )
    for pat in forbidden_forks:
        assert pat not in src, f"auto_approve.py must not reimplement apply: found {pat!r}"


def test_grep_lock_classify_tier_reads_no_db():
    """Invariant #5: classify_tier only READS caller-supplied dicts —
    no DB pool, no HTTP, no os.stat, no file I/O. Assert the function's
    source is a pure branch tree."""
    src = inspect.getsource(classify_tier)
    forbidden = ("pool.acquire", "await conn.", "httpx.", "requests.",
                 "open(", "Path(", ".execute(", ".fetch(", ".fetchrow(")
    for pat in forbidden:
        assert pat not in src, f"classify_tier must be pure — found {pat!r} in source"


def test_rule_version_is_stamped_in_every_signature():
    """The audit log needs to know which Rule R produced each decision.
    Verify the signature dict always carries rule_version."""
    proposal = {"status": "pending_approval"}
    axes = {a: "PASS" for a in AXES}
    d = classify_tier(proposal, _mk_v2_verdict(axes))
    assert d.signature.get("rule_version") == RULE_VERSION
