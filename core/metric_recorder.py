"""Metric recorder — snapshot free CURRENT values on a schedule so future
backtests have local ground-truth history.

Motivation: the dominance / global-mkt-cap / global-volume metrics have
no free HISTORICAL endpoint (CoinGecko /global/market_cap_chart is
Pro-only). But CoinPaprika's /v1/global returns those three values
CURRENT for free, and history can be accumulated rather than bought.

FORWARD-ONLY (state it plainly): this recorder does NOT recover past
theses. The ~48 historical unreachable rows in the descriptive backtest
stay unscoreable — they were stamped BEFORE the series began. Theses
stamped AFTER the recorder started running gain a reachable ground-
truth series for the three metrics; that's the ceiling of what we can
promise.

Design:
  · One snapshot per interval (default 15 min → 96 snapshots/day per
    metric, 288 CoinPaprika calls/day total). CoinPaprika's free tier
    is generous; the 15-min cadence is comfortably below any published
    ceiling and matches the granularity at which dominance moves.
  · Reuses the EXISTING get_crypto_global_market tool via the tool
    router — no new HTTP client, inherits retries and rate accounting.
  · Skipped entirely when the connectivity probe says offline (no
    point recording into an outage). Non-fatal on any error.
  · Writes to metric_series (metric, value, observed_at, source).
    One row per metric per snapshot; 3 rows per successful snapshot.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any


# Env-tunable snapshot interval. 15 min default = 96 samples/day/metric.
# CoinPaprika free tier documents ~25k requests/day — three metrics per
# snapshot × 96 snapshots = 288 upstream calls/day, well under 2 % of the
# free ceiling. Setting the interval to 1 min would still fit (4320/day),
# but dominance moves slower than that; 15 min is honest sampling.
def snapshot_interval_secs() -> int:
    raw = os.environ.get("METRIC_RECORDER_INTERVAL_SECS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 60:
                return v
        except ValueError:
            pass
    return 15 * 60


def recorder_enabled() -> bool:
    raw = os.environ.get("METRIC_RECORDER_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


# Metric names — stable identifiers that the descriptive-backtest scorer
# maps into MetricKind. Kept as module-level constants so tests and the
# backtest reader agree on the exact spellings.
METRIC_BTC_DOMINANCE = "btc_dominance"
METRIC_GLOBAL_MARKET_CAP = "global_market_cap"
METRIC_GLOBAL_VOLUME_24H = "global_volume_24h"

_FIELD_MAP = {
    "bitcoin_dominance_percentage": METRIC_BTC_DOMINANCE,
    "market_cap_usd": METRIC_GLOBAL_MARKET_CAP,
    "volume_24h_usd": METRIC_GLOBAL_VOLUME_24H,
}


def extract_metrics(tool_result: dict[str, Any]) -> list[tuple[str, float]]:
    """Pull the three tracked metrics from a get_crypto_global_market result.

    Accepts either a raw tool payload (dict of digest fields) or the
    wrapping success envelope {"success": True, "result": {...}}. Missing
    keys are skipped, not fabricated. Returns [(metric_name, value), …].
    """
    if not isinstance(tool_result, dict):
        return []
    payload = tool_result
    if "result" in tool_result and isinstance(tool_result["result"], dict):
        payload = tool_result["result"]
    out: list[tuple[str, float]] = []
    for source_field, metric in _FIELD_MAP.items():
        if source_field in payload:
            try:
                out.append((metric, float(payload[source_field])))
            except (TypeError, ValueError):
                continue
    return out


async def snapshot_once(persistent_memory, tool_router) -> int:
    """Take one snapshot: call get_crypto_global_market, write metric rows.

    Returns the number of rows written (0 on any failure). Non-fatal —
    every error is logged and swallowed so the calling loop never dies
    on a recorder issue.
    """
    from loguru import logger
    try:
        tr = await tool_router.execute_tool("get_crypto_global_market", {})
    except Exception as exc:
        logger.warning("metric recorder: tool call raised {}: {}",
                       type(exc).__name__, exc)
        return 0
    if not isinstance(tr, dict) or not tr.get("success"):
        return 0
    metrics = extract_metrics(tr)
    if not metrics:
        return 0
    now = datetime.now(timezone.utc)
    written = 0
    for name, value in metrics:
        try:
            await persistent_memory.record_metric_sample(
                name, value, now, "coinpaprika_global",
            )
            written += 1
        except Exception as exc:
            logger.warning("metric_series insert failed for {}: {}", name, exc)
    return written


class ScheduleState:
    """Rolling monotonic timestamp of the last snapshot. One instance per
    running cycle loop; snapshot_due() drives when to call snapshot_once."""

    __slots__ = ("last_snapshot_ts",)

    def __init__(self) -> None:
        self.last_snapshot_ts: float = 0.0

    def snapshot_due(self, now_ts: float | None = None) -> bool:
        now_ts = now_ts if now_ts is not None else time.monotonic()
        return (now_ts - self.last_snapshot_ts) >= snapshot_interval_secs()

    def mark_snapshot(self, now_ts: float | None = None) -> None:
        self.last_snapshot_ts = now_ts if now_ts is not None else time.monotonic()
