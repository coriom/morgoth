"""Provider heartbeat + memory-pressure sampling.

Contract under test:
  · Heartbeat NEVER calls a paid endpoint. api-key probe is PRESENCE only.
  · Heartbeat persists ONLY on state change — a stable OK loop writes
    zero rows.
  · Heartbeat is non-fatal — a probe exception yields status='down',
    never propagates.
  · Resource classifier hits the recorded-incident values as THRASHING.
  · TIGHT is the leading edge (any swap in use / low RAM headroom).
  · No auto-throttle / auto-switch anywhere (grep-lock).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from analysis import resources as R
from core.llm import heartbeat as HB


# ═════════════════════════════════════════════════════════════════════
# HEARTBEAT — transition-only + no-paid + non-fatal
# ═════════════════════════════════════════════════════════════════════


class _AsyncCtx:
    def __init__(self, conn): self._c = conn
    async def __aenter__(self): return self._c
    async def __aexit__(self, *a): return False


@pytest.mark.asyncio
async def test_persist_on_change_writes_zero_rows_when_state_unchanged():
    """A steady-state OK loop writes ZERO rows. 144 identical rows/day
    would be noise; transition-only is the whole point."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = MagicMock(); pm._require_pool = MagicMock(return_value=pool)
    prior = {"ollama": "ok", "claude-cli": "ok", "api": "down"}
    now = [
        HB.ProbeResult("ollama", "ok", "healthy"),
        HB.ProbeResult("claude-cli", "ok", "healthy"),
        HB.ProbeResult("api", "down", "no key"),
    ]
    updated = await HB.persist_on_change(pm, prior, now)
    assert updated == prior
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_persist_on_change_writes_only_transitioned_rows():
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = MagicMock(); pm._require_pool = MagicMock(return_value=pool)
    prior = {"ollama": "ok", "claude-cli": "ok", "api": "down"}
    now = [
        HB.ProbeResult("ollama", "ok", "healthy"),         # unchanged
        HB.ProbeResult("claude-cli", "down", "PATH gone"), # TRANSITION
        HB.ProbeResult("api", "down", "still no key"),     # unchanged
    ]
    updated = await HB.persist_on_change(pm, prior, now)
    assert conn.execute.await_count == 1
    args = conn.execute.await_args.args
    assert "provider_health" in args[0]
    assert args[1] == "claude-cli" and args[2] == "down"
    assert updated["claude-cli"] == "down"


@pytest.mark.asyncio
async def test_persist_on_change_first_run_writes_every_provider():
    """Empty prior state means every probe is a transition (new→status).
    First run establishes the baseline; subsequent runs use it."""
    conn = MagicMock(); conn.execute = AsyncMock()
    pool = MagicMock(); pool.acquire = MagicMock(return_value=_AsyncCtx(conn))
    pm = MagicMock(); pm._require_pool = MagicMock(return_value=pool)
    now = [
        HB.ProbeResult("ollama", "ok", "healthy"),
        HB.ProbeResult("claude-cli", "ok", "healthy"),
        HB.ProbeResult("api", "down", "no key"),
    ]
    updated = await HB.persist_on_change(pm, {}, now)
    assert conn.execute.await_count == 3
    assert updated == {"ollama": "ok", "claude-cli": "ok", "api": "down"}


def test_probe_api_key_never_reads_value(monkeypatch):
    """PRESENCE only — even when the key is present, its VALUE must not
    appear in the ProbeResult.detail (which is persisted + logged)."""
    secret = "sk-ant-CANARY-VALUE-DO-NOT-LEAK"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    r = HB.probe_api_key()
    assert r.status == "ok"
    assert secret not in r.detail
    assert secret not in repr(r)


def test_grep_lock_no_paid_call_in_heartbeat():
    """Heartbeat must never make a paid API request. Grep-lock: no
    'api.anthropic.com' or '/v1/messages' string in the module."""
    src = Path("core/llm/heartbeat.py").read_text()
    assert "api.anthropic.com" not in src
    assert "/v1/messages" not in src


def test_heartbeat_interval_default_is_10min(monkeypatch):
    monkeypatch.delenv("PROVIDER_HEARTBEAT_MINUTES", raising=False)
    assert HB.heartbeat_interval_secs() == 600


def test_heartbeat_interval_floor_at_60s(monkeypatch):
    """A too-small interval could hammer the providers; enforce a floor."""
    monkeypatch.setenv("PROVIDER_HEARTBEAT_MINUTES", "0")
    assert HB.heartbeat_interval_secs() >= 60


# ═════════════════════════════════════════════════════════════════════
# RESOURCE CLASSIFIER — thresholds justified against the incident
# ═════════════════════════════════════════════════════════════════════


