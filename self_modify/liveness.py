"""Deterministic field-liveness gate.

Runs 4 GETs at t=0/150/300/450s on the proposal's endpoint and applies
three rules:

  (a) ROLLING-NAMED FROZEN — a digest field whose name matches a
      rolling window / flow metric (24h|7d|30d|volume|change|flow|rate,
      case-insensitive) that returns the IDENTICAL value across all
      four hits → rejected_static. A rolling-window metric that
      doesn't move over 7.5 minutes is endpoint-freeze evidence.

  (b) DEAD DIGEST FIELD — any digest field observed only at
      zero/null/empty across all four hits → rejected_static. A field
      observed only at zero across the window carries no digest signal;
      the rejection is by information content, not by suspected defect.

  (c) NON-ROLLING STATIC — a non-rolling field static across the
      window → WARN appended to status_reason (advisory, proposal
      proceeds). Some fields are legitimately static in-window
      (config, version, chain-id); an operator note surfaces the
      observation without blocking.

Rule (a) and (b) are the operator's three manual-probe kills promoted
to machinery: BlockCypher's ``peer_count`` (dead, rule b), the
blockchain.info + DefiLlama frozen rolling aggregates (rule a).

Scheduling
----------
The probe runs CONCURRENTLY with ``gate_tests`` (sandbox pytest,
~547s under xdist). The 450s probe window nests inside the sandbox
window; the caller waits for both. If sandbox tests finish first
(smaller suite in some future), the caller WAITS for the probe —
correctness over wall time.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable

import httpx


# 24h/7d/30d intentionally captured as substrings to catch total24h,
# total_7d, volume_24h, etc. The other tokens land on their own.
_ROLLING_RE = re.compile(
    r"(24h|7d|30d|volume|change|flow|rate)", re.IGNORECASE,
)


DEFAULT_HITS: int = 4
DEFAULT_GAP_SECS: int = 150
DEFAULT_HIT_TIMEOUT_SECS: float = 15.0


def is_rolling_named(field: str) -> bool:
    """True if the field name denotes a rolling window / flow metric."""
    if not field:
        return False
    return _ROLLING_RE.search(field) is not None


# ---------------------------------------------------------------------------
# probe scheduler
# ---------------------------------------------------------------------------

async def _one_hit(
    url: str, timeout: float = DEFAULT_HIT_TIMEOUT_SECS,
) -> dict[str, Any]:
    """Single GET. HTTP or JSON errors become the returned value —
    never crash the scheduler."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        body = r.json()
    except Exception:  # noqa: BLE001
        return {"ok": False, "status": r.status_code,
                "error": f"non-json (status={r.status_code})"}
    return {"ok": True, "status": r.status_code, "body": body}


async def run_liveness_probe(
    url: str,
    digest_fields: list[str],
    *,
    hits: int = DEFAULT_HITS,
    gap_secs: int = DEFAULT_GAP_SECS,
    hit_timeout: float = DEFAULT_HIT_TIMEOUT_SECS,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """4 GETs at t=0/150/300/450s. Returns per-hit projected values.

    Per hit per digest field: the value itself, OR the HTTP error
    string as the value (per user spec: "value, or the HTTP error AS
    the value"). Downstream classification treats an error-string as
    "not observed as movement" but does not raise.
    """
    per_hit: list[dict[str, Any]] = []
    for i in range(hits):
        t0 = now_fn()
        h = await _one_hit(url, timeout=hit_timeout)
        vals: dict[str, Any] = {}
        if h.get("ok") and isinstance(h.get("body"), dict):
            body = h["body"]
            for f in digest_fields:
                vals[f] = body.get(f)
        else:
            err = h.get("error") or f"HTTP {h.get('status')}"
            for f in digest_fields:
                vals[f] = f"error:{err}"
        per_hit.append({
            "i": i, "ok": h.get("ok"), "vals": vals,
            "status": h.get("status"), "error": h.get("error"),
        })
        if i < hits - 1:
            elapsed = now_fn() - t0
            await sleep_fn(max(0.0, gap_secs - elapsed))
    return {"url": url, "hits": per_hit, "n_hits": hits,
            "digest_fields": list(digest_fields)}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

_DEAD_VALUES: tuple[Any, ...] = (0, 0.0, None, "")


def _is_dead(v: Any) -> bool:
    if isinstance(v, str) and v.startswith("error:"):
        return False  # an error is neither dead nor alive — see rule design
    return v in _DEAD_VALUES


def classify_probe(
    probe: dict[str, Any], digest_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Apply rules (a)/(b)/(c). Returns:
      {"outcome": "reject" | "warn" | "pass",
       "rule": "a" | "b" | "c" | None,
       "reason": str,
       "per_field": {field: [hit_values...]}}

    Precedence: (b) dead-field is checked before (a) rolling-frozen —
    a frozen zero is more severe than a frozen nonzero. (c) fires only
    if neither (a) nor (b) applied.
    """
    fields = list(digest_fields or probe.get("digest_fields") or [])
    per_field: dict[str, list[Any]] = {f: [] for f in fields}
    for hit in probe.get("hits", []):
        for f in fields:
            per_field[f].append(hit.get("vals", {}).get(f))

    # (b) dead field across all hits
    dead: list[str] = []
    for f in fields:
        vals = per_field[f]
        if vals and all(_is_dead(v) for v in vals):
            dead.append(f)
    if dead:
        return {
            "outcome": "reject", "rule": "b",
            "reason": (f"rejected_static (b): digest field(s) at zero/null "
                       f"across all {len(probe.get('hits', []))} hits: "
                       f"{dead} — no digest signal"),
            "per_field": per_field, "dead_fields": dead,
        }

    # (a) rolling-named frozen across all hits
    frozen_rolling: list[str] = []
    for f in fields:
        if not is_rolling_named(f):
            continue
        vals = per_field[f]
        # Any error hit disqualifies "frozen" — we can't tell if it
        # would have moved. Frozen means all hits succeeded with the
        # identical value.
        if not vals or any(isinstance(v, str) and v.startswith("error:")
                           for v in vals):
            continue
        if len({repr(v) for v in vals}) == 1:
            frozen_rolling.append(f)
    if frozen_rolling:
        return {
            "outcome": "reject", "rule": "a",
            "reason": (f"rejected_static (a): rolling-named field(s) frozen "
                       f"across probe window: {frozen_rolling} — "
                       f"endpoint-freeze evidence"),
            "per_field": per_field, "frozen_fields": frozen_rolling,
        }

    # (c) non-rolling static → warn
    static_nonrolling: list[str] = []
    for f in fields:
        if is_rolling_named(f):
            continue
        vals = per_field[f]
        if not vals or any(isinstance(v, str) and v.startswith("error:")
                           for v in vals):
            continue
        if len({repr(v) for v in vals}) == 1:
            static_nonrolling.append(f)
    if static_nonrolling:
        return {
            "outcome": "warn", "rule": "c",
            "reason": (f"note: non-rolling digest field(s) static across "
                       f"probe: {static_nonrolling}"),
            "per_field": per_field, "static_fields": static_nonrolling,
        }

    return {"outcome": "pass", "rule": None,
            "reason": "all digest fields moved or plausibly live",
            "per_field": per_field}
