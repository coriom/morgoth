"""Pre-cycle connectivity probe: two-host DNS resolution, false-positive
guards, kill-switch, and structural fences on the brain-loop wiring."""

from __future__ import annotations

import asyncio
import os
import socket
from unittest.mock import AsyncMock, patch

import pytest

from core import connectivity as cx


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # Every test starts from a clean env — kill-switch defaults etc.
    for k in ("CONNECTIVITY_CHECK_ENABLED", "CONNECTIVITY_PROBE_TIMEOUT_SECS",
              "CONNECTIVITY_PROBE_INTERVAL_SECS", "CONNECTIVITY_OFFLINE_STREAK"):
        monkeypatch.delenv(k, raising=False)


class TestKnobs:
    def test_flag_enabled_by_default(self):
        assert cx._flag_enabled() is True

    @pytest.mark.parametrize("val", ["false", "FALSE", "0", "no", "off"])
    def test_flag_disabled_by_env(self, monkeypatch, val):
        monkeypatch.setenv("CONNECTIVITY_CHECK_ENABLED", val)
        assert cx._flag_enabled() is False

    def test_default_streak_is_two(self):
        assert cx._offline_streak_threshold() == 2

    def test_env_streak_override(self, monkeypatch):
        monkeypatch.setenv("CONNECTIVITY_OFFLINE_STREAK", "4")
        assert cx._offline_streak_threshold() == 4

    def test_env_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("CONNECTIVITY_OFFLINE_STREAK", "not-int")
        assert cx._offline_streak_threshold() == 2

    def test_probe_interval_default_thirty_seconds(self):
        assert cx._probe_interval_secs() == 30.0


@pytest.mark.asyncio
class TestResolveOne:
    async def test_success_returns_true(self):
        with patch.object(cx.asyncio, "to_thread",
                           AsyncMock(return_value=[("family", "sock", 0, "", ("1.1.1.1", 0))])):
            assert await cx._resolve_one("one.one.one.one", 2.0) is True

    async def test_gaierror_returns_false(self):
        async def _raise():
            raise socket.gaierror("nope")
        # Route to_thread through an awaitable that raises.
        async def _fake_to_thread(*a, **kw):
            raise socket.gaierror("nope")
        with patch.object(cx.asyncio, "to_thread", _fake_to_thread):
            assert await cx._resolve_one("x", 2.0) is False

    async def test_timeout_returns_false(self):
        async def _slow(*a, **kw):
            await asyncio.sleep(5)
            return []
        with patch.object(cx.asyncio, "to_thread", _slow):
            assert await cx._resolve_one("x", 0.05) is False


@pytest.mark.asyncio
class TestProbeOnce:
    async def test_at_least_one_success_makes_probe_ok(self):
        # Cloudflare fails, Google succeeds → probe is OK.
        results_iter = iter([False, True])
        async def _one(*a, **kw):
            return next(results_iter)
        with patch.object(cx, "_resolve_one", _one):
            assert await cx.probe_once() is True

    async def test_both_fail_makes_probe_fail(self):
        async def _one(*a, **kw): return False
        with patch.object(cx, "_resolve_one", _one):
            assert await cx.probe_once() is False


@pytest.mark.asyncio
class TestMonitorTransitions:
    async def test_single_failure_stays_online(self):
        mon = cx.ConnectivityMonitor()
        with patch.object(cx, "probe_once", AsyncMock(return_value=False)):
            t = await mon.update()
        assert t is None
        assert mon.is_online is True
        assert mon.consecutive_failures == 1

    async def test_two_consecutive_failures_flip_to_offline(self):
        mon = cx.ConnectivityMonitor()
        with patch.object(cx, "probe_once", AsyncMock(return_value=False)):
            await mon.update()
            t = await mon.update()
        assert t == "online→offline"
        assert mon.is_online is False

    async def test_first_success_flips_back_to_online_immediately(self):
        mon = cx.ConnectivityMonitor()
        # Two failures → offline.
        with patch.object(cx, "probe_once", AsyncMock(return_value=False)):
            await mon.update()
            await mon.update()
        assert mon.is_online is False
        # First success → immediate online, no streak needed.
        with patch.object(cx, "probe_once", AsyncMock(return_value=True)):
            t = await mon.update()
        assert t == "offline→online"
        assert mon.is_online is True
        assert mon.consecutive_failures == 0

    async def test_success_while_online_produces_no_transition(self):
        mon = cx.ConnectivityMonitor()
        with patch.object(cx, "probe_once", AsyncMock(return_value=True)):
            assert await mon.update() is None
        assert mon.is_online is True

    async def test_failure_after_recovery_starts_new_streak(self):
        mon = cx.ConnectivityMonitor()
        # Two fails, then success, then another single fail → still online.
        with patch.object(cx, "probe_once", AsyncMock(return_value=False)):
            await mon.update()
            await mon.update()
        with patch.object(cx, "probe_once", AsyncMock(return_value=True)):
            await mon.update()
        assert mon.consecutive_failures == 0
        with patch.object(cx, "probe_once", AsyncMock(return_value=False)):
            t = await mon.update()
        assert t is None  # streak == 1, still below threshold
        assert mon.is_online is True

    async def test_kill_switch_short_circuits(self, monkeypatch):
        monkeypatch.setenv("CONNECTIVITY_CHECK_ENABLED", "false")
        mon = cx.ConnectivityMonitor()
        # probe_once MUST NOT be called when the flag is off.
        with patch.object(cx, "probe_once",
                           AsyncMock(side_effect=AssertionError("probe called with flag off"))):
            t = await mon.update()
        assert t is None
        assert mon.is_online is True

    async def test_higher_streak_threshold_via_env(self, monkeypatch):
        monkeypatch.setenv("CONNECTIVITY_OFFLINE_STREAK", "3")
        mon = cx.ConnectivityMonitor()
        with patch.object(cx, "probe_once", AsyncMock(return_value=False)):
            assert await mon.update() is None  # 1
            assert await mon.update() is None  # 2 — still online at threshold=3
            assert mon.is_online is True
            assert await mon.update() == "online→offline"


class TestBrainWiringGrepLocks:
    def test_brain_imports_connectivity_module(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "from core.connectivity import" in src
        assert "ConnectivityMonitor" in src
        assert "_flag_enabled" in src or "_cx_enabled" in src

    def test_offline_branch_skips_the_cycle(self):
        # Structural: the offline branch MUST `continue` without touching
        # claim_next_objective. Grep-lock protects the intent.
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        offline_block_start = src.find("if not _connectivity.is_online")
        assert offline_block_start >= 0
        # The offline block must reach `continue` before claim_next_objective.
        claim_pos = src.find("claim_next_objective", offline_block_start)
        continue_pos = src.find("continue", offline_block_start)
        assert continue_pos > 0
        assert continue_pos < claim_pos, (
            "offline branch must `continue` BEFORE claim_next_objective is reached"
        )

    def test_transition_persistence_is_only_on_flips(self):
        # record_connectivity_transition is called ONLY inside the transition
        # branches, never on every probe.
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert src.count("record_connectivity_transition") == 2

    def test_session_report_renders_connectivity_line(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.connectivity_state = "online"
        r.connectivity_outages = 3
        r.connectivity_offline_seconds = 240  # 4 minutes
        out = r.render()
        assert "CONNECTIVITY" in out
        assert "online" in out
        assert "3 outages" in out
        assert "total 4m offline" in out

    def test_session_report_renders_offline_state(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.connectivity_state = "offline"
        out = r.render()
        assert "CONNECTIVITY             : offline" in out
