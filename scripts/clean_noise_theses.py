"""One-off: mark noise theses as stale.

Finds active theses whose claim contains a non-directional hedge word
(substring stoplist matching the live extraction filter) OR whose evidence
array is empty OR whose subject is a long fragment sentence (>10 words —
conservative bound to avoid dropping legitimate multi-word subjects).

Default: DRY RUN. Prints what would be marked. Use --apply to actually
update status from 'active' to 'stale'. Staleness is REVERSIBLE (a follow-up
UPDATE can restore it); no rows are hard-deleted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


# Mirrors core/brain.py:_parse_thesis_json
NON_DIRECTIONAL_STOPLIST = {
    "unclear",
    "mixed",
    "unknown",
    "complex",
    "uncertain",
    "unrelated",
    "n/a",
}
# Plus a few phrases the prompt now explicitly rejects (longer hedges that
# do NOT contain a stoplist word as substring).
HEDGE_PHRASES = {
    "no correlation",
    "inaccurate",
    "possible indirect relationship",
    "potential minor adjustment",
}
# Conservative bound for fragment-subject detection. Legitimate observed
# subjects max at ~6 words ("24-hour change rate of BTC price"). Anything
# >10 is almost certainly a sentence/headline.
FRAGMENT_SUBJECT_WORD_THRESHOLD = 10


def _decode_evidence(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _classify(row: dict[str, Any]) -> str | None:
    """Return the noise reason for a row, or None if the row is clean."""
    claim = (row.get("claim") or "").strip().lower()
    if any(word in claim for word in NON_DIRECTIONAL_STOPLIST):
        return f"non-directional claim (stoplist substring): {claim!r}"
    if claim in HEDGE_PHRASES:
        return f"non-directional hedge phrase: {claim!r}"
    evidence = _decode_evidence(row.get("evidence"))
    if not evidence:
        return "empty evidence array"
    subject = (row.get("subject") or "").strip()
    if len(subject.split()) > FRAGMENT_SUBJECT_WORD_THRESHOLD:
        return f"fragment subject (>{FRAGMENT_SUBJECT_WORD_THRESHOLD} words): {subject!r}"
    return None


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually UPDATE noisy theses from status='active' to 'stale'. "
        "Without this flag, the script only prints what it would do.",
    )
    args = parser.parse_args()

    config = await load_config()
    pm = PersistentMemory(config)
    await pm.initialize()
    try:
        rows = await pm.get_theses(limit=10000)
        targets: list[tuple[str, dict[str, Any], str]] = []
        for row in rows:
            if row.get("status") != "active":
                continue
            reason = _classify(row)
            if reason is None:
                continue
            targets.append((str(row["thesis_id"]), row, reason))

        mode = "APPLY" if args.apply else "DRY RUN"
        print(f"== Noise theses sweep [{mode}] ==")
        print(f"active theses scanned: {sum(1 for r in rows if r.get('status') == 'active')}")
        print(f"noise candidates: {len(targets)}\n")
        for thesis_id, row, reason in targets:
            subject = row.get("subject") or ""
            claim = row.get("claim") or ""
            print(f"  - {thesis_id[:8]}  subject={subject!r}")
            print(f"    claim={claim!r}  status=active -> stale")
            print(f"    reason: {reason}")
            print()

        if args.apply and targets:
            pool = pm._require_pool()  # noqa: SLF001 — one-off script
            import uuid as _uuid

            async with pool.acquire() as conn:
                for thesis_id, _row, _reason in targets:
                    await conn.execute(
                        "UPDATE theses SET status='stale' WHERE thesis_id=$1",
                        _uuid.UUID(thesis_id),
                    )
            print(f"== applied: marked {len(targets)} theses as stale ==")
        elif args.apply:
            print("== applied: nothing to mark ==")
        else:
            print("== dry run: no changes written. Re-run with --apply to commit. ==")
    finally:
        await pm.close()


if __name__ == "__main__":
    asyncio.run(main())
