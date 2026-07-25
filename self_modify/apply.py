"""Apply — the door. The ONLY writer of the live tree.

Called from ``python -m self_modify.cli apply <id>`` (human action). Never
called from Morgoth. Never wired into brain.py.

Sequence (each step logs to loguru and updates the DB ``status_reason``):

  1. Preconditions — refuse with an explicit reason if ANY fails:
       - status == approved_pending_apply
       - change_type == new_file (edits are all red anyway; this is
         defense in depth)
       - classify_proposal(target, change_type) is STILL "green" (a
         second call after time has passed; the wall may have moved)
       - the target path does not already exist on disk
       - the live git tree is clean (``git status --porcelain`` empty)
  2. Write the file at target_path.
  3. Run the FULL pytest suite on the live tree. Non-zero exit →
     delete the file, ``apply_failed_rolled_back`` (reason = pytest tail).
  4. ``git add <file>`` && ``git commit -m "[self-modify] apply proposal
     #<id>: <target_path>"`` — LOCAL ONLY. Push remains a human act.
  5. Restart: ``sudo -n /usr/bin/systemctl restart morgoth.service``
     (covered by the NOPASSWD whitelist installed for the CLI).
  6. Health check ≤ 90s: ``/api/brain/status`` returns ``ready=true``
     AND ``/api/tools`` contains the new tool's ``name``. On failure:
     ``git reset --hard HEAD~1`` to drop the local apply commit, restart
     again to bring service back to the pre-apply state, and record
     ``apply_failed_rolled_back``.
  7. Success → ``applied``.

Design note: reset vs revert
----------------------------
The commit is LOCAL and unpushed. ``git reset --hard HEAD~1`` removes it
cleanly (no dangling revert-of-revert to carry). The DB row preserves the
audit trail: the proposal row records the attempt, the failure reason,
and the rolled-back status. Nothing is lost from history because the
history-of-record is the DB, not the local commit.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from self_modify import gates as _gates
from self_modify import proposals as P
from self_modify import zones


# --- constants used only by apply -------------------------------------------
_REPO_ROOT = Path("/home/corio/Morgoth/morgoth")
_VENV_PYTHON = _REPO_ROOT / ".venv" / "bin" / "python"
_SYSTEMCTL = "/usr/bin/systemctl"
_SERVICE = "morgoth.service"
_API_BASE = "http://localhost:8000"
_HEALTH_WAIT_SECS = 90
# Single source of truth — apply and gate_tests hit the same live suite
# through different entry points; a divergent budget was the bug that
# killed 1182ee96 (liveness PASS, shadow APPROVE, operator-approved) on
# a 300s hardcoded timeout while the suite runs ~2700s under load.
# See gates.PYTEST_BUDGET_SECS docstring.
_PYTEST_TIMEOUT_SECS = _gates.PYTEST_BUDGET_SECS

# Re-export from proposals for a shorter local reference.
STATUS_APPLIED = P.STATUS_APPLIED
STATUS_APPLY_FAILED_ROLLED_BACK = P.STATUS_APPLY_FAILED_ROLLED_BACK


# --- git helpers ------------------------------------------------------------

def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _git_tree_is_clean(repo: Path) -> bool:
    result = _git(["status", "--porcelain"], repo)
    return result.returncode == 0 and result.stdout.strip() == ""


def _git_add(repo: Path, path: str) -> bool:
    return _git(["add", "--", path], repo).returncode == 0


def _git_commit(repo: Path, message: str) -> bool:
    return _git(["commit", "-m", message], repo).returncode == 0


def _git_reset_hard(repo: Path, ref: str) -> bool:
    return _git(["reset", "--hard", ref], repo).returncode == 0


# --- extraction -------------------------------------------------------------

_NAME_RE = re.compile(r'''^\s*name\s*=\s*["']([A-Za-z0-9_\-]+)["']''', re.MULTILINE)


def _extract_tool_name(content: str) -> str | None:
    """Best-effort extraction of the tool's ``name`` class attribute."""
    match = _NAME_RE.search(content)
    return match.group(1) if match else None


# --- health check -----------------------------------------------------------

