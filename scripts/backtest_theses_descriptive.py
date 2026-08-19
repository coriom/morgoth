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
    score_funding,
    score_gas,
    score_hashrate,
    score_market_cap,
    score_sentiment,
    score_volume,
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


async def _safe_fetch(name: str, coro):
    """Wrap a fetcher so a source outage → (None, None, ...) instead of a
    crash — matches Phase B's "graceful degrade → SKIP, never fabricate"."""
    try:
        return await coro
    except Exception as exc:  # httpx network, timeout, 4xx, 5xx, JSON parse
        print(f"  WARN: {name} fetch failed ({type(exc).__name__}: {exc}); those rows will SKIP.")
        return None


async def _fetch_coingecko_all_series(client: httpx.AsyncClient, asset: str, api_key: str | None):
    """Fetch prices, market_caps, total_volumes for asset (90d). Returns
    dict with three (sorted_ms, values) tuples or None on failure."""
    headers = {"x-cg-demo-api-key": api_key} if api_key else {}
    resp = await client.get(
        f"https://api.coingecko.com/api/v3/coins/{asset}/market_chart",
        params={"vs_currency": "usd", "days": 90},
        headers=headers,
    )
    resp.raise_for_status()
    j = resp.json()
    def _series(key):
        arr = j.get(key, [])
        return [int(p[0]) for p in arr], [float(p[1]) for p in arr]
    return {"prices": _series("prices"), "market_caps": _series("market_caps"),
            "total_volumes": _series("total_volumes")}


async def _fetch_owlracle_gas(client: httpx.AsyncClient):
    """Owlracle 90d daily gas history (keyless). Returns (sorted_ms, gwei_avg)."""
    resp = await client.get(
        "https://api.owlracle.info/v4/eth/history",
        params={"candles": 90, "timeframe": 1440},
    )
    resp.raise_for_status()
    candles = resp.json().get("candles", [])
    # Owlracle candles (probed 2026-08-19): {timestamp: ISO, gasPrice:
    # {open, close, low, high}, avgGas, samples}. No "avg" gas price key —
    # use close as end-of-day representative value in gwei.
    ms, vals = [], []
    for c in candles:
        ts_raw = c.get("timestamp")
        gp = c.get("gasPrice") or {}
        avg = gp.get("close")  # end-of-day gas price
        if ts_raw is None or avg is None:
            continue
        # timestamp may be int (unix seconds) or ISO string
        if isinstance(ts_raw, (int, float)):
            ms.append(int(ts_raw) * 1000)
        else:
            ms.append(int(datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).timestamp() * 1000))
        vals.append(float(avg))
    # Owlracle returns newest-first typically; ensure sorted for bisect.
    paired = sorted(zip(ms, vals))
    if not paired:
        return [], []
    return [m for m, _ in paired], [v for _, v in paired]


