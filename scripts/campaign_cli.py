"""CLI backing for ``morgoth campaign`` — start / status / end / report.

Same shape as scripts/focus_cli.py. Reuses PersistentMemory methods on
the campaigns table + core.campaign for the report formatter.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402
from core.campaign import format_campaign_report  # noqa: E402


async def _cmd_start(pm: PersistentMemory, args: argparse.Namespace) -> int:
    subj = (args.subject or "").strip()
    if not subj:
        print("morgoth campaign: subject cannot be empty", file=sys.stderr)
        return 2
    cid = await pm.start_campaign(subj, int(args.days))
    print(f"campaign started: {cid}")
    print(f"subject: {subj}")
    print(f"duration: {args.days} day(s)")
    return 0


async def _cmd_status(pm: PersistentMemory, _args: argparse.Namespace) -> int:
    row = await pm.get_active_campaign()
    if not row:
        print("campaign: none active")
        return 0
    print(f"campaign_id: {row['campaign_id']}")
    print(f"subject:     {row['subject']}")
    print(f"started_at:  {row['started_at']}")
    print(f"ends_at:     {row['ends_at']}")
    objs = await pm.list_campaign_objectives(str(row["campaign_id"]))
    print(f"objectives:  {len(objs)}")
    return 0


async def _cmd_end(pm: PersistentMemory, _args: argparse.Namespace) -> int:
    if await pm.end_campaign(status="cancelled"):
        print("campaign ended")
    else:
        print("campaign: none active")
    return 0


async def _cmd_report(pm: PersistentMemory, args: argparse.Namespace) -> int:
    row = None
    if args.campaign_id:
        rows = await pm.list_campaigns(limit=50)
        for r in rows:
            if str(r["campaign_id"]).startswith(str(args.campaign_id)):
                row = r; break
    else:
        row = await pm.get_active_campaign()
        if not row:
            rows = await pm.list_campaigns(limit=1)
            row = rows[0] if rows else None
    if not row:
        print("no campaign to report on")
        return 1
    objs = await pm.list_campaign_objectives(str(row["campaign_id"]))
    theses: list[dict] = []
    for o in objs:
        theses.extend(await pm.get_theses_by_objective(o["objective_id"]))
    print(format_campaign_report(row, objs, theses, []))
    return 0


async def _main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="campaign", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("start", help="start a new campaign (auto-closes any active)")
    ps.add_argument("subject"); ps.add_argument("--days", type=int, default=7)
    ps.set_defaults(_fn=_cmd_start)
    sub.add_parser("status", help="show the active campaign").set_defaults(_fn=_cmd_status)
    sub.add_parser("end", help="close the active campaign as cancelled").set_defaults(_fn=_cmd_end)
    pr = sub.add_parser("report", help="print the campaign report")
    pr.add_argument("campaign_id", nargs="?")
    pr.set_defaults(_fn=_cmd_report)
    args = p.parse_args(argv)
    config = await load_config()
    pm = PersistentMemory(config); await pm.initialize()
    try:
        return await args._fn(pm, args)
    finally:
        await pm.close()


def main() -> None:
    sys.exit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
