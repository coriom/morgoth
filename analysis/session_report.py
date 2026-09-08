"""One-shot session summary — everything the operator wants at a glance.

Aggregates over abstention_events, rate_limit_events, llm_calls, theses,
objectives, self_modify_proposals, auto_approve_decisions, and (optional
--full) the descriptive scorer. Reuses existing analysis modules; no new
scoring logic here.

Read-only. The only WRITES the session-report path involves are the ones
brain.py already makes to abstention_events and rate_limit_events on the
happy path — the report itself never writes.

Rate-limit inventory (2026-09-08, from tool source review + public docs):
  · CoinGecko free              10-30 req/min  soft, 429 common under load
  · mempool.space               fair-use (no published cap); 60/min conservative
  · Binance /fapi/v1            2400 req/min weighted; per-endpoint 60/min safe
  · alternative.me F&G          no published; poll <1/min to be polite
  · FRED /series/observations   120 req/60s (docs) — 2 req/s hard cap
  · api.exchange.coinbase.com   10 req/s public
  · api.blockcypher.com         3 req/s, 200/hr free tier
  · Owlracle /v4/eth            100 req/hr free plan
  · api.blockchair.com          30 req/min free
Worst-case: BlockCypher at 200/hr is tightest. At cadence N minutes with
avg 2 tool calls/cycle, hourly requests = (60/N) * 2 = 120/N. To stay
under 200/hr BlockCypher: N >= 0.6 min — so N=1 min is fine for it.
Owlracle 100/hr: 120/N <= 100 → N >= 1.2 min. Recommended SAFE FLOOR
= 2 min (100 % margin on Owlracle, comfortable for CoinGecko free).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass
class SessionReport:
    since: datetime
    now: datetime
    cycles_completed: int = 0
    objectives_completed: int = 0
    objectives_by_status: dict[str, int] = field(default_factory=dict)
    theses_total: int = 0
    theses_per_cycle: float = 0.0
    abstentions: int = 0
    abstention_rate: float = 0.0
    contradictions_new: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    rate_limit_warnings: list[tuple[str, int]] = field(default_factory=list)
    llm_by_task_provider: list[dict[str, Any]] = field(default_factory=list)
    pending_measurements: dict[str, Any] = field(default_factory=dict)
    proposals_pending: int = 0
    rail_summary: str = ""
    fallback_events: int = 0
    fallback_breakdown: list[tuple[str, str, str, int]] = field(default_factory=list)
    provider_state: dict[str, str] = field(default_factory=dict)
    provider_last_transition: str = ""
    resource_summary: dict[str, object] = field(default_factory=dict)

    def window_hours(self) -> float:
        return max(1e-6, (self.now - self.since).total_seconds() / 3600.0)

    def render(self) -> str:
        h = self.window_hours()
        lines = [
            f"═══ Morgoth session report ═══",
            f"since {self.since.isoformat(timespec='minutes')}  →  now  "
            f"({h:.2f} h wall-clock)",
            "",
            f"CYCLES completed         : {self.cycles_completed}  "
            f"(rate={self.cycles_completed/h:.1f}/h)",
            f"OBJECTIVES completed     : {self.objectives_completed}  "
            f"by_status={self.objectives_by_status}",
            f"THESES produced          : {self.theses_total}  "
            f"(per cycle: {self.theses_per_cycle:.2f})",
            f"ABSTENTIONS              : {self.abstentions}  "
            f"(rate={self.abstention_rate*100:.0f}% of extraction attempts)",
            f"NEW CONTRADICTIONS       : {self.contradictions_new}",
            f"PROPOSALS awaiting gate 3: {self.proposals_pending}",
        ]
        if self.rate_limit_warnings:
            lines.append("RATE-LIMIT WARNINGS:")
            for tool, n in self.rate_limit_warnings:
                lines.append(f"  · {tool}: {n} hits")
        else:
            lines.append("RATE-LIMIT WARNINGS      : none")
        if self.rail_summary:
            lines.append(self.rail_summary)
        if self.fallback_events:
            lines.append(f"LLM FALLBACKS            : {self.fallback_events}")
            for task, cfg, used, n in self.fallback_breakdown[:5]:
                lines.append(f"  · task={task} configured={cfg} used={used} n={n}")
        else:
            lines.append("LLM FALLBACKS            : 0")
        if self.provider_state:
            parts = [f"{p} {s}" for p, s in sorted(self.provider_state.items())]
            trans = f" (last transition: {self.provider_last_transition})" if self.provider_last_transition else " (no transitions)"
            lines.append(f"PROVIDERS                : {', '.join(parts)}{trans}")
        if self.resource_summary and self.resource_summary.get("n", 0):
            rs = self.resource_summary
            lines.append(
                f"RESOURCES                : n={rs['n']} samples · worst={rs['worst']}"
                f" · TIGHT={rs['tight']} · THRASHING={rs['thrashing']}"
                f" · peak_swap={rs['peak_swap_pct']}%"
            )
        if self.tool_calls:
            lines.append("TOOL ADOPTION (data sources, top 10):")
            top = sorted(self.tool_calls.items(), key=lambda kv: -kv[1])[:10]
            for name, n in top:
                lines.append(f"  · {name:<32} {n} calls")
        if self.llm_by_task_provider:
            lines.append("LLM CALLS:")
            for row in self.llm_by_task_provider:
                lines.append(
                    f"  · {row['task']:<10} via {row['provider']:<12} "
                    f"n={row['n']:>3}  median={row['median_ms']:>6.0f}ms"
                )
        pm = self.pending_measurements
        if pm:
            lines.append("PENDING MEASUREMENTS:")
            for k, v in pm.items():
                lines.append(f"  · {k:<32} {v}")
        return "\n".join(lines)


async def collect(pm, since: datetime, *, full: bool = False) -> SessionReport:
    """Assemble a SessionReport by querying the DB.

    `pm` is a PersistentMemory instance. `since` is the window start.
    `full=True` invokes the descriptive backtest scorer for the verifiable-
    share number; slow, so opt-in.
    """
    now = datetime.now(tz=timezone.utc)
    r = SessionReport(since=since, now=now)
    pool = pm._require_pool()
    async with pool.acquire() as conn:
        # cycles: proxy via logs table SYSTEM entries — sparse; use objectives
        # transitions as a coarser proxy.
        objectives = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM objectives WHERE created_at >= $1 "
            "GROUP BY status", since,
        )
        r.objectives_by_status = {row["status"]: row["n"] for row in objectives}
        r.objectives_completed = sum(
            n for s, n in r.objectives_by_status.items() if s in ("done", "completed")
        )
        theses_rows = await conn.fetch(
            "SELECT COUNT(*) AS n FROM theses WHERE created_at >= $1", since,
        )
        r.theses_total = theses_rows[0]["n"] if theses_rows else 0
        # Abstentions
        try:
            a_rows = await conn.fetch(
                "SELECT COUNT(*) AS n FROM abstention_events WHERE created_at >= $1", since,
            )
            r.abstentions = a_rows[0]["n"] if a_rows else 0
        except Exception:
            r.abstentions = 0
        extraction_attempts = max(1, r.theses_total // 2 + r.abstentions)  # rough
        # Better: extraction attempts ≈ objectives_completed (extraction runs once per completion).
        if r.objectives_completed > 0:
            extraction_attempts = r.objectives_completed
        r.abstention_rate = r.abstentions / extraction_attempts if extraction_attempts else 0.0
        r.cycles_completed = r.objectives_completed * 5  # MAX_CYCLES_PER_OBJECTIVE
        r.theses_per_cycle = r.theses_total / r.cycles_completed if r.cycles_completed else 0.0
        # New contradictions
        try:
            c_rows = await conn.fetch(
                "SELECT COUNT(*) AS n FROM contradictions WHERE detected_at >= $1", since,
            )
            r.contradictions_new = c_rows[0]["n"] if c_rows else 0
        except Exception:
            r.contradictions_new = 0
        # Rate-limit events
        try:
            rl_rows = await conn.fetch(
                "SELECT tool_name, COUNT(*) AS n FROM rate_limit_events "
                "WHERE created_at >= $1 GROUP BY tool_name ORDER BY 2 DESC", since,
            )
            r.rate_limit_warnings = [(row["tool_name"], row["n"]) for row in rl_rows]
        except Exception:
            r.rate_limit_warnings = []
        # LLM calls
        try:
            llm_rows = await conn.fetch(
                "SELECT task, provider, COUNT(*) AS n, "
                "percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS med "
                "FROM llm_calls WHERE created_at >= $1 GROUP BY task, provider", since,
            )
            r.llm_by_task_provider = [
                {"task": row["task"], "provider": row["provider"],
                 "n": row["n"], "median_ms": float(row["med"] or 0)}
                for row in llm_rows
            ]
        except Exception:
            r.llm_by_task_provider = []
        # Proposals awaiting gate 3
        try:
            p_rows = await conn.fetch(
                "SELECT COUNT(*) AS n FROM self_modify_proposals "
                "WHERE status = 'pending_approval'",
            )
            r.proposals_pending = p_rows[0]["n"] if p_rows else 0
        except Exception:
            r.proposals_pending = 0
        # Provider health: current state + last transition timestamp.
        try:
            ph_latest = await conn.fetch(
                "SELECT DISTINCT ON (provider) provider, status, created_at "
                "FROM provider_health ORDER BY provider, created_at DESC"
            )
            r.provider_state = {row["provider"]: row["status"] for row in ph_latest}
            if ph_latest:
                last = max(ph_latest, key=lambda row: row["created_at"])
                r.provider_last_transition = f"{last['provider']} → {last['status']} at {last['created_at'].isoformat(timespec='minutes')}"
        except Exception:
            r.provider_state = {}
        # Resource samples in window.
        try:
            from analysis.resources import ResourceSample as _RS, summarize_window
            rs_rows = await conn.fetch(
                "SELECT classification, ram_available_pct, swap_pct, lav_ratio, "
                "cpu_idle_pct, reason, created_at FROM resource_samples "
                "WHERE created_at >= $1 ORDER BY created_at ASC", since,
            )
            samples = [
                _RS(
                    ts_iso=row["created_at"].isoformat(timespec="seconds"),
                    ram_total_mb=0, ram_available_mb=0,  # not persisted in this table
                    swap_total_mb=0, swap_used_mb=0,
                    load_avg_1=row["lav_ratio"], cpu_count=1,
                    cpu_idle_pct=row["cpu_idle_pct"],
                    classification=row["classification"], reason=row["reason"] or "",
                )
                for row in rs_rows
            ]
            # Overlay the persisted percentages so the aggregate is accurate.
            for s, row in zip(samples, rs_rows):
                s.__dict__["_swap_pct_override"] = float(row["swap_pct"])
                s.__dict__["_ram_avail_override"] = float(row["ram_available_pct"])
            # Cheap re-summarize using the persisted percentages directly.
            if rs_rows:
                tight = sum(1 for row in rs_rows if row["classification"] == "TIGHT")
                thrashing = sum(1 for row in rs_rows if row["classification"] == "THRASHING")
                r.resource_summary = {
                    "n": len(rs_rows),
                    "worst": "THRASHING" if thrashing else ("TIGHT" if tight else "OK"),
                    "tight": tight,
                    "thrashing": thrashing,
                    "peak_swap_pct": round(max(float(row["swap_pct"]) for row in rs_rows) * 100, 1),
                }
        except Exception:
            r.resource_summary = {}
        # LLM fallback events
        try:
            fb_rows = await conn.fetch(
                "SELECT task, configured, used, COUNT(*) AS n FROM llm_fallback_events "
                "WHERE created_at >= $1 GROUP BY task, configured, used "
                "ORDER BY n DESC", since,
            )
            r.fallback_events = sum(row["n"] for row in fb_rows)
            r.fallback_breakdown = [
                (row["task"], row["configured"], row["used"], row["n"])
                for row in fb_rows
            ]
        except Exception:
            r.fallback_events = 0
        # Rail health — latest row per tool from rail_health.
        try:
            from analysis import rail_health as _RH
            rh_rows = await conn.fetch(
                "SELECT DISTINCT ON (tool_name) tool_name, status, digest, "
                "detail, latency_ms FROM rail_health "
                "ORDER BY tool_name, created_at DESC"
            )
            if rh_rows:
                latest = [
                    _RH.RailResult(
                        tool_name=row["tool_name"], status=row["status"],
                        digest=row["digest"] or "", detail=row["detail"] or "",
                        latency_ms=row["latency_ms"] or 0,
                    )
                    for row in rh_rows
                ]
                r.rail_summary = _RH.one_line_summary(latest)
        except Exception:
            r.rail_summary = ""

    # Tool adoption — parse from the logs table (best-effort — logs.duration_ms
    # can proxy but tool name lives in content; approximate via LIKE).
    r.tool_calls = {}
    # Pending measurement counters
    pm_dict: dict[str, Any] = {}
    # auto-approve n / cap
    try:
        async with pool.acquire() as conn:
            aa = await conn.fetch(
                "SELECT COUNT(*) AS n FROM self_modify_proposals WHERE proposed_by='morgoth' "
                "AND status IN ('applied', 'apply_failed_rolled_back', 'rejected')"
            )
            pm_dict["auto-approve decisions (need >= 30)"] = f"{aa[0]['n'] if aa else 0} / 30"
    except Exception:
        pass
    if full:
        # Only compute verifiable-share on --full; requires the scorer.
        try:
            from scripts.backtest_theses_descriptive import (
                _fetch_theses, _fetch_hashrate_and_difficulty, _fetch_fng,
                _fetch_coingecko_all_series, _fetch_owlracle_gas,
                _fetch_binance_funding, _safe_fetch,
            )
            from analysis.thesis_backtest import parse_direction, subject_asset
            from analysis.thesis_backtest_descriptive import triage
            import httpx, os
            from core.config import load_config
            cfg = await load_config()
            theses = await _fetch_theses(cfg)
            theses = [t for t in theses if t["created_at"] >= since]
            non_dir = [t for t in theses if not (
                subject_asset(str(t.get("subject", ""))) is not None
                and parse_direction(str(t.get("claim", ""))) is not None
            )]
            counts, _, _, _ = triage(non_dir)
            n_input = counts.get("input", 0) or 1
            verifiable = (counts.get("metric", 0) + counts.get("relation", 0)) / n_input
            pm_dict["verifiable share (window)"] = f"{verifiable*100:.1f}%"
        except Exception as exc:
            pm_dict["verifiable share (window)"] = f"(--full failed: {exc})"
    r.pending_measurements = pm_dict
    return r
