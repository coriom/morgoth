"""Codex's exact Bubblewrap envelope: pure checks plus bounded host proof."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

from core.llm.codex_sandbox import SandboxUnavailable, build_sandbox_argv
from core.llm import codex_cli as C


def _fake_auth(root: Path) -> Path:
    home = root / "codex-home"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    auth.write_text("SYNTHETIC_AUTH_NOT_A_CREDENTIAL", encoding="utf-8")
    auth.chmod(0o600)
    return home


def _envelope(cwd: str, home: Path, *, synthetic_root: Path | None = None
              ) -> tuple[list[str], dict[str, str]]:
    if synthetic_root is None:
        binary = shutil.which("codex")
        assert binary is not None
        native_path = os.environ["PATH"]
    else:
        package = synthetic_root / "codex-package"
        binary_file = package / "bin/codex.js"
        binary_file.parent.mkdir(parents=True, exist_ok=True)
        binary_file.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        binary_file.chmod(0o700)
        (package / "package.json").write_text("{}", encoding="utf-8")
        native = package / "node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text("fixture", encoding="utf-8")
        native.chmod(0o700)
        node = synthetic_root / "node-bin/node"
        node.parent.mkdir(exist_ok=True)
        node.write_text("fixture", encoding="utf-8")
        node.chmod(0o700)
        binary = str(binary_file)
        native_path = str(node.parent)
    return build_sandbox_argv(binary, cwd,
                              {"HOME": str(home.parent), "CODEX_HOME": str(home),
                               "PATH": native_path}, ["--version"])


def test_envelope_has_no_direct_codex_or_parent_environment(monkeypatch, tmp_path):
    home = _fake_auth(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "TOP_SECRET_DO_NOT_PERSIST")
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        argv, child_env = _envelope(cwd, home, synthetic_root=tmp_path)
        assert argv[0] == "/usr/bin/bwrap"
        assert ["--unshare-all", "--unshare-user"] == argv[1:3]
        assert "--share-net" in argv and "--disable-userns" in argv
        assert ["--ro-bind", str(home / "auth.json"), "/home/codex/.codex/auth.json"] \
            in [argv[i:i + 3] for i in range(len(argv) - 2)]
        assert ["--bind", cwd, cwd] in [argv[i:i + 3] for i in range(len(argv) - 2)]
        assert "--bind-try" not in argv and "--ro-bind-try" not in argv
        assert child_env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        assert "TOP_SECRET_DO_NOT_PERSIST" not in repr((argv, child_env))


def test_exact_standalone_layout_mounts_only_the_executable(tmp_path):
    home = _fake_auth(tmp_path)
    release = tmp_path / ".codex/packages/standalone/releases/0.162.1-x86_64-unknown-linux-musl"
    binary = release / "bin/codex"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic executable", encoding="utf-8")
    binary.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        argv, child_env = build_sandbox_argv(
            str(binary), cwd, {"CODEX_HOME": str(home), "HOME": str(tmp_path),
                               "PATH": "/usr/bin:/bin"}, ["--version"])
        assert argv[argv.index("--") + 1:] == ["/opt/codex/bin/codex", "--version"]
        assert ["--ro-bind", str(binary), "/opt/codex/bin/codex"] in [
            argv[i:i + 3] for i in range(len(argv) - 2)]
        assert str(release) not in argv
        assert "--bind-try" not in argv and "--ro-bind-try" not in argv
        assert child_env == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    wrong = tmp_path / ".codex/packages/standalone/releases/0.162.2-x86_64-unknown-linux-musl/bin/codex"
    wrong.parent.mkdir(parents=True)
    wrong.write_text("synthetic executable", encoding="utf-8")
    wrong.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        with pytest.raises(SandboxUnavailable, match="sandbox_codex_layout_invalid"):
            build_sandbox_argv(str(wrong), cwd, {"CODEX_HOME": str(home)}, [])


def test_symlink_auth_and_unsafe_workspace_fail_closed(tmp_path):
    home = _fake_auth(tmp_path)
    auth = home / "auth.json"
    auth.rename(home / "real-auth.json")
    auth.symlink_to(home / "real-auth.json")
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        with pytest.raises(SandboxUnavailable, match="sandbox_auth_unsafe"):
            _envelope(cwd, home, synthetic_root=tmp_path)
    auth.unlink()
    (home / "real-auth.json").rename(auth)
    aliased = tmp_path / "aliased-home"
    aliased.symlink_to(home, target_is_directory=True)
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        with pytest.raises(SandboxUnavailable, match="sandbox_auth_unsafe"):
            _envelope(cwd, aliased, synthetic_root=tmp_path)
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        Path(cwd).chmod(0o755)
        with pytest.raises(SandboxUnavailable, match="sandbox_workspace_invalid"):
            _envelope(cwd, home, synthetic_root=tmp_path)


@pytest.mark.integration
def test_exact_envelope_denies_host_files_and_cleans_workspace(tmp_path):
    """No model claim: the OS denies a synthetic process using production mounts."""
    home = _fake_auth(tmp_path)
    outside = tmp_path / "outside-marker"
    outside.write_text("TOP_SECRET_DO_NOT_PERSIST", encoding="utf-8")
    fake_ssh = tmp_path / "fake-ssh-key"
    fake_ssh.write_text("SYNTHETIC_PRIVATE_KEY", encoding="utf-8")
    unrelated_home = tmp_path / "unrelated-home"
    unrelated_home.mkdir()
    (unrelated_home / "marker").write_text("SYNTHETIC_HOME_FILE", encoding="utf-8")
    workspace_name = ""
    script = '''import json,os,pathlib,sys
work=pathlib.Path.cwd()
outside=pathlib.Path(sys.argv[1]); fake_ssh=pathlib.Path(sys.argv[2])
def readable(path):
 try: path.read_text(); return True
 except OSError: return False
def writable(path):
 try: path.write_text("changed"); return True
 except OSError: return False
(work/"escape").symlink_to(outside)
(work/"allowed").write_text("allowed")
print(json.dumps({"outside_read":readable(outside),"outside_write":writable(outside),
"ssh_read":readable(fake_ssh),"home_read":readable(pathlib.Path(sys.argv[3])),
"repo_visible":pathlib.Path(sys.argv[4]).exists(),
"symlink_read":readable(work/"escape"),"symlink_write":writable(work/"escape"),
"parent_traversal":readable(work/".."/outside.name),
"allowed_write":(work/"allowed").read_text()=="allowed"}))'''
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        workspace_name = cwd
        argv, child_env = _envelope(cwd, home)
        separator = argv.index("--")
        argv = argv[:separator + 1] + ["/usr/bin/python3", "-c", script,
                                     str(outside), str(fake_ssh),
                                     str(unrelated_home / "marker"), str(Path(__file__).parents[1])]
        result = subprocess.run(argv, cwd=cwd, env=child_env, capture_output=True,
                                text=True, timeout=15, check=True)
        verdict = json.loads(result.stdout)
        assert verdict == {"outside_read": False, "outside_write": False,
                           "ssh_read": False, "home_read": False,
                           "repo_visible": False, "symlink_read": False,
                           "symlink_write": False, "parent_traversal": False,
                           "allowed_write": True}
        assert outside.read_text(encoding="utf-8") == "TOP_SECRET_DO_NOT_PERSIST"
        assert (Path(cwd) / "allowed").read_text(encoding="utf-8") == "allowed"
    assert not Path(workspace_name).exists()


@pytest.mark.integration
def test_exact_envelope_preserves_read_only_chatgpt_login():
    """Read-only auth mount is sufficient for CLI status, without inference."""
    binary = shutil.which("codex")
    assert binary is not None
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
        argv, env = build_sandbox_argv(binary, cwd,
                                       {k: os.environ[k] for k in ("HOME", "PATH", "CODEX_HOME")
                                        if k in os.environ}, ["login", "status"])
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=20)
        assert result.returncode == 0
        assert "Logged in using ChatGPT" in result.stdout + result.stderr


@pytest.mark.integration
def test_timeout_and_cancellation_reap_confined_process(monkeypatch, tmp_path):
    """A delayed synthetic write must not survive either owned termination path."""
    home = _fake_auth(tmp_path)
    python = '''import pathlib,time
pathlib.Path("ready").write_text("ready")
time.sleep(1.5)
pathlib.Path("late").write_text("orphan")'''
    def fixture_command(argv, cwd, env, args):
        wrapped, cleared = build_sandbox_argv(argv, cwd, env, args)
        return wrapped[:wrapped.index("--") + 1] + ["/usr/bin/python3", "-c", python], cleared
    monkeypatch.setattr(C, "build_sandbox_argv", fixture_command)
    binary = shutil.which("codex")
    assert binary is not None
    env = {"HOME": str(home.parent), "CODEX_HOME": str(home), "PATH": os.environ["PATH"]}
    for cancelled in (False, True):
        with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
            if cancelled:
                stop = threading.Event()
                result: list[str] = []
                def worker():
                    token = C._CANCEL_EVENT.set(stop)
                    try:
                        C._run([binary, "exec"], cwd, env, "", 5)
                    except C.CodexCliError as exc:
                        result.append(exc.code)
                    finally:
                        C._CANCEL_EVENT.reset(token)
                thread = threading.Thread(target=worker)
                thread.start()
                deadline = time.monotonic() + 2
                while not (Path(cwd) / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                assert (Path(cwd) / "ready").exists()
                stop.set()
                thread.join(timeout=3)
                assert not thread.is_alive() and result == ["cancelled"]
            else:
                with pytest.raises(C.CodexCliError, match="timeout"):
                    C._run([binary, "exec"], cwd, env, "", 0.5)
            time.sleep(1.6)
            assert not (Path(cwd) / "late").exists()
