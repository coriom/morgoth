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
)

# Path to the venv interpreter used to drive pytest inside the sandbox.
# Kept absolute so it works regardless of caller cwd or sandbox path.
_VENV_PYTHON = "/home/corio/Morgoth/morgoth/.venv/bin/python"

# Hard timeout for pytest under the sandbox — a proposal that hangs the
# suite is a failure, not an outage.
#
# Sizing rationale (adaptive budget):
#   The FULL suite runs inside the ``unshare --user --map-root-user
#   --net`` wrapper — namespace setup + no-DNS behavior pushes suite
#   wall time far above the host baseline. Measured at commit
#   c780263 (584 tests): 2701s under the wrapper (host baseline ~143s;
#   the 18× multiplier is namespace + network-loss cost, not per-test
#   slowness). Budget = ceil(measured × 2.5 / 60) × 60 = 6780s, which
#   defers the next raise by roughly a year of deliverables at the
#   current growth rate. Env-overridable via SANDBOX_TIMEOUT_SECONDS
#   for one-off operator triage without editing the constant.
#
# Subset-run alternative is REJECTED: gate_tests must prove the
# proposal doesn't break ANYTHING (a proposal only proving it doesn't
# break its own module has not proved integration safety). A slower
# gate is preferable to a weaker gate.
_SANDBOX_MEASURED_SECS: int = 2701
_SANDBOX_HEADROOM: float = 2.5
_SANDBOX_DEFAULT_TIMEOUT_SECS: int = int(math.ceil(
    _SANDBOX_MEASURED_SECS * _SANDBOX_HEADROOM / 60
) * 60)


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

# Cached feasibility probe for user+net namespace isolation. The probe
# tries to enter a user+net ns and immediately exit; anything non-zero
# means the kernel or seccomp policy is denying the operation and we
# should fail-open with a loud warning (defense-in-depth degraded —
# the repr-based template render is still the primary injection
# barrier). Cached because the probe runs ~10ms and gate_tests can
# fire in a tight cycle.
_isolation_available_cache: bool | None = None


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


def _build_pytest_argv(sandbox: Path, *, isolated: bool) -> list[str]:
    """Return the argv used to invoke pytest inside ``sandbox``.

    Isolated form wraps the invocation in ``unshare --user
    --map-root-user --net sh -c "ip link set lo up; cd <sandbox> &&
    exec <venv-python> -m pytest -q"`` so the sandbox has loopback
    reachable (needed by any test that binds to 127.0.0.1) but no
    route to the outside world. Non-isolated form is the plain call
    the sandbox always used.
    """
    if isolated:
        inner = (
            f"ip link set lo up; cd {sandbox} && "
            f"exec {_VENV_PYTHON} -m pytest -q"
        )
        return ["unshare", "--user", "--map-root-user", "--net",
                "sh", "-c", inner]
    return [_VENV_PYTHON, "-m", "pytest", "-q"]


def _run_pytest_in_sandbox(sandbox: Path) -> subprocess.CompletedProcess[str]:
    """Blocking call — run pytest -q from ``sandbox``, wrapped in a
    user+net namespace when the kernel supports it.

    The wrapper appears in argv when isolation is available; when it
    is not, the caller sees the plain invocation AND a warning line
    in the logs. The isolation decision is made per-call (probe is
    cached) so a kernel update that flips the feature does not
    require a Morgoth restart.
    """
    isolated = _isolation_available()
    if not isolated:
        logger.warning(
            "sandbox network isolation UNAVAILABLE — running with host "
            "network (defense-in-depth degraded)"
        )
    argv = _build_pytest_argv(sandbox, isolated=isolated)
    # cwd needs to point at the sandbox for the non-isolated form;
    # under isolation the ``cd`` inside the shell script does that job
    # from inside the ns and cwd here is irrelevant.
    cwd = None if isolated else str(sandbox)
    completed = subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECS,
    )
    # Attach the isolation marker to the completed process for the
    # caller to append to status_reason. subprocess.CompletedProcess
    # has no free slot, so we stash on a wrapper attribute.
    completed.isolated = isolated  # type: ignore[attr-defined]
    return completed


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

    sandbox_root = Path("/tmp/morgoth_sandbox")
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox = sandbox_root / f"proposal_{proposal_id}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    try:
        # 1. Copy live tree to sandbox (excludes venv/git/data/etc).
        logger.info("gate_tests: copying tree to sandbox {}", sandbox)
        await asyncio.to_thread(
            shutil.copytree, str(repo_root), str(sandbox), ignore=_SANDBOX_IGNORE
        )
        # 2. Write proposal's content at target_path inside sandbox.
        target = sandbox / proposal["target_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(proposal["content"], encoding="utf-8")
        # 3. Run pytest.
        logger.info("gate_tests: running pytest in {}", sandbox)
        try:
            completed = await asyncio.to_thread(_run_pytest_in_sandbox, sandbox)
        except subprocess.TimeoutExpired as exc:
            reason = f"gate_tests: pytest timed out after {_PYTEST_TIMEOUT_SECS}s"
            logger.warning("{}: {}", reason, exc)
            await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
            return P.STATUS_TESTS_FAILED

        # Isolation posture — attached by _run_pytest_in_sandbox for
        # the status_reason tail so the operator sees it at gate 3.
        isolation_marker = (
            "isolation=on" if getattr(completed, "isolated", False) else "isolation=off"
        )

        if completed.returncode != 0:
            # Capture the tail of stdout+stderr for the operator (kept
            # bounded — full pytest output can be huge).
            tail = (completed.stdout + completed.stderr)[-2000:]
            reason = (
                f"gate_tests: pytest exit={completed.returncode} "
                f"({isolation_marker})\n---tail---\n{tail}"
            )
            logger.warning(
                "gate_tests: FAIL proposal_id={} exit={} {}",
                proposal_id,
                completed.returncode,
                isolation_marker,
            )
            await store.update_status(proposal_id, P.STATUS_TESTS_FAILED, reason)
            return P.STATUS_TESTS_FAILED

        # PASS
        logger.info(
            "gate_tests: PASS proposal_id={} {}", proposal_id, isolation_marker,
        )
        await store.update_status(
            proposal_id,
            P.STATUS_PENDING_APPROVAL,
            f"gate_tests: pytest passed in sandbox ({isolation_marker})",
        )
        return P.STATUS_PENDING_APPROVAL
    finally:
        # Always clean the sandbox — success or failure.
        if sandbox.exists():
            shutil.rmtree(sandbox, ignore_errors=True)


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
