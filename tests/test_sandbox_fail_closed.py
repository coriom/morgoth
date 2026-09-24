"""Sandbox FAIL-CLOSED enforcement — 2026-09-24.

Before this commit, gate_tests degraded to a WARNING and ran pytest
anyway when bwrap / unshare / systemd-run were unavailable. That's
how an LLM-authored data-feed proposal (d4d1cb8e, get_gemini_usdt_peg)
executed under `morgoth reflect --provider claude-cli` with
filesystem confinement OFF: "sandbox filesystem confinement
UNAVAILABLE (bwrap missing) — sandbox may read files outside its
copied tree". A warning is not a control.

Post-fix contract:
  · If ANY of the three confinement layers is unavailable, gate_tests
    lands the proposal at STATUS_REJECTED_SANDBOX_UNAVAILABLE without
    spawning pytest.
  · The `_run_pytest_in_sandbox` helper raises SandboxUnavailableError
    when called with a degraded posture — no fallback path exists.
  · `sandbox_posture()` is a cheap probe surfaced in `morgoth env`
    and `morgoth session-report` so degradation is visible without
    running reflect.
  · KeyboardInterrupt / asyncio.CancelledError during gate_tests
    marks the proposal ABORTED_INTERRUPTED and re-raises.
  · Stale /tmp/morgoth_sandbox/proposal_* are swept at reflect start.
  · No code path runs pytest on a proposal tree outside
    _run_pytest_in_sandbox (grep-lock).
"""

from __future__ import annotations

import asyncio
import inspect
import re
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import gates
from self_modify import proposals as P


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    gates.reset_probe_cache()
    yield
    gates.reset_probe_cache()


class TestBwrapProbeUsesRealBindSet:
    def test_probe_binds_lib_lib64_bin(self):
        # The probe MUST mirror the real invocation's bind set.
        # Earlier version bound only /usr; on usrmerge systems the
        # ELF interpreter is at /lib64 → probe silently returned False
        # while bwrap itself was fine. Grep-lock the fix.
        src = inspect.getsource(gates._bwrap_available)
        assert '"--ro-bind", "/lib", "/lib"' in src
        assert '"--ro-bind", "/lib64", "/lib64"' in src
        assert '"--ro-bind", "/bin", "/bin"' in src
        # Uses the absolute /bin/true so no PATH lookup can drift.
        assert '"/bin/true"' in src


class TestSandboxPosture:
    def test_all_layers_ok(self):
        with patch.object(gates, "_isolation_available", return_value=True), \
             patch.object(gates, "_bwrap_available", return_value=True), \
             patch.object(gates, "_cgroup_limits_available", return_value=True):
            p = gates.sandbox_posture()
            assert p["ok"] is True
            assert p["reason"] == ""

    def test_bwrap_missing_reports_reason(self):
        with patch.object(gates, "_isolation_available", return_value=True), \
             patch.object(gates, "_bwrap_available", return_value=False), \
             patch.object(gates, "_cgroup_limits_available", return_value=True):
            p = gates.sandbox_posture()
            assert p["ok"] is False
            assert "bwrap" in p["reason"]


class TestRunPytestRaisesOnDegraded:
    def test_raises_when_bwrap_missing(self, tmp_path):
        with patch.object(gates, "_isolation_available", return_value=True), \
             patch.object(gates, "_bwrap_available", return_value=False), \
             patch.object(gates, "_cgroup_limits_available", return_value=True), \
             patch.object(subprocess, "run") as run_mock:
            with pytest.raises(gates.SandboxUnavailableError):
                gates._run_pytest_in_sandbox(tmp_path)
            # CRITICAL: subprocess.run MUST NOT have been called —
            # we never spawn pytest on a degraded posture.
            run_mock.assert_not_called()

    def test_raises_when_isolation_missing(self, tmp_path):
        with patch.object(gates, "_isolation_available", return_value=False), \
             patch.object(subprocess, "run") as run_mock:
            with pytest.raises(gates.SandboxUnavailableError):
                gates._run_pytest_in_sandbox(tmp_path)
            run_mock.assert_not_called()


class TestGateTestsShortCircuits:
    def _proposal(self):
        return {
            "proposal_id": "aaaaaaaa-1111-2222-3333-444444444444",
            "target_path": "tools/data_feeds/get_x.py",
            "change_type": "new_file",
            "content": "# harmless\n",
        }

    def test_short_circuits_when_sandbox_unavailable(self, tmp_path):
        store = MagicMock()
        store.update_status = AsyncMock()
        with patch.object(gates, "_isolation_available", return_value=True), \
             patch.object(gates, "_bwrap_available", return_value=False), \
             patch.object(gates, "_cgroup_limits_available", return_value=True), \
             patch("shutil.copytree") as copytree_mock, \
             patch.object(gates, "_run_pytest_in_sandbox") as run_mock:
            result = asyncio.get_event_loop().run_until_complete(
                gates.gate_tests(store, self._proposal(), repo_root=tmp_path)
            )
        assert result == P.STATUS_REJECTED_SANDBOX_UNAVAILABLE
        # CRITICAL: we did NOT copy the tree and did NOT spawn pytest.
        copytree_mock.assert_not_called()
        run_mock.assert_not_called()
        # DB call carries the reason.
        args, _ = store.update_status.call_args
        assert args[1] == P.STATUS_REJECTED_SANDBOX_UNAVAILABLE
        assert "sandbox unavailable" in args[2].lower()


