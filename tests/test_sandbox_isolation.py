"""Sandbox network-isolation wrapper tests.

Every subprocess call is mocked. The probe, argv-shape decision, and
status_reason marker are exercised without spawning a real ``unshare``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import gates
from self_modify import proposals as P


@pytest.fixture(autouse=True)
def _clear_isolation_cache() -> None:
    """Every test starts from an unprobed cache — the module-level
    cache lives across tests otherwise and would leak decisions."""
    gates._isolation_available_cache = None
    gates._bwrap_available_cache = None
    gates._cgroup_limits_available_cache = None


# ---------- _isolation_available probe caching --------------------------

def test_probe_caches_after_first_call() -> None:
    fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(gates.subprocess, "run", return_value=fake_ok) as m:
        assert gates._isolation_available() is True
        assert gates._isolation_available() is True
        # Second call hits cache, not subprocess.
        assert m.call_count == 1


def test_probe_returns_false_on_nonzero_exit() -> None:
    fake_bad = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    with patch.object(gates.subprocess, "run", return_value=fake_bad):
        assert gates._isolation_available() is False


def test_probe_returns_false_on_missing_unshare() -> None:
    with patch.object(gates.subprocess, "run", side_effect=FileNotFoundError("unshare")):
        assert gates._isolation_available() is False


def test_probe_returns_false_on_timeout() -> None:
    with patch.object(
        gates.subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="unshare", timeout=5),
    ):
        assert gates._isolation_available() is False


# ---------- argv construction ------------------------------------------

def test_argv_isolated_baseline_wraps_in_unshare() -> None:
    """Isolated + not confined + not cgroup-bound → the legacy shape."""
    argv = gates._build_pytest_argv(
        Path("/tmp/sbx/x"), isolated=True, confined=False, cgroup_bound=False,
    )
    assert argv[0] == "unshare"
    assert "--user" in argv
    assert "--map-root-user" in argv
    assert "--net" in argv
    assert argv[-2] == "-c"
    inner = argv[-1]
    assert "ip link set lo up" in inner
    assert "/tmp/sbx/x" in inner
    assert gates._VENV_PYTHON in inner
    assert "pytest -q" in inner


def test_argv_non_isolated_is_plain_pytest() -> None:
    argv = gates._build_pytest_argv(Path("/tmp/sbx/x"), isolated=False)
    assert argv[0] == gates._VENV_PYTHON
    # pytest-xdist ``-n auto`` was wired at the timeout-fix commit.
    assert argv[1:] == ["-m", "pytest", "-q", "-n", "auto"]
    assert "unshare" not in argv
    assert "bwrap" not in argv
    assert "systemd-run" not in argv


def test_argv_confined_composes_bwrap_inside_unshare() -> None:
    """confined=True → bwrap invocation nested inside the unshare sh -c.

    The outer unshare provides the netns (with lo raiseable), bwrap
    then --share-net's into it while adding fs isolation + env scrub.
    """
    argv = gates._build_pytest_argv(
        Path("/tmp/sbx/x"), isolated=True, confined=True, cgroup_bound=False,
    )
    assert argv[0] == "unshare"
    inner = argv[-1]
    # bwrap runs INSIDE the outer unshare sh, and it clears env,
    # tmpfs's /tmp, binds only the sandbox, shares the outer netns.
    assert "bwrap" in inner
    assert "--clearenv" in inner
    assert "--tmpfs /tmp" in inner or "--tmpfs\\ /tmp" in inner or "--tmpfs" in inner
    assert "--bind /tmp/sbx/x /tmp/sbx/x" in inner or "/tmp/sbx/x" in inner
    assert "--share-net" in inner
    assert "--die-with-parent" in inner
    assert "--ro-bind /usr /usr" in inner or "--ro-bind" in inner
    assert gates._VENV_ROOT in inner
    assert "ip link set lo up" in inner  # runs in outer unshare, not bwrap


def test_argv_cgroup_bound_wraps_in_systemd_run() -> None:
    """cgroup_bound=True → systemd-run --user --scope outermost."""
    argv = gates._build_pytest_argv(
        Path("/tmp/sbx/x"), isolated=True, confined=True, cgroup_bound=True,
    )
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert "--scope" in argv
    joined = " ".join(argv)
    assert f"MemoryMax={gates._MEMORY_MAX_BYTES}" in joined
    assert f"TasksMax={gates._TASKS_MAX}" in joined
    assert f"CPUQuota={gates._CPU_QUOTA_PCT}%" in joined
    # unshare is still there, nested after the systemd-run --
    assert "unshare" in argv


def test_argv_pytest_call_is_wrapped_in_prlimit() -> None:
    """The pytest invocation itself must sit behind ``prlimit --as=…``.

    RLIMIT_AS is kernel-enforced per process — this is the reliable
    memory bound because WSL2 silently ignores cgroup memory.max at
    the user-scope level (an empirical 150 MB cap let 500 MB through)."""
    argv = gates._build_pytest_argv(
        Path("/tmp/sbx/x"), isolated=True, confined=True, cgroup_bound=True,
    )
    joined = " ".join(argv)
    assert "prlimit" in joined
    assert f"--as={gates._PER_PROCESS_AS_BYTES}" in joined
    # Also present in the plain-unshare (non-confined) form.
    argv2 = gates._build_pytest_argv(
        Path("/tmp/sbx/x"), isolated=True, confined=False, cgroup_bound=False,
    )
    assert "prlimit" in argv2[-1]
    assert f"--as={gates._PER_PROCESS_AS_BYTES}" in argv2[-1]


def test_hardened_outer_env_is_minimal_whitelist() -> None:
    """Env passed to the outer subprocess must NOT carry parent secrets."""
    env = gates._hardened_outer_env()
    assert set(env.keys()) == {"PATH", "LANG", "XDG_RUNTIME_DIR"}
    assert env["PATH"] == "/usr/sbin:/usr/bin:/bin"
    # A parent env with FRED_API_KEY etc. must NOT be reflected here.
    with patch.dict("os.environ", {"FRED_API_KEY": "leak", "POSTGRES_URL": "leak",
                                    "ANTHROPIC_API_KEY": "leak"}):
        env2 = gates._hardened_outer_env()
    assert "FRED_API_KEY" not in env2
    assert "POSTGRES_URL" not in env2
    assert "ANTHROPIC_API_KEY" not in env2


def test_sandbox_ignore_excludes_env_file() -> None:
    """The sandbox copy must never contain the live .env — even under
    fs confinement, a leaked-into-copy secret is a leak."""
    ignored = gates._SANDBOX_IGNORE("/repo", [".env", ".envrc", "secrets", "main.py"])
    assert ".env" in ignored
    assert ".envrc" in ignored
    assert "secrets" in ignored
    assert "main.py" not in ignored


# ---------- _bwrap_available / _cgroup_limits_available probes ----------

def test_bwrap_probe_caches_after_first_call() -> None:
    fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(gates.subprocess, "run", return_value=fake_ok) as m:
        assert gates._bwrap_available() is True
        assert gates._bwrap_available() is True
        assert m.call_count == 1


def test_bwrap_probe_false_on_missing() -> None:
    with patch.object(gates.subprocess, "run", side_effect=FileNotFoundError("bwrap")):
        assert gates._bwrap_available() is False


def test_cgroup_limits_probe_caches_and_uses_hardened_env() -> None:
    fake_ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(gates.subprocess, "run", return_value=fake_ok) as m:
        assert gates._cgroup_limits_available() is True
        assert gates._cgroup_limits_available() is True
        assert m.call_count == 1
        # Probe must pass XDG_RUNTIME_DIR — else systemd-run --user can't
        # find the session bus when we're inside morgoth.service.
        env = m.call_args.kwargs.get("env") or {}
        assert "XDG_RUNTIME_DIR" in env


def test_cgroup_limits_probe_false_on_missing_or_error() -> None:
    with patch.object(gates.subprocess, "run", side_effect=FileNotFoundError()):
        assert gates._cgroup_limits_available() is False


# ---------- _run_pytest_in_sandbox: dispatch + marker attached ----------

def test_run_pytest_in_sandbox_full_hardening_when_all_layers_available() -> None:
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    with patch.object(gates, "_isolation_available", return_value=True), \
         patch.object(gates, "_bwrap_available", return_value=True), \
         patch.object(gates, "_cgroup_limits_available", return_value=True), \
         patch.object(gates.subprocess, "run", return_value=fake_completed) as m:
        result = gates._run_pytest_in_sandbox(Path("/tmp/sbx/x"))
    argv = m.call_args.args[0]
    assert argv[0] == "systemd-run"
    # Env passed to the outer subprocess is the minimal whitelist.
    env = m.call_args.kwargs.get("env") or {}
    assert set(env.keys()) == {"PATH", "LANG", "XDG_RUNTIME_DIR"}
    assert result.isolated is True  # type: ignore[attr-defined]
    assert result.confined is True  # type: ignore[attr-defined]
    assert result.cgroup_bound is True  # type: ignore[attr-defined]


def test_run_pytest_in_sandbox_partial_degrade_warns_per_missing_layer() -> None:
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    with patch.object(gates, "_isolation_available", return_value=True), \
         patch.object(gates, "_bwrap_available", return_value=False), \
         patch.object(gates, "_cgroup_limits_available", return_value=False), \
         patch.object(gates.subprocess, "run", return_value=fake_completed), \
         patch.object(gates.logger, "warning") as warn_mock:
        result = gates._run_pytest_in_sandbox(Path("/tmp/sbx/x"))
    # One warning per missing layer (bwrap + cgroup); not for isolation.
    assert warn_mock.call_count == 2
    messages = [c.args[0] for c in warn_mock.call_args_list]
    assert any("filesystem confinement UNAVAILABLE" in m for m in messages)
    assert any("cgroup limits UNAVAILABLE" in m for m in messages)
    assert result.confined is False  # type: ignore[attr-defined]
    assert result.cgroup_bound is False  # type: ignore[attr-defined]


def test_run_pytest_in_sandbox_falls_open_when_isolation_unavailable() -> None:
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    with patch.object(gates, "_isolation_available", return_value=False), \
         patch.object(gates, "_bwrap_available", return_value=True), \
         patch.object(gates, "_cgroup_limits_available", return_value=True), \
         patch.object(gates.subprocess, "run", return_value=fake_completed) as m, \
         patch.object(gates.logger, "warning") as warn_mock:
        result = gates._run_pytest_in_sandbox(Path("/tmp/sbx/x"))
    argv = m.call_args.args[0]
    # Without isolation the whole hardening stack is skipped — the fresh
    # netns is what makes bwrap's --share-net meaningful, and without
    # unshare there's no user_ns for bwrap either. Plain venv pytest.
    assert argv[0] == gates._VENV_PYTHON
    assert argv[1:] == ["-m", "pytest", "-q", "-n", "auto"]
    assert m.call_args.kwargs["cwd"] == "/tmp/sbx/x"
    warn_mock.assert_called_once()
    warned = warn_mock.call_args.args[0]
    assert "isolation UNAVAILABLE" in warned
    assert result.isolated is False  # type: ignore[attr-defined]


# ---------- gate_tests: isolation marker appears in status_reason -------

def _stub_completed(rc: int, isolated: bool) -> subprocess.CompletedProcess[str]:
    cp = subprocess.CompletedProcess(
        args=[], returncode=rc, stdout="1 passed" if rc == 0 else "", stderr="",
    )
    cp.isolated = isolated  # type: ignore[attr-defined]
    return cp


@pytest.mark.asyncio
async def test_gate_tests_status_reason_contains_isolation_on_marker(
    tmp_path: Path,
) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "data_feeds").mkdir()
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "iso-on",
        "target_path": "tools/data_feeds/dummy.py",
        "change_type": "new_file",
        "content": "# harmless\n",
    }
    with patch.object(gates, "_run_pytest_in_sandbox",
                      return_value=_stub_completed(0, isolated=True)):
        result = await gates.gate_tests(store, proposal, repo_root=tmp_path)
    assert result == P.STATUS_PENDING_APPROVAL
    reason = store.update_status.await_args.args[2]
    assert "isolation=on" in reason


@pytest.mark.asyncio
async def test_gate_tests_status_reason_contains_isolation_off_marker(
    tmp_path: Path,
) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "data_feeds").mkdir()
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "iso-off",
        "target_path": "tools/data_feeds/dummy.py",
        "change_type": "new_file",
        "content": "# harmless\n",
    }
    with patch.object(gates, "_run_pytest_in_sandbox",
                      return_value=_stub_completed(0, isolated=False)):
        result = await gates.gate_tests(store, proposal, repo_root=tmp_path)
    assert result == P.STATUS_PENDING_APPROVAL
    reason = store.update_status.await_args.args[2]
    assert "isolation=off" in reason


@pytest.mark.asyncio
async def test_gate_tests_failure_reason_carries_isolation_marker(
    tmp_path: Path,
) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "data_feeds").mkdir()
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "iso-fail",
        "target_path": "tools/data_feeds/dummy.py",
        "change_type": "new_file",
        "content": "raise SystemExit(1)\n",
    }
    fake = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="E ConnectError",
    )
    fake.isolated = True  # type: ignore[attr-defined]
    with patch.object(gates, "_run_pytest_in_sandbox", return_value=fake):
        result = await gates.gate_tests(store, proposal, repo_root=tmp_path)
    assert result == P.STATUS_TESTS_FAILED
    reason = store.update_status.await_args.args[2]
    assert "isolation=on" in reason
    assert "ConnectError" in reason
