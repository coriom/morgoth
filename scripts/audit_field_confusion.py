"""Audit: does each cited number come from the RIGHT field of its
source tool? Read-only. Persists a row per detected confusion into
field_confusion_events for the operator to review.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402
from core.field_confusion import (  # noqa: E402
    FIELD_PHRASES, phrase_to_field, iter_numbers_with_context,
    classify_number,
)


async def _latest_snapshots(pm: PersistentMemory) -> dict[str, dict]:
    """Return {source: latest_payload_dict} for cached sources.
    Fallback: empty dict — classification returns 'no_mapping'."""
    out: dict[str, dict] = {}
    for source in FIELD_PHRASES:
        try:
            row = await pm.latest_source_snapshot(source)
        except Exception:
            row = None
        if not row:
            continue
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        out[source] = payload if isinstance(payload, dict) else {}
    return out


async def main() -> int:
    cfg = await load_config()
    pm = PersistentMemory(cfg); await pm.initialize()
    snapshots = await _latest_snapshots(pm)
    pool = pm._require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT thesis_id::text AS tid, subject, claim, evidence::text AS ev "
            "FROM theses WHERE status <> 'quarantined'"
        )
    per_tool: dict[str, Counter] = {}
    confusion_examples: list[tuple] = []
    persisted = 0
    for r in rows:
        try:
            evs = json.loads(r["ev"]) if r["ev"] else []
        except Exception:
            evs = []
        for e in evs:
            if not isinstance(e, dict):
                continue
            source = str(e.get("source") or "")
            detail = str(e.get("detail") or "")
            if source not in snapshots or not detail:
                continue
            for value, prefix in iter_numbers_with_context(detail):
                expected = phrase_to_field(source, prefix)
                verdict, matched = classify_number(value, expected, snapshots[source])
                per_tool.setdefault(source, Counter())[verdict] += 1
                if verdict == "field_confusion" and len(confusion_examples) < 5:
                    confusion_examples.append(
                        (r["tid"][:8], r["subject"], value, expected, matched, detail[:80])
                    )
                if verdict == "field_confusion":
                    await pm.record_field_confusion_event(
                        r["tid"], source, value, expected, matched, detail,
                    )
                    persisted += 1
    print("=== FIELD-CONFUSION AUDIT ===")
    for source in sorted(per_tool):
        c = per_tool[source]
        print(f"  {source:32}  true_pass={c['true_pass']:>4}  "
              f"field_confusion={c['field_confusion']:>3}  no_mapping={c['no_mapping']:>4}")
    print(f"\nfield_confusion events persisted: {persisted}")
    print()
    print("Examples (first 5 confusions):")
    for tid, subj, val, exp, matched, det in confusion_examples:
        print(f"  {tid}  {subj[:36]:36}  cited={val:g}")
        print(f"      expected={exp!r}  matched={matched!r}")
        print(f"      detail: {det}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
