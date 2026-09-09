"""Provider heartbeat — periodic liveness check for providers in the
current routing. Transition-only logging so it doesn't spam.

Contract (from the chantier design brief):
  · NEVER a paid API call. api-key check is PRESENCE-only via env, as
    everywhere else in the codebase.
  · NEVER raises into the caller. Every probe wraps I/O in try/except.
  · Persists to provider_health ONLY on state change (ok↔down). 144
    identical rows per day would be noise.
  · Interval from PROVIDER_HEARTBEAT_MINUTES env, default 10.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

HeartbeatStatus = Literal["ok", "down"]


@dataclass
class ProbeResult:
    provider: str
    status: HeartbeatStatus
    detail: str


def heartbeat_interval_secs() -> int:
    try:
        m = int(os.environ.get("PROVIDER_HEARTBEAT_MINUTES") or 10)
    except ValueError:
        m = 10
    return max(60, m * 60)


async def probe_ollama() -> ProbeResult:
    """Two-second /api/tags probe. Never raises."""
    from core.config import load_config
    try:
        cfg = await load_config()
        import httpx
        host = str(cfg.ollama_base_url).rstrip('/')
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.get(f"{host}/api/tags")
        if r.status_code == 200:
            return ProbeResult("ollama", "ok", f"HTTP 200 at {host}")
        return ProbeResult("ollama", "down", f"HTTP {r.status_code}")
    except Exception as exc:
        return ProbeResult("ollama", "down", f"{type(exc).__name__}: {str(exc)[:80]}")


def probe_claude_cli() -> ProbeResult:
    """`claude --version` presence probe. No network call.

    Fallback resolution: systemd's Environment=PATH doesn't always
    populate the child's search path uniformly across runners. When
    shutil.which('claude') returns None, try the known install path
    (~/.npm-global/bin/claude) so a system misconfig doesn't
    false-negative-report claude-cli as down.
    """
    binary = shutil.which("claude")
    if not binary:
        candidate = os.path.expanduser("~/.npm-global/bin/claude")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            binary = candidate
    if not binary:
        return ProbeResult("claude-cli", "down", "not on PATH")
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            ver = (out.stdout or "").strip().splitlines()[0] if out.stdout else ""
            return ProbeResult("claude-cli", "ok", f"{ver}")
        return ProbeResult("claude-cli", "down", f"exit={out.returncode}")
    except Exception as exc:
        return ProbeResult("claude-cli", "down", f"{type(exc).__name__}")


def probe_api_key() -> ProbeResult:
    """PRESENCE only. Never reads value beyond bool(). Never makes a
    paid call to check if the key still works — that's an operator
    decision (cost), not a heartbeat concern."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ProbeResult("api", "ok", "ANTHROPIC_API_KEY present (presence-only, never paid-probed)")
    return ProbeResult("api", "down", "ANTHROPIC_API_KEY not set")


async def one_heartbeat_round() -> list[ProbeResult]:
    """Probe every provider referenced by the current routing. Order
    doesn't matter — this returns a snapshot, not a state machine."""
    return [await probe_ollama(), probe_claude_cli(), probe_api_key()]


async def persist_on_change(
    pm: Any,
    prior: dict[str, str],
    now: list[ProbeResult],
) -> dict[str, str]:
    """Insert a row into provider_health ONLY for providers whose status
    differs from ``prior``. Returns the new prior dict for the caller
    to hold across ticks.

    Transition-only logging keeps the table small and every row
    meaningful — an ok→down row IS an incident, not noise.
    """
    updated = dict(prior)
    for r in now:
        old = prior.get(r.provider)
        if old == r.status:
            continue
        logger.warning(
            "provider heartbeat TRANSITION: {} {}→{} ({})",
            r.provider, old or "(new)", r.status, r.detail,
        )
        updated[r.provider] = r.status
        if pm is None:
            continue
        try:
            pool = pm._require_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO provider_health (provider, status, detail) "
                    "VALUES ($1, $2, $3)",
                    r.provider, r.status, r.detail[:400],
                )
        except Exception as exc:
            logger.warning("provider_health insert failed (non-fatal): {}", exc)
    return updated
