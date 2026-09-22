"""Audit: does each cited number come from the RIGHT field of its
source tool? Read-only. Historical replay against each thesis's OWN
objective evidence (the stored TOOL RESULTS payloads) — the correct
reference, not today's cached snapshots.
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
    parse_findings_payloads, classify_thesis_evidence,
)


async def main() -> int:
    cfg = await load_config()
    pm = PersistentMemory(cfg); await pm.initialize()
    pool = pm._require_pool()
    # Load thesis rows joined with their objective's evidence array.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT t.thesis_id::text AS tid, t.subject, t.evidence::text AS thev, "
            "       o.evidence::text AS objev "
            "FROM theses t "
            "LEFT JOIN objectives o ON o.objective_id::text = t.objective_id::text "
            "WHERE t.status <> 'quarantined'"
        )
    per_tool: dict[str, Counter] = {}
    confusion_examples: list[tuple] = []
    truncated_total = 0
    persisted = 0
    for r in rows:
        try:
            thev = json.loads(r["thev"]) if r["thev"] else []
        except Exception:
            thev = []
        # Extract findings text from the objective's evidence blobs.
        findings: list[str] = []
        try:
            for ev in json.loads(r["objev"]) if r["objev"] else []:
                if isinstance(ev, dict):
                    txt = ev.get("summary") or ev.get("content") or ""
                    if txt:
                        findings.append(str(txt))
        except Exception:
            findings = []
        payloads, trunc = parse_findings_payloads(findings)
        truncated_total += trunc
        for rec in classify_thesis_evidence(thev, payloads):
            per_tool.setdefault(rec["source"], Counter())[rec["verdict"]] += 1
            if rec["verdict"] == "field_confusion":
                if len(confusion_examples) < 5:
                    confusion_examples.append(
                        (r["tid"][:8], r["subject"], rec["cited_value"],
                          rec["expected_field"], rec["matched_field"],
                          rec["detail_snippet"])
                    )
                await pm.record_field_confusion_event(
                    r["tid"], rec["source"], rec["cited_value"],
                    rec["expected_field"], rec["matched_field"],
                    rec["detail_snippet"],
                )
                persisted += 1
    print("=== FIELD-CONFUSION AUDIT (historical replay vs objective evidence) ===")
    header = f"  {'tool':32}  {'true_pass':>9}  {'confusion':>9}  {'no_phrase':>9}  {'v!ref':>7}"
    print(header)
    for source in sorted(per_tool):
        c = per_tool[source]
        print(f"  {source:32}  {c['true_pass']:>9}  "
              f"{c['field_confusion']:>9}  {c['no_phrase_mapping']:>9}  "
              f"{c['value_not_in_reference']:>7}")
    print(f"\ntruncated-JSON tool-result lines skipped: {truncated_total}")
    print(f"field_confusion events persisted: {persisted}\n")
    print("Examples (first 5 confusions):")
    for tid, subj, val, exp, matched, det in confusion_examples:
        print(f"  {tid}  {subj[:36]:36}  cited={val:g}")
        print(f"      expected={exp!r}  matched={matched!r}")
        print(f"      detail: {det}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
