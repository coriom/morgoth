"""Rail health check — per-tool status classification against ground truth.

Runs ONE polite call per data-source tool, classifies:
  OK        → 2xx AND every declared digest_field is present + non-null
  DEGRADED  → 2xx AND at least one declared digest_field missing/null
  FROZEN    → 2xx AND digest byte-identical to the previous rail_health row
              for this tool (needs a persisted history — see rail_health
              table in memory/persistent.py)
  DEAD      → 4xx / 5xx / timeout / exception (with the status code / type)

The classifier is a PURE function on (tool_response, prior_digest). All
I/O (running the tool, reading the prior digest, writing the current row)
is orchestrated by the CLI so this module stays unit-testable.

Rate-limit awareness: caller iterates sequentially with a polite delay
so the tightest source (Owlracle 100 req/hr) is respected. At the
default 6-second inter-tool spacing with 11 tools, the whole sweep
takes ~66 s and hits each source at most once — well under every
documented limit.

Read-only against the tool router. NEVER writes to a data source. NEVER
disables a tool — a FROZEN daily-cadence metric (difficulty) is
legitimate, so the classifier reports; the operator decides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["OK", "DEGRADED", "FROZEN", "DEAD"]

# One polite call per tool. Ordered so the tightest-limit source runs
# LAST (single call adds negligible pressure). The list is kept explicit
# so a future data-source addition surfaces here — an unlisted tool
# would silently not be checked.
DEFAULT_INTER_TOOL_DELAY_SECS = 6.0

# Minimum time between rail-check RUNS. Enforced by the CLI so a
# runaway invocation can't blow through Owlracle's 100/hr budget.
MIN_INTER_RUN_MINUTES = 5


@dataclass
class RailResult:
    tool_name: str
    status: Status
    digest: str
    detail: str = ""
    latency_ms: int = 0
    digest_fields_missing: list[str] = field(default_factory=list)

    def summary_line(self) -> str:
        tag = self.status
        if self.digest_fields_missing:
            tag += f" ({', '.join(self.digest_fields_missing)} null)"
        if self.detail and self.status in ("DEAD", "FROZEN"):
            tag += f" — {self.detail[:80]}"
        return f"  {self.tool_name:<32}  {tag}"


def digest_of_result(result: dict[str, Any]) -> str:
    """Deterministic digest of a tool's result payload for FROZEN detection.

    Hashes the 'result' subtree only (ignoring metadata/timestamps that
    would legitimately change between runs even for a static source).
    Sorted keys so dict ordering doesn't perturb the hash.
    """
    payload = result.get("result") or {}
    try:
        canonical = json.dumps(payload, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _missing_digest_fields(result: dict[str, Any], declared: tuple[str, ...]) -> list[str]:
    """Return the list of declared digest_fields that are missing/null in the
    tool's result payload. Empty list = every declared field is present + non-null."""
    payload = result.get("result") or {}
    if not isinstance(payload, dict):
        # Some tools return list payloads (fred observations) — treat as
        # DEGRADED if declared but list-shaped is a coarse fallback.
        return list(declared) if declared and not payload else []
    missing: list[str] = []
    for f in declared:
        v = payload.get(f)
        if v is None or v == "" or v == []:
            missing.append(f)
    return missing


def classify(
    tool_name: str,
    result: dict[str, Any] | Exception,
    declared_digest_fields: tuple[str, ...],
    prior_digest: str | None,
    *,
    latency_ms: int = 0,
) -> RailResult:
    """Pure classifier. Given a tool's response (or an exception), the
    declared digest_fields, and the prior digest for this tool from
    rail_health, return the RailResult.

    Order of precedence (matters for the FROZEN vs DEGRADED tie-break
    when a source returns 2xx with unchanged partial data):
      1. DEAD wins if the response is an exception or success=False
      2. FROZEN wins over DEGRADED if the digest matches prior_digest
         AND declared fields ARE present — a fully-static payload is
         a rail-health issue, not a data-shape issue.
      3. DEGRADED if declared fields missing.
      4. OK otherwise.
    """
    if isinstance(result, Exception):
        return RailResult(
            tool_name=tool_name, status="DEAD", digest="",
            detail=f"{type(result).__name__}: {str(result)[:120]}",
            latency_ms=latency_ms,
        )
    if not isinstance(result, dict):
        return RailResult(
            tool_name=tool_name, status="DEAD", digest="",
            detail=f"non-dict result: {type(result).__name__}",
            latency_ms=latency_ms,
        )
    if not result.get("success", True):
        return RailResult(
            tool_name=tool_name, status="DEAD", digest="",
            detail=str(result.get("error") or "success=false")[:120],
            latency_ms=latency_ms,
        )
    digest = digest_of_result(result)
    missing = _missing_digest_fields(result, declared_digest_fields)
    if prior_digest and digest == prior_digest and not missing:
        return RailResult(
            tool_name=tool_name, status="FROZEN", digest=digest,
            detail=f"payload byte-identical to prior run", latency_ms=latency_ms,
        )
    if missing:
        return RailResult(
            tool_name=tool_name, status="DEGRADED", digest=digest,
            detail=f"declared fields null: {', '.join(missing)}",
            latency_ms=latency_ms, digest_fields_missing=missing,
        )
    return RailResult(
        tool_name=tool_name, status="OK", digest=digest, latency_ms=latency_ms,
    )


def render_table(results: list[RailResult]) -> str:
    if not results:
        return "  (no data-source tools checked)"
    lines = [f"  {'TOOL':<32}  STATUS"]
    for r in results:
        lines.append(r.summary_line())
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    tally = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    lines.append(f"\n  ─── {tally} ───")
    return "\n".join(lines)


def one_line_summary(results: list[RailResult]) -> str:
    """Compact single-line summary for `morgoth session-report`."""
    if not results:
        return "RAIL: (no rail-check run yet)"
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    parts = [f"{n} {s}" for s, n in sorted(counts.items())]
    problem_details = []
    for r in results:
        if r.status in ("DEGRADED", "FROZEN", "DEAD"):
            problem_details.append(f"{r.tool_name}={r.status}")
    if problem_details:
        return f"RAIL: {', '.join(parts)} ({'; '.join(problem_details[:3])})"
    return f"RAIL: {', '.join(parts)}"
