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
  · Each Domain-declared collector has its own cadence and maps one
    registered tool payload to logical metric fields and a source label.
    The built-in crypto pack retains its historical 15-minute interval.
  · Calls tools through the existing router, inheriting retries and rate
    accounting without another HTTP client.
  · Skipped entirely when the connectivity probe says offline (no
    point recording into an outage). Non-fatal on any error.
  · Writes to metric_series (metric, value, observed_at, source).
    One row per metric per snapshot; 3 rows per successful snapshot.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any, Mapping


# Optional operator-wide cadence override; each Domain collector otherwise
# uses its declared interval. The minimum protects upstream rate budgets.
def _interval_override() -> int | None:
    raw = os.environ.get("METRIC_RECORDER_INTERVAL_SECS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 60:
                return v
        except ValueError:
            pass
    return None


def snapshot_interval_secs(domain=None) -> int:
    """Return the shortest configured cadence, or zero without collectors."""
    collections = (domain or _current_domain()).metric_collections
    if not collections:
        return 0
    return _interval_override() or min(item.interval_secs for item in collections.values())


def recorder_enabled() -> bool:
    raw = os.environ.get("METRIC_RECORDER_ENABLED", "true").strip().lower()
    return bool(_current_domain().metric_collections) and raw not in ("false", "0", "no", "off")


# Historical metric identifiers stay import-stable for the crypto scorer.
# 2026-09-30 chantier-1: metric names + field map + source tool
# sourced from the active domain pack. Module-level names stay for
# import stability (analysis/thesis_backtest_descriptive.py and tests
# import METRIC_BTC_DOMINANCE etc).
from core.domain import current_domain as _current_domain  # noqa: E402
_names = _current_domain().metric_names
METRIC_BTC_DOMINANCE = _names.get("btc_dominance", "btc_dominance")
METRIC_GLOBAL_MARKET_CAP = _names.get("global_market_cap", "global_market_cap")
METRIC_GLOBAL_VOLUME_24H = _names.get("global_volume_24h", "global_volume_24h")
_FIELD_MAP = dict(_current_domain().metric_field_map)
_COLLECTIONS = _current_domain().metric_collections


def extract_metrics(tool_result: dict[str, Any], *, fields: tuple[str, ...] | None = None,
                    field_map: Mapping[str, str] | None = None) -> list[tuple[str, float]]:
    """Pull Domain-declared metric fields from a tool result.

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
    mapping = field_map if field_map is not None else _FIELD_MAP
    selected = fields if fields is not None else tuple(mapping)
    for source_field in selected:
        metric = mapping[source_field]
        if source_field in payload:
            try:
                out.append((metric, float(payload[source_field])))
            except (TypeError, ValueError):
                continue
    return out


async def snapshot_once(persistent_memory, tool_router, *, due_tools: tuple[str, ...] | None = None,
                        domain=None) -> int:
    """Take due Domain-declared snapshots and write metric rows.

    Returns the number of rows written (0 on any failure). Non-fatal —
    every error is logged and swallowed so the calling loop never dies
    on a recorder issue.
    """
    from loguru import logger
    written = 0
    pack = domain or _current_domain()
    specs = pack.metric_collections
    selected = due_tools if due_tools is not None else tuple(specs)
    for tool in selected:
        spec = specs[tool]
        try:
            tr = await tool_router.execute_tool(tool, dict(spec.args))
        except Exception as exc:
            logger.warning("metric recorder: tool call raised {}: {}", type(exc).__name__, exc)
            continue
        if not isinstance(tr, dict) or not tr.get("success"):
            continue
        now = datetime.now(timezone.utc)
        for name, value in extract_metrics(tr, fields=spec.fields, field_map=pack.metric_field_map):
            try:
                await persistent_memory.record_metric_sample(name, value, now, spec.source)
                written += 1
            except Exception as exc:
                logger.warning("metric_series insert failed for {}: {}", name, exc)
    return written


class ScheduleState:
    """Rolling monotonic timestamp of the last snapshot. One instance per
    running cycle loop; snapshot_due() drives when to call snapshot_once."""

    __slots__ = ("last_snapshot_ts", "_last_by_tool", "_collections")

    def __init__(self, collections: Mapping[str, Any] | None = None) -> None:
        self.last_snapshot_ts: float = 0.0
        self._last_by_tool: dict[str, float] = {}
        self._collections = collections if collections is not None else _COLLECTIONS

    def due_tools(self, now_ts: float | None = None) -> tuple[str, ...]:
        """Return each configured collector whose interval has elapsed."""
        now_ts = now_ts if now_ts is not None else time.monotonic()
        override = _interval_override()
        return tuple(tool for tool, spec in self._collections.items()
                     if now_ts - self._last_by_tool.get(tool, 0.0) >= (override or spec.interval_secs))

    def snapshot_due(self, now_ts: float | None = None) -> bool:
        return bool(self.due_tools(now_ts))

    def mark_snapshot(self, now_ts: float | None = None, *, tools: tuple[str, ...] | None = None) -> None:
        self.last_snapshot_ts = now_ts if now_ts is not None else time.monotonic()
        for tool in tools if tools is not None else self._collections:
            self._last_by_tool[tool] = self.last_snapshot_ts
