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


CHECK_ARTIFACT = "artifact"
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


def _run_coroutine_sync(coro: Any) -> Any:
    """Run a coroutine to completion regardless of whether an event
    loop is already running in the current thread. Used by the
    artifact check so it composes both inside async callers (reflect,
    recheck via ``await run_post_submission_checks(...)``) and inside
    plain sync unit tests."""
    import asyncio, threading
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is already running in this thread; run the coroutine in
    # a fresh loop in a worker thread and wait for the result.
    box: dict[str, Any] = {}
    def _target() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _artifact_check(
    proposal_row: dict[str, Any],
    probe: dict[str, Any] | None,
) -> CheckResult:
    """Execute the GENERATED FILE against the responses the liveness
    probe recorded. Rendering bugs OUTSIDE the resolver (URL/query
    formatting, header wiring, class body, execute() loop, exception
    handling) surface here — a shape ``resolve_digest_fields`` handles
    correctly could still crash at the wrapper layer. 9f446bb4 was the
    call-out: gate_tests happily reported 46→46 while the generated
    tool raised ``TypeError: unhashable type: 'dict'`` on every real
    request. This check runs the tool end-to-end in-process against
    the probe's recorded body — no network, no re-hit — and rejects
    on crash or all-null digest with the traceback tail attached.
    """
    import asyncio, importlib, traceback as _tb
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    content = proposal_row.get("content") or ""
    if not content:
        return CheckResult(CHECK_ARTIFACT, "skipped", "no proposal content")
    if not probe:
        return CheckResult(CHECK_ARTIFACT, "skipped", "no probe supplied")
    ok_hits = [h for h in probe.get("hits", [])
               if h.get("ok") and h.get("body") is not None]
    if not ok_hits:
        return CheckResult(
            CHECK_ARTIFACT, "skipped",
            "no OK probe body recorded — nothing to replay",
        )
    body = ok_hits[0]["body"]

    # Exec the file in an isolated namespace. Imports (BaseTool,
    # resolve_digest_fields, httpx, etc.) resolve against the live
    # repo — the check runs AFTER gate_tests, so any harmful imports
    # already failed in the sandbox. This check judges FUNCTIONAL
    # correctness of the artifact, not safety.
    ns: dict[str, Any] = {}
    try:
        exec(compile(content, "<artifact>", "exec"), ns, ns)
    except Exception as exc:  # noqa: BLE001
        tail = "\n".join(_tb.format_exception(type(exc), exc, exc.__traceback__)
                         )[-500:]
        return CheckResult(
            CHECK_ARTIFACT, "reject",
            f"module exec crashed: {type(exc).__name__}: {exc}",
            detail={"traceback_tail": tail},
        )
    cls = next(
        (v for v in ns.values()
         if isinstance(v, type) and getattr(v, "is_data_source", False)),
        None,
    )
    if cls is None:
        return CheckResult(
            CHECK_ARTIFACT, "reject",
            "no data_source tool class found in the rendered module",
        )

    fake_resp = SimpleNamespace(
        status_code=200, json=lambda: body,
        raise_for_status=lambda: None,
    )
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=fake_resp)
    fake_client.aclose = AsyncMock()
    cfg = SimpleNamespace(
        permissions=SimpleNamespace(
            permissions=SimpleNamespace(can_access_internet=True),
        )
    )
    try:
        tool = cls(cfg, client=fake_client)
        # Route the tool's async execute() through a dedicated helper
        # so we can be called from either a running loop (reflect,
        # recheck) or a plain sync test — asyncio.run() alone would
        # raise "cannot be called from a running event loop" inside
        # pytest-asyncio and inside run_post_submission_checks.
        out = _run_coroutine_sync(tool.execute())
    except Exception as exc:  # noqa: BLE001
        tail = "\n".join(_tb.format_exception(type(exc), exc, exc.__traceback__)
                         )[-500:]
        return CheckResult(
            CHECK_ARTIFACT, "reject",
            f"execute() crashed: {type(exc).__name__}: {exc}",
            detail={"traceback_tail": tail},
        )

    if not (isinstance(out, dict) and out.get("success")):
        return CheckResult(
            CHECK_ARTIFACT, "reject",
            f"execute() returned failure: "
            f"{(out or {}).get('error') if isinstance(out, dict) else out!r}",
            detail={"out": out},
        )
    values = out.get("result", {}) or {}
    if not values or all(v is None for v in values.values()):
        return CheckResult(
            CHECK_ARTIFACT, "reject",
            f"execute() extracted no values from the recorded body "
            f"(digest all-null): {list(values)}",
            detail={"result": values},
        )
    return CheckResult(
        CHECK_ARTIFACT, "ok",
        f"executed against recorded body → "
        f"{len(values)} value(s) extracted",
        detail={"result": values},
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

    # 0. artifact — execute the generated file against the probe's
    # recorded body. Rendering bugs outside the resolver surface here.
    proposal_row_now = await store.get(proposal_id)
    try:
        results.append(_artifact_check(proposal_row_now or {}, probe))
    except Exception as exc:  # noqa: BLE001
        results.append(_crashed(CHECK_ARTIFACT, exc))

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

    # 2026-09-29: artifact reject OR liveness reject → rejected_static.
    # An artifact failure is the most severe (the tool literally can't
    # produce a value on any real request); a liveness null/no-info
    # reject is next. Both share the terminal so downstream negative-
    # list logic and retry policy treats them uniformly.
    artifact_res = next(
        (r for r in results if r.name == CHECK_ARTIFACT), None,
    )
    if artifact_res is not None and artifact_res.status == "reject":
        reason = f"rejected_static: artifact: {artifact_res.message}"[:2000]
        await store.update_status(
            proposal_id, P.STATUS_REJECTED_STATIC, reason,
        )
        return results, P.STATUS_REJECTED_STATIC
    liveness_res = next(
        (r for r in results if r.name == CHECK_LIVENESS), None,
    )
    if liveness_res is not None and liveness_res.status == "reject":
        reason = f"rejected_static: {liveness_res.message}"[:2000]
        await store.update_status(
            proposal_id, P.STATUS_REJECTED_STATIC, reason,
        )
        return results, P.STATUS_REJECTED_STATIC

    # PASS path — write a FRESH composite reason from the current
    # check results (never append to the row's prior reason, so a
    # recheck cleanly reflects the current verdict slate instead of
    # accumulating stale reasons).
    parts: list[str] = []
    for r in results:
        if r.status == "warn":
            parts.append(f"{r.name}: {r.message}")
        elif r.status == "ok" and r.name in (CHECK_LIVENESS, CHECK_ARTIFACT):
            parts.append(f"{r.name}: {r.message}")
    if parts:
        await store.update_status(
            proposal_id, P.STATUS_PENDING_APPROVAL,
            (" | ".join(parts))[:2000],
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
