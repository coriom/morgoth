"""Real PTY consent checks; never create an Ollama client or model request."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from agents import generalist_ollama_plan_probe as probe
from agents.generalist_capability import CapabilityRefusal


_PTY_PROBE = textwrap.dedent("""
    import io, json, os, pty, select, signal, sys, time
    from agents import generalist_ollama_plan_probe as probe

    mode, answer = sys.argv[1:]
    read_fd, write_fd = os.pipe()
    try:
        pid, master = pty.fork()
    except OSError:
        print('PTY_UNAVAILABLE')
        raise SystemExit(0)
    if pid == 0:
        os.close(read_fd)
        signal.alarm(4)
        if mode == 'background':
            child = os.fork()
            if child:
                os.close(write_fd)
                os.waitpid(child, 0)
                os._exit(0)
            os.setpgid(0, 0)
        elif mode in ('stdin_redirected', 'stdout_redirected', 'stderr_redirected'):
            stream = {'stdin_redirected': 0, 'stdout_redirected': 1,
                      'stderr_redirected': 2}[mode]
            replacement = os.open(os.devnull, os.O_RDONLY if stream == 0 else os.O_WRONLY)
            os.dup2(replacement, stream)
            os.close(replacement)
        elif mode == 'different_stream':
            other_master, other_slave = pty.openpty()
            os.dup2(other_slave, 2)
            os.close(other_master)
            os.close(other_slave)
        elif mode == 'ci':
            os.environ['CI'] = 'true'
        before = len(os.listdir('/proc/self/fd'))
        try:
            production_false = False
            if mode == 'decline':
                sys.argv = ['probe']
                async def no_inference():
                    raise AssertionError('inference reached')
                probe._one_proposal = no_inference
                sys.stdout = io.StringIO()
                verdict = 'BLOCKED' if probe.main() == 2 else 'UNEXPECTED'
                production_false = json.loads(sys.stdout.getvalue())['PRODUCTION_QUALIFIED'] is False
            else:
                verdict = 'ACCEPTED' if probe._foreground_consent() else 'BLOCKED'
        except BaseException:
            verdict = 'UNEXPECTED'
        after = len(os.listdir('/proc/self/fd'))
        different = os.fstat(0).st_rdev != os.stat('/dev/tty').st_rdev
        os.write(write_fd, json.dumps({'verdict': verdict, 'fds_closed': before == after,
                                       'tty_alias_differs': different,
                                       'production_false': production_false}).encode('ascii'))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    output = b''
    result = b''
    deadline = time.monotonic() + 5
    sent = False
    while time.monotonic() < deadline and not result:
        ready, _, _ = select.select([master, read_fd], [], [], 0.2)
        if master in ready:
            try:
                output += os.read(master, 1024)
            except OSError:
                pass
            if b'Type PLAN ONCE: ' in output and not sent:
                os.write(master, answer.encode('ascii') + b'\\n')
                sent = True
        if read_fd in ready:
            result = os.read(read_fd, 1024)
    if not result:
        os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    os.close(read_fd)
    os.close(master)
    print(json.dumps({'result': json.loads(result) if result else {'verdict': 'TIMEOUT'},
                      'prompt': b'Type PLAN ONCE: ' in output}))
