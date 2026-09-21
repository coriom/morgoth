"""Reversibly mark contaminated theses as quarantined.

Three reason codes used in the 2026-09-21 audit:
  fred_oldest_first     — thesis cites a pre-2025 FRED observation as
                           if current (tools/connectors/fred.py used to
                           reverse the desc response, putting May 2018
                           at obs[0]).
  interestrate_as_funding — thesis reports Binance's constant
                           interestRate = 0.00010000 as the funding
                           rate itself (get_bitcoin_futures_funding
                           digest carried the confusion-source field).
  training_derived      — claim quotes a specific past date/period
                           (e.g. hashrate "since early 2023", mining
                           difficulty "plateauing until 2024") that
                           the tool couldn't have produced — pure
                           model-training leakage. Set manually
                           after the automated FRED sweep.

`quarantine` sets status='quarantined' + quarantine_reason=<code>.
`unquarantine` restores status='active' and NULLs the reason.
No row is ever deleted; both operations are fully reversible.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


_YEAR_RE = re.compile(r"\b(201\d|202[0-4])\b")
_MONTHYR_RE = re.compile(
    r"(?i)(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* (\d{4})",
)


def _pre2025_hit(text: str) -> bool:
    if not text:
        return False
    if _YEAR_RE.search(text):
        return True
    m = _MONTHYR_RE.search(text)
    if m:
        try:
            y = int(m.group(1))
            return 2010 <= y <= 2024
        except ValueError:
            pass
    return False


async def _cmd_quarantine(pm: PersistentMemory, _args) -> int:
    pool = pm._require_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE theses ADD COLUMN IF NOT EXISTS quarantine_reason TEXT"
        )
        rows = await conn.fetch(
            "SELECT thesis_id::text AS tid, subject, evidence::text AS ev "
            "FROM theses WHERE status <> 'quarantined'"
        )
        fred_ids, funding_ids = [], []
        for r in rows:
            txt = (r["subject"] or "") + " | " + (r["ev"] or "")
            if _pre2025_hit(txt):
                fred_ids.append(r["tid"])
            if (r["subject"] or "").lower().find("funding") >= 0 \
                    and "0.00010000" in (r["ev"] or ""):
                funding_ids.append(r["tid"])
        import uuid as _u
        for tid in fred_ids:
            await conn.execute(
                "UPDATE theses SET status='quarantined', "
                "quarantine_reason='fred_oldest_first' WHERE thesis_id=$1",
                _u.UUID(tid),
            )
        for tid in set(funding_ids) - set(fred_ids):
            await conn.execute(
                "UPDATE theses SET status='quarantined', "
                "quarantine_reason='interestrate_as_funding' WHERE thesis_id=$1",
                _u.UUID(tid),
            )
        # Auto-void open contradictions that reference at least one
        # quarantined thesis. resolution='voided_quarantine' marks
        # them so `undo` can restore only the ones we voided.
        voided = await conn.execute(
            "UPDATE contradictions SET resolution='voided_quarantine' "
            "WHERE resolution IS NULL AND ("
            "  thesis_id_a IN (SELECT thesis_id FROM theses WHERE status='quarantined') "
            "  OR thesis_id_b IN (SELECT thesis_id FROM theses WHERE status='quarantined'))"
        )
    voided_n = int(str(voided).split()[-1]) if voided else 0
    print(f"quarantined: fred_oldest_first={len(fred_ids)}  "
          f"interestrate_as_funding={len(set(funding_ids) - set(fred_ids))}  "
          f"contradictions voided: {voided_n}")
    return 0


async def _cmd_unquarantine(pm: PersistentMemory, args) -> int:
    pool = pm._require_pool()
    reason = args.reason
    async with pool.acquire() as conn:
        if reason:
            n = await conn.execute(
                "UPDATE theses SET status='active', quarantine_reason=NULL "
                "WHERE status='quarantined' AND quarantine_reason=$1",
                reason,
            )
        else:
            n = await conn.execute(
                "UPDATE theses SET status='active', quarantine_reason=NULL "
                "WHERE status='quarantined'"
            )
        # Reopen ONLY the contradictions we auto-voided at quarantine
        # time. Contradictions the operator resolved manually keep
        # their resolution.
        reopened = await conn.execute(
            "UPDATE contradictions SET resolution=NULL "
            "WHERE resolution='voided_quarantine' AND ("
            "  thesis_id_a IN (SELECT thesis_id FROM theses WHERE status='active') "
            "  AND thesis_id_b IN (SELECT thesis_id FROM theses WHERE status='active'))"
        )
    print(f"unquarantined: {n}   contradictions reopened: {reopened}")
    return 0


async def _cmd_list(pm: PersistentMemory, _args) -> int:
    pool = pm._require_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT quarantine_reason, COUNT(*) AS n FROM theses "
            "WHERE status='quarantined' GROUP BY quarantine_reason "
            "ORDER BY n DESC"
        )
    total = 0
    for r in rows:
        total += r["n"]
        print(f"  {r['quarantine_reason']:30}  {r['n']}")
    print(f"total quarantined: {total}")
    return 0


async def _main(argv):
    p = argparse.ArgumentParser(prog="quarantine_theses", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("apply").set_defaults(_fn=_cmd_quarantine)
    sub.add_parser("list").set_defaults(_fn=_cmd_list)
    un = sub.add_parser("undo")
    un.add_argument("--reason", default=None,
                     help="restrict to one reason code; omit to undo all")
    un.set_defaults(_fn=_cmd_unquarantine)
    args = p.parse_args(argv)
    cfg = await load_config()
    pm = PersistentMemory(cfg); await pm.initialize()
    try:
        return await args._fn(pm, args)
    finally:
        await pm.close()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
