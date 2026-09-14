"""Network-outage guard: classifier + trigger predicate.

The guard must fire on all-network-failure cycles and MUST NOT fire
when any call succeeded or when any failure was non-network (e.g. a
KeyError from a missing required arg — that's a model problem, not
an outage). The threshold + env override are exercised too.
"""

from __future__ import annotations

import os

import pytest

from core.outage_guard import (
    classify_failure,
    cycle_is_all_network_outage,
    outage_abort_cycles,
)


class TestClassifyFailure:
    @pytest.mark.parametrize("txt", [
        "[Errno -3] Temporary failure in name resolution",
        "api.coinpaprika.com request failed: [Errno -3] Temporary failure in name resolution",
        "getaddrinfo failed",
        "nodename nor servname provided",
        "No address associated with hostname",
        "Connection refused",
        "Connection reset by peer",
        "Connection aborted.",
        "connect timeout",
        "Read timeout on GET https://x",
        "Network is unreachable",
        "No route to host",
        "Operation timed out",
    ])
    def test_known_network_patterns_classify_as_network(self, txt):
        assert classify_failure(txt) == "network"

    @pytest.mark.parametrize("txt", [
        "'symbol'",                       # KeyError from missing arg
        "'query'",                        # KeyError from missing arg
        "HTTP 400 bad request",           # 4xx
        "HTTP 500 internal server error", # 5xx
        "invalid JSON in response body",
        "",                               # empty
        None,                             # None
    ])
    def test_non_network_errors_classify_as_other(self, txt):
        assert classify_failure(txt) == "other"


class TestCycleIsAllNetworkOutage:
    def _fail(self, err):
        return {"tool": "x", "result": {"success": False, "error": err}}

    def _ok(self):
        return {"tool": "x", "result": {"success": True, "result": {}}}

    def test_all_network_failures_triggers(self):
        cycle = [
            self._fail("[Errno -3] Temporary failure in name resolution"),
            self._fail("Connection refused"),
        ]
        is_out, sample = cycle_is_all_network_outage(cycle)
        assert is_out is True
        assert len(sample) == 2

    def test_one_success_disqualifies(self):
        cycle = [
            self._ok(),
            self._fail("[Errno -3] Temporary failure in name resolution"),
        ]
        is_out, sample = cycle_is_all_network_outage(cycle)
        assert is_out is False
        assert sample == []

    def test_one_malformed_arg_disqualifies_the_whole_cycle(self):
        # A KeyError('symbol') mid-cycle proves the model got at least
        # one call wrong — that's a model problem, not an outage. Guard
        # must not fire.
        cycle = [
            self._fail("[Errno -3] Temporary failure in name resolution"),
            self._fail("'symbol'"),  # KeyError from missing required arg
            self._fail("Connection refused"),
        ]
        is_out, sample = cycle_is_all_network_outage(cycle)
        assert is_out is False

    def test_zero_calls_not_an_outage(self):
        # A cycle where the model narrated instead of calling any tool
        # is a different pathology — not an outage; the guard has
        # nothing to classify.
        is_out, sample = cycle_is_all_network_outage([])
        assert is_out is False
        assert sample == []

    def test_all_http_errors_do_not_count(self):
        # 5xx from an upstream API is NOT a network outage — the DNS
        # resolved, the connection opened. Requeuing the objective on
        # 5xx would be wrong (targeted API problem, not global).
        cycle = [
            self._fail("HTTP 502 bad gateway"),
            self._fail("HTTP 500 internal error"),
        ]
        is_out, sample = cycle_is_all_network_outage(cycle)
        assert is_out is False

    def test_missing_success_key_defaults_to_true(self):
        # Defensive: a tool wrapper that returns dict without 'success'
        # is treated as success (existing brain.py behaviour). Guard
        # must inherit that default, else it fires on legitimate calls.
        cycle = [{"tool": "x", "result": {"result": "ok"}}]
        is_out, _ = cycle_is_all_network_outage(cycle)
        assert is_out is False


class TestOutageAbortCycles:
    def test_default_is_two(self):
        os.environ.pop("MORGOTH_OUTAGE_ABORT_CYCLES", None)
        assert outage_abort_cycles() == 2

    def test_env_override_positive(self):
        os.environ["MORGOTH_OUTAGE_ABORT_CYCLES"] = "5"
        try:
            assert outage_abort_cycles() == 5
        finally:
            del os.environ["MORGOTH_OUTAGE_ABORT_CYCLES"]

    def test_env_override_invalid_falls_back_to_default(self):
        os.environ["MORGOTH_OUTAGE_ABORT_CYCLES"] = "not-an-int"
        try:
            assert outage_abort_cycles() == 2
        finally:
            del os.environ["MORGOTH_OUTAGE_ABORT_CYCLES"]


class TestBrainWiringGrepLocks:
    """Structural fences — the wiring in brain.py must call the guard
    functions with the expected shape. A rename or accidental deletion
    breaks these tests loudly."""

    def test_brain_imports_and_calls_outage_helpers(self):
        import inspect
        from core import brain
        src = inspect.getsource(brain)
        assert "from core.outage_guard import" in src
        assert "cycle_is_all_network_outage" in src
        assert "outage_abort_cycles" in src
        assert "increment_outage_streak" in src
        assert "requeue_objective_after_outage" in src
        assert "reset_outage_streak" in src
        assert "record_outage_event" in src

    def test_persistent_memory_exposes_outage_methods(self):
        from memory.persistent import PersistentMemory
        for name in ("increment_outage_streak", "reset_outage_streak",
                     "requeue_objective_after_outage", "record_outage_event"):
            assert hasattr(PersistentMemory, name), name

    def test_session_report_renders_outage_line(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        r.network_outages = 3
        r.network_outages_cycles_saved = 7
        out = r.render()
        assert "NETWORK OUTAGES" in out
        assert "3 events" in out
        assert "7 cycles saved" in out

    def test_session_report_none_line_when_zero(self):
        from analysis.session_report import SessionReport
        from datetime import datetime, timedelta, timezone
        r = SessionReport(
            since=datetime.now(timezone.utc) - timedelta(hours=1),
            now=datetime.now(timezone.utc),
        )
        out = r.render()
        assert "NETWORK OUTAGES          : none" in out
