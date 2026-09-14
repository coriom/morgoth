"""Pre-cycle connectivity probe — cheap, non-rail DNS resolution.

Purpose: the outage guard in core/outage_guard.py is a POST-cycle safety
net that requeues an objective AFTER two all-network-failure cycles.
This module runs BEFORE cycle work begins: if the host is offline we
skip the cycle entirely (no claim, no tool call, no cycle_count increment)
and poll cheaply until the network returns. An outage costs zero
objectives, not two.

Probe design:
  · DNS resolution ONLY — no HTTP. socket.getaddrinfo against a NEUTRAL
    host, never against a data-source rail API (probing a rail would
    consume its rate budget and pollute usage counters).
  · Two hosts polled in parallel — a single dead resolver (Cloudflare
    outage while Google is fine) MUST NOT flip us to offline.
  · Bounded timeout (default 2s). getaddrinfo runs in a thread via
    asyncio.to_thread; asyncio.wait_for enforces the wall clock.
  · Never raises — every failure mode returns False.

False-positive guards:
  · Offline is declared only after CONSECUTIVE failed probes (default 2).
    One failed resolution is normal jitter.
  · Online returns on the FIRST successful probe, no confirmation streak.
    Being slow to resume costs cycles for nothing — the point of the
    module is to save cycles, not to hoard them.

Kill-switch:
  · CONNECTIVITY_CHECK_ENABLED=false disables the probe entirely; the
    caller then behaves byte-identically to pre-guard code.
  · Failure mode considered: a getaddrinfo that neither returns nor
    raises would freeze the loop. Bounded by asyncio.wait_for(timeout)
    which cancels the underlying thread's future — worst case we leak
    one thread per stuck probe (bounded by OS thread quota, not a
    correctness problem). Never able to freeze the loop.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass, field


# Neutral probe hosts — public DNS operator names, never a Morgoth rail
# API. Two so a single dead resolver doesn't trigger a false positive.
# Cloudflare + Google — different infra, different anycast networks;
# both being unreachable is a real host-side outage.
_PROBE_HOSTS: tuple[str, ...] = ("one.one.one.one", "dns.google")

# Env-tunable knobs — read at call time (not at import) so tests and
# operators can flip without a restart.
def _flag_enabled() -> bool:
    raw = os.environ.get("CONNECTIVITY_CHECK_ENABLED", "true").strip().lower()
    return raw not in ("false", "0", "no", "off")


def _probe_timeout_secs() -> float:
    raw = os.environ.get("CONNECTIVITY_PROBE_TIMEOUT_SECS", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 2.0


def _probe_interval_secs() -> float:
    """Sleep between probes when offline — the cheap-poll cadence."""
    raw = os.environ.get("CONNECTIVITY_PROBE_INTERVAL_SECS", "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 30.0


def _offline_streak_threshold() -> int:
    raw = os.environ.get("CONNECTIVITY_OFFLINE_STREAK", "").strip()
    if raw:
        try:
            v = int(raw)
            if v >= 1:
                return v
        except ValueError:
            pass
    return 2


async def _resolve_one(host: str, timeout_secs: float) -> bool:
    """DNS-resolve `host` in a thread, bounded by `timeout_secs`.

    Returns True on any successful resolution, False on any failure
    (timeout, gaierror, cancellation, unexpected exception). Never raises.
    """
    try:
        await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None),
            timeout=timeout_secs,
        )
        return True
    except (asyncio.TimeoutError, socket.gaierror, OSError, Exception):
        return False


async def probe_once() -> bool:
    """Return True iff AT LEAST ONE probe host resolved. Two-host quorum
    of the loose kind — one is enough because a single dead resolver
    already accounts for most false-positive risk."""
    timeout = _probe_timeout_secs()
    results = await asyncio.gather(
        *[_resolve_one(h, timeout) for h in _PROBE_HOSTS],
        return_exceptions=False,
    )
    return any(results)


@dataclass
class ConnectivityMonitor:
    """Rolling state — one instance per running cycle loop.

    Transition semantics:
      · online → offline: after `_offline_streak_threshold()` consecutive
        failed probes.
      · offline → online: on the first successful probe (no streak needed).
    """
    is_online: bool = True
    consecutive_failures: int = 0
    last_transition_at: float = field(default_factory=time.monotonic)

    async def update(self) -> str | None:
        """Run one probe, update state, return a transition kind:
          · 'online→offline' when we just flipped to offline
          · 'offline→online' when we just recovered
          · None when state is unchanged
        Also returns None immediately when the kill-switch is off."""
        if not _flag_enabled():
            return None
        ok = await probe_once()
        if ok:
            if not self.is_online:
                self.is_online = True
                self.consecutive_failures = 0
                self.last_transition_at = time.monotonic()
                return "offline→online"
            self.consecutive_failures = 0
            return None
        # Probe failed
        self.consecutive_failures += 1
        if self.is_online and self.consecutive_failures >= _offline_streak_threshold():
            self.is_online = False
            self.last_transition_at = time.monotonic()
            return "online→offline"
        return None

    def offline_duration_secs(self) -> float:
        """Wall time elapsed since the last transition — meaningful only
        when is_online is False, but the value is available in both states."""
        return max(0.0, time.monotonic() - self.last_transition_at)
