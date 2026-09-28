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


def _target_change_note(orig: dict, retry: dict) -> str:
    """Return "" if the retry keeps the same host + tool_name as the
    original, else a short summary of the divergence. Compares the
    stored spec-content (pre-submit rows carry JSON) or the assembled
    template body (gate-3 rows carry Python — extract via regex)."""
    import json as _json
    import re as _re
    from urllib.parse import urlparse as _urlparse

    def _host_tool(row: dict) -> tuple[str, str]:
        content = row.get("content") or ""
        if not isinstance(content, str):
            return "", ""
        stripped = content.lstrip()
        if stripped.startswith("{"):
            try:
                s = _json.loads(content)
            except Exception:
                return "", ""
            if isinstance(s, dict):
                url = s.get("api_base_url") or ""
                host = _urlparse(url).hostname or ""
                return host, str(s.get("tool_name") or "")
        m_url = _re.search(r"^_BASE_URL\s*=\s*['\"]([^'\"]+)['\"]", content, _re.M)
        host = _urlparse(m_url.group(1)).hostname if m_url else ""
        tp = row.get("target_path") or ""
        tool = tp.split("/")[-1].replace(".py", "") if tp else ""
        return host or "", tool
    h1, t1 = _host_tool(orig)
    h2, t2 = _host_tool(retry)
    diffs = []
    if h1 and h2 and h1 != h2:
        diffs.append(f"host {h1!r} → {h2!r}")
    if t1 and t2 and t1 != t2:
        diffs.append(f"tool_name {t1!r} → {t2!r}")
    return "; ".join(diffs)


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
    # Retry-target lock (2026-09-24): if this row is a retry, compare its
    # host + tool_name against the original's; a divergence means the
    # retry SUBSTITUTED a different metric to pass the gate, which the
    # rejected_shape corrective prompt now forbids. Loud gate-3 note.
    retry_of = row.get("retry_of")
    if retry_of:
        try:
            orig = await store.get(str(retry_of))
        except Exception:
            orig = None
        if orig is not None:
            note = _target_change_note(orig, row)
            if note:
                print(f"TARGET CHANGED: {note}")
            else:
                print(f"retry_of:      {str(retry_of)[:8]} (target UNCHANGED)")
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
    # Amendments — operator edits recorded via `morgoth amend` (2026-09-30).
    amendments = row.get("amendments")
    if isinstance(amendments, str):
        import json as _json
        try:
            amendments = _json.loads(amendments)
        except Exception:  # noqa: BLE001
            amendments = None
    if amendments:
        print(f"--- amendments ({len(amendments)}) ---")
        for a in amendments:
            print(f"  {a.get('ts')}  by {a.get('who')}")
            if a.get("note"):
                print(f"    note: {a['note']}")
            for k, v in (a.get("changes") or {}).items():
                b = v.get("before"); af = v.get("after")
                print(f"    {k}: BEFORE={b!r}")
                print(f"    {' ' * len(k)}  AFTER ={af!r}")
    print("--- content ---")
    print(row.get("content") or "")
    return 0


