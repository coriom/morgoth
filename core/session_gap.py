"""Session-gap detection — make downtime a first-class fact.

The operator now runs in short bursts. Nothing in the loop KNOWS a gap
happened, so a thesis stamped right after resume is compared against a
pre-gap belief state as if the last observation were still current.

Signal chosen: metric_series.observed_at. The recorder writes every 15
min while alive; a gap larger than one snapshot interval is bounded by
the two adjacent observed_at values. Alternatives rejected:
  · last cycle log — logs are gossipy; a cycle that dies mid-flight
    still leaves partial entries. Timestamp reliability is per-log-line,
    not per-uptime.
  · objectives.updated_at — only advances when an objective is claimed.
    An idle-but-alive loop looks the same as a dead one.
The metric recorder is heartbeat-precise: gap = now − last observed_at,
minus one interval (that interval is a normal wait, not downtime).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger


def gap_threshold_secs() -> int:
    """Below this, treat as restart (log line only). Above, record row."""
    raw = os.environ.get("SESSION_GAP_THRESHOLD_SECS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 60:
                return v
        except ValueError:
            pass
    return 30 * 60


async def compute_and_record_gap(persistent_memory) -> dict[str, Any] | None:
    """On startup, compute downtime vs the last metric_series row and,
    if it exceeds the threshold, insert a session_gaps row. Returns the
    inserted row (dict) or None. Non-fatal on any error — startup MUST
    NOT die because a gap detector failed."""
    try:
        pool = persistent_memory._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT observed_at FROM metric_series "
                "ORDER BY observed_at DESC LIMIT 1"
            )
    except Exception as exc:
        logger.warning("session_gap probe failed (non-fatal): {}", exc)
        return None
    if not row:
        return None
    now = datetime.now(timezone.utc)
    last = row["observed_at"]
    # Subtract one interval — the recorder writes at 15 min cadence, so
    # a gap smaller than one interval is normal waiting, not downtime.
    from core.metric_recorder import snapshot_interval_secs
    delta = (now - last).total_seconds() - float(snapshot_interval_secs())
    if delta < gap_threshold_secs():
        return None
    try:
        pool = persistent_memory._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO session_gaps (started_at, ended_at, duration_secs) "
                "VALUES ($1, $2, $3)",
                last, now, int(delta),
            )
    except Exception as exc:
        logger.warning("session_gaps insert failed (non-fatal): {}", exc)
        return None
    logger.info("resumed after {:.1f}h gap (started {}, duration {}s)",
                 delta / 3600.0, last.isoformat(), int(delta))
    return {"started_at": last, "ended_at": now, "duration_secs": int(delta)}


async def load_gaps(persistent_memory) -> list[tuple[datetime, datetime]]:
    """Load recorded gaps as (started_at, ended_at) intervals."""
    try:
        pool = persistent_memory._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT started_at, ended_at FROM session_gaps "
                "ORDER BY started_at"
            )
    except Exception:
        return []
    return [(r["started_at"], r["ended_at"]) for r in rows]


def pair_spans_gap(
    ts_a: datetime, ts_b: datetime,
    gaps: list[tuple[datetime, datetime]],
) -> bool:
    """True iff any gap interval lies between ts_a and ts_b. A pair
    entirely inside one live session returns False → today's behaviour
    is byte-identical."""
    lo, hi = (ts_a, ts_b) if ts_a <= ts_b else (ts_b, ts_a)
    for start, end in gaps:
        # Gap is between the two theses iff it starts after the older
        # AND ends before the newer (or straddles either).
        if start <= hi and end >= lo:
            # Overlap; more specifically: the gap must sit BETWEEN, not
            # coincide with the point of either thesis.
            if lo <= start <= hi or lo <= end <= hi:
                return True
    return False


def ts_inside_gap(
    ts: datetime, gaps: list[tuple[datetime, datetime]],
) -> bool:
    """True iff ts lands inside a recorded gap window. Used by the
    descriptive backtest to SKIP theses stamped mid-outage."""
    for start, end in gaps:
        if start <= ts <= end:
            return True
    return False
