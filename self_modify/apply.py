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
     AND ``/api/tools/catalog`` contains the installed tool's ``name``. On failure:
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
# 2026-09-30 apply-refused-precheck: precheck REFUSAL is not a rollback.
# It's a pure read — the row stays at approved_pending_apply and the
# refusal is a diagnostic for the operator, not a state transition,
# not a rollback in the auto_approve metric. The CLI shows this
# return honestly ("final status: apply_refused_precheck") instead
# of pretending a rollback occurred. auto_approve.apply_outcomes
# already filters by row.status, so a refusal never enters the
# rollback-rate denominator by construction.
APPLY_REFUSED_PRECHECK = "apply_refused_precheck"


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
    """Poll ready=true and the installed catalog, without Domain activation.

    None requests ready-only for the rollback health probe; successful apply
    requires a parsed name before this helper is called.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        for _ in range(_HEALTH_WAIT_SECS):
            try:
                brain = await client.get(f"{_API_BASE}/api/brain/status")
                if brain.status_code == 200 and brain.json().get("ready") is True:
                    if tool_name is None:
                        return True
                    tools_resp = await client.get(f"{_API_BASE}/api/tools/catalog")
                    if tools_resp.status_code == 200:
                        names = {t["name"] for t in tools_resp.json()}
                        if tool_name in names:
                            return True
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(1)
    return False


# --- subprocess wrappers ----------------------------------------------------

def _run_live_pytest(
    repo: Path,
    junit_out: Path | None = None,
    *,
    inject_file: tuple[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Sandboxed pytest — SAME wrapper as gate_tests + `morgoth test`.

    2026-09-30 fix: prior versions ran pytest on the LIVE tree with
    an inherited env. On the operator's box the same suite that
    completes in ~9 s under `morgoth test` took 30+ min on the live
    tree — tests stalled to ``--timeout=60`` each because the live
    tree carries ``.env`` at cwd, ``.pytest_cache/lastfailed``, live
    ~/.claude tokens, and the production POSTGRES_URL. The single-
    source argv alone is not enough; the isolation is what makes
    hermetic tests hermetic.

    Now:
      1. Copy the current live tree into a fresh sandbox (excluding
         ``.env``, ``.pytest_cache``, ``data``, ``vault``, ``backups``,
         ``.venv``, ``.git`` — same as gate_tests).
      2. If ``inject_file=(target_path, content)`` is set, write the
         file into the sandbox (proposal run). Baseline run leaves
         the sandbox as-is.
      3. os.utime(sandbox) — copystat propagates the source dir's
         mtime; sweep_stale_sandboxes would otherwise treat a fresh
         copy of a >1 h-old repo as stale mid-run (same bug the
         earlier gate_tests commit fixed).
      4. Run pytest under ``gates.wrap_command_in_sandbox`` (unshare
         --user --map-root-user --net + bwrap --clearenv --tmpfs /tmp
         --bind sandbox + --ro-bind system dirs + venv + systemd-run
         cgroup). Junit is written INSIDE the sandbox tree at
         ``junit.xml`` and copied out to ``junit_out``.
      5. Sandbox is cleaned up in ``finally``.

    Grep-locked in tests/test_apply_shared_argv.py.
    """
    import shutil as _shu
    import tempfile as _tempfile
    import uuid as _uuid
    sandbox_root = _gates._SANDBOX_ROOT
    sandbox_root.mkdir(parents=True, exist_ok=True)
    sandbox = sandbox_root / f"apply_{_uuid.uuid4().hex[:12]}"
    if sandbox.exists():
        _shu.rmtree(sandbox)
    try:
        _shu.copytree(str(repo), str(sandbox), ignore=_gates._SANDBOX_IGNORE)
        import os as _os
        _os.utime(sandbox, None)
        # Refuse to proceed if a .env slipped past the ignore list.
        for stray in list(sandbox.rglob(".env")):
            if stray.is_file():
                raise RuntimeError(
                    f"apply sandbox contains .env at {stray} — refusing to run"
                )
        if inject_file is not None:
            target_path, content = inject_file
            target = sandbox / target_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        inner_junit = sandbox / "junit.xml"
        pytest_call = [
            str(_VENV_PYTHON), "-m", "pytest", "-q",
            "-n", str(_gates._SANDBOX_XDIST_WORKERS),
            "--max-worker-restart=3",
            "--dist=loadfile",
        ] + list(_gates.HERMETIC_PYTEST_EXTRA_ARGS) + [
            f"--junitxml={inner_junit}",
        ]
        argv = _gates.wrap_command_in_sandbox(sandbox, pytest_call)
        completed = subprocess.run(
            argv, capture_output=True, text=True,
            timeout=_PYTEST_TIMEOUT_SECS,
            env=_gates._hardened_outer_env(),
            start_new_session=True,
        )
        if junit_out is not None and inner_junit.exists():
            _shu.copy(str(inner_junit), str(junit_out))
        return completed
    finally:
        _shu.rmtree(sandbox, ignore_errors=True)


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
    # PURE READ CONTRACT: refusal must NOT change the row's STATUS. Prior
    # bug: the four casualties (1735f617, 580d247c, ...) had their real
    # outcome clobbered by a rerun's refusal writing status='apply_failed_
    # rolled_back'. 2026-09-30 tightening: a refusal ALSO must not be
    # counted as a rollback in the auto_approve metric (an operator's
    # dirty git tree is not evidence Morgoth broke anything). We return
    # a distinct APPLY_REFUSED_PRECHECK code and append the refusal
    # reason to status_reason WITHOUT touching status. Grep-lock:
    # precheck branches never call ``update_status``.
    async def _note_precheck(reason: str) -> None:
        """Append a refusal diagnostic to status_reason WITHOUT moving
        status. Guarded by ``require_status=approved_pending_apply``
        so a concurrent restore between refusal and note can't land
        the diagnostic on a row that's already moved on. Safe when
        ``store.set_status_reason`` is missing (unit-test mock)."""
        setter = getattr(store, "set_status_reason", None)
        if setter is None:
            return
        try:
            await setter(
                proposal_id, f"[precheck-refused] {reason}",
                require_status=P.STATUS_APPROVED_PENDING_APPLY,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("_note_precheck: {}", exc)

    if row["status"] != P.STATUS_APPROVED_PENDING_APPLY:
        reason = (
            f"apply refused: status is {row['status']!r}, "
            f"must be {P.STATUS_APPROVED_PENDING_APPLY!r}"
        )
        _log("precheck", reason)
        # No note here — row isn't approved_pending_apply, so we don't
        # touch it (guard clause + WHERE status = ...).
        return APPLY_REFUSED_PRECHECK

    if row["change_type"] != "new_file":
        reason = f"apply refused: change_type is {row['change_type']!r}, only new_file supported"
        _log("precheck", reason)
        await _note_precheck(reason)
        return APPLY_REFUSED_PRECHECK

    zone_now = zones.classify_proposal(row["target_path"], row["change_type"])
    if zone_now != "green":
        reason = (
            f"apply refused: re-classify at apply time is {zone_now!r}, "
            f"only green proposals may apply (defense in depth)"
        )
        _log("precheck", reason)
        await _note_precheck(reason)
        return APPLY_REFUSED_PRECHECK

    target = repo_root / row["target_path"]
    if target.exists():
        reason = f"apply refused: target path {row['target_path']!r} already exists in live tree"
        _log("precheck", reason)
        await _note_precheck(reason)
        return APPLY_REFUSED_PRECHECK

    if not _git_tree_is_clean(repo_root):
        reason = "apply refused: live git tree is not clean (uncommitted changes present)"
        _log("precheck", reason)
        await _note_precheck(reason)
        return APPLY_REFUSED_PRECHECK

    _log("precheck", "OK — all preconditions pass")

    # ---- 2a. BASELINE pytest ----------------------------------------------
    # 2026-09-30: baseline diff (mirrors gate_tests). With 46 legacy
    # failures on the live tree, ``if returncode != 0: rollback`` would
    # fail every apply. Run pytest BEFORE writing the file to record
    # the current failing set; then rerun AFTER the write and reject
    # only if NEW failures appear. Junit files go under /var/tmp so
    # they never contaminate the git tree.
    import tempfile
    # Use the default temp dir (respects $TMPDIR; falls back to /tmp)
    # so the tests running INSIDE bwrap (which has --tmpfs /tmp and no
    # /var/tmp mount) can still create the junit staging dir.
    _staging = Path(tempfile.mkdtemp(prefix="morgoth_apply_"))
    baseline_junit = _staging / "junit.baseline.xml"
    proposal_junit = _staging / "junit.proposal.xml"
    try:
        _log("baseline", "running SANDBOXED pytest on live-tree snapshot")
        try:
            await asyncio.to_thread(_pytest_runner, repo_root, baseline_junit)
        except subprocess.TimeoutExpired:
            reason = f"apply baseline pytest timed out after {_PYTEST_TIMEOUT_SECS}s"
            _log("baseline", reason)
            await _note_precheck(reason)
            return APPLY_REFUSED_PRECHECK
        except (KeyboardInterrupt, asyncio.CancelledError) as exc:
            reason = f"apply baseline pytest interrupted ({type(exc).__name__}); no file was written"
            _log("baseline", reason)
            await _note_precheck(reason)
            raise
        base_fail, base_err = _gates._junit_failing_ids(baseline_junit)
        baseline_failures = base_fail | base_err
        _log("baseline", f"failures={len(baseline_failures)}")

        # ---- 2b. proposal pytest (SANDBOXED — file NOT yet on live) ------
        # 2026-09-30: the proposal file is injected into a FRESH sandbox
        # copy, not into the live tree. If pytest introduces new
        # failures OR crashes, no cleanup is needed — the live tree is
        # untouched. The actual live-tree write happens AFTER pytest
        # verdicts converge.
        try:
            completed = await asyncio.to_thread(
                _pytest_runner, repo_root, proposal_junit,
                inject_file=(row["target_path"], row["content"]),
            )
        except subprocess.TimeoutExpired:
            reason = f"apply proposal pytest timed out after {_PYTEST_TIMEOUT_SECS}s"
            _log("pytest", reason)
            await _note_precheck(reason)
            return APPLY_REFUSED_PRECHECK
        prop_fail, prop_err = _gates._junit_failing_ids(proposal_junit)
        proposal_failures = prop_fail | prop_err
        new_failures = proposal_failures - baseline_failures
        if new_failures:
            tail = (completed.stdout + completed.stderr)[-1500:]
            reason = (
                f"apply pytest introduced {len(new_failures)} NEW failure(s) "
                f"vs baseline (baseline={len(baseline_failures)}, "
                f"proposal={len(proposal_failures)}); live tree untouched\n"
                f"---new failures---\n" + "\n".join(sorted(new_failures)[:20]) +
                f"\n---tail---\n{tail}"
            )
            _log("pytest", f"FAIL {len(new_failures)} new failure(s)")
            await store.update_status(proposal_id, STATUS_APPLY_FAILED_ROLLED_BACK, reason)
            return STATUS_APPLY_FAILED_ROLLED_BACK
        _log("pytest",
              f"PASS baseline={len(baseline_failures)} proposal={len(proposal_failures)} — 0 new")

        # ---- 2c. write to LIVE tree (only after verdicts converge) -------
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(row["content"], encoding="utf-8")
        _log("write", f"wrote {row['target_path']}")
    finally:
        import shutil as _shu
        _shu.rmtree(_staging, ignore_errors=True)

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
    _log("health", f"waiting for ready=true and installed tool={tool_name!r} in /api/tools/catalog")
    healthy = bool(tool_name) and await _health_check(tool_name)
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
    _log("health", "OK — ready=true and implementation installed")

    # ---- 7. success --------------------------------------------------------
    await store.update_status(
        proposal_id,
        STATUS_APPLIED,
        f"applied and verified installed (tool={tool_name!r}; Domain activation separate)",
    )
    return STATUS_APPLIED