async def _cmd_amend(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Operator amendment: rename digest fields (units go here so every
    downstream step sees them), edit description/rationale. Regenerates
    the tool source and reruns ALL post-submission checks. The
    amendment is appended to the row's ``amendments`` jsonb (who, ts,
    changes, note) and surfaced by ``morgoth show``.

    Rationale (2026-09-30): synthesis reads payload FIELD NAMES and
    VALUES, never descriptions. Units that live only in a description
    are invisible to every downstream consumer. ``morgoth amend`` is
    the third way at gate 3 — between approve and reject — so an
    operator can correct a unit-carrying detail without dropping the
    proposal (and its already-earned artifact/shadow verdicts).
    """
    from self_modify import post_submission_checks as _pchecks
    from self_modify import liveness as _liveness
    from self_modify import reflect as _reflect
    from self_modify.digest_path import normalize_digest_fields as _norm
    from urllib.parse import urlparse as _urlparse
    import ast as _ast, json as _json, os as _os
    import re as _re
    from datetime import datetime as _dt, timezone as _tz

    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1

    # Parse existing spec facts out of the rendered source (same shape
    # as _cmd_recheck's regenerate path).
    content = row.get("content") or ""
    def _grab(sym: str) -> str | None:
        m = _re.search(rf"^{sym}\s*=\s*(.+)$", content, _re.MULTILINE)
        return m.group(1).strip() if m else None
    base_raw, ep_raw, df_raw = _grab("_BASE_URL"), _grab("_ENDPOINT_PATH"), _grab("_DIGEST_FIELDS")
    desc_raw = _grab("_TOOL_DESCRIPTION")
    if not (base_raw and ep_raw and df_raw):
        print("amend: could not parse spec facts from proposal content", file=sys.stderr)
        return 2
    try:
        base_url = _ast.literal_eval(base_raw)
        endpoint_path = _ast.literal_eval(ep_raw)
        digest_fields = _ast.literal_eval(df_raw)
        description = _ast.literal_eval(desc_raw) if desc_raw else ""
    except (ValueError, SyntaxError) as exc:
        print(f"amend: proposal content parse failed: {exc}", file=sys.stderr)
        return 2

    # Snapshot BEFORE.
    before = {
        "digest_fields": list(digest_fields),
        "description": description,
        "rationale": row.get("rationale") or "",
    }
    # Apply --rename NEW_NAME=OLD_NAME and --path FIELD_NAME=EXPR.
    rename = dict(pair.split("=", 1) for pair in (args.rename or []) if "=" in pair)
    path_edits = dict(pair.split("=", 1) for pair in (args.path or []) if "=" in pair)
    entries = _norm(digest_fields)
    new_entries: list[dict[str, str]] = []
    for e in entries:
        n, p = e["name"], e["path"]
        # rename maps NEW=OLD so operators think in "what should this be called"
        for new_name, old_name in rename.items():
            if n == old_name:
                n = new_name
                break
        if n in path_edits:
            p = path_edits[n]
        new_entries.append({"name": n, "path": p})
    new_description = args.description if args.description is not None else description
    new_rationale = args.rationale if args.rationale is not None else (row.get("rationale") or "")
    after = {
        "digest_fields": new_entries,
        "description": new_description,
        "rationale": new_rationale,
    }
    if after == before:
        print("amend: no changes — nothing to do")
        return 0

    # Re-render with the current TOOL_TEMPLATE + amended fields.
    tool_name = (row.get("target_path") or "").split("/")[-1].rstrip(".py")
    class_name = _reflect._snake_to_class_name(tool_name)
    source_label = _urlparse(base_url).hostname or ""
    endpoint_declaration = _reflect._normalize_endpoint(base_url, endpoint_path)
    new_content = _reflect.TOOL_TEMPLATE.format(
        tool_name=tool_name, class_name=class_name,
        tool_name_repr=repr(tool_name),
        base_url_repr=repr(base_url),
        endpoint_path_repr=repr(endpoint_path),
        digest_fields_repr=repr(new_entries),
        description_repr=repr(new_description or tool_name),
        source_label_repr=repr(source_label),
        endpoint_declaration_repr=repr(endpoint_declaration),
        requires_key_env_repr=repr(None),
        key_in_repr=repr(None), key_param_repr=repr(None),
    )
    compile(new_content, f"<amended:{pid}>", "exec")

    who = _os.getenv("USER") or _os.getenv("USERNAME") or "operator"
    amendment = {
        "ts": _dt.now(_tz.utc).isoformat(),
        "who": who,
        "changes": {k: {"before": before[k], "after": after[k]}
                     for k in before if before[k] != after[k]},
        "note": args.note or "",
    }
    # Persist the amendment + rendered content + updated rationale.
    pm = store._pm  # noqa: SLF001
    await pm.execute(
        "UPDATE self_modify_proposals SET content = $1, rationale = $2, "
        "updated_at = now(), amendments = COALESCE(amendments, '[]'::jsonb) "
        "|| $3::jsonb, status = $4, status_reason = $5 "
        "WHERE proposal_id = $6::uuid",
        new_content, new_rationale, _json.dumps([amendment]),
        P.STATUS_PENDING_APPROVAL,
        f"amended by {who}: {', '.join(amendment['changes'].keys())}",
        pid,
    )
    print(f"amend: recorded {len(amendment['changes'])} change(s) by {who}; "
          f"content re-rendered ({len(new_content)} bytes)")

    # Rerun ALL post-submission checks: fresh liveness probe + artifact
    # + overlap + shadow + delegation. Uses the recheck argv machinery.
    smoke_target = base_url + endpoint_path
    hits = 4 if args.full else 1
    probe = None
    try:
        print(f"amend: liveness probe ({hits} hit{'s' if hits > 1 else ''}) → {smoke_target}")
        probe = await _liveness.run_liveness_probe(smoke_target, new_entries, hits=hits)
    except Exception as exc:  # noqa: BLE001
        print(f"amend: liveness probe crashed: {type(exc).__name__}: {exc}")

    spec = {
        "tool_name": tool_name, "api_base_url": base_url,
        "endpoint_path": endpoint_path,
        "digest_fields": new_entries,
        "description": new_description, "rationale": new_rationale,
    }
    config = await load_config()
    registered_field_names = _reflect._registered_digest_fields(config, pm)
    results, final_status = await _pchecks.run_post_submission_checks(
        store=store, proposal_id=pid, spec=spec, probe=probe,
        registered_field_names=registered_field_names,
        config=config, pm=pm,
        delegation_enabled=_reflect._delegation_enabled(),
    )
    print(f"amend: recheck final_status={final_status}")
    for r in results:
        print(f"  [{r.status:<8}] {r.name:<12} {r.message[:200]}")
    row = await store.get(pid)
    print(f"  status_reason: {(row or {}).get('status_reason') or ''}")
    return 0 if final_status in (
        P.STATUS_PENDING_APPROVAL, P.STATUS_SHADOW_REJECTED,
    ) else 1


async def _cmd_recheck(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Re-run the post-submission checks (liveness + overlap + shadow +
    delegation) against a pending proposal WITHOUT invoking reflect.

    Reason: a check crash held the proposal at ``checks_incomplete`` (or
    an earlier reflect release left it at ``pending_approval`` with a
    silent skip). ``recheck`` re-drives the exact same checks — a fresh
    liveness probe, overlap against the currently-discovered rail, a
    fresh shadow verdict, and (if delegation is on) the flip hook — and
    reports each with status + message so the operator can review the
    full slate without waiting for another reflect cycle.
    """
    from self_modify import post_submission_checks as _pchecks
    from self_modify import liveness as _liveness
    from self_modify import reflect as _reflect
    import json as _json

    pid, rc = await _resolve_or_bail(store, args.proposal_id)
    if pid is None:
        return rc
    row = await store.get(pid)
    if not row:
        print(f"no proposal with id {args.proposal_id!r}", file=sys.stderr)
        return 1
    # Spec comes from the proposal's rationale-adjacent payload: the
    # rejected/pending row's content is the rendered tool source. Parse
    # the spec back out of the source header so we can rebuild the
    # canonical name list and (probe URL, digest_fields). For a submitted
    # row the tool source contains _BASE_URL, _ENDPOINT_PATH and
    # _DIGEST_FIELDS — enough to rebuild what liveness needs.
    content = row.get("content") or ""
    import re as _re
    def _grab(sym: str) -> str | None:
        m = _re.search(rf"^{sym}\s*=\s*(.+)$", content, _re.MULTILINE)
        return m.group(1).strip() if m else None
    base = _grab("_BASE_URL")
    ep = _grab("_ENDPOINT_PATH")
    df = _grab("_DIGEST_FIELDS")
    if not (base and ep and df):
        print(
            "recheck: could not parse _BASE_URL/_ENDPOINT_PATH/_DIGEST_FIELDS "
            "from proposal content — refusing to guess.",
            file=sys.stderr,
        )
        return 2
    # Evaluate the literal strings safely — repr'd primitives only.
    import ast as _ast
    try:
        base_url = _ast.literal_eval(base)
        endpoint_path = _ast.literal_eval(ep)
        digest_fields = _ast.literal_eval(df)
    except (ValueError, SyntaxError) as exc:
        print(f"recheck: proposal content parse failed: {exc}", file=sys.stderr)
        return 2
    tool_name = (row.get("target_path") or "").split("/")[-1].rstrip(".py")
    # Try to recover the description too (repr'd string on _TOOL_DESCRIPTION).
    desc_raw = _grab("_TOOL_DESCRIPTION")
    try:
        description = _ast.literal_eval(desc_raw) if desc_raw else ""
    except (ValueError, SyntaxError):
        description = ""
    spec = {
        "tool_name": tool_name,
        "api_base_url": base_url,
        "endpoint_path": endpoint_path,
        "digest_fields": digest_fields,
        "description": description, "rationale": row.get("rationale") or "",
    }
    smoke_target = base_url + endpoint_path

    config = await load_config()
    pm = store._pm  # noqa: SLF001

    # 2026-09-29: --regenerate re-renders the proposal's content using
    # the CURRENT TOOL_TEMPLATE. Motivated by 9f446bb4: its stored
    # content was rendered with a pre-fix template that dropped path
    # info and would have raised TypeError at runtime. Regenerate
    # uses ONLY the spec facts we already extracted from the row
    # (base_url, endpoint_path, digest_fields, description) — no LLM
    # call, no re-reflect. The row's content is updated in place so
    # subsequent gates + apply see the fixed source.
    if getattr(args, "regenerate", False):
        from self_modify.digest_path import normalize_digest_fields as _norm
        from urllib.parse import urlparse as _urlparse
        entries = _norm(digest_fields)
        source_label = _urlparse(base_url).hostname or ""
        endpoint_declaration = _reflect._normalize_endpoint(base_url, endpoint_path)
        new_content = _reflect.TOOL_TEMPLATE.format(
            tool_name=tool_name,
            class_name=_reflect._snake_to_class_name(tool_name),
            tool_name_repr=repr(tool_name),
            base_url_repr=repr(base_url),
            endpoint_path_repr=repr(endpoint_path),
            digest_fields_repr=repr(entries),
            description_repr=repr(description or tool_name),
            source_label_repr=repr(source_label),
            endpoint_declaration_repr=repr(endpoint_declaration),
            requires_key_env_repr=repr(None),
            key_in_repr=repr(None),
            key_param_repr=repr(None),
        )
        # Compile-check before persisting — a template bug must not
        # replace a working proposal with garbage.
        compile(new_content, f"<regenerated:{pid}>", "exec")
        await pm.execute(
            "UPDATE self_modify_proposals SET content = $1, "
            "updated_at = now() WHERE proposal_id = $2::uuid",
            new_content, pid,
        )
        print(f"recheck: regenerated proposal content ({len(new_content)} bytes)")

    # Fresh liveness probe. Under `recheck` we run 1 hit (not 4) unless
    # the operator opts into --full: the goal is "did the crash reproduce",
    # not "did the source freeze over 7.5 min". The overlap + shadow checks
    # don't care about probe depth.
    # Pass RAW digest_fields to the probe (may be a mix of strings and
    # {name, path} dicts). resolve_digest_fields handles both natively.
    hits = 4 if args.full else 1
    probe = None
    try:
        print(f"recheck: liveness probe ({hits} hit{'s' if hits > 1 else ''}) → {smoke_target}")
        probe = await _liveness.run_liveness_probe(smoke_target, digest_fields, hits=hits)
    except Exception as exc:  # noqa: BLE001
        print(f"recheck: liveness probe crashed: {type(exc).__name__}: {exc}")
        probe = None

    registered_field_names = _reflect._registered_digest_fields(config, pm)
    results, final_status = await _pchecks.run_post_submission_checks(
        store=store, proposal_id=pid, spec=spec, probe=probe,
        registered_field_names=registered_field_names,
        config=config, pm=pm,
        delegation_enabled=_reflect._delegation_enabled(),
    )
    print(f"recheck: final_status={final_status}")
    for r in results:
        print(f"  [{r.status:<8}] {r.name:<12} {r.message[:200]}")
    # Refresh row and print the updated status_reason.
    row = await store.get(pid)
    print(f"  status_reason: {(row or {}).get('status_reason') or ''}")
    return 0 if final_status in (
        P.STATUS_PENDING_APPROVAL, P.STATUS_SHADOW_REJECTED,
    ) else 1


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
    from self_modify.digest_path import (
        normalize_digest_fields as _norm_digest,
    )
    _digest_entries = _norm_digest(spec.get("digest_fields"))
    _dnames_list = [e["name"] for e in _digest_entries]
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
        digest_fields_repr=repr(_digest_entries),
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
        smoke_target, list(spec.get("digest_fields") or []),
    ))
    pipeline_status = await _gates.run_pipeline(store, new_row)
    print(f"provision: pipeline final_status={pipeline_status}")
    try:
        probe = await probe_task
        verdict = _liveness.classify_probe(probe, _dnames_list)
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


