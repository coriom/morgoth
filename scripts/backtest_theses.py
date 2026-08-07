"""CLI: run the thesis calibration backtest against live DB + CoinGecko.

READ-ONLY. Fetches theses from Postgres and BTC/ETH price history from
CoinGecko, scores every resolvable directional thesis, and prints hit-
rate tables. Nothing is written back — the theses table is untouched.

Usage:
    python -m scripts.backtest_theses                        # sweep 24h/3d/7d
    python -m scripts.backtest_theses --horizons 24          # single horizon
    python -m scripts.backtest_theses --horizons 24,72,168 --base-band 0.01

Flat-band scaling: the band widens with sqrt(horizon/24h) — random-walk
variance grows linearly with time so std grows as sqrt(t). A fixed ±1%
band at 7d would flag ~everything as directional; sqrt-scaling keeps
"flat" a meaningful bucket across horizons.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analysis.thesis_backtest import (  # noqa: E402
    aggregate,
    resolve_theses,
)
from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/{asset}/market_chart"


async def _fetch_series(client: httpx.AsyncClient, asset: str, days: int, api_key: str | None):
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    resp = await client.get(
        COINGECKO_URL.format(asset=asset),
        params={"vs_currency": "usd", "days": days},
        headers=headers,
    )
    resp.raise_for_status()
    prices = resp.json().get("prices", [])
    sorted_ms = [int(p[0]) for p in prices]
    values = [float(p[1]) for p in prices]
    return sorted_ms, values


async def _fetch_theses(config) -> list[dict[str, Any]]:
    pm = PersistentMemory(config)
    await pm.initialize()
    try:
        # Pull ALL statuses so a thesis marked stale after being generated
        # still counts as a prediction that either hit or missed.
        pool = pm._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT thesis_id, subject, claim, confidence, evidence, status, "
                "objective_id, created_at FROM theses ORDER BY created_at ASC"
            )
        out = []
        for r in rows:
            d = dict(r)
            ev = d.get("evidence")
            if isinstance(ev, str):
                import json
                try:
                    d["evidence"] = json.loads(ev)
                except json.JSONDecodeError:
                    d["evidence"] = []
            out.append(d)
        return out
    finally:
        await pm.close()


def _render_table(title: str, rows: list[tuple], min_n_note: int = 10) -> str:
    if not rows:
        return f"{title}\n  (empty)\n"
    lines = [title, f"  {'bucket':<38} {'n':>4} {'hits':>5} {'rate':>7}  note"]
    for key, n, hits, rate in rows:
        note = "" if n >= min_n_note else f"n<{min_n_note} noise"
        lines.append(f"  {str(key)[:38]:<38} {n:>4} {hits:>5} {rate*100:>6.1f}%  {note}")
    return "\n".join(lines) + "\n"


def _band_for(base_band: float, horizon_h: int) -> float:
    """Scale the flat band with sqrt(horizon/24h)."""
    from math import sqrt
    return base_band * sqrt(horizon_h / 24.0)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--horizons", type=str, default="24,72,168",
        help="Comma-separated horizons in hours (default: 24,72,168 = 24h,3d,7d)",
    )
    parser.add_argument(
        "--base-band", type=float, default=0.01,
        help="Base ±band at 24h (default: 0.01 = ±1%%); scales as sqrt(h/24)",
    )
    parser.add_argument(
        "--days", type=int, default=90,
        help="CoinGecko history window in days (default: 90)",
    )
    args = parser.parse_args()

    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
    if not horizons:
        print("No horizons specified.")
        return 1

    config = await load_config()
    theses = await _fetch_theses(config)
    print(f"Loaded {len(theses)} theses from DB.")
    if not theses:
        print("No theses to score.")
        return 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        btc = await _fetch_series(client, "bitcoin", args.days, config.coingecko_api_key)
        eth = await _fetch_series(client, "ethereum", args.days, config.coingecko_api_key)

    price_series = {"bitcoin": btc, "ethereum": eth}
    covered_from = datetime.fromtimestamp(btc[0][0] / 1000, tz=timezone.utc) if btc[0] else None
    covered_to = datetime.fromtimestamp(btc[0][-1] / 1000, tz=timezone.utc) if btc[0] else None
    print(
        f"Price window: {covered_from} → {covered_to} "
        f"({len(btc[0])} BTC samples, {len(eth[0])} ETH samples)"
    )

    # Store aggregates per horizon so we can print a horizon × confidence matrix.
    per_horizon_records: dict[int, list] = {}
    per_horizon_counts: dict[int, dict] = {}
    per_horizon_agg: dict[int, dict] = {}
    for h in horizons:
        band = _band_for(args.base_band, h)
        records, counts = resolve_theses(
            theses, price_series, horizon_hours=h, flat_band=band,
        )
        per_horizon_records[h] = records
        per_horizon_counts[h] = counts
        per_horizon_agg[h] = aggregate(records) if records else {"overall": [], "by_confidence": [], "by_subject": [], "by_source": [], "by_predicted": []}

    # Per-horizon funnels + overall row
    print("\n=== FUNNEL PER HORIZON ===")
    print(f"  {'horizon':>7}  {'band':>7}  {'input':>5}  {'non_price':>9}  {'no_dir':>6}  {'h_open':>6}  {'no_px':>5}  {'scored':>6}")
    for h in horizons:
        c = per_horizon_counts[h]
        band = _band_for(args.base_band, h)
        print(
            f"  {h}h".rjust(9)
            + f"  ±{band*100:>5.2f}%  {c['input']:>5}  {c['non_price']:>9}  "
            f"{c['no_direction']:>6}  {c['horizon_open']:>6}  {c['no_price']:>5}  {c['scored']:>6}"
        )

    # Horizon × overall/confidence matrix
    print("\n=== HORIZON × CONFIDENCE (n / hits / rate) ===")
    conf_buckets = ["overall", "high", "medium", "low"]
    header = f"  {'horizon':>7}  " + "  ".join(f"{b:>18}" for b in conf_buckets)
    print(header)
    for h in horizons:
        agg = per_horizon_agg[h]
        # overall
        parts = [f"  {str(h)+'h':>7}  "]
        overall = agg["overall"][0] if agg["overall"] else ("all", 0, 0, 0.0)
        parts.append(f"{overall[1]:>3}/{overall[2]:<3} {overall[3]*100:>5.1f}%".rjust(18))
        conf_map = {k: (n, hits, rate) for k, n, hits, rate in agg["by_confidence"]}
        for b in ("high", "medium", "low"):
            n, hits, rate = conf_map.get(b, (0, 0, 0.0))
            note = " *" if 0 < n < 10 else "  "
            parts.append(f"{n:>3}/{hits:<3} {rate*100:>5.1f}%{note}".rjust(18))
        print("  ".join(parts))
    print("  (* = n<10, noise-level)")

    # Full breakdown at 24h (the operator's primary window)
    if 24 in per_horizon_agg and per_horizon_records[24]:
        agg = per_horizon_agg[24]
        print()
        print(_render_table("=== 24h · BY SUBJECT ===", agg["by_subject"]))
        print(_render_table("=== 24h · BY SOURCE ===", agg["by_source"]))
        print(_render_table("=== 24h · BY PREDICTED DIRECTION ===", agg["by_predicted"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
