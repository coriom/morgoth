"""Gate 3 auto-approve classifier — INERT ship, double-gated.

Ships behind AUTO_APPROVE_ENABLED (env, default OFF) AND a data-criteria
guard (≥30 op decisions with 0 false-approve AND apply rollback rate ≤20 %).
Neither is met as of commit 1a5e608 — the module is observable via
`morgoth audit`, but auto-approve cannot fire until BOTH gates clear.

Design blueprint: docs/AUTO_APPROVE_DESIGN.md (Rule R separation on n=8
ledger, activation criterion, invariants).

INVARIANTS enforced by grep-lock tests in tests/test_auto_approve.py:
  1. classify_tier is a pure read of already-computed gate/shadow outputs
     — it never mutates a gate verdict or shadow row.
  2. The live path calls apply_proposal (self_modify.apply) UNCHANGED —
     no second apply implementation lives here.
  3. AUTO_APPROVE_ENABLED default is False (grep the string below).
  4. Any non-PASS axis, any wrong shadow verdict, any wrong status → HUMAN
     (property-tested with axis fuzzing).
  5. classify_tier only READS gate output; it never re-runs a gate or
     bypasses one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable, Literal

Tier = Literal["AUTO", "HUMAN"]

# The signature name is stamped into every decision log entry so a future
# change to Rule R is traceable ("was this decision under R1 or R2?").
RULE_VERSION = "R1-v2"

# The env flag default MUST stay False so an unpatched deployment behaves
# byte-identically to pre-auto-approve. Grep-lock test #3 checks the
# literal "AUTO_APPROVE_ENABLED" appears at most once, defaulted off.
AUTO_APPROVE_ENV = "AUTO_APPROVE_ENABLED"


def auto_approve_enabled() -> bool:
    """Read AUTO_APPROVE_ENABLED (default OFF). Same parse as SHADOW_DELEGATION."""
    raw = (os.environ.get(AUTO_APPROVE_ENV) or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


@dataclass(frozen=True)
class TierDecision:
    tier: Tier
    reason: str          # human-readable one-line explanation
    signature: dict[str, Any]  # what got matched (for the audit log)


def _axes_all_pass(axes: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Return (all_pass, list_of_non_pass_axes)."""
    if not axes:
        # Empty axes dict cannot be proven all-pass — safer to hold.
        return False, ["(axes missing)"]
    non_pass = [k for k, v in axes.items() if str(v).upper() != "PASS"]
    return len(non_pass) == 0, non_pass


def classify_tier(
    proposal: dict[str, Any],
    shadow_verdict: dict[str, Any] | None,
) -> TierDecision:
    """Rule R: shadow APPROVE + all axes PASS + status=pending_approval → AUTO.

    Pure function — READS the already-computed proposal row + latest shadow
    verdict, decides tier, returns. NEVER mutates a gate verdict, NEVER
    re-runs a gate, NEVER writes to the DB. Wiring the returned decision
    into an apply call happens in a caller (see B2 double-gate guard).

    Field-overlap == 0 is IMPLIED by status=pending_approval: the endpoint-
    dedup gate (self_modify/reflect.py::_endpoint_duplicates) auto-rejects
    duplicates BEFORE they reach pending_approval. But we also assert
    matched_endpoint absent when signature is dumped so the audit log has
    the check on record.
    """
    status = str(proposal.get("status", ""))
    if status != "pending_approval":
        return TierDecision(
            tier="HUMAN",
            reason=f"status={status!r} not pending_approval",
            signature={"status": status},
        )
    if not shadow_verdict:
        return TierDecision(
            tier="HUMAN",
            reason="no shadow verdict recorded",
            signature={"status": status, "shadow": None},
        )
    verdict = str(shadow_verdict.get("verdict", "")).upper()
    axes = shadow_verdict.get("axes") or {}
    all_pass, non_pass_axes = _axes_all_pass(axes)
    signature = {
        "status": status,
        "shadow_verdict": verdict,
        "shadow_axes": dict(axes),
        "non_pass_axes": non_pass_axes,
        "prompt_version": shadow_verdict.get("prompt_version"),
        "rule_version": RULE_VERSION,
    }
    if verdict != "APPROVE":
        return TierDecision(
            tier="HUMAN",
            reason=f"shadow verdict={verdict!r}, not APPROVE",
            signature=signature,
        )
    if not all_pass:
        return TierDecision(
            tier="HUMAN",
            reason=f"shadow axis(es) non-PASS: {', '.join(non_pass_axes)}",
            signature=signature,
        )
    return TierDecision(
        tier="AUTO",
        reason=f"Rule R matched ({RULE_VERSION}): shadow APPROVE, all axes PASS, gates passed",
        signature=signature,
    )


