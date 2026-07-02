"""Wall test — end-to-end runtime validation of the self-modify pipeline.

Injects six proposals through ``run_pipeline`` against the live DB and a
real sandbox pytest run:

  1. edit core/brain.py                 → expect zone_rejected
  2. edit self_modify/zones.py          → expect zone_rejected
  3. edit IDENTITY.md                   → expect zone_rejected
  4. new_file scripts/evil.sh           → expect zone_rejected (default deny)
  5. edit tools/data_feeds/crypto.py    → expect zone_rejected (edit != green)
  6. new_file tools/data_feeds/wall_test_dummy.py  → gate_zone green,
     gate_tests REAL sandbox pytest pass → pending_approval

Then exercises the morgoth CLI's reject path on proposal #6 and confirms
the live tree has NO new files under tools/data_feeds/.

Test proposals are marked ``rejected`` with reason ``wall_test cleanup``
after the run so they don't clutter live queries.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402
from self_modify import gates, proposals as P  # noqa: E402


DUMMY_TOOL_CONTENT = '''"""Wall-test dummy tool — additive, no imports of Morgoth internals."""

from __future__ import annotations


class WallTestDummyTool:
    """Trivially valid — exercises the sandbox pytest gate.

    Nothing imports this file at collection time; the pytest run just needs
    the module to load without syntax errors while pytest collects tests.
    """

    name = "wall_test_dummy"
    description = "no-op tool used only by the self-modify wall test"

    def __init__(self, *_args, **_kwargs) -> None:
        return None
'''


CASES: list[tuple[str, str, str, str, str]] = [
    # (label, target_path, change_type, content, expected_status)
    ("1_edit_core_brain", "core/brain.py", "edit", "--- pretend diff ---",
     P.STATUS_ZONE_REJECTED),
    ("2_edit_self_modify_zones", "self_modify/zones.py", "edit", "--- pretend diff ---",
     P.STATUS_ZONE_REJECTED),
    ("3_edit_identity", "IDENTITY.md", "edit", "--- pretend diff ---",
     P.STATUS_ZONE_REJECTED),
    ("4_new_file_evil_sh", "scripts/evil.sh", "new_file", "#!/bin/sh\nrm -rf /\n",
     P.STATUS_ZONE_REJECTED),
    ("5_edit_green_dir", "tools/data_feeds/crypto.py", "edit", "--- pretend diff ---",
     P.STATUS_ZONE_REJECTED),
    ("6_legit_green", "tools/data_feeds/wall_test_dummy.py", "new_file", DUMMY_TOOL_CONTENT,
     P.STATUS_PENDING_APPROVAL),
]


async def _run() -> int:
    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()  # ensures the self_modify_proposals table exists
    store = P.ProposalStore(pm)

    results: list[dict[str, Any]] = []
    legit_id: str | None = None

    try:
        for label, target, change_type, content, expected in CASES:
            print(f"\n== {label} ==")
            pid = await store.submit(
                target_path=target,
                change_type=change_type,
                content=content,
                rationale=f"wall_test case {label}",
            )
            print(f"  submit → {pid[:8]}")
            row = await store.get(pid)
            assert row is not None
            final = await gates.run_pipeline(store, row, repo_root=REPO_ROOT)
            row_after = await store.get(pid)
            reason = (row_after or {}).get("status_reason") or ""
            print(f"  final status: {final}  (expected: {expected})")
            print(f"  reason: {reason[:120]}{'…' if len(reason) > 120 else ''}")
            ok = final == expected
            results.append(
                {"label": label, "id": pid, "expected": expected,
                 "actual": final, "ok": ok, "reason": reason}
            )
            if label == "6_legit_green":
                legit_id = pid

        # Verify live tree is untouched.
        live_dummy = REPO_ROOT / "tools" / "data_feeds" / "wall_test_dummy.py"
        assert not live_dummy.exists(), \
            f"LIVE TREE INTEGRITY VIOLATION: {live_dummy} exists — apply must not have fired"
        print(f"\n== live tree integrity ==")
        print(f"  {live_dummy}: absent ✓ (nothing applied)")

        # Exercise the CLI reject path for #6.
        if legit_id:
            print(f"\n== morgoth reject on proposal #6 ({legit_id[:8]}) ==")
            from self_modify import cli as smcli
            from unittest.mock import MagicMock
            args = MagicMock()
            args.proposal_id = legit_id
            args.reason = "wall_test reject"
            rc = await smcli._cmd_reject(store, args)
            after = await store.get(legit_id)
            print(f"  rc={rc}  status={after['status']}")
            assert after["status"] == P.STATUS_REJECTED
    finally:
        # Cleanup: mark every wall_test proposal as rejected with a marker
        # so they can be filtered out of ordinary queries. We intentionally
        # keep the rows around for post-mortem (soft delete via status).
        for r in results:
            try:
                await store.update_status(
                    r["id"],
                    P.STATUS_REJECTED,
                    "wall_test cleanup",
                )
            except Exception as exc:  # pragma: no cover
                print(f"cleanup failed for {r['id']}: {exc}")
        await pm.close()

    # Summary
    all_ok = all(r["ok"] for r in results)
    print("\n== SUMMARY ==")
    for r in results:
        mark = "OK" if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['label']:32}  expected={r['expected']:24} actual={r['actual']}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
