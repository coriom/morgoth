"""Gates — the runtime pipeline for self-modify proposals.

Two gates, in order:

1. ``gate_zone``:  pure classification via ``zones.classify_proposal``.
   Red → ``zone_rejected`` (terminal). Green → advance.

2. ``gate_tests``: copy the working tree to a sandbox, write the proposal's
   new_file content into the sandbox at ``target_path``, run the FULL
   pytest suite from the sandbox using the main venv's Python. Non-zero
   exit → ``tests_failed`` (terminal, with the tail of pytest output as
   reason). Zero exit → ``pending_approval``.

``run_pipeline`` is the only entry point.

Notes
-----
- The sandbox NEVER writes into the live tree. Sandbox copy excludes
  ``.venv``, ``.git``, ``data``, ``__pycache__``, ``vault``, ``backups``
  to keep the copy cheap. Sandbox is cleaned in ``finally``.
- APPLY DOES NOT EXIST in this module. Reaching ``pending_approval`` /
  ``approved_pending_apply`` is a status-only transition; the file the
  proposal describes is never merged into the live tree by this code.
"""

from __future__ import annotations

import asyncio
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from self_modify import proposals as P
from self_modify import zones


# Directory names to skip when copying the working tree into the sandbox.
# .env is excluded — the copy tree must never contain production secrets;
# even under fs confinement, a leaked-into-copy secret is a leak.
_SANDBOX_IGNORE = shutil.ignore_patterns(
    ".venv",
    ".git",
    "data",
    "__pycache__",
    "vault",
    "backups",
    "*.egg-info",
    ".pytest_cache",
    "node_modules",
    ".env",
    ".envrc",
    "secrets",
)

# Path to the venv interpreter used to drive pytest inside the sandbox.
# Kept absolute so it works regardless of caller cwd or sandbox path.
_VENV_PYTHON = "/home/corio/Morgoth/morgoth/.venv/bin/python"
_VENV_ROOT = "/home/corio/Morgoth/morgoth/.venv"


# 2026-09-27 SINGLE-SOURCE PYTEST ARGV. Both gate_tests and `morgoth
# test` build their pytest command from this same list. Adding a new
# arg (e.g. `--fail-on-open-file`) here reaches both callers with no
# drift. Grep-locked in tests/test_positive_control_defillama.py.
HERMETIC_PYTEST_EXTRA_ARGS: list[str] = [
    "-m", "not integration",
    "--disable-socket", "--allow-unix-socket",
    # 2026-09-28 pytest-timeout: no hermetic test should take longer
    # than 60 s. A hang fails with a per-test stack trace instead of
    # freezing the run (and the gate). thread method covers async
    # code paths where signal-based alarms can miss.
    "--timeout=60", "--timeout-method=thread",
]

# Hardening budget — two enforcement paths because WSL2's kernel does
# not reliably honor cgroup ``memory.max`` at the user-scope level (a
# 150 MB cap allowed a 500 MB allocator through in the empirical probe
# at wiring time). We therefore combine:
#   * ``prlimit --as=<bytes>``  → kernel-enforced RLIMIT_AS per process
#     (a bloated pytest-xdist worker is killed with MemoryError even
#     when cgroup MemoryMax is a no-op).
#   * ``systemd-run --property=MemoryMax=…``  → cumulative cap when the
#     controller is actually active (works on kernels that do enforce).
#   * ``systemd-run --property=TasksMax=…``   → pids cgroup IS enforced
#     under WSL2 — bounds fork bombs at the cgroup level.
#   * ``systemd-run --property=CPUQuota=…``   → cpu cgroup bounds CPU %.
#
# 7.6 GB host → 5 GB cumulative cap (leaves ~2.6 GB headroom for
# morgoth + host). Per-process RLIMIT_AS 3 GB — bigger than any legit
# xdist worker under our suite, smaller than a real memory bomb.
_MEMORY_MAX_BYTES = 5 * 1024**3        # cumulative cgroup cap
_TASKS_MAX = 1024
_CPU_QUOTA_PCT = 800
# 2026-09-28: RLIMIT_AS removed. `prlimit --as=3G` killed workers that
# reserved large VIRTUAL memory but modest RSS — onnxruntime behind
# chromadb's DefaultEmbeddingFunction hits ~1.9 GB VmSize on load,
# and xdist could crash a worker under transient AS pressure. Real
# memory is bounded by the cgroup MemoryMax below; virtual memory
# is not the axis we want to police.
# Worker count sized to cgroup budget: MemoryMax / peak-per-worker.
# Peak per worker measured at ~1.9 GB VmSize but ~126 MB RSS —
# use a modest -n 4 so 4 workers × ~500 MB RSS steady + spikes fit
# in 5 GB with headroom for the outer bwrap/unshare processes.
_SANDBOX_XDIST_WORKERS = 4

