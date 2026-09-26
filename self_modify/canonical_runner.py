"""Canonical hermetic test runner — the exact wrapper gate_tests uses.

Developer and gate see ONE environment. Sockets disabled
(pytest-socket --disable-socket --allow-unix-socket), marker
exclusion `-m "not integration"`, full netns + bwrap + cgroup
confinement, MORGOTH_SANDBOX=1 unset (marker-based exclusion is
the only gate of test selection).

`morgoth test [pytest-args...]` — extra args pass through to pytest.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path("/home/corio/Morgoth/morgoth")


def main() -> int:
    from self_modify import gates
    posture = gates.sandbox_posture()
    if not posture["ok"]:
        print(f"ERROR: sandbox unavailable ({posture['reason']}); "
              f"install missing layer(s) and re-run.", file=sys.stderr)
        return 2

    sandbox = Path(tempfile.mkdtemp(prefix="morgoth_test_"))
    try:
        print(f"copying tree → {sandbox}")
        shutil.copytree(REPO_ROOT, sandbox / "repo",
                         ignore=gates._SANDBOX_IGNORE)
        venv = "/home/corio/Morgoth/morgoth/.venv"
        extra = " ".join(shlex.quote(a) for a in sys.argv[1:])
        # Same wrapper as gate_tests's _build_pytest_argv, plus
        # --disable-socket --allow-unix-socket to enforce the network
        # invariant. Note: `-m "not integration"` is passed AS pytest
        # args — the developer test runner and gate_tests agree.
        # SINGLE-SOURCE argv (see gates.HERMETIC_PYTEST_EXTRA_ARGS).
        # gate_tests and `morgoth test` build from the SAME list so
        # they cannot drift.
        hermetic = " ".join(shlex.quote(a) for a in gates.HERMETIC_PYTEST_EXTRA_ARGS)
        pytest_call = (
            f"prlimit --as={3*1024**3} -- {venv}/bin/python -m pytest "
            f"-q -n auto {hermetic} {extra}"
        )
        bwrap = [
            "bwrap", "--clearenv",
            "--setenv", "PATH", "/usr/sbin:/usr/bin:/bin",
            "--setenv", "HOME", str(sandbox / "repo"),
            "--setenv", "LANG", "C.UTF-8",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
            "--ro-bind", "/lib64", "/lib64", "--ro-bind", "/bin", "/bin",
            "--ro-bind", "/etc", "/etc", "--ro-bind", venv, venv,
            "--tmpfs", "/tmp",
            "--bind", str(sandbox / "repo"), str(sandbox / "repo"),
            "--proc", "/proc", "--dev", "/dev",
            "--share-net", "--die-with-parent",
            "--chdir", str(sandbox / "repo"), "--",
            "sh", "-c", pytest_call,
        ]
        inner = "ip link set lo up; exec " + " ".join(shlex.quote(a) for a in bwrap)
        outer = ["unshare", "--user", "--map-root-user", "--net",
                  "sh", "-c", inner]
        t0 = time.monotonic()
        r = subprocess.run(
            outer, env=gates._hardened_outer_env(),
        )
        wall = time.monotonic() - t0
        print(f"\n═══ morgoth test: wall={wall:.1f}s  exit={r.returncode} ═══")
        return r.returncode
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
