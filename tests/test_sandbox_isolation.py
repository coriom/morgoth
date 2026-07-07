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

def test_argv_isolated_wraps_in_unshare() -> None:
    argv = gates._build_pytest_argv(Path("/tmp/sbx/x"), isolated=True)
    assert argv[0] == "unshare"
    assert "--user" in argv
    assert "--map-root-user" in argv
    assert "--net" in argv
    assert argv[-2] == "-c"
    inner = argv[-1]
    # Loopback UP inside the ns, then cd + exec the venv pytest.
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


# ---------- _run_pytest_in_sandbox: dispatch + marker attached ----------

def test_run_pytest_in_sandbox_uses_isolated_wrapper_when_available() -> None:
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    with patch.object(gates, "_isolation_available", return_value=True), \
         patch.object(gates.subprocess, "run", return_value=fake_completed) as m:
        result = gates._run_pytest_in_sandbox(Path("/tmp/sbx/x"))
    argv = m.call_args.args[0]
    assert argv[0] == "unshare"
    # cwd is None under isolation — the sh -c script cds itself.
    assert m.call_args.kwargs["cwd"] is None
    assert result.isolated is True  # type: ignore[attr-defined]


def test_run_pytest_in_sandbox_falls_open_when_isolation_unavailable() -> None:
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    with patch.object(gates, "_isolation_available", return_value=False), \
         patch.object(gates.subprocess, "run", return_value=fake_completed) as m, \
         patch.object(gates.logger, "warning") as warn_mock:
        result = gates._run_pytest_in_sandbox(Path("/tmp/sbx/x"))
    argv = m.call_args.args[0]
    assert argv[0] == gates._VENV_PYTHON
    assert argv[1:] == ["-m", "pytest", "-q", "-n", "auto"]
    assert m.call_args.kwargs["cwd"] == "/tmp/sbx/x"
    # Warning fires loudly.
    warn_mock.assert_called_once()
    warned = warn_mock.call_args.args[0]
    assert "isolation UNAVAILABLE" in warned
    assert "degraded" in warned
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