# Hard timeout for pytest under the sandbox — a proposal that hangs the
# suite is a failure, not an outage.
#
# Sizing rationale (adaptive budget, floor-guarded):
#   Under the xdist wrapper, gate_tests replays the full suite AND
#   the reflect flow waits for the concurrent 450s liveness probe on
#   gate exit ("correctness over wall time"). A 2.5× headroom on the
#   test wall time alone under-covers when tests ≈ probe window: a
#   valid proposal whose sandbox completed near the headroom edge
#   would still miss the probe window. The floor formula covers this:
#
#     _SANDBOX_DEFAULT_TIMEOUT_SECS = max(
#         ceil(measured × 2.5 / 60) × 60,
#         PROBE_WINDOW_SECS + measured + 300,
#     )
#
#   The first term is the classic headroom on the tests. The second
#   term guarantees budget ≥ tests + probe + variance regardless of
#   how the headroom multiplier lands. Whichever is larger wins.
#
#   Measured under the exact gate_tests invocation (unshare --user
#   --map-root-user --net + venv interpreter + pytest -n auto,
#   631-test suite): 2672s. PROBE_WINDOW_SECS = 450s (4 hits × 150s
#   gap, see self_modify.liveness). Env override unchanged.
#
# Subset-run alternative is REJECTED: gate_tests must prove the
# proposal doesn't break ANYTHING. A slower gate is preferable to a
# weaker gate — pytest-xdist is the correct lever because it
# preserves full-suite coverage.
_SANDBOX_MEASURED_SECS: int = 2672
_SANDBOX_HEADROOM: float = 2.5
_PROBE_WINDOW_SECS: int = 450  # 4 hits × 150s gap (self_modify.liveness)
_PROBE_VARIANCE_MARGIN_SECS: int = 300  # tolerate probe-side + gate-exit slop

_SANDBOX_DEFAULT_TIMEOUT_SECS: int = max(
    int(math.ceil(_SANDBOX_MEASURED_SECS * _SANDBOX_HEADROOM / 60) * 60),
    _PROBE_WINDOW_SECS + _SANDBOX_MEASURED_SECS + _PROBE_VARIANCE_MARGIN_SECS,
)


def _resolve_sandbox_timeout() -> int:
    """Env override > default. Non-positive/non-int env → default."""
    raw = os.environ.get("SANDBOX_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _SANDBOX_DEFAULT_TIMEOUT_SECS


SANDBOX_TIMEOUT_SECONDS: int = _resolve_sandbox_timeout()
_PYTEST_TIMEOUT_SECS = SANDBOX_TIMEOUT_SECONDS
# Single source of truth for every pytest-invoking subprocess in
# self_modify/. Both gate_tests (sandbox suite) and apply's
# _run_live_pytest (live-tree suite) consume this — divergent budgets
# were the bug class:
#   - 180s sandbox limit → 4/? proposals died tests_failed (742e7d5e).
#   - 1380s sandbox limit → 1/? died (acdba238).
#   - 300s apply limit    → 1182ee96 (liveness PASS, shadow APPROVE,
#                            operator-approved) died apply_failed on
#                            infrastructure (first live rollback of
#                            the project — rollback itself worked).
# Env override SANDBOX_TIMEOUT_SECONDS propagates to BOTH sites.
PYTEST_BUDGET_SECS: int = SANDBOX_TIMEOUT_SECONDS

# Cached feasibility probes for hardening layers. Each probe runs at
# most once per process lifetime; a False result degrades that ONE
# layer with a loud warning, while the surviving layers still apply.
# Layers (independent, additive):
#   isolation:      unshare --user --map-root-user --net  (netns + user_ns)
#   confinement:    bwrap  (fs isolation + env scrubbing via --clearenv)
#   cgroup_limits:  systemd-run --user --scope --property=MemoryMax/...
_isolation_available_cache: bool | None = None
_bwrap_available_cache: bool | None = None
_cgroup_limits_available_cache: bool | None = None


def _isolation_available() -> bool:
    """True iff ``unshare --user --map-root-user --net`` returns 0.

    Cached after first call. Loopback UP inside the ns requires
    ``--map-root-user`` on WSL2 kernels; the probe just checks that
    the ns can be entered at all — the wrapper takes care of raising
    lo. A False here means the sandbox will run un-isolated with a
    logged warning and a status_reason marker so the operator can
    see the degraded posture at gate 3.
    """
    global _isolation_available_cache
    if _isolation_available_cache is not None:
        return _isolation_available_cache
    try:
        rc = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "true"],
            capture_output=True,
            timeout=5,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        rc = 1
    _isolation_available_cache = rc == 0
    return _isolation_available_cache


