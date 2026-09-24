"""Empirical hardening tests — spawn REAL subprocesses under the same
wrappers gate_tests uses and prove each independent security property:

  * canary env absent under bwrap --clearenv
  * canary file unreachable under bwrap --tmpfs+--ro-bind view
  * canary secret files (~/.env, ~/.ssh, ~/.deopt, claude-cli creds)
    unreadable from inside the sandbox
  * network unreachable to external + loopback host services
  * runaway allocator killed by cgroup MemoryMax (fast — 200 MB cap)
  * fork-bomb bounded by cgroup TasksMax

2026-09-24: canaries FAIL, never skip. A missing confinement layer is
a failed test, not a "green pass because we didn't check". If the host
lacks bwrap/unshare/systemd-run, install them — this suite is the
control that proves gate_tests is safe to run. Setting
MORGOTH_ALLOW_SANDBOX_TESTS_SKIP=1 in the environment permits skipping
(for CI images that genuinely can't install these tools); the default
is DENY.
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
_ALLOW_SKIP = os.environ.get("MORGOTH_ALLOW_SANDBOX_TESTS_SKIP") == "1"


def _require(tool_present: bool, tool_name: str) -> None:
    """Hard-fail if the tool is absent. Skips only when the operator
    has explicitly opted in via MORGOTH_ALLOW_SANDBOX_TESTS_SKIP=1."""
    if tool_present:
        return
    msg = (
        f"{tool_name} unavailable — gate_tests fails closed without it. "
        f"Install {tool_name} (see docs) or set "
        f"MORGOTH_ALLOW_SANDBOX_TESTS_SKIP=1 to skip locally."
    )
    if _ALLOW_SKIP:
        pytest.skip(msg)
    pytest.fail(msg)


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


def _need_bwrap():
    _require(_HAS_UNSHARE, "unshare"); _require(_HAS_BWRAP, "bwrap")


def test_canary_env_absent_under_bwrap(tmp_path: Path) -> None:
    """A canary env var set in the parent MUST not reach the sandbox."""
    _need_bwrap()
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


def test_canary_file_unreachable_under_bwrap(tmp_path: Path) -> None:
    """A canary file at a known host path MUST be invisible inside."""
    _need_bwrap()
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


def test_canary_home_dotfiles_unreadable(tmp_path: Path) -> None:
    """The allowlist confinement must hide $HOME dotfiles that carry
    secrets: ~/Morgoth/morgoth/.env, ~/.ssh, ~/.deopt, and the
    claude-cli credential dir ~/.claude*. Each target is probed
    inside the sandbox; NONE may be readable."""
    _need_bwrap()
    targets = [
        Path.home() / "Morgoth" / "morgoth" / ".env",
        Path.home() / ".ssh",
        Path.home() / ".deopt",
        Path.home() / ".claude",
        Path.home() / ".config" / "claude",
    ]
    for t in targets:
        argv = _bwrap_wrap(
            tmp_path,
            f"if [ -r {shlex.quote(str(t))} ]; then echo LEAK; else echo OK; fi",
        )
        r = subprocess.run(argv, env=gates._hardened_outer_env(),
                            capture_output=True, text=True, timeout=15)
        assert "LEAK" not in r.stdout, (
            f"sandbox could read {t} — allowlist broken: {r.stdout!r}"
        )


def test_no_env_file_in_copied_tree(tmp_path: Path) -> None:
    """gate_tests copies the working tree into the sandbox with
    _SANDBOX_IGNORE excluding .env. Prove the copy has no .env
    anywhere under it."""
    _need_bwrap()
    import shutil as _shutil
    repo_root = Path("/home/corio/Morgoth/morgoth")
    dest = tmp_path / "copy"
    _shutil.copytree(repo_root, dest, ignore=gates._SANDBOX_IGNORE)
    hits = list(dest.rglob(".env"))
    assert not hits, f".env leaked into sandbox copy: {[str(h) for h in hits]}"


def test_canary_host_unix_sockets_unreachable(tmp_path: Path) -> None:
    """A netns does NOT isolate Unix sockets — a socket is a filesystem
    object. The ONLY defense is the bwrap allowlist. Prove each host
    socket path is not reachable via connect() from inside the sandbox.

    Regression-lock: if a future refactor binds /run, /var/run, or the
    XDG_RUNTIME_DIR into the sandbox, this canary starts CONNECTING
    (rather than getting FileNotFoundError) and fails.
    """
    _need_bwrap()
    uid = os.getuid()
    targets = [
        # Postgres — carries the whole store; MORGOTH's persistent memory.
        "/var/run/postgresql/.s.PGSQL.5432",
        "/run/postgresql/.s.PGSQL.5432",
        # systemd user bus — DBus session for the user.
        f"/run/user/{uid}/bus",
        # systemd user manager private socket — permits `systemd-run
        # --user` outside the sandbox if reachable. HIGH IMPACT.
        f"/run/user/{uid}/systemd/private",
        # Docker daemon — full root escape if reachable.
        "/var/run/docker.sock",
        "/run/docker.sock",
        # Ollama — locally exposed on 127.0.0.1:11434 usually, but
        # some hosts add a Unix socket. Belt+braces.
        "/var/run/ollama/ollama.sock",
    ]
    probe = tmp_path / "socket_probe.py"
    probe.write_text(textwrap.dedent(f"""
        import socket
        for p in {targets!r}:
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(1)
            try:
                s.connect(p)
                print("CONNECT", p)
                s.close()
            except Exception as e:
                print("REFUSED", p, type(e).__name__)
    """).strip(), encoding="utf-8")
    argv = _bwrap_wrap(tmp_path, f"python3 {shlex.quote(str(probe))}")
    r = subprocess.run(argv, env=gates._hardened_outer_env(),
                        capture_output=True, text=True, timeout=15)
    lines = [ln for ln in r.stdout.splitlines() if ln]
    connects = [ln for ln in lines if ln.startswith("CONNECT ")]
    assert not connects, (
        f"host unix socket(s) reachable inside sandbox — allowlist has a "
        f"hole:\n{connects}\nfull stdout:\n{r.stdout}"
    )
    # Every target must have produced a REFUSED line (proof the probe
    # ran end-to-end, not truncated early).
    assert len(lines) == len(targets), (
        f"probe did not report on all targets: {lines}"
    )


def _tcp_probe_argv(host: str, port: int) -> list[str]:
    """Try to open a TCP connection inside a fresh netns. Python is
    used (not bash /dev/tcp) so we can assert exit code cleanly —
    bash's exec redirect continues past connect() failure."""
    py = (f"import socket, sys; s = socket.socket(); s.settimeout(2); "
          f"s.connect(({host!r}, {port})); print('REACHED'); s.close()")
    inner = f'python3 -c "{py}"'
    return ["unshare", "--user", "--map-root-user", "--net",
            "sh", "-c", f"ip link set lo up; {inner}"]