async def _fetch_binance_funding(client: httpx.AsyncClient):
    """Binance /fapi/v1/fundingRate BTCUSDT — keyless, 8h cadence.
    Returns (sorted_ms, funding_rate_fraction). Limit 1000 → ~333 days."""
    resp = await client.get(
        "https://fapi.binance.com/fapi/v1/fundingRate",
        params={"symbol": "BTCUSDT", "limit": 1000},
    )
    resp.raise_for_status()
    data = resp.json()
    ms = [int(d["fundingTime"]) for d in data]
    vals = [float(d["fundingRate"]) for d in data]
    paired = sorted(zip(ms, vals))
    if not paired:
        return [], []
    return [m for m, _ in paired], [v for _, v in paired]


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
    parser.add_argument("--since", default=None,
                        help="ISO datetime cutoff — score only theses with created_at >= this")
    parser.add_argument("--until", default=None,
                        help="ISO datetime upper bound — score only theses with created_at < this")
    args = parser.parse_args()

    config = await load_config()
    theses = await _fetch_theses(config)
    if args.since or args.until:
        # Support the pre/post-grounding split without touching the scorer's
        # core logic. Cutoffs match the format git prints (2026-08-07T19:26:44+00:00).
        from datetime import datetime as _dt
        since = _dt.fromisoformat(args.since) if args.since else None
        until = _dt.fromisoformat(args.until) if args.until else None
        before = len(theses)
        theses = [
            t for t in theses
            if (since is None or t["created_at"] >= since)
            and (until is None or t["created_at"] < until)
        ]
        print(f"Filter: since={args.since} until={args.until} → {before} → {len(theses)} theses")
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

    # Fetch reachable sources. _safe_fetch wraps each so a source outage
    # → those rows go SKIP, other metrics still score. No fabrication.
    async with httpx.AsyncClient(timeout=30.0) as client:
        hd = await _safe_fetch("mempool hashrate/difficulty",
                               _fetch_hashrate_and_difficulty(client))
        hr_series, diff_epochs = hd if hd else (None, [])
        fng_series = await _safe_fetch("alternative.me F&G", _fetch_fng(client))
        btc_cg = await _safe_fetch("CoinGecko BTC market_chart",
                                   _fetch_coingecko_all_series(client, "bitcoin", config.coingecko_api_key))
        eth_cg = await _safe_fetch("CoinGecko ETH market_chart",
                                   _fetch_coingecko_all_series(client, "ethereum", config.coingecko_api_key))
        gas_series = await _safe_fetch("Owlracle gas history", _fetch_owlracle_gas(client))
        funding_series = await _safe_fetch("Binance BTC funding", _fetch_binance_funding(client))
    now = datetime.now(timezone.utc)
    print(f"\n=== PHASE B · GROUND-TRUTH SOURCES ===")
    print(f"  BTC hashrate     : {len(hr_series[0]) if hr_series else 0} points (mempool 3m)")
    print(f"  BTC difficulty   : {len(diff_epochs)} epochs (mempool 3m)")
    print(f"  Fear & Greed idx : {len(fng_series[0]) if fng_series else 0} points (alternative.me 90d)")
    print(f"  BTC market_chart : {len(btc_cg['prices'][0]) if btc_cg else 0} points (CoinGecko 90d)")
    print(f"  ETH market_chart : {len(eth_cg['prices'][0]) if eth_cg else 0} points (CoinGecko 90d)")
    print(f"  ETH gas          : {len(gas_series[0]) if gas_series else 0} points (Owlracle 90d)")
    print(f"  BTC funding rate : {len(funding_series[0]) if funding_series else 0} points (Binance /fapi 8h)")
    print(f"  STILL UNREACHABLE: dominance, global-mkt-cap (paid), long-short")

    # Route each metric_row to the right scorer. All new metrics use a
    # 7-day horizon and ±1 % flat band by default — matches the reasonable
    # 24h→7d timescale for describing "the state of" a rolling metric.
    NEW_HORIZON = 168
    MKTCAP_BAND = 0.01
    VOLUME_BAND = 0.15   # daily crypto volumes are noisy — wider band
    GAS_BAND = 0.10      # gwei fluctuates ±10 % daily easily
    FUNDING_BAND = 0.0005  # 5 bp change over 8h is meaningful

    records = []
    metric_row_map = {(subj, claim): metric for subj, claim, metric in metric_rows}
    for row in non_directional:
        metric = metric_row_map.get((str(row.get("subject", "")), str(row.get("claim", ""))))
        if metric is None:
            continue
        if metric == "btc_hashrate":
            r = score_hashrate(row, hr_series, horizon_hours=args.hashrate_horizon,
                               flat_pct=args.hashrate_band, now=now) if hr_series else None
        elif metric == "btc_difficulty":
            r = score_difficulty(row, diff_epochs, flat_pct=args.difficulty_band, now=now) if diff_epochs else None
        elif metric == "market_sentiment":
            r = score_sentiment(row, fng_series, now=now) if fng_series else None
        elif metric == "btc_market_cap":
            r = score_market_cap(row, btc_cg["market_caps"] if btc_cg else None,
                                 NEW_HORIZON, MKTCAP_BAND, now, "bitcoin")
        elif metric == "eth_market_cap":
            r = score_market_cap(row, eth_cg["market_caps"] if eth_cg else None,
                                 NEW_HORIZON, MKTCAP_BAND, now, "ethereum")
        elif metric == "btc_volume":
            r = score_volume(row, btc_cg["total_volumes"] if btc_cg else None,
                             NEW_HORIZON, VOLUME_BAND, now, "bitcoin")
        elif metric == "eth_volume":
            r = score_volume(row, eth_cg["total_volumes"] if eth_cg else None,
                             NEW_HORIZON, VOLUME_BAND, now, "ethereum")
        elif metric == "eth_gas":
            r = score_gas(row, gas_series, NEW_HORIZON, GAS_BAND, now)
        elif metric == "btc_funding":
            r = score_funding(row, funding_series, NEW_HORIZON, FUNDING_BAND, now)
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