def _bwrap_available() -> bool:
    """True iff a minimal ``bwrap`` invocation succeeds. Cached.

    False → sandbox_unavailable at gate_tests (fail-closed since
    2026-09-24 — was a WARNING before, but WARN is not a control).

    IMPORTANT: the probe MUST mirror the real invocation's bind set.
    Earlier version bound only /usr; on usrmerge systems /bin is a
    symlink into /usr/bin whose ELF interpreter is at /lib64/ld-*.
    Without /lib and /lib64 bound, `execvp true` fails with ENOENT
    and the probe FALSELY reports bwrap missing while bwrap itself
    is fine. This is the bug that let the operator's run continue
    with confinement off.
    """
    global _bwrap_available_cache
    if _bwrap_available_cache is not None:
        return _bwrap_available_cache
    try:
        rc = subprocess.run(
            ["bwrap",
             "--ro-bind", "/usr", "/usr",
             "--ro-bind", "/lib", "/lib",
             "--ro-bind", "/lib64", "/lib64",
             "--ro-bind", "/bin", "/bin",
             "--proc", "/proc", "--dev", "/dev",
             "--unshare-user", "--",
             "/bin/true"],
            capture_output=True,
            timeout=5,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        rc = 1
    _bwrap_available_cache = rc == 0
    return _bwrap_available_cache


def sandbox_posture() -> dict[str, Any]:
    """Cheap probe used by `morgoth env` and `morgoth session-report`.
    Returns {isolated, confined, cgroup_bound, ok, reason}. `ok` is
    True iff ALL three layers are available. Reason lists the missing
    layer(s) for the operator. NEVER runs pytest.
    """
    iso = _isolation_available()
    cnf = _bwrap_available() if iso else False
    cgb = _cgroup_limits_available() if iso else False
    missing: list[str] = []
    if not iso:
        missing.append("netns/unshare")
    if not cnf:
        missing.append("bwrap")
    if not cgb:
        missing.append("systemd-run cgroup")
    return {
        "isolated": iso, "confined": cnf, "cgroup_bound": cgb,
        "ok": iso and cnf and cgb,
        "reason": "" if (iso and cnf and cgb) else "missing: " + ", ".join(missing),
    }


def reset_probe_cache() -> None:
    """Test hook — clears the memoized layer probes so a monkeypatched
    subprocess result is picked up on the next call."""
    global _isolation_available_cache, _bwrap_available_cache, _cgroup_limits_available_cache
    _isolation_available_cache = None
    _bwrap_available_cache = None
    _cgroup_limits_available_cache = None


def _cgroup_limits_available() -> bool:
    """True iff ``systemd-run --user --scope`` can attach cgroup properties.

    Requires the user session bus reachable via XDG_RUNTIME_DIR. From
    morgoth.service (system slice), we inject XDG_RUNTIME_DIR in
    _hardened_outer_env below so the probe and the real call both
    have the runtime dir path. False here means a runaway allocator
    cannot be killed by the kernel — it only dies at the pytest
    timeout (thrashing risk).
    """
    global _cgroup_limits_available_cache
    if _cgroup_limits_available_cache is not None:
        return _cgroup_limits_available_cache
    try:
        rc = subprocess.run(
            ["systemd-run", "--user", "--scope", "--quiet",
             "--property=MemoryMax=100M", "--property=TasksMax=10",
             "--", "true"],
            capture_output=True,
            env=_hardened_outer_env(),
            timeout=5,
        ).returncode
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        rc = 1
    _cgroup_limits_available_cache = rc == 0
    return _cgroup_limits_available_cache


def _hardened_outer_env() -> dict[str, str]:
    """Minimal env passed to the outer subprocess (systemd-run / bwrap).

    Explicit whitelist — parent env is NOT propagated. This is the
    first line of defense against secret leakage: FRED_API_KEY,
    POSTGRES_URL, TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY that live in
    morgoth.service's Environment= directives must not reach the
    sandboxed pytest process. XDG_RUNTIME_DIR is required so
    ``systemd-run --user`` reaches the session bus from a system
    service context.
    """
    uid = os.getuid()
    return {
        "PATH": "/usr/sbin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
    }


async def gate_zone(
    store: P.ProposalStore,
    proposal: dict[str, Any],
) -> str:
    """Classify + persist. Return the resulting status."""
    zone = zones.classify_proposal(
        target_path=proposal["target_path"],
        change_type=proposal["change_type"],
    )
    logger.info(
        "gate_zone: proposal_id={} target={!r} change_type={!r} → zone={}",
        proposal["proposal_id"],
        proposal["target_path"],
        proposal["change_type"],
        zone,
    )
    if zone != "green":
        reason = (
            f"zone={zone}: {proposal['change_type']} on {proposal['target_path']!r} "
            f"is not in the additive-only green zone"
        )
        await store.update_status(
            str(proposal["proposal_id"]),
            P.STATUS_ZONE_REJECTED,
            reason,
        )
        return P.STATUS_ZONE_REJECTED
    # green — leave status untouched; caller advances to gate_tests
    return "green"


def _build_pytest_argv(
    sandbox: Path,
    *,
    isolated: bool,
    confined: bool = False,
    cgroup_bound: bool = False,
    junit_out: Path | None = None,
    extra_pytest_args: list[str] | None = None,
    serial: bool = False,
) -> list[str]:
    """Return the argv used to invoke pytest inside ``sandbox``.

    Three additive hardening layers, each detected independently:

    - isolated   → wrap in ``unshare --user --map-root-user --net``
                   (fresh netns + user_ns, loopback raised inside).
    - confined   → wrap the inner pytest in ``bwrap --clearenv --tmpfs
                   /tmp --ro-bind <system-dirs> --bind <sandbox>``
                   so the FS view is: sandbox (rw) + system+venv (ro),
                   nothing else. ~/.env / vault / live repo unreachable.
                   ``--share-net`` inherits the outer unshare's netns.
    - cgroup_bound → wrap the whole thing in ``systemd-run --user
                   --scope --property=MemoryMax=... TasksMax=... CPUQuota=...``
                   so a runaway is killed by the kernel, not by the
                   pytest timeout — protects host from thrashing.

    ``-n auto`` distributes tests across CPU cores (pytest-xdist),
    dropping the sandbox suite wall time from ~2701s serial to ~547s
    parallel on a 12-core host. Suite is fully DB-mocked so xdist
    parallelizes clean; the two consecutive stability runs at wiring
    time produced identical 593/593 passes. When ``confined`` and the
    outer sh runs under unshare, ``ip link set lo up`` runs there
    (needs CAP_NET_ADMIN, which bwrap drops) and bwrap uses
    ``--share-net`` to inherit the netns with lo already UP.
    """
    _SANDBOX_MARKER_ARGS = list(HERMETIC_PYTEST_EXTRA_ARGS)
    # 2026-09-29: structured results. gate_tests parses junit XML, not
    # stdout — see _junit_failing_ids. The XML is written INSIDE the
    # sandbox tree (bwrap --bind sandbox → same path visible inside and
    # outside the confinement).
    if junit_out is not None:
        _SANDBOX_MARKER_ARGS = _SANDBOX_MARKER_ARGS + [f"--junitxml={junit_out}"]
    if extra_pytest_args:
        _SANDBOX_MARKER_ARGS = _SANDBOX_MARKER_ARGS + list(extra_pytest_args)
    # 2026-09-28: -n auto → -n 4 (see _SANDBOX_XDIST_WORKERS rationale).
    # --max-worker-restart=3 restarts a crashed worker up to three times
    # and reports the culprit test as failed instead of aborting the
    # whole run with `INTERNALERROR (no tests ran)`.
    if serial:
        # Flake-guard rerun path: no xdist, no worker restarts, no
        # loadfile scheduling. -p no:xdist DISABLES the plugin so
        # `-n` / `--dist` would be unknown flags — we simply omit them.
        _XDIST: list[str] = ["-p", "no:xdist"]
    else:
        _XDIST = [
            "-n", str(_SANDBOX_XDIST_WORKERS),
            "--max-worker-restart=3",
            # 2026-09-28: --dist=loadfile keeps all tests in one file on ONE
            # worker. Reduces cross-file cross-worker interactions (module
            # import ordering + shared C-extension state) that caused the
            # test_campaign_quality/TestLearnedServedPhrases worker crash
            # in the operator's `morgoth test` run.
            # NOTE (2026-09-29): --dist=loadfile is WHY workers restart
            # inside gate_tests but not in `morgoth test`. Under loadfile
            # a single worker owns every test in a file; corrupt one
            # C-extension or asyncio loop and every remaining test on
            # that file crashes, tripping --max-worker-restart. Under
            # canonical_runner's default `load`, tests are load-balanced
            # individually so a poisoned worker only affects the next
            # scheduled test, not a whole file. We keep --dist=loadfile
            # (avoids the campaign_quality cascade the operator saw)
            # and rely on junit + flake-guard rerun to catch amplified
            # regressions.
            "--dist=loadfile",
        ]
    if not isolated:
        return [_VENV_PYTHON, "-m", "pytest", "-q"] + _XDIST + _SANDBOX_MARKER_ARGS

    pytest_call = [
        _VENV_PYTHON, "-m", "pytest", "-q",
    ] + _XDIST + _SANDBOX_MARKER_ARGS

    if confined:
        import shlex
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
            "--ro-bind", _VENV_ROOT, _VENV_ROOT,
            "--tmpfs", "/tmp",
            "--bind", str(sandbox), str(sandbox),
            "--proc", "/proc",
            "--dev", "/dev",
            "--share-net",
            "--die-with-parent",
            "--chdir", str(sandbox),
            "--",
        ] + pytest_call
        inner = "ip link set lo up; exec " + " ".join(shlex.quote(a) for a in bwrap)
    else:
        pytest_str = " ".join(pytest_call)
        inner = (
            f"ip link set lo up; cd {sandbox} && "
            f"exec {pytest_str}"
        )
    outer = ["unshare", "--user", "--map-root-user", "--net",
             "sh", "-c", inner]

    if cgroup_bound:
        return [
            "systemd-run", "--user", "--scope", "--quiet",
            f"--property=MemoryMax={_MEMORY_MAX_BYTES}",
            f"--property=TasksMax={_TASKS_MAX}",
            f"--property=CPUQuota={_CPU_QUOTA_PCT}%",
            "--",
        ] + outer
    return outer