async def _cmd_env(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Print the environment snapshot + current routing vs recommended.

    Read-only. NEVER writes .env. NEVER auto-selects a paid provider —
    api appears in the RECOMMENDED column only when ANTHROPIC_API_KEY
    is present, and only with an explicit 'requires operator opt-in
    (cost)' marker (see suggest_routing).
    """
    from core.llm.environment import detect_environment, suggest_routing
    from core.llm import registry as _reg, tasks as _tasks
    env = await detect_environment()
    print("═══ Environment snapshot ═══")
    for line in env.to_lines():
        print(line)
    # Sandbox posture — cheap probe (no pytest). Fail-closed since
    # 2026-09-24: gate_tests refuses to run pytest unless all three
    # layers apply. Surface degradation here so the operator sees it
    # before invoking `morgoth reflect`.
    from self_modify.gates import sandbox_posture
    _sp = sandbox_posture()
    if _sp["ok"]:
        print("SANDBOX      : confined (netns + bwrap + cgroup)")
    else:
        print(f"SANDBOX      : UNAVAILABLE ({_sp['reason']}) — gate_tests fails closed")
    recommendations = {r.task: r for r in suggest_routing(env)}
    print("\n═══ Routing (CURRENT vs RECOMMENDED) ═══")
    print(f"  {'TASK':<10}  {'CURRENT':<26}  {'RECOMMENDED':<26}  MATCH")
    print("  " + "-" * 78)
    diffs: list[tuple[str, str, str]] = []
    for task in _tasks.all_tasks():
        cur_p, cur_m = _reg.resolve(task)
        cur = f"{cur_p}:{cur_m}"
        rec = recommendations.get(task)
        rec_str = f"{rec.provider}:{rec.model}" if rec else "(none)"
        if not rec:
            marker = " "
        elif rec.provider == cur_p and (rec.model == cur_m or rec.model == "default"):
            marker = "MATCH"
        elif rec.provider == cur_p:
            marker = "DIFFERS (model)"
            diffs.append((task, cur, rec_str))
        else:
            # Configured provider unavailable on THIS machine → BROKEN.
            unavailable = (
                (cur_p == "ollama" and env.ollama.status == "unavailable")
                or (cur_p == "claude-cli" and env.claude_cli.status != "ok")
                or (cur_p == "api" and env.api_key.status == "unavailable")
            )
            marker = "BROKEN" if unavailable else "DIFFERS"
            diffs.append((task, cur, rec_str))
        print(f"  {task:<10}  {cur:<26}  {rec_str:<26}  {marker}")
    if diffs:
        print("\n═══ To apply the recommendation, add to .env (or export): ═══")
        for task, _cur, rec_str in diffs:
            env_key = f"MORGOTH_LLM_{task.upper()}"
            print(f"  {env_key}={rec_str}")
        print("\n  (System does NOT write .env for you — operator's decision.)")
    return 0


async def _cmd_rail_check(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """One polite call per registered data-source tool; classify each as
    OK / DEGRADED / FROZEN / DEAD; persist to rail_health for cross-run
    FROZEN detection.

    Sequential with a polite delay so the tightest source (Owlracle
    100 req/hr) is respected. Never disables a tool — report only.
    """
    import asyncio as _asyncio
    import time as _time
    from unittest.mock import MagicMock
    from analysis import rail_health as RH
    from core.brain import DATA_SOURCE_TOOLS
    from core.config import load_config
    from api.server import build_tool_router
    from memory.episodic import EpisodicMemory

    pm = store._pm
    pool = pm._require_pool()

    # Load prior digests (one per tool, newest first).
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT ON (tool_name) tool_name, digest FROM rail_health "
            "ORDER BY tool_name, created_at DESC"
        )
    prior = {r["tool_name"]: r["digest"] for r in rows if r["digest"]}

    config = await load_config()
    # Data-source tools only touch config + persistent_memory; agent_manager
    # and notifier are used by non-rail tools (create_agent/notify). Mock
    # them so the rail-check CLI doesn't need to spin up a full runtime.
    em = EpisodicMemory(config.chroma_dir)
    router = build_tool_router(config, pm, em, MagicMock(), MagicMock())

    results: list[RH.RailResult] = []
    tool_list = sorted(DATA_SOURCE_TOOLS)
    for i, name in enumerate(tool_list):
        try:
            tool = router.get_tool(name)
        except Exception as exc:
            results.append(RH.RailResult(
                tool_name=name, status="DEAD", digest="",
                detail=f"tool not registered: {exc}",
            ))
            continue
        declared = tuple(getattr(type(tool), "digest_fields", ()) or ())
        # Pick minimal args per tool — most take none; a few need symbol/series_id.
        kwargs: dict[str, object] = {}
        if name == "get_crypto_price":
            kwargs = {"symbol": "btc"}
        elif name == "get_crypto_history":
            kwargs = {"symbol": "btc", "days": 3}
        elif name == "fred_series_observations":
            kwargs = {"series_id": "UNRATE"}
        elif name == "get_news":
            kwargs = {"query": "bitcoin"}
        elif name == "web_search":
            kwargs = {"query": "bitcoin"}
        t0 = _time.monotonic()
        try:
            result = await router.execute_tool(name, kwargs)
        except Exception as exc:
            result = exc
        latency_ms = int((_time.monotonic() - t0) * 1000)
        rail = RH.classify(name, result, declared, prior.get(name), latency_ms=latency_ms)
        results.append(rail)
        # Persist for FROZEN detection on subsequent runs.
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO rail_health (tool_name, status, digest, detail, latency_ms) "
                    "VALUES ($1, $2, $3, $4, $5)",
                    rail.tool_name, rail.status, rail.digest,
                    rail.detail[:500], rail.latency_ms,
                )
        except Exception as exc:
            print(f"WARN: could not persist rail_health for {name}: {exc}")
        if i < len(tool_list) - 1:
            await _asyncio.sleep(RH.DEFAULT_INTER_TOOL_DELAY_SECS)
    print(RH.render_table(results))
    return 0


async def _cmd_session_report(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """One-shot session summary for the operator's short cycling window.

    Read-only aggregator over abstention_events, rate_limit_events,
    llm_calls, theses, objectives, proposals. --full also invokes the
    descriptive backtest to compute the window's verifiable share
    (slower — one HTTP round-trip per source).

    --since accepts the same '7 days'/'24h'/'30m' grammar as `morgoth
    audit --since` (reuses auto_approve.parse_since).
    """
    from datetime import datetime, timezone
    from analysis import session_report as SR
    from self_modify.auto_approve import parse_since as _parse_since
    if args.since:
        try:
            delta = _parse_since(args.since)
        except ValueError as exc:
            print(f"error: {exc}")
            return 2
        since = datetime.now(tz=timezone.utc) - delta
    else:
        # Default: since service (re)start — best proxy is uptime from
        # /api/brain/status; falls back to last 24 h if that fails.
        since = datetime.now(tz=timezone.utc) - timedelta_from_uptime()
    r = await SR.collect(store._pm, since, full=args.full)
    print(r.render())
    return 0


def timedelta_from_uptime():
    """Best-effort: query the running service for its uptime; if unreachable,
    default to 24h. Kept trivial to avoid pulling httpx into cli.py imports."""
    from datetime import timedelta
    import httpx
    try:
        r = httpx.get("http://localhost:8000/api/brain/status", timeout=3.0)
        secs = int(r.json().get("uptime_seconds") or 0)
        if secs > 0:
            return timedelta(seconds=secs)
    except Exception:
        pass
    return timedelta(days=1)


async def _cmd_models(store: P.ProposalStore, args: argparse.Namespace) -> int:
    """Print the live task→provider routing table and reachability of each
    provider. NEVER prints the ANTHROPIC_API_KEY value — presence only."""
    from core.llm import registry as _reg
    from core.llm.providers import probe_reachability
    print(f"{'TASK':<10}  {'PROVIDER':<12}  {'MODEL':<24}  SOURCE")
    print("-" * 70)
    for row in _reg.routing_table():
        default_note = "" if row["source"] == "env" else "(default)"
        print(
            f"  {row['task']:<8}  {row['provider']:<12}  "
            f"{row['model']:<24}  {row['source']:<7} {default_note}"
        )
    print("\nProvider reachability:")
    for name, (ok, note) in probe_reachability().items():
        mark = "OK  " if ok else "FAIL"
        print(f"  [{mark}] {name:<12}  {note}")
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

    p_recheck = subparsers.add_parser(
        "recheck",
        help=(
            "re-run the post-submission checks on a proposal without "
            "invoking reflect; use after a checks_incomplete or when "
            "you want a fresh liveness+shadow slate."
        ),
    )
    p_recheck.add_argument("proposal_id")
    p_recheck.add_argument(
        "--full", action="store_true",
        help="4-hit liveness probe (7.5 min) instead of the default 1 hit",
    )
    p_recheck.add_argument(
        "--regenerate", action="store_true",
        help=(
            "re-render the proposal's tool source with the current "
            "TOOL_TEMPLATE (uses only the stored spec facts — no LLM "
            "call). Fixes proposals whose stored source pre-dates a "
            "template fix, e.g. 9f446bb4 which was rendered before "
            "the 2026-09-29 ONE-EXTRACTOR change."
        ),
    )
    p_recheck.set_defaults(_fn=_cmd_recheck)

    p_amend = subparsers.add_parser(
        "amend",
        help=(
            "amend a pending proposal's spec (digest field names, "
            "description, rationale) and rerun ALL checks. Units "
            "belong in FIELD NAMES so every downstream step sees "
            "them; descriptions are invisible to synthesis."
        ),
    )
    p_amend.add_argument("proposal_id")
    p_amend.add_argument(
        "--rename", action="append", default=[], metavar="NEW=OLD",
        help=(
            "rename a digest field. Repeatable. Format 'NEW=OLD' — "
            "so 'open_interest_usd=open_interest' reads as 'the new "
            "name is open_interest_usd, replacing open_interest'."
        ),
    )
    p_amend.add_argument(
        "--path", action="append", default=[], metavar="FIELD=EXPR",
        help=(
            "override the resolver path expression for a field. "
            "Repeatable. Format 'FIELD_NAME=path.expression'."
        ),
    )
    p_amend.add_argument(
        "--description", default=None,
        help="replace the tool's description text",
    )
    p_amend.add_argument(
        "--rationale", default=None,
        help="replace the proposal's rationale text",
    )
    p_amend.add_argument(
        "--note", default=None,
        help="a short note (e.g. 'verified from docs.deribit.com') "
             "recorded alongside the amendment for gate-3 audit",
    )
    p_amend.add_argument(
        "--full", action="store_true",
        help="4-hit liveness probe (7.5 min) after amend; default 1 hit",
    )
    p_amend.set_defaults(_fn=_cmd_amend)

    p_provision = subparsers.add_parser(
        "provision",
        help=(
            "re-drive a pending_key proposal once the env var is set "
            "(checks PRESENCE only; the value is never read or logged)"
        ),
    )
    p_provision.add_argument("proposal_id")
    p_provision.set_defaults(_fn=_cmd_provision)

    p_models = subparsers.add_parser(
        "models", help="print task→provider routing table + provider reachability",
    )
    p_models.set_defaults(_fn=_cmd_models)

    p_env = subparsers.add_parser(
        "env",
        help=(
            "print environment capability snapshot + current vs recommended "
            "LLM routing. Read-only; NEVER writes .env; NEVER auto-selects "
            "a paid provider."
        ),
    )
    p_env.set_defaults(_fn=_cmd_env)

    p_rail = subparsers.add_parser(
        "rail-check",
        help=(
            "one polite call per data-source tool; classify OK/DEGRADED/"
            "FROZEN/DEAD. Persists to rail_health for cross-run FROZEN "
            "detection. Read-only against the rail."
        ),
    )
    p_rail.set_defaults(_fn=_cmd_rail_check)

    p_session = subparsers.add_parser(
        "session-report",
        help=(
            "one-shot session summary since restart (or --since '7 days'). "
            "Add --full to compute verifiable-share (slower — hits data sources)."
        ),
    )
    p_session.add_argument("--since", default=None,
                           help="'7 days'|'24h'|'30m' — window start; default: since service restart")
    p_session.add_argument("--full", action="store_true",
                           help="also compute verifiable-share (slow; hits CoinGecko/mempool/etc.)")
    p_session.set_defaults(_fn=_cmd_session_report)

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
