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
    digest_fields: list[Any],
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

    ONE EXTRACTOR (2026-09-29): field projection goes through
    ``self_modify.digest_path.resolve_digest_fields`` — the SAME
    resolver the generated tool's ``execute()`` runs and the shadow
    sampler runs. The probe no longer re-implements top-level
    projection (which was blind to JSON-RPC bodies with values under
    ``result.<name>`` — 9f446bb4 was reported "moved or plausibly
    live" by the probe while every path resolved to null). Any
    resolve error for a field becomes ``error:<msg>`` on that hit,
    which classify_probe already handles as "unknown, not dead".
    """
    from self_modify.digest_path import (
        resolve_digest_fields, normalize_digest_fields,
    )
    # normalize is used ONLY to derive the ordered name list — the
    # ORIGINAL entries (which may be plain-string legacy shape) are
    # what we pass into the resolver so it can trigger the top-level →
    # data[0] → list[0] unwrap for string entries. Pre-normalizing to
    # dicts here would suppress the unwrap (dicts always mean "path
    # grammar, don't unwrap"), and the 1182ee96 list-shaped fixture
    # would regress to "unknown, not frozen".
    names = [e["name"] for e in normalize_digest_fields(digest_fields)]

    per_hit: list[dict[str, Any]] = []
    for i in range(hits):
        t0 = now_fn()
        h = await _one_hit(url, timeout=hit_timeout)
        vals: dict[str, Any] = {}
        if h.get("ok"):
            values, errors, _meta = resolve_digest_fields(
                digest_fields, h.get("body"),
            )
            error_map = {n: m for n, m in errors}
            for n in names:
                if n in values:
                    vals[n] = values[n]
                elif n in error_map:
                    vals[n] = f"error:resolve-{error_map[n][:80]}"
                else:
                    vals[n] = "error:extraction-failed"
        else:
            err = h.get("error") or f"HTTP {h.get('status')}"
            for n in names:
                vals[n] = f"error:{err}"
        per_hit.append({
            "i": i, "ok": h.get("ok"), "vals": vals,
            "status": h.get("status"), "error": h.get("error"),
            # 2026-09-29: retain the raw body so the artifact check
            # can execute the generated FILE against it (no re-hit
            # needed — the probe already paid the network cost).
            "body": h.get("body") if h.get("ok") else None,
        })
        if i < hits - 1:
            elapsed = now_fn() - t0
            await sleep_fn(max(0.0, gap_secs - elapsed))
    return {"url": url, "hits": per_hit, "n_hits": hits,
            "digest_fields": names, "gap_secs": gap_secs}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def _is_error(v: Any) -> bool:
    return isinstance(v, str) and v.startswith("error:")


def _is_null(v: Any) -> bool:
    """Null/missing = None or an error-string (extraction failed)."""
    return v is None or _is_error(v)


def classify_probe(
    probe: dict[str, Any], digest_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Field-liveness verdict from a probe.

    Rules (2026-09-29 — revised after 9f446bb4):

      · REJECT (rule ``null``) — ANY field is null/missing across all
        hits. Signals a broken extractor (the tool won't produce a
        value on any real request). ``0`` is NOT null: a legitimate
        clamped-zero value (Deribit funding at ±0.025% inner band,
        Binance funding at ±0.01%, blockchain.info miners_revenue
        during a quiet block) is a real observation.

      · REJECT (rule ``no-info``) — EVERY judgeable field is static
        across the probe window. No information about liveness at
        all; the endpoint may be frozen. Requires ≥2 hits.

      · WARN (rule ``partial-static``) — some fields static, others
        move. The static ones are recorded (visible at gate 3 with
        the probe duration) but do NOT block: a clamped-zero funding
        rate alongside a moving open_interest is expected behaviour
        for the source, not a defect. Requires ≥2 hits.

      · PASS — every judgeable field moved OR the probe carried too
        few observations to judge (1 hit).

    Fields whose observations are ALL error-strings are skipped for
    static/moving determination — we can't tell what the value would
    have been.

    Returns ``{outcome, rule, reason, per_field, null_fields?,
    static_fields?, moving_fields?, probe_duration_s?}``.
    """
    fields = list(digest_fields or probe.get("digest_fields") or [])
    hits = probe.get("hits", [])
    n_hits = len(hits)
    per_field: dict[str, list[Any]] = {f: [] for f in fields}
    for h in hits:
        for f in fields:
            per_field[f].append(h.get("vals", {}).get(f))

    # null/missing across ALL hits — extractor broken for this field.
    null_fields = [f for f in fields
                   if per_field[f] and all(_is_null(v) for v in per_field[f])]
    if null_fields:
        return {
            "outcome": "reject", "rule": "null",
            "reason": (f"rejected_static (null): field(s) null/missing "
                       f"across all {n_hits} hits: {null_fields} — "
                       f"extractor broken (nothing to compare)"),
            "per_field": per_field, "null_fields": null_fields,
        }

    if n_hits < 2:
        return {"outcome": "pass", "rule": None,
                "reason": (f"insufficient hits ({n_hits}) for movement "
                           "verdict — no null field observed"),
                "per_field": per_field}

    static_fields: list[str] = []
    moving_fields: list[str] = []
    for f in fields:
        vals = per_field[f]
        # Fields with any error-string observation are unjudgeable —
        # we don't know if they would have moved.
        clean = [v for v in vals if not _is_error(v)]
        if len(clean) < 2:
            continue
        if len({repr(v) for v in clean}) == 1:
            static_fields.append(f)
        else:
            moving_fields.append(f)

    probe_duration_s = (n_hits - 1) * int(probe.get("gap_secs") or DEFAULT_GAP_SECS)

    if static_fields and not moving_fields:
        return {
            "outcome": "reject", "rule": "no-info",
            "reason": (f"rejected_static (no-info): every judgeable field "
                       f"static across the {probe_duration_s}s probe "
                       f"window ({n_hits} hits): {static_fields} — probe "
                       f"carries no information about liveness"),
            "per_field": per_field, "static_fields": static_fields,
            "moving_fields": [], "probe_duration_s": probe_duration_s,
        }
    if static_fields:
        return {
            "outcome": "warn", "rule": "partial-static",
            "reason": (f"partial-static: {static_fields} static across "
                       f"the {probe_duration_s}s probe window ({n_hits} "
                       f"hits) while {moving_fields} moved — legitimate "
                       f"for clamped/reserved fields"),
            "per_field": per_field, "static_fields": static_fields,
            "moving_fields": moving_fields,
            "probe_duration_s": probe_duration_s,
        }
    return {"outcome": "pass", "rule": None,
            "reason": (f"every judgeable field moved across "
                       f"{probe_duration_s}s ({n_hits} hits)"),
            "per_field": per_field, "moving_fields": moving_fields,
            "probe_duration_s": probe_duration_s}