class SandboxUnavailableError(RuntimeError):
    """Raised when the sandbox cannot be established with ALL three
    confinement layers (netns, bwrap, cgroup). fail-closed: pytest MUST
    NOT run on a proposal tree unless every layer applies."""


import xml.etree.ElementTree as _ET


def _junit_failing_ids(xml_path: Path) -> tuple[set[str], set[str]]:
    """Return ``(failures, errors)`` — the two disjoint sets of node
    identifiers extracted from a pytest --junitxml file.

    Node id = ``f"{classname}::{name}"``. This is stable across runs
    (same test → same key regardless of xdist worker interleave or
    worker restarts) and is disjoint from any output-stream artefact.

    Why junit instead of `^FAILED <id>` regex over stdout: under
    xdist ``--max-worker-restart=3``, a crashed worker's tests get
    re-scheduled, and each restart re-emits FAILED lines for the
    dead worker's culprit test. Counting those lines double-counts
    a single failure and inflates the diff by up to 4× — the
    "194 new" wrapper artefact behind the last positive-control
    result. Junit records each testcase ONCE with either <failure>,
    <error> (crashed-worker cases), or <skipped>; failing set is
    authoritative.

    Return (failures, errors) so gate_tests can log them separately:
    a crash and a legit assertion failure are both regressions if
    they are NEW vs baseline, but they call for different
    debugging.
    """
    try:
        tree = _ET.parse(str(xml_path))
    except (_ET.ParseError, FileNotFoundError, OSError):
        return set(), set()
    failures: set[str] = set()
    errors: set[str] = set()
    for tc in tree.getroot().iter("testcase"):
        nid = f"{tc.attrib.get('classname','')}::{tc.attrib.get('name','')}"
        if tc.find("failure") is not None:
            failures.add(nid)
        elif tc.find("error") is not None:
            errors.add(nid)
    return failures, errors