async def _wait_for_ready_and_tool(tool_name: str | None) -> bool:
    """Poll /api/brain/status ready=true AND /api/tools contains tool_name.

    If tool_name is None (couldn't parse it), only ready=true is required.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(_HEALTH_WAIT_SECS):
            try:
                brain = await client.get(f"{_API_BASE}/api/brain/status")
                if brain.status_code == 200 and brain.json().get("ready") is True:
                    if tool_name is None:
                        return True
                    tools_resp = await client.get(f"{_API_BASE}/api/tools")
                    if tools_resp.status_code == 200:
                        names = {t["name"] for t in tools_resp.json()}
                        if tool_name in names:
                            return True
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(1)
    return False


# --- subprocess wrappers ----------------------------------------------------

def _run_live_pytest(repo: Path) -> subprocess.CompletedProcess[str]:
    """Live-tree pytest — same suite the sandbox ran, same budget.

    ``-n auto`` matches the sandbox invocation so the live budget's
    variance envelope matches the measured value (a live serial run
    at ~2700s wall time would routinely exceed even the sandbox
    xdist budget).
    """
    return subprocess.run(
        [str(_VENV_PYTHON), "-m", "pytest", "-q", "-n", "auto"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=_PYTEST_TIMEOUT_SECS,
    )


def _systemctl_restart() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sudo", "-n", _SYSTEMCTL, "restart", _SERVICE],
        capture_output=True,
        text=True,
        check=False,
    )


# --- the main entry point ---------------------------------------------------

async def apply_proposal(
    store: P.ProposalStore,
    proposal_id: str,
    repo_root: Path = _REPO_ROOT,
    *,
    _pytest_runner=_run_live_pytest,
    _restart_runner=_systemctl_restart,
    _health_check=_wait_for_ready_and_tool,
) -> str:
    """Attempt to apply an approved proposal. Return the final status.

    Injectable runners exist for the git-integration tests: substituting
    them lets a test drive the rollback branches without a real restart.
    """
    row = await store.get(proposal_id)
    if row is None:
        raise ValueError(f"proposal {proposal_id!r} not found")

    def _log(step: str, msg: str) -> None:
        logger.info("apply[{}] {}: {}", proposal_id[:8], step, msg)

    # ---- 1. preconditions --------------------------------------------------
    #
    # PURE READ CONTRACT: refusal must NOT mutate the row. A second-launch
    # `morgoth apply <id>` on an already-terminal proposal used to overwrite
    # the row's status_reason with the refusal message, destroying the
    # historical outcome. Two casualties: 1735f617 (original failure
    # reason destroyed 07-24, "unknown cause" traced back to this) and
    # 580d247c (real state 'applied' 07-24 20:20 clobbered to
    # 'apply_failed_rolled_back' 07-25). A refusal is diagnostic
    # information for the operator; it is not a state transition.
    # Grep-lock: this branch and the four below must never carry an
    # update_status call.
    if row["status"] != P.STATUS_APPROVED_PENDING_APPLY:
        reason = (
            f"apply refused: status is {row['status']!r}, "
            f"must be {P.STATUS_APPROVED_PENDING_APPLY!r}"
        )
        _log("precheck", reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    if row["change_type"] != "new_file":
        reason = f"apply refused: change_type is {row['change_type']!r}, only new_file supported"
        _log("precheck", reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    zone_now = zones.classify_proposal(row["target_path"], row["change_type"])
    if zone_now != "green":
        reason = (
            f"apply refused: re-classify at apply time is {zone_now!r}, "
            f"only green proposals may apply (defense in depth)"
        )
        _log("precheck", reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    target = repo_root / row["target_path"]
    if target.exists():
        reason = f"apply refused: target path {row['target_path']!r} already exists in live tree"
        _log("precheck", reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    if not _git_tree_is_clean(repo_root):
        reason = "apply refused: live git tree is not clean (uncommitted changes present)"
        _log("precheck", reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    _log("precheck", "OK — all preconditions pass")

    # ---- 2. write ----------------------------------------------------------
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(row["content"], encoding="utf-8")
    _log("write", f"wrote {row['target_path']}")

    # ---- 3. live pytest ----------------------------------------------------
    try:
        completed = await asyncio.to_thread(_pytest_runner, repo_root)
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        reason = f"apply pytest timed out after {_PYTEST_TIMEOUT_SECS}s; file removed"
        _log("pytest", reason)
        await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        tail = (completed.stdout + completed.stderr)[-2000:]
        reason = f"apply pytest FAILED exit={completed.returncode}; file removed\n---tail---\n{tail}"
        _log("pytest", f"FAILED exit={completed.returncode}")
        await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK
    _log("pytest", "PASS")

    # ---- 4. commit (local only) --------------------------------------------
    if not _git_add(repo_root, row["target_path"]):
        target.unlink(missing_ok=True)
        reason = "git add failed; file removed"
        _log("commit", reason)
        await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK

    commit_msg = f"[self-modify] apply proposal #{proposal_id[:8]}: {row['target_path']}"
    if not _git_commit(repo_root, commit_msg):
        # Restore working tree — the file is staged but commit failed;
        # `git reset --hard HEAD` unstages and removes the file.
        _git_reset_hard(repo_root, "HEAD")
        reason = "git commit failed; working tree reset to HEAD"
        _log("commit", reason)
        await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK
    _log("commit", f"created local commit for {row['target_path']} (NOT pushed)")

    # ---- 5. restart --------------------------------------------------------
    restart_result = await asyncio.to_thread(_restart_runner)
    if restart_result.returncode != 0:
        _log("restart", f"systemctl restart FAILED exit={restart_result.returncode}")
        # Fall through to rollback + second restart to bring the pre-apply
        # code back into memory.
        _git_reset_hard(repo_root, "HEAD~1")
        await asyncio.to_thread(_restart_runner)
        reason = f"restart failed exit={restart_result.returncode}; rolled back to previous HEAD"
        await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK
    _log("restart", "systemctl restart accepted")

    # ---- 6. health check ---------------------------------------------------
    tool_name = _extract_tool_name(row["content"])
    _log("health", f"waiting for ready=true and tool={tool_name!r} in /api/tools")
    healthy = await _health_check(tool_name)
    if not healthy:
        _log("health", "TIMEOUT — rolling back")
        _git_reset_hard(repo_root, "HEAD~1")
        # Second restart to bring the pre-apply code back live.
        await asyncio.to_thread(_restart_runner)
        # And a final health probe: we WANT ready=true here too (the
        # pre-apply build should be healthy). If the second restart also
        # fails to come up, that's a bigger problem than a proposal
        # rollback and gets logged loudly.
        recovered = await _health_check(None)
        recovery_note = "recovered" if recovered else "RECOVERY ALSO FAILED"
        reason = (
            "apply health check FAILED — reset --hard HEAD~1 + restart; "
            f"recovery: {recovery_note}"
        )
        _log("health", reason)
        await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
        return STATUS_APPLY_FAILED_ROLLED_BACK
    _log("health", "OK — ready=true and tool present")

    # ---- 7. success --------------------------------------------------------
    await store.update_status(
        proposal_id,
        STATUS_APPLIED,
        f"applied and verified live (tool={tool_name!r})",
    )
    return STATUS_APPLIED
