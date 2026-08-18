"""CLI surface for the self-modify pipeline.

Invoked via ``python -m self_modify.cli <subcommand> [args]``. The
morgoth-cli wrapper (``scripts/morgoth-cli.sh``) delegates to this
module for the ``proposals``, ``show``, ``approve``, ``reject``, and
``apply`` subcommands.

``approve`` moves a proposal to ``approved_pending_apply``; ``apply``
then runs the full sequence in ``self_modify.apply`` (preconditions →
write → live pytest → local commit → restart → health probe → rollback
on failure).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from core.config import load_config
from memory.persistent import PersistentMemory
from self_modify import auto_approve as AA
from self_modify import proposals as P


def _fmt_row(row: dict[str, Any]) -> str:
    proposal_id = str(row["proposal_id"])[:8]
    return "  ".join(
        [
            proposal_id,
            (row.get("status") or "").ljust(24),
            (row.get("change_type") or "").ljust(9),
            row.get("target_path") or "",
        ]
    )


def _print_table(rows: list[dict[str, Any]], header: str) -> None:
    print(f"== {header} ==")
    if not rows:
        print("(no proposals)")
        return
    print(f"{'id':8}  {'status':24}  {'change':9}  target_path")
    print("-" * 80)
    for row in rows:
        print(_fmt_row(row))


async def _cmd_list(store: P.ProposalStore, args: argparse.Namespace) -> int:
    pending = await store.list_pending(limit=args.limit)
    _print_table(pending, "pending approval")

    # Keyed park lot — non-terminal, awaiting operator provisioning.
    # Always shown so the operator doesn't lose track of the park.
    try:
        parked = await store.list_by_status(P.STATUS_PENDING_KEY, limit=args.limit)
    except Exception:  # noqa: BLE001
        parked = []
    if parked:
        print()
        _print_table(parked, f"pending key ({len(parked)}) — needs provisioning")
        for row in parked:
            import json as _json
            try:
                spec = _json.loads(row.get("content") or "{}")
            except Exception:  # noqa: BLE001
                spec = {}
            rk = spec.get("requires_key") if isinstance(spec, dict) else None
            if isinstance(rk, dict):
                print(
                    f"  {str(row['proposal_id'])[:8]}: env_var={rk.get('env_var')}"
                    f"  signup={rk.get('signup_url')}"
                )

    if getattr(args, "all", False):
        shadow = await store.list_shadow_rejected(limit=args.limit)
        # Tag each row with [shadow] in the target_path column so the
        # operator sees the source at a glance in the compact table.
        for row in shadow:
            row["target_path"] = "[shadow] " + (row.get("target_path") or "")
        print()
        _print_table(shadow, f"shadow_rejected ({len(shadow)}) — audit")
    if args.recent:
        recent = await store.list_recent(limit=args.limit)
        print()
        _print_table(recent, f"recent ({len(recent)})")
    return 0


async def _resolve_or_bail(
    store: P.ProposalStore, ref: str,
) -> tuple[str | None, int]:
    """Resolve a short or full ID; on failure print a clean one-liner
    and return (None, exit_code). Success returns (full_uuid, 0).

    Every ID-consuming subcommand routes through this — replaces the
    raw uuid.UUID(...) ValueError tracebacks with the reflect_llm
    hygiene pattern (message + exit 2 on operator-input errors).
    """
    try:
        pid = await store.resolve_id(ref)
    except (LookupError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return None, 2
    return pid, 0


async def _cmd_show(store: P.ProposalStore, args: argparse.Namespace) -> int:
    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    print(f"proposal_id:   {row['proposal_id']}")
    print(f"created_at:    {row['created_at']}")
    print(f"updated_at:    {row['updated_at']}")
    print(f"target_path:   {row['target_path']}")
    print(f"change_type:   {row['change_type']}")
    print(f"status:        {row['status']}")
    print(f"status_reason: {row.get('status_reason') or ''}")
    print(f"rationale:     {row.get('rationale') or ''}")
    # Shadow verdicts (Gate 2.5) — recorded, never enforced.
    try:
        verdicts = await store._pm.get_shadow_verdicts(str(row["proposal_id"]))  # noqa: SLF001
    except Exception:  # noqa: BLE001
        verdicts = []
    if verdicts:
        print("--- shadow verdicts (Gate 2.5, recorded not enforced) ---")
        for v in verdicts:
            print(
                f"  {v.get('created_at')}  {v.get('verdict')}  "
                f"engine={v.get('engine')}  prompt={v.get('prompt_version')}"
            )
            for axis, level in (v.get("axes") or {}).items():
                print(f"    {axis:<26} {level}")
            for r in (v.get("reasons") or []):
                print(f"    - {r}")
    print("--- content ---")
    print(row.get("content") or "")
    return 0


async def _cmd_shadow(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Manually re-run the shadow verifier on any proposal."""
    from self_modify import shadow as _shadow

    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    config = await load_config()
    v = await _shadow.run_shadow_verdict(
        proposal=row, config=config, pm=store._pm,  # noqa: SLF001
    )
    print(f"shadow verdict: {v.get('verdict')}  engine={v.get('engine')}  "
          f"prompt={v.get('prompt_version')}")
    for axis, level in (v.get("axes") or {}).items():
        print(f"  {axis:<26} {level}")
    for r in (v.get("reasons") or []):
        print(f"  - {r}")
    return 0


