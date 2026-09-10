"""Empirical hardening tests — spawn REAL subprocesses under the same
wrappers gate_tests uses and prove each independent security property:

  * canary env absent under bwrap --clearenv
  * canary file unreachable under bwrap --tmpfs+--ro-bind view
  * network unreachable to external + loopback host services
  * runaway allocator killed by cgroup MemoryMax (fast — 200 MB cap)
  * fork-bomb bounded by cgroup TasksMax

Each test skips itself if the layer's tool isn't available on the host,
so CI stays green on kernels/images without the tool. Locally on WSL2
all should run.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

from self_modify import gates


def _has(cmd: str) -> bool:
    try:
        rc = subprocess.run(
            [cmd, "--version"], capture_output=True, timeout=5,
        ).returncode
        return rc == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


_HAS_UNSHARE = gates._isolation_available()
_HAS_BWRAP = _has("bwrap")
_HAS_SYSTEMD_RUN = _has("systemd-run")


def _bwrap_wrap(sandbox: Path, inner_cmd: str) -> list[str]:
    """Full stacked wrapper — outer unshare for netns+lo, bwrap for fs+env."""
    bwrap = [
        "bwrap", "--clearenv",
        "--setenv", "PATH", "/usr/sbin:/usr/bin:/bin",
        "--setenv", "HOME", str(sandbox),
        "--setenv", "LANG", "C.UTF-8",
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64",
        "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/etc", "/etc",
        "--tmpfs", "/tmp",
        "--bind", str(sandbox), str(sandbox),
        "--proc", "/proc", "--dev", "/dev",
        "--share-net", "--die-with-parent",
        "--chdir", str(sandbox),
        "--", "sh", "-c", inner_cmd,
    ]
    inner = "ip link set lo up; exec " + " ".join(shlex.quote(a) for a in bwrap)
    return ["unshare", "--user", "--map-root-user", "--net", "sh", "-c", inner]


@pytest.mark.skipif(not (_HAS_UNSHARE and _HAS_BWRAP),
                    reason="unshare or bwrap unavailable")
def test_canary_env_absent_under_bwrap(tmp_path: Path) -> None:
    """A canary env var set in the parent MUST not reach the sandbox."""
    argv = _bwrap_wrap(tmp_path, "env")
    parent_env = {
        **gates._hardened_outer_env(),
        # Simulate a secret in the parent that must be scrubbed.
        "MORGOTH_CANARY_SECRET": "leaked-token-do-not-reveal",
    }
    r = subprocess.run(argv, env=parent_env, capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    assert "MORGOTH_CANARY_SECRET" not in r.stdout
    assert "leaked-token-do-not-reveal" not in r.stdout


@pytest.mark.skipif(not (_HAS_UNSHARE and _HAS_BWRAP),
                    reason="unshare or bwrap unavailable")
def test_canary_file_unreachable_under_bwrap(tmp_path: Path) -> None:
    """A canary file at a known host path MUST be invisible inside."""
    canary = Path.home() / ".morgoth-sandbox-canary"
    canary.write_text("canary-content-must-not-leak\n", encoding="utf-8")
    try:
        argv = _bwrap_wrap(tmp_path, f"cat {canary} 2>&1; echo DONE")
        r = subprocess.run(argv, env=gates._hardened_outer_env(),
                            capture_output=True, text=True, timeout=15)
        assert "canary-content-must-not-leak" not in r.stdout
        assert "canary-content-must-not-leak" not in r.stderr
        assert "DONE" in r.stdout
    finally:
        canary.unlink(missing_ok=True)


def _tcp_probe_argv(host: str, port: int) -> list[str]:
    """Try to open a TCP connection inside a fresh netns. Python is
    used (not bash /dev/tcp) so we can assert exit code cleanly —
    bash's exec redirect continues past connect() failure."""
    py = (f"import socket, sys; s = socket.socket(); s.settimeout(2); "
          f"s.connect(({host!r}, {port})); print('REACHED'); s.close()")
    inner = f'python3 -c "{py}"'
    return ["unshare", "--user", "--map-root-user", "--net",
            "sh", "-c", f"ip link set lo up; {inner}"]


