"""Session-gap detection: signal source, threshold, pair_spans_gap,
contradiction-detector wiring, backtest series_gap SKIP path."""

from __future__ import annotations

import inspect
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import session_gap as sg


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("SESSION_GAP_THRESHOLD_SECS", raising=False)


class TestKnobs:
    def test_default_threshold_is_30_minutes(self):
        assert sg.gap_threshold_secs() == 30 * 60

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("SESSION_GAP_THRESHOLD_SECS", "3600")
        assert sg.gap_threshold_secs() == 3600

    def test_below_min_env_falls_back(self, monkeypatch):
        # A 10 s threshold would fire on every restart; refused.
        monkeypatch.setenv("SESSION_GAP_THRESHOLD_SECS", "10")
        assert sg.gap_threshold_secs() == 1800


@pytest.mark.asyncio
class TestComputeAndRecordGap:
    def _pm_with_last_observed(self, last_dt):
        pm = MagicMock()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"observed_at": last_dt} if last_dt else None)
        conn.execute = AsyncMock()

        class _AsyncCtx:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return None

        pool = MagicMock()
        pool.acquire = MagicMock(return_value=_AsyncCtx())
        pm._require_pool = MagicMock(return_value=pool)
        return pm, conn

    async def test_no_metric_series_row_returns_none(self):
        pm, conn = self._pm_with_last_observed(None)
        assert await sg.compute_and_record_gap(pm) is None
        conn.execute.assert_not_called()

    async def test_sub_threshold_records_nothing(self):
        # 10 min ago → below default 30 min threshold (minus 15 min recorder
        # interval → effective threshold ~30 min). No row written.
        last = datetime.now(timezone.utc) - timedelta(minutes=10)
        pm, conn = self._pm_with_last_observed(last)
        assert await sg.compute_and_record_gap(pm) is None
        conn.execute.assert_not_called()

    async def test_over_threshold_inserts_and_returns_row(self):
        last = datetime.now(timezone.utc) - timedelta(hours=3)
        pm, conn = self._pm_with_last_observed(last)
        got = await sg.compute_and_record_gap(pm)
        assert got is not None
        assert got["duration_secs"] > 0
        conn.execute.assert_awaited_once()

    async def test_probe_failure_is_non_fatal(self):
        pm = MagicMock()
        pm._require_pool = MagicMock(side_effect=RuntimeError("db down"))
        # Must not raise.
        assert await sg.compute_and_record_gap(pm) is None


class TestPairSpansGap:
    def _dt(self, h): return datetime(2026, 9, 16, h, 0, tzinfo=timezone.utc)

    def test_no_gaps_returns_false(self):
        assert sg.pair_spans_gap(self._dt(10), self._dt(12), []) is False

    def test_gap_between_the_two_returns_true(self):
        gaps = [(self._dt(9), self._dt(11))]
        assert sg.pair_spans_gap(self._dt(8), self._dt(12), gaps) is True

    def test_gap_entirely_before_pair_returns_false(self):
        gaps = [(self._dt(1), self._dt(3))]
        assert sg.pair_spans_gap(self._dt(10), self._dt(12), gaps) is False

    def test_gap_entirely_after_pair_returns_false(self):
        gaps = [(self._dt(20), self._dt(22))]
        assert sg.pair_spans_gap(self._dt(10), self._dt(12), gaps) is False

    def test_order_independent(self):
        # ts_a > ts_b must still work.
        gaps = [(self._dt(9), self._dt(11))]
        assert sg.pair_spans_gap(self._dt(12), self._dt(8), gaps) is True


class TestTsInsideGap:
    def _dt(self, h): return datetime(2026, 9, 16, h, 0, tzinfo=timezone.utc)

    def test_ts_inside_returns_true(self):
        assert sg.ts_inside_gap(self._dt(10), [(self._dt(8), self._dt(12))]) is True

    def test_ts_before_gap_returns_false(self):
        assert sg.ts_inside_gap(self._dt(6), [(self._dt(8), self._dt(12))]) is False

    def test_ts_after_gap_returns_false(self):
        assert sg.ts_inside_gap(self._dt(14), [(self._dt(8), self._dt(12))]) is False

    def test_no_gaps_returns_false(self):
        assert sg.ts_inside_gap(self._dt(10), []) is False


class TestDetectorWiring:
    def test_brain_loads_gap_cache_once_per_detector_run(self):
        from core import brain
        src = inspect.getsource(brain.Brain.detect_contradictions)
        # Cache load exists AND happens before the pairwise loop.
        assert "load_gaps" in src
        cache_pos = src.find("_thesis_gap_cache = await")
        loop_pos = src.find("for group in groups")
        assert 0 < cache_pos < loop_pos

    def test_detector_uses_pair_spans_gap_in_supersession_branch(self):
        from core import brain
        src = inspect.getsource(brain.Brain.detect_contradictions)
        assert "pair_spans_gap" in src
        # The gap-spanning check must OR with the window check — same
        # supersession codepath, not a new one.
        assert "gap >= window_seconds or _spans_gap" in src

    def test_startup_calls_compute_and_record_gap_before_first_cycle(self):
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        cg_pos = src.find("compute_and_record_gap")
        while_pos = src.find("while True:")
        assert 0 < cg_pos < while_pos


class TestSessionReportGaps:
    def test_report_renders_gap_line_zero(self):
        from analysis.session_report import SessionReport
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        out = r.render()
        assert "SESSION GAPS" in out
        assert "0 gaps" in out

    def test_report_renders_gap_line_with_data(self):
        from analysis.session_report import SessionReport
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.session_gaps = 3
        r.session_gaps_longest_secs = 7200  # 2.0h
        r.session_gaps_last_resume = "2026-09-16T10:00+00:00"
        out = r.render()
        assert "3 gaps" in out
        assert "longest 2.0h" in out
        assert "2026-09-16T10:00+00:00" in out


class TestBacktestSeriesGapSkip:
    def test_backtest_cli_wires_series_gap_reason(self):
        import inspect
        from scripts import backtest_theses_descriptive as mod
        src = inspect.getsource(mod.main)
        # The three local-recorder metrics are named in a tuple; the
        # skip reason is series_gap; ts_inside_gap is consulted.
        assert "_LOCAL_METRICS" in src
        assert "series_gap" in src
        assert "ts_inside_gap" in src or "_in_gap" in src