async def _cmd_approve(store: P.ProposalStore, args: argparse.Namespace) -> int:
    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    if row["status"] != P.STATUS_PENDING_APPROVAL:
        print(
            f"proposal is {row['status']!r}, not {P.STATUS_PENDING_APPROVAL!r}; "
            "only pending_approval proposals can be approved",
            file=sys.stderr,
        )
        return 1
    await store.update_status(
        pid, P.STATUS_APPROVED_PENDING_APPLY,
        "approved via morgoth cli",
    )
    print(f"proposal {pid} → approved_pending_apply")
    print(f"Next: `morgoth apply {pid[:8]}` to write the file, run")
    print("the live pytest, commit locally, restart, and verify.")
    return 0


async def _cmd_apply(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Apply an approved proposal via ``self_modify.apply.apply_proposal``.

    Prints the final status. Detailed step-by-step logs go through loguru
    to the systemd log; the DB row's status_reason carries the summary.
    """
    from self_modify import apply as apply_mod

    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    print(f"applying proposal {pid} …")
    final = await apply_mod.apply_proposal(store, pid)
    after = await store.get(pid)
    print(f"final status: {final}")
    if after:
        print(f"reason:       {after.get('status_reason') or ''}")
    return 0 if final == apply_mod.STATUS_APPLIED else 1


async def _cmd_provision(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Re-drive a pending_key proposal once the operator has set the env var.

    KEY-VALUE HYGIENE: this command inspects env-var PRESENCE only.
    It never reads, prints, logs, or otherwise surfaces the value.
    The env var must be set in the process environment (e.g. in .env
    which the systemd unit sources, or exported before the CLI call).
    """
    import json as _json
    import os as _os

    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    if row["status"] != P.STATUS_PENDING_KEY:
        print(
            f"proposal is {row['status']!r}, not {P.STATUS_PENDING_KEY!r}; "
            "only pending_key rows can be provisioned",
            file=sys.stderr,
        )
        return 1
    try:
        spec = _json.loads(row.get("content") or "{}")
    except Exception:  # noqa: BLE001
        print("could not parse spec from row.content", file=sys.stderr)
        return 1
    rk = spec.get("requires_key") if isinstance(spec, dict) else None
    if not isinstance(rk, dict) or not rk.get("env_var"):
        print("row has no requires_key.env_var; nothing to provision", file=sys.stderr)
        return 1
    env_var = rk["env_var"]
    # PRESENCE-only check. The value itself is not read by this code
    # path — comparing len > 0 doesn't require the value to appear on
    # any stack frame we log.
    present = bool(_os.environ.get(env_var, "").strip())
    if not present:
        print(
            f"provision refused: env var {env_var} is not set in the process "
            f"environment. Add it to .env (or export it) then restart the "
            f"service and re-run `morgoth provision {pid[:8]}`.",
            file=sys.stderr,
        )
        return 1
    print(f"provision: env var {env_var} present; re-driving walk from smoke …")

    # Import lazily so `morgoth provision` doesn't pay the reflect
    # import cost when the env var isn't set.
    from self_modify import gates as _gates
    from self_modify import liveness as _liveness
    from self_modify import reflect as _reflect

    config = await load_config()
    smoke_target = spec["api_base_url"].rstrip("/") + spec["endpoint_path"]
    # Re-render content from the current (possibly newer) TOOL_TEMPLATE
    # so a template improvement lands on provisioning too.
    tool_name = spec["tool_name"]
    class_name = _reflect._snake_to_class_name(tool_name)
    from urllib.parse import urlparse as _urlparse
    source_label = _urlparse(spec["api_base_url"]).hostname or ""
    endpoint_declaration = _reflect._normalize_endpoint(
        spec["api_base_url"], spec["endpoint_path"],
    )
    _key_in = spec.get("key_in")
    _key_param = spec.get("key_param")
    content = _reflect.TOOL_TEMPLATE.format(
        tool_name=tool_name,
        class_name=class_name,
        tool_name_repr=repr(tool_name),
        base_url_repr=repr(spec["api_base_url"]),
        endpoint_path_repr=repr(spec["endpoint_path"]),
        digest_fields_repr=repr(list(spec["digest_fields"])),
        description_repr=repr(spec["description"]),
        source_label_repr=repr(source_label),
        endpoint_declaration_repr=repr(endpoint_declaration),
        requires_key_env_repr=repr(env_var),
        key_in_repr=repr(_key_in),
        key_param_repr=repr(_key_param),
    )
    target_path = f"tools/data_feeds/{tool_name}.py"
    new_id = await store.submit(
        target_path=target_path,
        change_type="new_file",
        content=content,
        rationale=spec.get("rationale") or "",
        proposed_by="morgoth",
        engine=(row.get("engine") or "claude-cli"),
        retry_of=str(row["proposal_id"]),
    )
    print(f"provision: new proposal {new_id} (retry_of={pid[:8]})")
    new_row = await store.get(new_id)
    import asyncio as _asyncio
    probe_task = _asyncio.create_task(_liveness.run_liveness_probe(
        smoke_target, list(spec["digest_fields"]),
    ))
    pipeline_status = await _gates.run_pipeline(store, new_row)
    print(f"provision: pipeline final_status={pipeline_status}")
    try:
        probe = await probe_task
        verdict = _liveness.classify_probe(probe, list(spec["digest_fields"]))
        print(f"provision: liveness outcome={verdict['outcome']} rule={verdict.get('rule')}")
    except Exception as exc:  # noqa: BLE001
        print(f"provision: liveness probe error: {exc!r}")
    return 0 if pipeline_status == P.STATUS_PENDING_APPROVAL else 1


async def _cmd_reject(store: P.ProposalStore, args: argparse.Namespace) -> int:
    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    await store.update_status(
        pid, P.STATUS_REJECTED,
        args.reason or "rejected via morgoth cli",
    )
    print(f"proposal {pid} → rejected")
    return 0


async def _cmd_audit(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Gate-3 auto-approve observability.

    Two sub-actions:
      --now    → classify all CURRENT pending_approval proposals through
                 Rule R; print the tier + reason; if --write, persist
                 each judgement to auto_approve_decisions in observation
                 mode (flag_state reflects the env; criteria_state shows
                 what's missing).
      --since  → replay recent auto_approve_decisions log entries.

    Runs read-only against the DB (SELECT on proposals + shadow_verdicts).
    In observation mode NOTHING is applied — the decision log is the
    ONLY side-effect (and only if --write is passed).
    """
    pm = store._pm
    pool = pm._require_pool()

    # Pull recent operator decisions + apply outcomes for the criteria snapshot.
    # We look at the last 60 v2-shadow proposals that reached the operator surface.
    async with pool.acquire() as conn:
        op_rows = await conn.fetch(
            """
            SELECT p.proposal_id, p.status, p.status_reason, p.created_at,
                   v.verdict AS shadow_verdict, v.axes, v.prompt_version
            FROM self_modify_proposals p
            LEFT JOIN LATERAL (
                SELECT verdict, axes, prompt_version FROM shadow_verdicts
                WHERE proposal_id = p.proposal_id
                ORDER BY created_at DESC LIMIT 1
            ) v ON true
            WHERE p.proposed_by = 'morgoth' AND v.prompt_version = 'v2'
              AND p.status IN ('applied', 'apply_failed_rolled_back', 'rejected')
            ORDER BY p.created_at DESC
            LIMIT 60
            """
        )
    import json as _json
    op_decisions = []
    apply_outcomes = []
    for r in op_rows:
        raw_axes = r["axes"]
        if isinstance(raw_axes, str):
            try:
                raw_axes = _json.loads(raw_axes)
            except _json.JSONDecodeError:
                raw_axes = {}
        proposal = {"status": "pending_approval"}  # counterfactual: what tier would we have picked
        verdict = {
            "verdict": r["shadow_verdict"], "axes": raw_axes or {},
            "prompt_version": r["prompt_version"] or "",
        }
        counterfactual = AA.classify_tier(proposal, verdict)
        matched_R = counterfactual.tier == "AUTO"
        op_dec = "approve" if r["status"] in ("applied", "apply_failed_rolled_back") else "reject"
        op_decisions.append({"matched_signature_R": matched_R, "op_decision": op_dec})
        # Rollback-rate denominator = apply attempts only; rejects never
        # entered apply, so counting them would dilute the rate.
        if r["status"] in ("applied", "apply_failed_rolled_back"):
            apply_outcomes.append({"outcome": r["status"]})
    snapshot = AA.evaluate_criteria(op_decisions, apply_outcomes)
    flag = AA.auto_approve_enabled()
    print(f"AUTO_APPROVE_ENABLED={flag}  rule={AA.RULE_VERSION}")
    print(
        f"criteria: n={snapshot.n_decisions}/{AA.N_MIN_DECISIONS}  "
        f"false_approves={snapshot.n_false_approves}  "
        f"rollback_rate={snapshot.rollback_rate:.0%}  ok={snapshot.ok}"
    )
    if snapshot.reasons:
        for reason in snapshot.reasons:
            print(f"  · {reason}")

    if args.now:
        pending = await store.list_pending(limit=100)
        # Prefer status=='pending_approval' only.
        pending = [p for p in pending if p.get("status") == "pending_approval"]
        print(f"\nPending_approval proposals: {len(pending)}")
        for p in pending:
            verdicts = await pm.get_shadow_verdicts(str(p["proposal_id"]))
            v = verdicts[0] if verdicts else None
            decision = AA.classify_tier(p, v)
            may, why = AA.should_auto_apply(decision, snapshot)
            marker = "AUTO" if may else "HOLD"
            print(
                f"  [{marker}] {str(p['proposal_id'])[:8]}  tier={decision.tier}  "
                f"reason={decision.reason}  gate={why}"
            )
            if args.write:
                async with pool.acquire() as conn:
                    import json as _json
                    await conn.execute(
                        """
                        INSERT INTO auto_approve_decisions
                          (proposal_id, tier, reason, rule_version, flag_state,
                           criteria_state, signature, apply_outcome)
                        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, NULL)
                        """,
                        p["proposal_id"], decision.tier, decision.reason,
                        AA.RULE_VERSION, flag,
                        _json.dumps(snapshot.as_dict()),
                        _json.dumps(decision.signature),
                    )
        return 0

    # --since: replay the log. asyncpg::interval wants a datetime.timedelta,
    # not a Postgres string like '7 days' — so parse the CLI value here and
    # compute a UTC cutoff, then query WHERE created_at >= cutoff.
    from datetime import datetime, timezone
    since_clause = ""
    params: list[Any] = []
    if args.since:
        try:
            delta = AA.parse_since(args.since)
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        cutoff = datetime.now(tz=timezone.utc) - delta
        since_clause = "WHERE created_at >= $1"
        params.append(cutoff)
    async with pool.acquire() as conn:
        log_rows = await conn.fetch(
            f"SELECT proposal_id, tier, reason, rule_version, flag_state, "
            f"apply_outcome, created_at FROM auto_approve_decisions "
            f"{since_clause} ORDER BY created_at DESC LIMIT 100",
            *params,
        )
    print(f"\nDecision log entries: {len(log_rows)}")
    for r in log_rows:
        print(
            f"  {r['created_at'].isoformat()}  {str(r['proposal_id'])[:8]}  "
            f"tier={r['tier']}  flag={r['flag_state']}  outcome={r['apply_outcome'] or '(none)'}"
        )
        print(f"    reason: {r['reason']}")
    return 0


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="self_modify.cli",
        description="Inspect and gate self-modify proposals.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_list = subparsers.add_parser("list", help="list pending + recent proposals")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--recent", action="store_true", help="also show recent history")
    p_list.add_argument(
        "--all", action="store_true",
        help="also show shadow_rejected rows (audit surface)",
    )
    p_list.set_defaults(_fn=_cmd_list)

    p_show = subparsers.add_parser("show", help="show one proposal in full")
    p_show.add_argument("proposal_id")
    p_show.set_defaults(_fn=_cmd_show)

    p_approve = subparsers.add_parser("approve", help="approve a pending_approval proposal")
    p_approve.add_argument("proposal_id")
    p_approve.set_defaults(_fn=_cmd_approve)

    p_reject = subparsers.add_parser("reject", help="reject a proposal")
    p_reject.add_argument("proposal_id")
    p_reject.add_argument("--reason", default=None)
    p_reject.set_defaults(_fn=_cmd_reject)

    p_apply = subparsers.add_parser(
        "apply",
        help="apply an approved proposal (writes live tree; the door)",
    )
    p_apply.add_argument("proposal_id")
    p_apply.set_defaults(_fn=_cmd_apply)

    p_shadow = subparsers.add_parser(
        "shadow",
        help="manually re-run the Gate 2.5 shadow verifier on any proposal",
    )
    p_shadow.add_argument("proposal_id")
    p_shadow.set_defaults(_fn=_cmd_shadow)

    p_provision = subparsers.add_parser(
        "provision",
        help=(
            "re-drive a pending_key proposal once the env var is set "
            "(checks PRESENCE only; the value is never read or logged)"
        ),
    )
    p_provision.add_argument("proposal_id")
    p_provision.set_defaults(_fn=_cmd_provision)

    p_audit = subparsers.add_parser(
        "audit",
        help=(
            "gate-3 auto-approve observability: --now classifies current "
            "pending_approval proposals (dry unless --write); --since "
            "streams the decision log. Shipped INERT — the flag and the "
            "data-criteria guard both stay closed by default."
        ),
    )
    p_audit.add_argument("--now", action="store_true",
                         help="classify current pending_approval proposals through Rule R")
    p_audit.add_argument("--write", action="store_true",
                         help="persist each --now classification to the decision log")
    p_audit.add_argument("--since", default=None,
                         help="pg interval string, e.g. '7 days' — filters --since log listing")
    p_audit.set_defaults(_fn=_cmd_audit)

    args = parser.parse_args(argv)

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    store = P.ProposalStore(pm)
    try:
        return await args._fn(store, args)
    finally:
        await pm.close()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