def test_external_network_unreachable(tmp_path: Path) -> None:
    """TCP to a public IP MUST fail — fresh netns has no route out."""
    _require(_HAS_UNSHARE, "unshare")
    r = subprocess.run(_tcp_probe_argv("1.1.1.1", 443),
                        env=gates._hardened_outer_env(),
                        capture_output=True, text=True, timeout=15)
    assert "REACHED" not in r.stdout
    assert r.returncode != 0
    assert ("unreachable" in r.stderr.lower()
            or "network is unreachable" in r.stderr.lower())


def test_loopback_host_services_unreachable(tmp_path: Path) -> None:
    """Fresh netns's own lo has no listeners — host's 127.0.0.1
    postgres/ollama are unreachable from inside."""
    _require(_HAS_UNSHARE, "unshare")
    r = subprocess.run(_tcp_probe_argv("127.0.0.1", 5432),
                        env=gates._hardened_outer_env(),
                        capture_output=True, text=True, timeout=15)
    assert "REACHED" not in r.stdout
    assert r.returncode != 0
    # Fresh netns lo → connection refused (no listener) or timeout.
    err = r.stderr.lower()
    assert ("refused" in err or "timed out" in err or "unreachable" in err)


def test_memory_limit_kills_allocator(tmp_path: Path) -> None:
    _require(_has("prlimit"), "prlimit")
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


def test_task_limit_bounds_fork_bomb(tmp_path: Path) -> None:
    _require(_HAS_UNSHARE, "unshare")
    _require(_HAS_BWRAP, "bwrap")
    _require(_HAS_SYSTEMD_RUN, "systemd-run")
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
