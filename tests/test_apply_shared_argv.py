"""Grep-lock: apply's live-pytest argv MUST be built from the same
single-source list as gate_tests and canonical_runner.

Prior bug (2026-09-30): apply used a plain ``pytest -q -n auto``,
skipping ``-m "not integration"`` and ``--timeout=60``. Integration
tests requiring Postgres hung the baseline for 30+ minutes on the
operator's box while `morgoth test` ran the same suite in ~9 s. Any
future refactor that reintroduces divergent argv fails this test
before the operator hits the 30-minute hang.
"""

from __future__ import annotations

import inspect

from self_modify import apply as A
from self_modify import gates as G


def test_apply_reads_hermetic_extra_args() -> None:
    src = inspect.getsource(A._run_live_pytest)
    assert "HERMETIC_PYTEST_EXTRA_ARGS" in src, (
        "apply._run_live_pytest must read from gates.HERMETIC_PYTEST_EXTRA_ARGS"
    )


def test_apply_uses_xdist_workers_constant() -> None:
    src = inspect.getsource(A._run_live_pytest)
    assert "_SANDBOX_XDIST_WORKERS" in src


def test_apply_argv_carries_marker_exclusion_socket_cut_and_timeout(tmp_path, monkeypatch) -> None:
    """Materialize the argv (via a mocked subprocess) and check the
    shape. A grep on the source alone would miss a future refactor
    that inlines the args. Seeds a minimal live-tree stand-in so
    _run_live_pytest's copytree step has something to copy."""
    import subprocess as sp
    src = tmp_path / "repo"
    src.mkdir()
    (src / "seed.txt").write_text("")
    # 2026-09-29: apply._run_live_pytest calls wrap_command_in_sandbox
    # which fail-closes if sandbox_posture reports missing layers.
    # Under `morgoth test`'s nested bwrap, systemd-run cgroup is
    # absent — force ok=True so this pure argv-shape test can proceed.
    monkeypatch.setattr(G, "sandbox_posture", lambda: {
        "isolated": True, "confined": True, "cgroup_bound": True,
        "ok": True, "reason": "",
    })
    calls: list[list[str]] = []
    def _spy(argv, *a, **kw):
        calls.append(argv)
        return sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    real_run = sp.run
    sp.run = _spy
    try:
        A._run_live_pytest(src, junit_out=tmp_path / "j.xml")
    finally:
        sp.run = real_run
    assert calls, "subprocess.run must have been called"
    # sandbox_posture probes call subprocess.run too; pick the call
    # whose argv contains the pytest command (last matching call is
    # the actual test invocation).
    inner = None
    for argv in reversed(calls):
        for a in argv:
            if isinstance(a, str) and "python -m pytest" in a:
                inner = a
                break
        if inner is not None:
            break
    assert inner is not None, f"no pytest call among {len(calls)} subprocess.run calls"
    assert "not integration" in inner
    assert "--disable-socket" in inner
    assert "--allow-unix-socket" in inner
    assert "--timeout=60" in inner
    assert "--timeout-method=thread" in inner
    assert "--max-worker-restart=3" in inner
    assert f"-n {G._SANDBOX_XDIST_WORKERS}" in inner
    assert "--junitxml=" in inner
