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


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="self_modify.cli",
        description="Inspect and gate self-modify proposals.",
    )
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p_list = subparsers.add_parser("list", help="list pending + recent proposals")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--recent", action="store_true", help="also show recent history")
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
