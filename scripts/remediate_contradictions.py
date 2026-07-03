"""One-shot: reclassify the existing contradiction pairs under the new rules.

Dry-run default. ``--apply`` commits the changes. Rows are NEVER deleted;
``contradictions.resolution`` records why a pair was voided, and each
thesis's status change is recorded (superseded / re-activated).

Classification per pair (matches ``core.brain.detect_contradictions``):

  - **timeframe_guard** — one subject short-term, the other long-term.
    ``contradictions.resolution = 'voided_timeframe_guard'`` and each side
    is a restoration candidate.
  - **reclassified_supersession** — same timeframe, gap ≥
    ``CONTRADICTION_WINDOW_HOURS`` (default 6h). Older thesis → superseded
    with ``superseded_by`` = newer id. Newer is a restoration candidate.
    ``contradictions.resolution = 'reclassified_supersession'``.
  - **kept** — same timeframe, gap < window. Both stay contradicted;
    resolution stays NULL.

Restoration rule (applies to candidates from the voided/superseded paths):
a thesis returns to 'active' iff (a) the DB row is currently
'contradicted', (b) that thesis does NOT appear in any remaining KEPT
(<window) pair as a still-contradicted party.

Only prints; DB writes gated on ``--apply``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.config import load_config  # noqa: E402
from core.contradictions import (  # noqa: E402
    CONTRADICTION_WINDOW_HOURS,
    subjects_timeframe_conflict,
)
from memory.persistent import PersistentMemory  # noqa: E402


def _classify(pair: dict[str, Any], window_seconds: float) -> str:
    if subjects_timeframe_conflict(
        pair.get("subject_a") or "",
        pair.get("subject_b") or "",
    ):
        return "voided_timeframe_guard"
    ca, cb = pair.get("created_at_a"), pair.get("created_at_b")
    if isinstance(ca, datetime) and isinstance(cb, datetime):
        gap = abs((ca - cb).total_seconds())
        if gap >= window_seconds:
            return "reclassified_supersession"
    return "kept"


def _older_id(pair: dict[str, Any]) -> str:
    ca, cb = pair.get("created_at_a"), pair.get("created_at_b")
    if isinstance(ca, datetime) and isinstance(cb, datetime):
        if ca <= cb:
            return str(pair["thesis_id_a"])
        return str(pair["thesis_id_b"])
    return str(pair["thesis_id_a"])


def _newer_id(pair: dict[str, Any]) -> str:
    ca, cb = pair.get("created_at_a"), pair.get("created_at_b")
    if isinstance(ca, datetime) and isinstance(cb, datetime):
        if ca <= cb:
            return str(pair["thesis_id_b"])
        return str(pair["thesis_id_a"])
    return str(pair["thesis_id_b"])


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write to the DB. Without this, only prints.",
    )
    args = parser.parse_args()

    window_seconds = CONTRADICTION_WINDOW_HOURS * 3600.0
    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    try:
        # Only touch unresolved pairs (idempotent — a re-run does nothing).
        pairs = await pm.get_contradictions(limit=10000, unresolved_only=True)
        pair_actions: list[tuple[dict[str, Any], str]] = []
        for p in pairs:
            action = _classify(p, window_seconds)
            pair_actions.append((p, action))

        # Determine restoration candidates:
        # - voided_timeframe_guard: both theses
        # - reclassified_supersession: newer only (older becomes superseded)
        # - kept: neither (both stay contradicted)
        restoration_candidates: set[str] = set()
        superseded_older: dict[str, str] = {}  # older_id → newer_id
        kept_theses: set[str] = set()          # theses in any KEPT pair
        for p, action in pair_actions:
            id_a = str(p["thesis_id_a"])
            id_b = str(p["thesis_id_b"])
            if action == "voided_timeframe_guard":
                restoration_candidates.add(id_a)
                restoration_candidates.add(id_b)
            elif action == "reclassified_supersession":
                older = _older_id(p)
                newer = _newer_id(p)
                superseded_older[older] = newer
                restoration_candidates.add(newer)
            else:  # kept
                kept_theses.add(id_a)
                kept_theses.add(id_b)

        # A candidate becomes 'active' iff it's not still party to a KEPT pair.
        will_restore = {
            tid for tid in restoration_candidates if tid not in kept_theses
        }

        # Load current thesis statuses so we only touch those actually
        # 'contradicted' now (idempotent guard for --apply).
        rows = await pm.get_theses(limit=10000)
        by_id = {str(r["thesis_id"]): r for r in rows}

        # ---- REPORT --------------------------------------------------------
        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"== Contradiction remediation [{mode}] ==")
        print(f"window: {CONTRADICTION_WINDOW_HOURS}h")
        print(f"unresolved pairs found: {len(pair_actions)}\n")
        print(f"{'pair (id_a·id_b)':22}  {'gap_h':>6}  action")
        print("-" * 78)
        for p, action in pair_actions:
            ca, cb = p.get("created_at_a"), p.get("created_at_b")
            gap_h = 0.0
            if isinstance(ca, datetime) and isinstance(cb, datetime):
                gap_h = abs((ca - cb).total_seconds()) / 3600.0
            a_short = str(p["thesis_id_a"])[:8]
            b_short = str(p["thesis_id_b"])[:8]
            print(f"{a_short}·{b_short}      {gap_h:>6.1f}  {action}")
        print()
        print(f"pairs classified:")
        counts: dict[str, int] = {}
        for _, action in pair_actions:
            counts[action] = counts.get(action, 0) + 1
        for action, n in sorted(counts.items()):
            print(f"  {action:32}  {n}")
        print()

        thesis_transitions: list[tuple[str, str, str, str]] = []  # (id, subject, old, new)
        for tid, newer in superseded_older.items():
            row = by_id.get(tid)
            if not row:
                continue
            thesis_transitions.append(
                (tid, str(row.get("subject") or "")[:60], str(row.get("status")), f"superseded (by {newer[:8]})")
            )
        for tid in sorted(will_restore):
            row = by_id.get(tid)
            if not row:
                continue
            if row.get("status") == "contradicted":
                thesis_transitions.append(
                    (tid, str(row.get("subject") or "")[:60], "contradicted", "active")
                )

        print(f"thesis status transitions: {len(thesis_transitions)}")
        print(f"{'thesis_id':10}  {'subject':60}  transition")
        print("-" * 120)
        for tid, subject, old, new in thesis_transitions:
            print(f"{tid[:8]}  {subject:60}  {old} → {new}")
        print()

        if not args.apply:
            print("== dry run: nothing written. Re-run with --apply to commit. ==")
            return

        # ---- APPLY ---------------------------------------------------------
        for p, action in pair_actions:
            if action == "kept":
                continue
            await pm.set_contradiction_resolution(str(p["contradiction_id"]), action)

        for older_id, newer_id in superseded_older.items():
            await pm.mark_thesis_superseded(older_id, newer_id)

        for tid in will_restore:
            row = by_id.get(tid)
            if not row:
                continue
            if row.get("status") == "contradicted":
                await pm.update_thesis_status(tid, "active")

        print("== applied ==")
    finally:
        await pm.close()


if __name__ == "__main__":
    asyncio.run(main())