def _run_pytest_in_sandbox(
    sandbox: Path, *, junit_out: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Blocking call — run pytest -q from ``sandbox`` under FULL hardening
    (netns + bwrap + cgroup). FAIL-CLOSED (2026-09-24): if any layer is
    unavailable, raise SandboxUnavailableError and refuse to spawn pytest.

    Prior behavior degraded to a WARNING and ran anyway — that's how the
    operator's `morgoth reflect` executed an LLM-authored file with
    filesystem confinement OFF. A warning is not a control. Env passed
    to the outer subprocess is a minimal whitelist (``_hardened_outer_env``).
    """
    posture = sandbox_posture()
    if not posture["ok"]:
        raise SandboxUnavailableError(posture["reason"])
    isolated, confined, cgroup_bound = True, True, True
    argv = _build_pytest_argv(
        sandbox, isolated=isolated, confined=confined, cgroup_bound=cgroup_bound,
        junit_out=junit_out,
    )
    # Under full hardening cwd=None (systemd-run --user + unshare change
    # working directory via the inner sh). Passing cwd=sandbox is a
    # no-op here but was needed on the degraded no-unshare path.
    completed = subprocess.run(
        argv,
        cwd=None,
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECS,
        env=_hardened_outer_env(),
        start_new_session=True,  # new process group so we can group-kill
    )
    completed.isolated = isolated  # type: ignore[attr-defined]
    completed.confined = confined  # type: ignore[attr-defined]
    completed.cgroup_bound = cgroup_bound  # type: ignore[attr-defined]
    return completed


def _rerun_ids_alone(
    sandbox: Path, node_ids: set[str], junit_out: Path,
) -> tuple[set[str], set[str]]:
    """Rerun the given node IDs in ``sandbox`` ALONE (no xdist), writing
    a fresh junit file. Return (failures, errors) from that rerun.

    Used by the flake guard: a test that fails on the parallel first
    pass but passes on the single-process rerun is not a regression —
    it's cross-worker contamination or ordering-sensitivity, and we
    do not attribute it to the proposal.

    ``-p no:xdist`` disables the parallel plugin; ``--dist=no`` is not
    honored without xdist loaded. Marker exclusion, socket cut, and
    the per-test timeout still apply (via HERMETIC_PYTEST_EXTRA_ARGS).
    """
    if not node_ids:
        return set(), set()
    # Build the pytest selector as an OR of node IDs. junit stores
    # `<classname>::<name>` which is NOT a pytest node id — we need
    # to rerun by name pattern instead. -k accepts substrings.
    # Extract test names (rightmost `::` segment stripped of params).
    names: set[str] = set()
    for nid in node_ids:
        name = nid.split("::")[-1]
        # Strip parametrize suffix `[...]` for -k matching.
        name = name.split("[")[0]
        if name:
            names.add(name)
    k_expr = " or ".join(sorted(names))
    argv = _build_pytest_argv(
        sandbox, isolated=True, confined=True, cgroup_bound=True,
        junit_out=junit_out, serial=True,
        extra_pytest_args=["-k", k_expr],
    )
    subprocess.run(
        argv, cwd=None, capture_output=True, text=True,
        timeout=_PYTEST_TIMEOUT_SECS, env=_hardened_outer_env(),
        start_new_session=True,
    )
    # returncode may be nonzero even on flake — we trust the junit.
    return _junit_failing_ids(junit_out)


# 2026-09-29: moved from /tmp/morgoth_sandbox to /var/tmp/morgoth_sandbox.
# bwrap's `--tmpfs /tmp` shadows /tmp inside the sandbox with a fresh
# tmpfs, then bind-mounts <sandbox> back on top. With the previous
# root some interaction (bwrap+WSL2, path resolution during tmpfs
# overlay) caused the sandbox tree at /tmp/morgoth_sandbox/proposal_* to
# be wiped by the primary pytest run itself — the flake-guard rerun
# would then collect 0 tests and every candidate looked like a flake.
# /var/tmp is not shadowed by --tmpfs /tmp, so the host path survives.
_SANDBOX_ROOT = Path("/var/tmp/morgoth_sandbox")


def sweep_stale_sandboxes(max_age_secs: int = 3600) -> list[str]:
    """Remove <_SANDBOX_ROOT>/proposal_* dirs older than max_age.
    Returns the list of paths removed. Called at reflect start so a
    previously-Ctrl-C'd run doesn't leave a growing crumb trail."""
    removed: list[str] = []
    if not _SANDBOX_ROOT.exists():
        return removed
    import time as _time
    now = _time.time()
    for child in _SANDBOX_ROOT.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith("proposal_"):
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        if age >= max_age_secs:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(str(child))
    return removed


async def gate_tests(
    store: P.ProposalStore,
    proposal: dict[str, Any],
    repo_root: Path = Path("/home/corio/Morgoth/morgoth"),
) -> str:
    """Sandbox pytest gate. Returns the resulting status."""
    proposal_id = str(proposal["proposal_id"])
    change_type = proposal["change_type"]

    # Only new_file is supported for green today (edits are all red).
    # Defensive: if we ever expand to edits, this branch will need to
    # apply the diff instead of writing a whole file.
    if change_type != "new_file":
        reason = f"gate_tests: unsupported change_type={change_type!r}"
        await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
        return P.STATUS_TESTS_FAILED

    # FAIL-CLOSED PRE-FLIGHT: refuse to copy the tree if the sandbox
    # cannot be established. This prevents any secret-copying-then-
    # aborting window that a degraded run would otherwise create.
    posture = sandbox_posture()
    if not posture["ok"]:
        reason = (
            f"gate_tests: refusing to run pytest — sandbox unavailable "
            f"({posture['reason']}). Install missing layer(s) and re-run."
        )
        logger.warning("{} proposal_id={}", reason, proposal_id)
        await store.update_status(
            proposal_id, P.STATUS_REJECTED_SANDBOX_UNAVAILABLE, reason,
        )
        return P.STATUS_REJECTED_SANDBOX_UNAVAILABLE

    sandbox_root = _SANDBOX_ROOT
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox = sandbox_root / f"proposal_{proposal_id}"
    baseline_sbx = sandbox_root / f"baseline_{proposal_id}"
    for p in (sandbox, baseline_sbx):
        if p.exists():
            shutil.rmtree(p)
    try:
        # 1a. BASELINE — copy tree WITHOUT the proposal file. Records
        #     the set of tests already failing on the unmodified tree.
        # 1b. PROPOSAL — copy tree WITH the proposal file. gate_tests
        #     passes iff no test that PASSES in baseline FAILS in
        #     the proposal tree (2026-09-28 design change).
        logger.info("gate_tests: copying baseline tree to {}", baseline_sbx)
        await asyncio.to_thread(
            shutil.copytree, str(repo_root), str(baseline_sbx),
            ignore=_SANDBOX_IGNORE,
        )
        logger.info("gate_tests: copying proposal tree to {}", sandbox)
        await asyncio.to_thread(
            shutil.copytree, str(repo_root), str(sandbox), ignore=_SANDBOX_IGNORE
        )
        # 2026-09-29 sweep-collision fix: copytree propagates the SOURCE
        # directory's mtime (via copystat). If repo_root is >1 h old,
        # sweep_stale_sandboxes fires ON A FRESH SANDBOX because it
        # judges "age" from mtime. Any test that indirectly triggers
        # sweep (e.g. reflect.run_reflection under test) would then
        # wipe the sandbox tree MID-RUN — the tests directory
        # disappearing under pytest's feet was the bug behind
        # "no tests ran in 0.00s" on the flake-guard rerun.
        _now = None  # os.utime(path, None) → current time on both atime + mtime
        for p in (sandbox, baseline_sbx):
            os.utime(p, _now)
        for stray in list(sandbox.rglob(".env")) + list(baseline_sbx.rglob(".env")):
            if stray.is_file():
                raise RuntimeError(
                    f"sandbox contains .env at {stray} — refusing to run"
                )
        target = sandbox / proposal["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal["content"], encoding="utf-8")
        # 3a. Run baseline pytest FIRST (baseline_failures).
        baseline_junit = baseline_sbx / "junit.xml"
        proposal_junit = sandbox / "junit.xml"
        logger.info("gate_tests: running baseline pytest in {}", baseline_sbx)
        try:
            baseline_run = await asyncio.to_thread(
                _run_pytest_in_sandbox, baseline_sbx, junit_out=baseline_junit,
            )
        except SandboxUnavailableError as exc:
            reason = f"gate_tests: sandbox unavailable at baseline ({exc})"
            await store.update_status(
                proposal_id, P.STATUS_REJECTED_SANDBOX_UNAVAILABLE, reason,
            )
            return P.STATUS_REJECTED_SANDBOX_UNAVAILABLE
        baseline_fail, baseline_err = _junit_failing_ids(baseline_junit)
        baseline_failures = baseline_fail | baseline_err
        logger.info(
            "gate_tests: baseline junit → failures={} errors={}",
            len(baseline_fail), len(baseline_err),
        )
        # 3b. Run proposal pytest.
        logger.info("gate_tests: running proposal pytest in {}", sandbox)
        try:
            completed = await asyncio.to_thread(
                _run_pytest_in_sandbox, sandbox, junit_out=proposal_junit,
            )
        except SandboxUnavailableError as exc:
            # Race: posture flipped between pre-flight and run.
            reason = f"gate_tests: sandbox unavailable at run-time ({exc})"
            logger.warning("{} proposal_id={}", reason, proposal_id)
            await store.update_status(
                proposal_id, P.STATUS_REJECTED_SANDBOX_UNAVAILABLE, reason,
            )
            return P.STATUS_REJECTED_SANDBOX_UNAVAILABLE
        except subprocess.TimeoutExpired as exc:
            reason = f"gate_tests: pytest timed out after {_PYTEST_TIMEOUT_SECS}s"
            logger.warning("{}: {}", reason, exc)
            await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
            return P.STATUS_TESTS_FAILED
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            # Kill the sandbox process group best-effort. subprocess.run
            # already waited; on CancelledError from asyncio.to_thread
            # the child is typically already gone. Belt+braces: sweep
            # any stragglers under /tmp/morgoth_sandbox/*.
            logger.warning(
                "gate_tests: interrupted proposal_id={}: {}", proposal_id,
                type(exc).__name__,
            )
            reason = f"gate_tests: aborted by {type(exc).__name__}"
            await store.update_status(
                proposal_id, P.STATUS_ABORTED_INTERRUPTED, reason,
            )
            # Re-raise so the outer reflect loop can unwind cleanly.
            raise

        # Isolation posture — attached by _run_pytest_in_sandbox for
        # the status_reason tail so the operator sees it at gate 3.
        # Layers reported independently so a partial degrade is visible.
        iso = "on" if getattr(completed, "isolated", False) else "off"
        cnf = "on" if getattr(completed, "confined", False) else "off"
        cgb = "on" if getattr(completed, "cgroup_bound", False) else "off"
        isolation_marker = f"isolation={iso} confined={cnf} cgroup={cgb}"

        proposal_fail, proposal_err = _junit_failing_ids(proposal_junit)
        proposal_failures = proposal_fail | proposal_err
        logger.info(
            "gate_tests: proposal junit → failures={} errors={}",
            len(proposal_fail), len(proposal_err),
        )
        # 2026-09-28 GATE DESIGN: compare sets — pass iff no test that
        # passes in the baseline fails in the proposal tree. Existing
        # failures on the unmodified tree are OUT OF SCOPE for gate_tests.
        candidate_new = proposal_failures - baseline_failures
        # 2026-09-29 FLAKE GUARD: rerun each candidate-new node ID
        # ALONE (-p no:xdist) on BOTH baseline and proposal trees.
        # A candidate that does NOT reproduce on the proposal serial
        # rerun is a cross-worker/scheduling flake, not a regression.
        # Junit is the authoritative source; the naive stream-parser
        # rerun-both approach cancelled legit new failures.
        flakes: set[str] = set()
        if candidate_new:
            logger.info(
                "gate_tests: flake-guard rerun of {} candidate(s) alone",
                len(candidate_new),
            )
            prop_rerun_junit = sandbox / "junit.rerun.xml"
            base_rerun_junit = baseline_sbx / "junit.rerun.xml"
            try:
                prop_fail2, prop_err2 = await asyncio.to_thread(
                    _rerun_ids_alone, sandbox, candidate_new, prop_rerun_junit,
                )
                base_fail2, base_err2 = await asyncio.to_thread(
                    _rerun_ids_alone, baseline_sbx, candidate_new, base_rerun_junit,
                )
            except (subprocess.TimeoutExpired, SandboxUnavailableError) as exc:
                logger.warning(
                    "gate_tests: flake-guard rerun failed ({}); treating "
                    "all {} candidates as genuine",
                    exc, len(candidate_new),
                )
                prop_fail2, prop_err2 = candidate_new, set()
                base_fail2, base_err2 = set(), set()
            prop_rerun_failing = prop_fail2 | prop_err2
            base_rerun_failing = base_fail2 | base_err2
            for nid in candidate_new:
                # Rerun uses `-k <name>` (junit ids ≠ pytest node ids),
                # so match by rightmost segment + strip parametrize.
                name = nid.split("::")[-1].split("[")[0]
                prop_hit = any(
                    r.split("::")[-1].split("[")[0] == name
                    for r in prop_rerun_failing
                )
                base_hit = any(
                    r.split("::")[-1].split("[")[0] == name
                    for r in base_rerun_failing
                )
                if not prop_hit:
                    flakes.add(nid)          # didn't reproduce → flake
                elif base_hit:
                    flakes.add(nid)          # fails on baseline serial → not caused by us
        new_failures = candidate_new - flakes
        if flakes:
            logger.info(
                "gate_tests: flake-guard filtered {} → {} genuine (flakes={})",
                len(candidate_new), len(new_failures), sorted(flakes)[:5],
            )
        if new_failures:
            tail = (completed.stdout + completed.stderr)[-1500:]
            reason = (
                f"gate_tests: {len(new_failures)} NEW failure(s) not "
                f"present in baseline (baseline={len(baseline_failures)}, "
                f"proposal={len(proposal_failures)}) ({isolation_marker})\n"
                f"---new failures---\n" + "\n".join(sorted(new_failures)[:20]) +
                f"\n---tail---\n{tail}"
            )
            logger.warning(
                "gate_tests: FAIL proposal_id={} exit={} {}",
                proposal_id,
                completed.returncode,
                isolation_marker,
            )
            await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
            return P.STATUS_TESTS_FAILED

        # PASS — no NEW failures compared to baseline. Report both
        # counts so gate 3 review sees the delta.
        logger.info(
            "gate_tests: PASS proposal_id={} baseline={} proposal={} {}",
            proposal_id, len(baseline_failures), len(proposal_failures),
            isolation_marker,
        )
        await store.update_status(
            proposal_id,
            P.STATUS_PENDING_APPROVAL,
            f"gate_tests: no new failures "
            f"(baseline={len(baseline_failures)}, "
            f"proposal={len(proposal_failures)}) ({isolation_marker})",
        )
        return P.STATUS_PENDING_APPROVAL
    finally:
        # Always clean both sandboxes — success or failure.
        for p in (sandbox, baseline_sbx):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)


async def run_pipeline(
    store: P.ProposalStore,
    proposal: dict[str, Any],
    repo_root: Path = Path("/home/corio/Morgoth/morgoth"),
) -> str:
    """Zone gate then (if green) tests gate. Returns the final status."""
    zone_result = await gate_zone(store, proposal)
    if zone_result != "green":
        return zone_result
    return await gate_tests(store, proposal, repo_root=repo_root)