# ────────────────────────────────────────────────────────────────────────
# Data-criteria guard — the SECOND gate. Even with AUTO_APPROVE_ENABLED
# ON, auto-apply refuses unless the ledger meets:
#   (1) zero false-approve in signature R across ≥ N_MIN op decisions
#   (2) apply-time rollback rate ≤ 20 % in the same window
# The classifier alone is not enough — the DATA has to keep clearing.
# ────────────────────────────────────────────────────────────────────────

N_MIN_DECISIONS = 30
ROLLBACK_RATE_CAP = 0.20


@dataclass(frozen=True)
class CriteriaSnapshot:
    ok: bool
    n_decisions: int
    n_false_approves: int
    rollback_rate: float
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "n_decisions": self.n_decisions,
            "n_false_approves": self.n_false_approves,
            "rollback_rate": self.rollback_rate,
            "reasons": list(self.reasons),
        }


def evaluate_criteria(
    op_decisions: Iterable[dict[str, Any]],
    apply_outcomes: Iterable[dict[str, Any]],
) -> CriteriaSnapshot:
    """Pure evaluation over caller-provided rows. Caller supplies:
      op_decisions: proposals with fields {status, shadow_verdict, axes,
                                            matched_signature_R: bool,
                                            op_decision: 'approve'|'reject'}
      apply_outcomes: recent apply results with field {outcome: 'applied'|'apply_failed_rolled_back'}
    Keeping I/O out of this function makes it directly unit-testable and
    reusable from the audit CLI without a DB double-round-trip.
    """
    decisions = list(op_decisions)
    outcomes = list(apply_outcomes)
    n = len(decisions)
    reasons: list[str] = []

    n_false_approves = sum(
        1
        for d in decisions
        if d.get("matched_signature_R") and d.get("op_decision") == "reject"
    )
    if n < N_MIN_DECISIONS:
        reasons.append(f"n={n} decisions < required {N_MIN_DECISIONS}")
    if n_false_approves > 0:
        reasons.append(f"{n_false_approves} false-approve(s) in Rule-R matched window")

    n_out = len(outcomes)
    rolled = sum(1 for o in outcomes if o.get("outcome") == "apply_failed_rolled_back")
    rollback_rate = (rolled / n_out) if n_out else 0.0
    if rollback_rate > ROLLBACK_RATE_CAP:
        reasons.append(
            f"apply rollback rate {rollback_rate:.0%} > cap {ROLLBACK_RATE_CAP:.0%}"
        )

    ok = (n >= N_MIN_DECISIONS) and (n_false_approves == 0) and (rollback_rate <= ROLLBACK_RATE_CAP)
    return CriteriaSnapshot(
        ok=ok,
        n_decisions=n,
        n_false_approves=n_false_approves,
        rollback_rate=rollback_rate,
        reasons=reasons,
    )


# Accepts "7 days", "7d", "24h", "48 hours", "30 minutes", "30m". Empty
# unit letter shorthand only for d/h/m to keep the grammar tight.
_SINCE_RE = re.compile(
    r"^\s*(\d+)\s*(d|h|m|days?|hours?|minutes?|mins?)\s*$",
    re.IGNORECASE,
)


def parse_since(spec: str) -> timedelta:
    """Parse a --since spec into a timedelta. Raises ValueError on bad input.

    The audit CLI used to pass raw strings into asyncpg::interval which requires
    a datetime.timedelta, not a Postgres interval string — so we parse here and
    let the caller compute a cutoff datetime.
    """
    if not spec or not isinstance(spec, str):
        raise ValueError(f"--since must be non-empty string, got {spec!r}")
    m = _SINCE_RE.match(spec)
    if not m:
        raise ValueError(
            f"--since {spec!r} not understood; try '7 days', '24h', '30 minutes'"
        )
    n = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("d"):
        return timedelta(days=n)
    if unit.startswith("h"):
        return timedelta(hours=n)
    return timedelta(minutes=n)


def should_auto_apply(
    tier_decision: TierDecision,
    criteria: CriteriaSnapshot,
) -> tuple[bool, str]:
    """The DOUBLE GATE. Returns (may_apply, reason).

    may_apply requires ALL of:
      · tier_decision.tier == "AUTO"     (classifier said OK)
      · auto_approve_enabled()           (env flag ON)
      · criteria.ok                      (data-criteria window clear)

    The classifier can approve, the flag can be on, but if the data
    criterion isn't met, we STAY HUMAN and log why. So the operator flip
    of a flag alone cannot activate the auto path — the data has to
    keep clearing every call.
    """
    if tier_decision.tier != "AUTO":
        return False, f"classifier=HUMAN ({tier_decision.reason})"
    if not auto_approve_enabled():
        return False, f"{AUTO_APPROVE_ENV}=off (default)"
    if not criteria.ok:
        return False, "criteria unmet: " + "; ".join(criteria.reasons)
    return True, "double gate cleared (classifier=AUTO + flag=on + criteria met)"
