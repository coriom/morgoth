"""morgoth ui launcher tests.

Shells out to scripts/morgoth-cli.sh with MORGOTH_UI_DIR pointed at a
fixture UI dir containing a fake scripts/dev.sh — so the test asserts
the dispatch, arg parsing, and env plumbing without touching the real
UI tree or requiring the backend.

Never invokes sudo or systemctl (the real cmd_start needs both). The
backend-down auto-start path is exercised by a fixture that shims
cmd_start into a no-op via a wrapper script.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest


CLI_SH = Path(__file__).resolve().parent.parent / "scripts" / "morgoth-cli.sh"


def _make_fake_ui(tmp_path: Path) -> Path:
    """A tmp UI dir with a fake dev.sh that echoes its PORT and args."""
    ui = tmp_path / "morgoth_ui"
    (ui / "scripts").mkdir(parents=True)
    dev = ui / "scripts" / "dev.sh"
    dev.write_text(
        "#!/usr/bin/env bash\n"
        "set -u\n"
        "echo \"FAKE_DEV_SH port=${PORT:-unset}\"\n"
        "echo \"pwd=$(pwd)\"\n"
    )
    dev.chmod(dev.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return ui


def _cli(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Invoke morgoth-cli.sh with a controlled env."""
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(CLI_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# --- dispatch registered --------------------------------------------------

def test_ui_appears_in_help_output() -> None:
    r = _cli(["help"])
    assert r.returncode == 0
    assert "\n  ui " in r.stdout, "ui subcommand missing from usage"


def test_ui_help_flag_prints_local_help() -> None:
    r = _cli(["ui", "--help"], env_extra={"MORGOTH_UI_DIR": "/nonexistent"})
    assert r.returncode == 0
    assert "--no-backend" in r.stdout
    assert "--port" in r.stdout


# --- --no-backend skips the health check + invokes dev.sh with PORT -----

def test_ui_no_backend_skips_health_and_execs_dev_sh(tmp_path: Path) -> None:
    ui = _make_fake_ui(tmp_path)
    r = _cli(
        ["ui", "--no-backend", "--port", "3999"],
        env_extra={"MORGOTH_UI_DIR": str(ui)},
    )
    assert r.returncode == 0, r.stderr
    # The fake dev.sh ran with PORT=3999 exported.
    assert "FAKE_DEV_SH port=3999" in r.stdout
    # And it ran with cwd = UI dir.
    assert f"pwd={ui.resolve()}" in r.stdout
    # --no-backend visibly skipped the readiness check.
    assert "skipping readiness check" in r.stdout
    # The URL hint was printed BEFORE handoff.
    assert "http://localhost:3999/wiki" in r.stdout


def test_ui_no_backend_default_port_3010(tmp_path: Path) -> None:
    ui = _make_fake_ui(tmp_path)
    r = _cli(
        ["ui", "--no-backend"],
        env_extra={"MORGOTH_UI_DIR": str(ui)},
    )
    assert r.returncode == 0, r.stderr
    assert "FAKE_DEV_SH port=3010" in r.stdout
    assert "http://localhost:3010/wiki" in r.stdout


# --- missing UI dir → clean, one-line diagnostic + non-zero exit ---------

def test_ui_missing_ui_dir_exits_cleanly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    r = _cli(
        ["ui", "--no-backend"],
        env_extra={"MORGOTH_UI_DIR": str(missing)},
    )
    assert r.returncode != 0
    assert "UI dir not found" in r.stderr
    assert str(missing) in r.stderr
    # And the URL hint / dev.sh handoff did NOT happen.
    assert "FAKE_DEV_SH" not in r.stdout
    assert "http://localhost" not in r.stdout


def test_ui_missing_dev_sh_exits_cleanly(tmp_path: Path) -> None:
    """UI dir exists but scripts/dev.sh is absent — clean error."""
    ui = tmp_path / "morgoth_ui"
    (ui / "scripts").mkdir(parents=True)  # no dev.sh created
    r = _cli(
        ["ui", "--no-backend"],
        env_extra={"MORGOTH_UI_DIR": str(ui)},
    )
    assert r.returncode != 0
    assert "dev script missing" in r.stderr


# --- backend-down path calls cmd_start before dev.sh --------------------

def test_ui_backend_down_calls_start_then_dev_sh(tmp_path: Path) -> None:
    """When the backend isn't reachable, cmd_ui must invoke cmd_start
    and only then hand off to dev.sh. We verify the ORDER by sourcing
    the real script into a wrapper that stubs cmd_start + curl."""
    ui = _make_fake_ui(tmp_path)
    wrapper = tmp_path / "wrap.sh"
    wrapper.write_text(f"""#!/usr/bin/env bash
set -u
# Source the CLI to define its functions without executing dispatch.
# Extract the function-defining prefix (everything before the dispatch
# case) and eval it. Using sed to slice at the dispatch marker.
tmp_prefix=$(mktemp)
sed -n '1,/^# ---------- usage + dispatch/p' {CLI_SH} > "$tmp_prefix"
source "$tmp_prefix"
rm -f "$tmp_prefix"

# Stub cmd_start to record its call ordering.
ORDER_FILE={tmp_path / "order.txt"}
cmd_start() {{ echo "STARTED_BACKEND" >> "$ORDER_FILE"; return 0; }}
# Stub curl to return empty (backend not ready) so the health check
# takes the not-ready branch and cmd_start fires.
curl() {{ return 22; }}
export -f curl

# Now run cmd_ui with the fake UI dir.
cmd_ui --port 3998
rc=$?
echo "rc=$rc" >> "$ORDER_FILE"
""")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    r = subprocess.run(
        ["bash", str(wrapper)],
        capture_output=True,
        text=True,
        env={**os.environ, "MORGOTH_UI_DIR": str(ui)},
        timeout=30,
    )
    assert r.returncode == 0, (r.stdout, r.stderr)
    # cmd_start was called before dev.sh handoff.
    order = (tmp_path / "order.txt").read_text()
    assert "STARTED_BACKEND" in order
    assert "rc=0" in order
    # Dev.sh actually ran after (its output goes to stdout of wrapper).
    assert "FAKE_DEV_SH port=3998" in r.stdout
    # Order in combined output: STARTED_BACKEND log line printed to
    # stderr-or-stdout ahead of the dev.sh handoff.
    assert "backend not ready; starting" in r.stdout
    idx_start = r.stdout.find("backend not ready; starting")
    idx_dev = r.stdout.find("FAKE_DEV_SH")
    assert 0 <= idx_start < idx_dev, "cmd_start must run before dev.sh"


# --- unknown arg → non-zero + clean error --------------------------------

def test_ui_rejects_unknown_arg(tmp_path: Path) -> None:
    ui = _make_fake_ui(tmp_path)
    r = _cli(
        ["ui", "--bogus"],
        env_extra={"MORGOTH_UI_DIR": str(ui)},
    )
    assert r.returncode == 2
    assert "unknown ui arg" in r.stderr