@pytest.mark.skipif(not _HAS_UNSHARE, reason="unshare unavailable")
def test_external_network_unreachable(tmp_path: Path) -> None:
    """TCP to a public IP MUST fail — fresh netns has no route out."""
    r = subprocess.run(_tcp_probe_argv("1.1.1.1", 443),
                        env=gates._hardened_outer_env(),
                        capture_output=True, text=True, timeout=15)
    assert "REACHED" not in r.stdout
    assert r.returncode != 0
    assert ("unreachable" in r.stderr.lower()
            or "network is unreachable" in r.stderr.lower())


@pytest.mark.skipif(not _HAS_UNSHARE, reason="unshare unavailable")
def test_loopback_host_services_unreachable(tmp_path: Path) -> None:
    """Fresh netns's own lo has no listeners — host's 127.0.0.1
    postgres/ollama are unreachable from inside."""
    r = subprocess.run(_tcp_probe_argv("127.0.0.1", 5432),
                        env=gates._hardened_outer_env(),
                        capture_output=True, text=True, timeout=15)
    assert "REACHED" not in r.stdout
    assert r.returncode != 0
    # Fresh netns lo → connection refused (no listener) or timeout.
    err = r.stderr.lower()
    assert ("refused" in err or "timed out" in err or "unreachable" in err)


@pytest.mark.skipif(not _has("prlimit"), reason="prlimit unavailable")
def test_memory_limit_kills_allocator(tmp_path: Path) -> None:
    """A deliberate allocator MUST be killed by the per-process
    RLIMIT_AS. This is the kernel-enforced path — the same wrapper
    the sandbox uses at gate_tests time. WSL2's cgroup memory.max
    is unreliable (the wiring-time probe let 500 MB through a 150 MB
    cap), so prlimit is the primary memory bound."""
    inner_py = textwrap.dedent("""
        import ctypes
        blocks = []
        try:
            for _ in range(500):
                b = ctypes.create_string_buffer(1_000_000)
                b[0] = 1; b[999_999] = 1  # touch pages
                blocks.append(b)
            print("ALLOCATED_ALL")
        except MemoryError:
            print("MEMORY_ERROR")
    """).strip()
    # 150 MB cap, allocator wants 500 MB → must MemoryError.
    r = subprocess.run(
        ["prlimit", f"--as={150 * 1024 * 1024}", "--",
         "python3", "-c", inner_py],
        env=gates._hardened_outer_env(),
        capture_output=True, text=True, timeout=30,
    )
    assert "ALLOCATED_ALL" not in r.stdout, (
        f"allocator escaped the RLIMIT_AS cap: rc={r.returncode} out={r.stdout!r}"
    )
    assert "MEMORY_ERROR" in r.stdout or r.returncode != 0


@pytest.mark.skipif(
    not (_HAS_UNSHARE and _HAS_BWRAP and _HAS_SYSTEMD_RUN),
    reason="unshare/bwrap/systemd-run unavailable",
)
def test_task_limit_bounds_fork_bomb(tmp_path: Path) -> None:
    """TasksMax=32 bounds a fork loop; without it the process could
    exhaust host pids. We assert AT LEAST ONE fork failed."""
    # Fork 200 sleepers — TasksMax=32 means many will fail to fork.
    inner_py = textwrap.dedent("""
        import os, sys
        failed = 0
        for _ in range(200):
            try:
                pid = os.fork()
            except OSError:
                failed += 1
                continue
            if pid == 0:
                os._exit(0)
        print(f"FAILED_FORKS={failed}")
    """).strip()
    inner = f"python3 -c {shlex.quote(inner_py)}"
    argv = [
        "systemd-run", "--user", "--scope", "--quiet",
        "--property=MemoryMax=200M",
        "--property=TasksMax=32",
        "--",
    ] + _bwrap_wrap(tmp_path, inner)
    r = subprocess.run(argv, env=gates._hardened_outer_env(),
                        capture_output=True, text=True, timeout=30)
    # Either the process itself was killed by TasksMax (rc != 0) or it
    # got far enough to see fork failures. Either way, the cgroup
    # is enforcing a bound.
    if "FAILED_FORKS=" in r.stdout:
        n = int(r.stdout.split("FAILED_FORKS=")[1].split()[0])
        assert n > 0, "fork bomb was NOT bounded — no forks failed"
    else:
        assert r.returncode != 0, (
            f"fork bomb escaped: rc=0 stdout={r.stdout!r} stderr={r.stderr!r}"
        )