class TestNoStrayPytestSubprocess:
    def test_only_wrapper_spawns_pytest(self):
        """Grep-lock: gates.py must not spawn pytest anywhere except
        inside `_run_pytest_in_sandbox`. A future refactor that adds
        a second `subprocess.run([..., 'pytest', ...])` in another
        function would silently bypass the confinement guard."""
        src = Path("self_modify/gates.py").read_text(encoding="utf-8")
        # Locate every occurrence of 'pytest' in source; each occurrence
        # must sit either (a) inside a comment/docstring, (b) inside
        # `_run_pytest_in_sandbox`, (c) inside `_build_pytest_argv`
        # (which returns the argv but does not exec), or (d) inside a
        # test-facing constant (SANDBOX_TIMEOUT_SECONDS, PYTEST_BUDGET_SECS).
        offending = []
        for m in re.finditer(r"subprocess\.run\s*\(", src):
            # find the enclosing def
            head = src[: m.start()]
            fn_starts = list(re.finditer(r"\bdef\s+(\w+)\s*\(", head))
            fn = fn_starts[-1].group(1) if fn_starts else "<module>"
            # Snippet up to the closing paren, checked for a 'pytest' literal.
            snippet = src[m.start(): m.start() + 500]
            if '"pytest"' in snippet or "'pytest'" in snippet or "pytest_call" in snippet:
                if fn not in {"_run_pytest_in_sandbox"}:
                    offending.append((fn, snippet[:120]))
        assert not offending, (
            f"stray pytest subprocess call outside _run_pytest_in_sandbox: {offending}"
        )


class TestSweepStaleSandboxes:
    def test_removes_old_proposal_dirs(self, tmp_path, monkeypatch):
        # Redirect the sandbox root so this test doesn't touch /tmp.
        monkeypatch.setattr(gates, "_SANDBOX_ROOT", tmp_path)
        (tmp_path / "proposal_old").mkdir()
        (tmp_path / "proposal_new").mkdir()
        # Make one dir look 2h old.
        import os
        old = tmp_path / "proposal_old"
        two_hours_ago = int(__import__("time").time()) - 7200
        os.utime(old, (two_hours_ago, two_hours_ago))
        removed = gates.sweep_stale_sandboxes(max_age_secs=3600)
        assert any("proposal_old" in r for r in removed)
        assert not old.exists()
        # New dir untouched.
        assert (tmp_path / "proposal_new").exists()

    def test_ignores_non_proposal_dirs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gates, "_SANDBOX_ROOT", tmp_path)
        (tmp_path / "unrelated").mkdir()
        import os, time as _t
        os.utime(tmp_path / "unrelated", (int(_t.time()) - 7200,) * 2)
        removed = gates.sweep_stale_sandboxes(max_age_secs=3600)
        assert removed == []
        assert (tmp_path / "unrelated").exists()


class TestStatusesRegistered:
    def test_new_statuses_in_all_statuses(self):
        # ALL_STATUSES is the enum used by the state machine tests. Both
        # new statuses MUST appear or downstream tables can't accept them.
        assert P.STATUS_REJECTED_SANDBOX_UNAVAILABLE in P.ALL_STATUSES
        assert P.STATUS_ABORTED_INTERRUPTED in P.ALL_STATUSES


class TestReflectStartSweeps:
    def test_reflect_calls_sweep_at_start(self):
        # Grep-lock the wiring: reflect.run_reflection MUST call
        # sweep_stale_sandboxes so an interrupted prior run doesn't
        # leave a growing crumb trail.
        from self_modify import reflect
        src = inspect.getsource(reflect.run_reflection)
        assert "sweep_stale_sandboxes" in src


class TestEnvSurfacesSandboxLine:
    def test_env_command_prints_sandbox_status(self):
        # Grep-lock the wiring: _cmd_env in self_modify/cli.py MUST
        # call sandbox_posture and print a SANDBOX line so degradation
        # is visible without running reflect.
        from self_modify import cli
        src = inspect.getsource(cli._cmd_env)
        assert "sandbox_posture" in src
        assert "SANDBOX" in src


class TestSessionReportSurfacesSandboxLine:
    def test_session_report_render_prints_sandbox_status(self):
        # Grep-lock the wiring in analysis/session_report.SessionReport.render.
        from analysis import session_report
        src = inspect.getsource(session_report.SessionReport.render)
        assert "sandbox_posture" in src
        assert "SANDBOX" in src
