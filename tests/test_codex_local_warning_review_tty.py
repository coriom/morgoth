"""Linux PTY checks for the operator-only terminal gate; no model process."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest


_PTY_PROBE = textwrap.dedent("""
    import os, pty, select, signal, sys
    from scripts import codex_local_warning_review as review

    mode = sys.argv[1]
    read_fd, write_fd = os.pipe()
    try:
        pid, master = pty.fork()
    except OSError:
        print('PTY_UNAVAILABLE')
        raise SystemExit(0)

    if pid == 0:
        os.close(read_fd)
        signal.alarm(3)
        if mode == 'background':
            child = os.fork()
            if child:
                os.close(write_fd)
                os.waitpid(child, 0)
                os._exit(0)
            os.setpgid(0, 0)
        elif mode == 'stdout_redirected':
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.close(devnull)
        elif mode == 'stdin_redirected':
            devnull = os.open(os.devnull, os.O_RDONLY)
            os.dup2(devnull, 0)
            os.close(devnull)
        elif mode == 'different_stream':
            other_master, other_slave = pty.openpty()
            os.dup2(other_slave, 2)
            os.close(other_master)
            os.close(other_slave)
        elif mode == 'ci':
            os.environ['CI'] = 'true'
        try:
            with review.open_operator_terminal() as terminal:
                different = (os.fstat(terminal.fileno()).st_rdev !=
                             os.fstat(0).st_rdev)
            closed = terminal.reader.closed and terminal.writer.closed
            answer = ('PASS_DIFFERENT' if different and closed else
                      'UNEXPECTED' if not closed else 'PASS_SAME')
        except review.ReviewUnavailable:
            answer = 'BLOCKED'
        except BaseException:
            answer = 'UNEXPECTED'
        os.write(write_fd, answer.encode('ascii'))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    ready, _, _ = select.select([read_fd], [], [], 4)
    if not ready:
        os.kill(pid, signal.SIGKILL)
        answer = 'PTY_TIMEOUT'
    else:
        answer = os.read(read_fd, 64).decode('ascii')
    os.close(read_fd)
    os.waitpid(pid, 0)
    os.close(master)
    print(answer)
""")


def _pty_result(mode: str) -> str:
    """Run an actual owned PTY child and return only a fixed verdict."""
    completed = subprocess.run([sys.executable, "-c", _PTY_PROBE, mode],
                               text=True, capture_output=True, timeout=8,
                               check=False)
    assert completed.returncode == 0
    result = completed.stdout.strip()
    if result == "PTY_UNAVAILABLE":
        pytest.skip("Linux PTY allocation unavailable in this test environment")
    assert result in {"PASS_DIFFERENT", "PASS_SAME", "BLOCKED", "PTY_TIMEOUT"}, result
    return result


@pytest.mark.skipif(sys.platform != "linux", reason="Linux controlling-terminal contract")
def test_real_foreground_pty_accepts_dev_tty_alias() -> None:
    """A real foreground PTY passes even though /dev/tty has a different device."""
    assert _pty_result("foreground") == "PASS_DIFFERENT"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux controlling-terminal contract")
@pytest.mark.parametrize("mode", ["stdout_redirected", "stdin_redirected",
                                   "background", "different_stream", "ci"])
def test_real_pty_rejects_noninteractive_or_inconsistent_streams(mode: str) -> None:
    """Redirects, a background process, another PTY and CI remain blocked."""
    assert _pty_result(mode) == "BLOCKED"


def test_missing_controlling_terminal_is_blocked() -> None:
    """A child with three pipes cannot start the private review."""
    code = ("from scripts import codex_local_warning_review as r; "
            "print('UNEXPECTED' if r.main() == 0 else 'BLOCKED')")
    completed = subprocess.run([sys.executable, "-c", code], input="",
                               text=True, capture_output=True, timeout=5,
                               check=False)
    assert completed.returncode == 0
    assert completed.stdout.strip().endswith("BLOCKED")
    assert "PREFLIGHT_INTERACTIVE_TTY" in completed.stdout
