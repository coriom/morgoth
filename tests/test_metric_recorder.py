"""Metric recorder: extraction, snapshot, wiring, backtest classification.

FORWARD-ONLY sensitivity: theses older than the recorder's first sample
must SKIP, never fabricate. This suite locks that + the offline-skip
invariant + the classifier changes that promote dominance / global-cap
/ global-volume from unreachable to reachable.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from core import metric_recorder as mr
from analysis.thesis_backtest_descriptive import classify_subject


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for k in ("METRIC_RECORDER_ENABLED", "METRIC_RECORDER_INTERVAL_SECS"):
        monkeypatch.delenv(k, raising=False)


class TestKnobs:
    def test_enabled_by_default(self):
        assert mr.recorder_enabled() is True

    @pytest.mark.parametrize("v", ["false", "0", "no", "off"])
    def test_disabled_by_env(self, monkeypatch, v):
        monkeypatch.setenv("METRIC_RECORDER_ENABLED", v)
        assert mr.recorder_enabled() is False

    def test_default_interval_is_15_min(self):
        assert mr.snapshot_interval_secs() == 900

    def test_env_interval_override(self, monkeypatch):
        monkeypatch.setenv("METRIC_RECORDER_INTERVAL_SECS", "300")
        assert mr.snapshot_interval_secs() == 300

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("METRIC_RECORDER_INTERVAL_SECS", "not-int")
        assert mr.snapshot_interval_secs() == 900

    def test_below_min_env_falls_back(self, monkeypatch):
        # < 60 s is refused — CoinPaprika free tier is generous but not
        # infinite; the recorder is a nightly-batch style cadence, not
        # a poller.
        monkeypatch.setenv("METRIC_RECORDER_INTERVAL_SECS", "10")
        assert mr.snapshot_interval_secs() == 900


class TestExtractMetrics:
    def test_extracts_all_three_from_flat_payload(self):
        result = {
            "market_cap_usd": 2.7e12, "volume_24h_usd": 1.6e11,
            "bitcoin_dominance_percentage": 55.9, "cryptocurrencies_number": 12639,
        }
        got = dict(mr.extract_metrics(result))
        assert got["btc_dominance"] == 55.9
        assert got["global_market_cap"] == 2.7e12
        assert got["global_volume_24h"] == 1.6e11

    def test_unwraps_success_envelope(self):
        wrapped = {"success": True, "result": {"bitcoin_dominance_percentage": 55.9}}
        assert dict(mr.extract_metrics(wrapped)) == {"btc_dominance": 55.9}

    def test_missing_fields_are_skipped_not_fabricated(self):
        # Only one field present → only one metric emitted.
        assert dict(mr.extract_metrics({"market_cap_usd": 1e12})) == {"global_market_cap": 1e12}

    def test_non_dict_returns_empty(self):
        assert mr.extract_metrics(None) == []
        assert mr.extract_metrics("42") == []

    def test_bad_type_value_skipped(self):
        # A non-numeric value must NOT crash — skipped with no row.
        result = {"bitcoin_dominance_percentage": "n/a", "market_cap_usd": 1e12}
        got = dict(mr.extract_metrics(result))
        assert "btc_dominance" not in got
        assert got["global_market_cap"] == 1e12


class TestScheduleState:
    def test_snapshot_due_on_first_call(self):
        s = mr.ScheduleState()
        assert s.snapshot_due(now_ts=1000.0) is True

    def test_not_due_within_interval(self):
        s = mr.ScheduleState()
        s.mark_snapshot(now_ts=1000.0)
        assert s.snapshot_due(now_ts=1000.0 + 899) is False

    def test_due_after_interval_elapsed(self):
        s = mr.ScheduleState()
        s.mark_snapshot(now_ts=1000.0)
        assert s.snapshot_due(now_ts=1000.0 + 901) is True


@pytest.mark.asyncio
class TestSnapshotOnce:
    async def test_writes_three_rows_on_success(self):
        router = MagicMock()
        router.execute_tool = AsyncMock(return_value={
            "success": True, "result": {
                "market_cap_usd": 2.7e12, "volume_24h_usd": 1.6e11,
                "bitcoin_dominance_percentage": 55.9,
            },
        })
        pm = MagicMock()
        pm.record_metric_sample = AsyncMock()
        n = await mr.snapshot_once(pm, router)
        assert n == 3
        assert pm.record_metric_sample.await_count == 3

    async def test_tool_failure_is_non_fatal(self):
        router = MagicMock()
        router.execute_tool = AsyncMock(return_value={"success": False, "error": "429"})
        pm = MagicMock(); pm.record_metric_sample = AsyncMock()
        assert await mr.snapshot_once(pm, router) == 0
        pm.record_metric_sample.assert_not_called()

    async def test_tool_raises_is_non_fatal(self):
        router = MagicMock()
        router.execute_tool = AsyncMock(side_effect=RuntimeError("boom"))
        pm = MagicMock(); pm.record_metric_sample = AsyncMock()
        # Must not raise.
        assert await mr.snapshot_once(pm, router) == 0

    async def test_insert_failure_counts_only_successes(self):
        # Partial insert failure: two land, one raises — count is 2, no crash.
        router = MagicMock()
        router.execute_tool = AsyncMock(return_value={
            "success": True, "result": {
                "bitcoin_dominance_percentage": 55.9, "market_cap_usd": 2.7e12,
                "volume_24h_usd": 1.6e11,
            },
        })
        pm = MagicMock()
        pm.record_metric_sample = AsyncMock(
            side_effect=[None, RuntimeError("db"), None],
        )
        assert await mr.snapshot_once(pm, router) == 2


class TestClassifierPromotion:
    """The three metrics used to be UNVERIFIABLE-UNREACHABLE. With the
    recorder wired they must classify as metric/<kind>."""

    def test_dominance_now_maps(self):
        v, m, _ = classify_subject("Bitcoin dominance")
        assert (v, m) == ("metric", "btc_dominance")

    def test_global_market_cap_now_maps(self):
        v, m, _ = classify_subject("Crypto global market cap 24h")
        assert (v, m) == ("metric", "global_market_cap")

    def test_global_volume_now_maps(self):
        v, m, _ = classify_subject("Crypto global market volume 24h")
        assert (v, m) == ("metric", "global_volume_24h")

    def test_btc_market_cap_still_maps_correctly(self):
        # Regression: adding global-cap markers must not steal specific
        # BTC market-cap subjects.
        v, m, _ = classify_subject("BTC market cap")
        assert (v, m) == ("metric", "btc_market_cap")


class TestBrainWiringGrepLocks:
    def test_brain_imports_and_gates_on_connectivity(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain.Brain.run_autonomous_cycle)
        assert "from core.metric_recorder import" in src
        assert "_mr_snapshot" in src
        assert "_connectivity.is_online" in src
        # Recorder call must sit inside the online gate.
        online_gate = src.find("_connectivity.is_online")
        snapshot_call = src.find("_mr_snapshot(")
        assert 0 < online_gate < snapshot_call, (
            "snapshot must run only when connectivity.is_online is truthy"
        )

    def test_persistent_memory_exposes_metric_series_methods(self):
        from memory.persistent import PersistentMemory
        assert hasattr(PersistentMemory, "record_metric_sample")
        assert hasattr(PersistentMemory, "fetch_metric_series")

    def test_docstring_states_forward_only(self):
        # Regression fence: the recorder must plainly say it does NOT
        # recover past theses. Any refactor that quietly drops the
        # statement fails this test.
        import inspect
        src = inspect.getsource(mr)
        assert "FORWARD-ONLY" in src
        assert "does NOT recover past theses" in src or "not recover past" in src.lower()