class TestResourceClassifier:
    def test_recorded_incident_is_thrashing(self):
        """The 2026-09-08 host incident: 7.6 GB total, swap 61 % used,
        LAV 77.9 on 12 threads, CPU ~0 %. Must classify as THRASHING —
        this is the failure mode the sampler exists to expose."""
        klass, reason = R.classify_sample(
            ram_available_pct=0.10,  # tight-ish RAM
            swap_pct=0.61,           # 61 % swap
            lav_ratio=77.9 / 12,     # ≈ 6.5
            cpu_idle_pct=1.0,        # ~100 % idle
        )
        assert klass == "THRASHING"
        assert "swap" in reason

    def test_ok_when_none_of_the_conditions_hit(self):
        klass, _ = R.classify_sample(
            ram_available_pct=0.60, swap_pct=0.0,
            lav_ratio=0.5, cpu_idle_pct=0.8,
        )
        assert klass == "OK"

    def test_tight_on_swap_in_use_alone(self):
        klass, reason = R.classify_sample(
            ram_available_pct=0.60, swap_pct=0.01,
            lav_ratio=0.5, cpu_idle_pct=0.8,
        )
        assert klass == "TIGHT" and "swap" in reason

    def test_tight_on_low_ram_alone(self):
        klass, reason = R.classify_sample(
            ram_available_pct=0.10, swap_pct=0.0,
            lav_ratio=0.5, cpu_idle_pct=0.8,
        )
        assert klass == "TIGHT" and "avail RAM" in reason

    def test_thrashing_requires_all_three_conditions(self):
        """High LAV alone is NOT thrashing — a heavy build has high LAV
        with high CPU utilisation. The signature is all three at once."""
        # LAV high but CPU actually busy → TIGHT at most, never THRASHING.
        klass, _ = R.classify_sample(
            ram_available_pct=0.30, swap_pct=0.0,
            lav_ratio=10.0, cpu_idle_pct=0.05,  # 95 % utilisation
        )
        assert klass != "THRASHING"

    def test_boundary_at_thresholds(self):
        # Exactly at TIGHT_AVAILABLE_PCT (0.15) → OK (strict <).
        assert R.classify_sample(0.15, 0.0, 0.5, 0.8)[0] == "OK"
        # Just under → TIGHT.
        assert R.classify_sample(0.149, 0.0, 0.5, 0.8)[0] == "TIGHT"


class TestSampleNow:
    def test_sample_now_never_raises(self):
        """Even if /proc reads fail, sample_now returns a Sample with
        conservative zeros — never propagates."""
        s = R.sample_now()
        assert s.classification in ("OK", "TIGHT", "THRASHING")
        assert s.cpu_count >= 1


class TestSummarizeWindow:
    def test_empty_returns_baseline(self):
        r = R.summarize_window([])
        assert r["n"] == 0 and r["worst"] == "OK"

    def test_worst_is_thrashing_when_any_thrashing(self):
        s_ok = R.ResourceSample("t", 8000, 6000, 2000, 0, 0.5, 8, 0.9, "OK", "")
        s_thrash = R.ResourceSample("t", 8000, 200, 2000, 1500, 8.0, 8, 0.95,
                                    "THRASHING", "swap")
        r = R.summarize_window([s_ok, s_thrash, s_ok])
        assert r["worst"] == "THRASHING"
        assert r["thrashing"] == 1


# ═════════════════════════════════════════════════════════════════════
# NOT-DOING contract — grep-lock the design commitment
# ═════════════════════════════════════════════════════════════════════


def test_module_docstring_states_no_auto_throttle():
    """analysis/resources.py's module docstring must EXPLICITLY commit
    to observation-only. A future refactor that adds auto-throttle
    would break this test first."""
    src = Path("analysis/resources.py").read_text()
    assert "NO auto-throttling" in src
    assert "NO auto-pausing" in src
    assert "NO auto-provider-switch" in src


def test_brain_does_not_switch_provider_on_heartbeat_failure():
    """Grep-negative: the cycle loop must not call anything like
    'switch_provider' or 'set_provider' when a heartbeat fails.
    The call-time fallback ladder (core/llm/fallback.py) handles a
    dead provider VISIBLY — heartbeat only observes."""
    src = Path("core/brain.py").read_text()
    for forbidden in ("switch_provider(", "set_provider(", "reroute_task(",
                      "auto_throttle", "pause_cycles"):
        assert forbidden not in src, (
            f"{forbidden!r} appears in brain.py — heartbeat MUST NOT "
            f"auto-react to provider state"
        )
