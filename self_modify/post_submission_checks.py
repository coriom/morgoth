"""Post-submission checks — the block that runs AFTER gate_tests lands
a proposal at pending_approval and BEFORE the operator sees the row.

Contract (2026-09-29)
---------------------
Every check here obeys the same shape: it returns a ``CheckResult``
(name, status, message). It NEVER raises out: an internal exception is
converted to a ``crashed`` result with the traceback tail so the caller
can hold the proposal at ``STATUS_CHECKS_INCOMPLETE`` instead of
letting it slip into the operator queue with unevaluated gates.

Reason for the module: reflect.run_reflection previously inlined the
liveness + overlap + shadow + delegation flow. A single ``set(spec[…])``
that mixed strings and dicts raised, unwound the whole reflect
coroutine, and the proposal (9f446bb4) sat at pending_approval with
its post-submission checks silently unevaluated. The regression was
detected only when the operator asked why gate-3 review was empty.

Checks run in this order (each independent — one crash only holds
that one check):

  1. liveness — 4-hit probe classification against ``digest_fields``.
     Requires an existing probe result; ``run_reflection`` starts the
     probe concurrent with gate_tests. When called from the recheck
     command a fresh probe is spawned here.
  2. overlap — spec.digest_fields ∩ registered_digest_fields on the
     currently-discovered data_feed tools. Advisory note only.
  3. shadow — Gate 2.5 LLM verifier, records verdict.
  4. delegation — under SHADOW_DELEGATION=on, a REJECT verdict flips
     the proposal to ``shadow_rejected``. APPROVE/FLAG never flip.

The runner returns the full ordered list of results and the final
proposal status (typically ``pending_approval`` or, on a REJECT-under-
delegation, ``shadow_rejected``; ``checks_incomplete`` when ≥1 crashed).
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any

from loguru import logger

from self_modify import proposals as P
from self_modify.digest_path import digest_field_names


CHECK_LIVENESS = "liveness"
CHECK_OVERLAP = "overlap"
CHECK_SHADOW = "shadow"
CHECK_DELEGATION = "delegation"


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "reject" | "crashed" | "skipped"
    message: str
    detail: dict[str, Any] | None = None


def _crashed(name: str, exc: BaseException) -> CheckResult:
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.warning("post_submission[{}]: CRASHED — {}", name, exc)
    # Compact: type + message + last frame — keeps status_reason short.
    tail = tb.strip().splitlines()[-3:] if tb else []
    return CheckResult(
        name=name,
        status="crashed",
        message=f"{type(exc).__name__}: {exc}",
        detail={"traceback_tail": "\n".join(tail)},
    )


def _liveness_check(
    probe: dict[str, Any] | None,
    spec: dict[str, Any],
) -> CheckResult:
    from self_modify import liveness
    names = digest_field_names(spec.get("digest_fields"))
    if probe is None:
        return CheckResult(CHECK_LIVENESS, "skipped", "no probe supplied")
    verdict = liveness.classify_probe(probe, names)
    status = {"reject": "reject", "warn": "warn", "pass": "ok"}.get(
        verdict.get("outcome"), "ok"
    )
    return CheckResult(
        name=CHECK_LIVENESS,
        status=status,
        message=verdict.get("reason", ""),
        detail=verdict,
    )


def _overlap_check(spec: dict[str, Any], registered_names: set[str]) -> CheckResult:
    names = set(digest_field_names(spec.get("digest_fields")))
    overlap = sorted(names & (registered_names or set()))
    if overlap:
        return CheckResult(
            name=CHECK_OVERLAP,
            status="warn",
            message=f"field-name overlap with existing digests: {overlap}",
            detail={"overlap": overlap},
        )
    return CheckResult(CHECK_OVERLAP, "ok", "no field-name overlap")


async def _shadow_check(
    proposal_row: dict[str, Any],
    config: Any, pm: Any,
) -> CheckResult:
    from self_modify import shadow as _shadow
    verdict = await _shadow.run_shadow_verdict(
        proposal=proposal_row, config=config, pm=pm,
    )
    v = (verdict or {}).get("verdict")
    status = {"REJECT": "reject", "FLAG": "warn", "APPROVE": "ok"}.get(v, "ok")
    axes = (verdict or {}).get("axes") or {}
    return CheckResult(
        name=CHECK_SHADOW,
        status=status,
        message=f"verdict={v} axes={axes}",
        detail=verdict,
    )


async def run_post_submission_checks(
    *, store: P.ProposalStore,
    proposal_id: str,
    spec: dict[str, Any],
    probe: dict[str, Any] | None,
    registered_field_names: set[str],
    config: Any, pm: Any,
    delegation_enabled: bool,
) -> tuple[list[CheckResult], str]:
    """Run every post-submission check. Returns (results, final_status).

    · If ANY check crashed → final_status = STATUS_CHECKS_INCOMPLETE
      and the crash names are appended to status_reason via
      update_status. Delegation is SKIPPED under this condition
      (a crashed shadow check cannot authorize a status flip).
    · If shadow verdict is REJECT and delegation is on →
      STATUS_SHADOW_REJECTED, delegation reason recorded.
    · Otherwise → keep STATUS_PENDING_APPROVAL, append any warn
      messages to status_reason.
    """
    results: list[CheckResult] = []

    # 1. liveness
    try:
        results.append(_liveness_check(probe, spec))
    except Exception as exc:  # noqa: BLE001
        results.append(_crashed(CHECK_LIVENESS, exc))

    # 2. overlap
    try:
        results.append(_overlap_check(spec, registered_field_names))
    except Exception as exc:  # noqa: BLE001
        results.append(_crashed(CHECK_OVERLAP, exc))

    # 3. shadow
    row = await store.get(proposal_id)
    shadow_res: CheckResult | None = None
    try:
        if row is not None:
            shadow_res = await _shadow_check(row, config, pm)
            results.append(shadow_res)
        else:
            results.append(CheckResult(CHECK_SHADOW, "skipped", "row not found"))
    except Exception as exc:  # noqa: BLE001
        results.append(_crashed(CHECK_SHADOW, exc))

    crashed_names = [r.name for r in results if r.status == "crashed"]
    if crashed_names:
        reason_tail = " | ".join(
            f"{r.name}={r.message}" for r in results if r.status == "crashed"
        )[:1500]
        row = await store.get(proposal_id)
        existing = (row or {}).get("status_reason") or ""
        reason = (
            f"checks_incomplete: {','.join(crashed_names)} crashed. "
            f"{reason_tail}. Retry via `morgoth recheck {proposal_id}`. "
            f"(prior status_reason: {existing[:400]})"
        )[:2000]
        await store.update_status(
            proposal_id, P.STATUS_CHECKS_INCOMPLETE, reason,
        )
        return results, P.STATUS_CHECKS_INCOMPLETE

    # 4. delegation — only if shadow didn't crash and REJECT under
    # delegation-on.
    if (delegation_enabled and shadow_res is not None
            and (shadow_res.detail or {}).get("verdict") == "REJECT"):
        try:
            reason = _format_delegation_reason(shadow_res.detail or {})
            await store.update_status(
                proposal_id, P.STATUS_SHADOW_REJECTED, reason[:2000],
            )
            results.append(CheckResult(
                CHECK_DELEGATION, "reject",
                "shadow REJECT flipped proposal to shadow_rejected",
                detail={"reason": reason},
            ))
            return results, P.STATUS_SHADOW_REJECTED
        except Exception as exc:  # noqa: BLE001
            results.append(_crashed(CHECK_DELEGATION, exc))
            # A crashed delegation ALSO holds the proposal.
            row = await store.get(proposal_id)
            existing = (row or {}).get("status_reason") or ""
            await store.update_status(
                proposal_id, P.STATUS_CHECKS_INCOMPLETE,
                f"checks_incomplete: delegation crashed. "
                f"{type(exc).__name__}: {exc}. "
                f"Retry via `morgoth recheck {proposal_id}`. "
                f"(prior: {existing[:400]})",
            )
            return results, P.STATUS_CHECKS_INCOMPLETE

    # PASS path — assemble the composite note into status_reason so the
    # operator sees the liveness verdict and overlap note at `morgoth show`.
    notes: list[str] = []
    for r in results:
        if r.status == "warn":
            notes.append(f"{r.name}: {r.message}")
    if notes:
        row = await store.get(proposal_id)
        existing = (row or {}).get("status_reason") or ""
        combined = (existing + " | " + " | ".join(notes)).strip(" |") \
            if existing else " | ".join(notes)
        await store.update_status(
            proposal_id, P.STATUS_PENDING_APPROVAL, combined[:2000],
        )
    return results, P.STATUS_PENDING_APPROVAL


def _format_delegation_reason(verdict: dict[str, Any]) -> str:
    """Compact one-line reason for status_reason on shadow_rejected.

    The ``[shadow]`` prefix keys off the negative-list rendering — it
    distinguishes shadow-auto-reject from operator reject in the
    calibration axis. Mirrors reflect._format_delegation_reason so a
    recheck-driven rejection is indistinguishable from a reflect-driven one.
    """
    axes = verdict.get("axes") or {}
    axes_str = " ".join(f"{a}={l}" for a, l in axes.items())
    reasons = verdict.get("reasons") or []
    reason_str = " | ".join(str(r) for r in reasons[:3])
    return f"[shadow] verdict=REJECT ({axes_str}) reasons: {reason_str}"
