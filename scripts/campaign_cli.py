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
from analysis.campaign_quality import (  # noqa: E402
    render_report as _quality_render, score_campaign as _score_campaign,
)


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
    # Prior canonical subjects — anything the store already carried
    # before this campaign started. Used for the novelty section.
    prior: set[str] = set()
    started = row.get("started_at")
    if started is not None:
        try:
            pool = pm._require_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT DISTINCT canonical_subject FROM theses "
                    "WHERE created_at < $1 AND canonical_subject IS NOT NULL",
                    started,
                )
            prior = {r["canonical_subject"] for r in rows if r["canonical_subject"]}
        except Exception:
            prior = set()
    print(format_campaign_report(row, objs, theses, [],
                                    prior_canonical_subjects=prior))
    return 0


async def _cmd_quality(pm: PersistentMemory, args: argparse.Namespace) -> int:
    """Read-only quality scorer — classifies every thesis in a campaign
    into 6 error classes so two campaigns are comparable on the same
    scale. No writes.
    """
    fetch = None
    if not args.no_binance:
        import httpx

        async def _fetch():
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(
                    "https://fapi.binance.com/fapi/v1/fundingRate",
                    params={"symbol": "BTCUSDT", "limit": 1000},
                )
                r.raise_for_status()
                data = r.json()
                ms = [int(d["fundingTime"]) for d in data]
                vs = [float(d["fundingRate"]) for d in data]
                paired = sorted(zip(ms, vs))
                return [m for m, _ in paired], [v for _, v in paired]

        fetch = _fetch

    report = await _score_campaign(
        pm, args.campaign_id,
        fetch_binance_funding=fetch,
        limit_first_n=args.first_n,
    )
    print(_quality_render(report))
    # Persist unservable-angle phrases per campaign so reflect can
    # render them as evidence. Non-fatal on failure — the print above
    # is the primary output.
    if not args.no_persist and report.top_missing_themes:
        try:
            await pm.record_campaign_data_gaps(
                report.campaign_id, report.top_missing_themes,
            )
        except Exception as exc:
            print(f"warn: data-gaps persist failed: {exc}", file=sys.stderr)
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
    pq = sub.add_parser("quality", help="score a campaign on 6 error classes (read-only)")
    pq.add_argument("campaign_id")
    pq.add_argument("--no-binance", action="store_true",
                     help="skip Binance funding cross-check (B stays 'unknown')")
    pq.add_argument("--no-persist", action="store_true",
                     help="do NOT upsert unservable-angle phrases into "
                          "campaign_data_gaps (reflect reads it as evidence)")
    pq.add_argument("--first-n", type=int, default=None,
                     help="length-control: score only the FIRST N objectives "
                          "by created_at (fair comparison across campaigns of "
                          "different lengths)")
    pq.set_defaults(_fn=_cmd_quality)
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
