"""CLI: descriptive-thesis backtest (non-directional theses).

READ-ONLY. Runs alongside scripts/backtest_theses.py — that one scores
the directional price-subset; this one triages the other 248 rows and
scores the ones with a reachable historical ground truth.

Sources fetched:
    mempool.space  /api/v1/mining/hashrate/3m  →  hashrate + difficulty epochs
    alternative.me /fng/?limit=90              →  Fear & Greed history

Usage:
    python -m scripts.backtest_theses_descriptive

──────────────────────────────────────────────────────────────────────────
BASELINE (pre-grounding-rewrite of brain._extract_theses, 2026-08-07):
    Total theses:      327
    Non-directional:   248
    VERIFIABLE-METRIC      :  41  (16.5 %)   ← reachable historical source
    VERIFIABLE-RELATION    :  14  ( 5.6 %)
    UNVERIFIABLE-UNREACH.  : 108  (43.5 %)   ← no free history for metric
    UNVERIFIABLE-SUBJECT.  :  85  (34.3 %)   ← prose, no meter
    Verifiable share       :  22 %  (metric + relation)
    Phantom-metric count   :  18   (ETH hash rate — doesn't exist post-merge)
    Scored on verifiable   :  30/41
    Overall hit-rate       :  36.7 %  (up 71 %, down 8 % → base-rate artifact)
The grounding-rewrite success criterion (measured on FUTURE cycles, not now):
    verifiable share RISES from 22 % and phantom-count DROPS from 18. If
    both stay flat after several cycles under the new prompt, the fix
    didn't take and something else must intervene.
──────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from analysis.thesis_backtest import parse_direction, subject_asset  # noqa: E402
from analysis.thesis_backtest_descriptive import (  # noqa: E402
    aggregate,
    classify_subject,
    score_difficulty,
    score_hashrate,
    score_sentiment,
    triage,
)
from core.config import load_config  # noqa: E402
from memory.persistent import PersistentMemory  # noqa: E402


async def _fetch_theses(config) -> list[dict[str, Any]]:
    pm = PersistentMemory(config)
    await pm.initialize()
    try:
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
                try:
                    d["evidence"] = json.loads(ev)
                except json.JSONDecodeError:
                    d["evidence"] = []
            out.append(d)
        return out
    finally:
        await pm.close()


async def _fetch_hashrate_and_difficulty(client: httpx.AsyncClient):
    resp = await client.get("https://mempool.space/api/v1/mining/hashrate/3m")
    resp.raise_for_status()
    j = resp.json()
    hashrates = j.get("hashrates", [])
    difficulties = j.get("difficulty", [])
    hr_ms = [int(h["timestamp"]) * 1000 for h in hashrates]
    hr_val = [float(h["avgHashrate"]) for h in hashrates]
    return (hr_ms, hr_val), difficulties


async def _fetch_fng(client: httpx.AsyncClient):
    resp = await client.get("https://api.alternative.me/fng/?limit=90")
    resp.raise_for_status()
    data = resp.json().get("data", [])
    # F&G returns newest-first; flip to oldest-first for bisect.
    data = sorted(data, key=lambda d: int(d["timestamp"]))
    ms = [int(d["timestamp"]) * 1000 for d in data]
    val = [float(d["value"]) for d in data]
    return ms, val


def _is_directional(row: dict) -> bool:
    return (
        subject_asset(str(row.get("subject", ""))) is not None
        and parse_direction(str(row.get("claim", ""))) is not None
    )


def _render_table(title: str, rows: list[tuple], min_n_note: int = 10) -> str:
    if not rows:
        return f"{title}\n  (empty)\n"
    lines = [title, f"  {'bucket':<32} {'n':>4} {'hits':>5} {'rate':>7}  note"]
    for key, n, hits, rate in rows:
        note = "" if n >= min_n_note else f"n<{min_n_note} noise"
        lines.append(f"  {str(key)[:32]:<32} {n:>4} {hits:>5} {rate*100:>6.1f}%  {note}")
    return "\n".join(lines) + "\n"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hashrate-horizon", type=int, default=168,
                        help="Horizon in hours to check hashrate direction (default 168 = 7d)")
    parser.add_argument("--hashrate-band", type=float, default=0.02,
                        help="Flat band for hashrate % change (default 0.02 = ±2%%)")
    parser.add_argument("--difficulty-band", type=float, default=0.005,
                        help="Flat band for difficulty adjustment % (default 0.005 = ±0.5%%)")
    args = parser.parse_args()

    config = await load_config()
    theses = await _fetch_theses(config)
    non_directional = [t for t in theses if not _is_directional(t)]
    print(f"Total theses: {len(theses)}  |  Non-directional: {len(non_directional)}")

    counts, metric_rows, unreachable_reasons, subjective_subjects = triage(non_directional)
    print("\n=== PHASE A · TRIAGE ===")
    print(f"  VERIFIABLE-METRIC        : {counts['metric']:>4}  ({counts['metric']/counts['input']*100:.1f}%)")
    print(f"  VERIFIABLE-RELATION      : {counts['relation']:>4}  ({counts['relation']/counts['input']*100:.1f}%)")
    print(f"  UNVERIFIABLE-UNREACHABLE : {counts['unreachable']:>4}  ({counts['unreachable']/counts['input']*100:.1f}%)  ← objective metric, no free history")
    print(f"  UNVERIFIABLE-SUBJECTIVE  : {counts['subjective']:>4}  ({counts['subjective']/counts['input']*100:.1f}%)  ← genuine prose, no meter")
    from collections import Counter
    if unreachable_reasons:
        print("  unreachable breakdown:")
        for reason, n in Counter(unreachable_reasons).most_common():
            print(f"    {n:>3}  {reason}")

    # Fetch reachable sources
    async with httpx.AsyncClient(timeout=30.0) as client:
        (hr_series, diff_epochs) = await _fetch_hashrate_and_difficulty(client)
        fng_series = await _fetch_fng(client)
    now = datetime.now(timezone.utc)
    print(f"\n=== PHASE B · GROUND-TRUTH SOURCES ===")
    print(f"  BTC hashrate     : {len(hr_series[0])} points (mempool 3m)          ")
    print(f"  BTC difficulty   : {len(diff_epochs)} epochs (mempool 3m)             ")
    print(f"  Fear & Greed idx : {len(fng_series[0])} points (alternative.me 90d)")
    print(f"  UNREACHABLE      : dominance, funding, gas, volume, mkt cap, long-short")

    # Score reachable ones
    records = []
    metric_row_map = {(subj, claim): metric for subj, claim, metric in metric_rows}
    for row in non_directional:
        metric = metric_row_map.get((str(row.get("subject", "")), str(row.get("claim", ""))))
        if metric is None:
            continue
        if metric == "btc_hashrate":
            r = score_hashrate(row, hr_series, horizon_hours=args.hashrate_horizon,
                               flat_pct=args.hashrate_band, now=now)
        elif metric == "btc_difficulty":
            r = score_difficulty(row, diff_epochs, flat_pct=args.difficulty_band, now=now)
        elif metric == "market_sentiment":
            r = score_sentiment(row, fng_series, now=now)
        else:
            r = None
        if r is not None:
            records.append(r)

    print(f"\n=== PHASE C · SCORING ({len(records)} scored of {counts['metric']} VERIFIABLE-METRIC candidates) ===")
    if not records:
        print("  (no records scored — parser skipped all)")
        return 0
    agg = aggregate(records)
    print(_render_table("=== DESCRIPTIVE · OVERALL ===", agg["overall"], min_n_note=1))
    print(_render_table("=== DESCRIPTIVE · BY METRIC ===", agg["by_metric"]))
    print(_render_table("=== DESCRIPTIVE · BY CONFIDENCE ===", agg["by_confidence"]))
    print(_render_table("=== DESCRIPTIVE · BY PREDICTED CLASS ===", agg["by_predicted"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