""")


def _pty_result(mode: str, answer: str = "PLAN ONCE") -> dict[str, object]:
    """Exercise consent only in an owned Linux PTY subprocess."""
    clean_env = {key: value for key, value in os.environ.items()
                 if key not in {"CI", "GITHUB_ACTIONS", "CODEBUILD_BUILD_ID"}}
    result = subprocess.run([sys.executable, "-c", _PTY_PROBE, mode, answer],
                            text=True, capture_output=True, timeout=9, check=False,
                            env=clean_env)
    assert result.returncode == 0
    if result.stdout.strip() == "PTY_UNAVAILABLE":
        pytest.skip("Linux PTY allocation unavailable")
    return json.loads(result.stdout)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY contract")
def test_foreground_pty_prompts_and_accepts_exact_phrase() -> None:
    """The controlling-terminal alias differs from the stream PTY yet works."""
    result = _pty_result("foreground")
    assert result == {"result": {"verdict": "ACCEPTED", "fds_closed": True,
                                  "tty_alias_differs": True, "production_false": False},
                      "prompt": True}
    assert _pty_result("foreground", "PLAN ONCE ")["result"]["verdict"] == "BLOCKED"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY contract")
def test_decline_stops_before_inference_and_keeps_production_blocked() -> None:
    """Main emits its fixed refusal without calling the mocked model function."""
    result = _pty_result("decline", "NO")
    assert result["prompt"] is True
    assert result["result"]["production_false"] is True
    assert result["result"]["verdict"] == "BLOCKED"
    assert result["result"]["fds_closed"] is True


@pytest.mark.skipif(sys.platform != "linux", reason="Linux PTY contract")
@pytest.mark.parametrize("mode", ["stdin_redirected", "stdout_redirected",
                                   "stderr_redirected", "background", "ci",
                                   "different_stream"])
def test_unattended_or_inconsistent_pty_is_blocked(mode: str) -> None:
    """Redirects, a second PTY, background jobs and CI cannot grant consent."""
    result = _pty_result(mode)
    assert result["result"]["verdict"] == "BLOCKED"
    assert result["result"]["fds_closed"] is True
    assert result["prompt"] is False


def test_missing_controlling_terminal_is_blocked() -> None:
    """Piped process input cannot reach the consent prompt."""
    result = subprocess.run([sys.executable, "-c", "from agents import generalist_ollama_plan_probe as p; print(p._foreground_consent())"],
                            input="", capture_output=True, text=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "False"


def test_terminal_open_failure_closes_owned_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed second terminal open releases the already-open reader."""
    class Reader:
        closed = False

        def close(self) -> None:
            self.closed = True

    reader = Reader()
    monkeypatch.setattr(probe.os, "isatty", lambda fd: True)
    monkeypatch.setattr(probe.os, "getpgrp", lambda: 5)
    monkeypatch.setattr(probe.os, "tcgetpgrp", lambda fd: 5)
    terminal_stat = os.stat_result((0o020000, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(probe.os, "fstat", lambda fd: terminal_stat)
    calls = 0

    def fake_open(*args: object, **kwargs: object) -> Reader:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic terminal error")
        return reader

    monkeypatch.setattr("builtins.open", fake_open)
    assert probe._foreground_consent() is False
    assert reader.closed is True


def test_terminal_write_failure_closes_both_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failure after both handles open cannot leave the controlling TTY open."""
    class Handle:
        closed = False

        def fileno(self) -> int:
            return 9

        def write(self, value: str) -> None:
            raise OSError("synthetic write failure")

        def close(self) -> None:
            self.closed = True

    handles = [Handle(), Handle()]
    monkeypatch.setattr(probe.os, "isatty", lambda fd: True)
    monkeypatch.setattr(probe.os, "getpgrp", lambda: 5)
    monkeypatch.setattr(probe.os, "tcgetpgrp", lambda fd: 5)
    terminal_stat = os.stat_result((0o020000, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    monkeypatch.setattr(probe.os, "fstat", lambda fd: terminal_stat)
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: handles.pop(0))
    reader, writer = Handle(), Handle()
    handles[:] = [reader, writer]
    assert probe._foreground_consent() is False
    assert reader.closed and writer.closed


def test_local_config_rejects_unreviewed_path_without_reading_it(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The probe refuses a different config path without disclosing content."""
    private = tmp_path / "private.env"
    private.write_text("TOP_SECRET_DO_NOT_PERSIST")
    monkeypatch.setenv("MORGOTH_LOCAL_CONFIG", str(private))
    with pytest.raises(CapabilityRefusal, match="LOCAL_MODEL_CONFIG_UNAVAILABLE"):
        probe._local_model_config()


def test_reviewed_config_path_uses_local_values_without_file_output(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved path is loaded with synthetic local settings only."""
    expected = probe.ROOT.parent / "morgoth" / ".env"
    monkeypatch.setenv("MORGOTH_LOCAL_CONFIG", str(expected))
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_PRIMARY_MODEL", raising=False)
    monkeypatch.setattr(probe.Path, "exists", lambda path: False)
    monkeypatch.setattr(probe.Path, "is_file", lambda path: path == expected)
    monkeypatch.setattr(probe, "dotenv_values", lambda path: {
        "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
        "OLLAMA_PRIMARY_MODEL": "synthetic-model"})
    config = probe._local_model_config()
    assert config.ollama_base_url == "http://127.0.0.1:11434"
    assert config.ollama_primary_model == "synthetic-model"
