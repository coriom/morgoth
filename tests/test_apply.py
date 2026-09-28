"""Tests for self_modify.apply.

Two flavors:

- **Unit** (mocked subprocess + fake proposal_store): exercises every
  precondition-refusal and rollback branch. No git, no filesystem side
  effects beyond a tmp dir.
- **Git integration** (real ``git init`` in a tmp dir): drives the
  write → live-pytest → commit → restart → health path end-to-end with
  patched runners. The health-fail branch is the important one —
  asserts ``git reset --hard HEAD~1`` actually ran and the tmp tree is
  byte-identical to its pre-apply state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import apply as apply_mod, proposals as P


# --- helpers ----------------------------------------------------------------

def _fake_store_with_row(row: dict[str, Any]) -> MagicMock:
    store = MagicMock()
    store.get = AsyncMock(return_value=row)
    store.update_status = AsyncMock(return_value=True)
    return store


_MINIMAL_JUNIT_EMPTY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuites><testsuite name="pytest" errors="0" failures="0" '
    'skipped="0" tests="0"/></testsuites>'
)
_MINIMAL_JUNIT_ONE_FAILURE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuites><testsuite name="pytest" errors="0" failures="1" '
    'skipped="0" tests="1"><testcase classname="tests.test_x" '
    'name="test_new_failure"><failure message="new">boom</failure>'
    '</testcase></testsuite></testsuites>'
)


def _run_ok(*args, **_kw) -> subprocess.CompletedProcess[str]:
    """Live-pytest stub — writes an EMPTY junit when a junit_out is
    supplied so baseline-diff has a real file to parse. Both baseline
    and proposal runs share this stub → 0 new failures → PASS."""
    junit_out = args[1] if len(args) > 1 else _kw.get("junit_out")
    if junit_out is not None:
        Path(junit_out).write_text(_MINIMAL_JUNIT_EMPTY, encoding="utf-8")
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="OK", stderr="")


class _PytestBaselineDiff:
    """Call-counting pytest stub: first call = baseline (empty junit),
    second call = proposal (one NEW failure junit). Simulates "the
    write introduced a genuine new failure" for the baseline-diff
    branch of apply — the OLD ``returncode != 0 → rollback`` rule
    would have fired on any pre-existing failure in the live suite,
    which is exactly what this refactor eliminates."""

    def __init__(self) -> None:
        self.call = 0

    def __call__(self, *args, **_kw) -> subprocess.CompletedProcess[str]:
        junit_out = args[1] if len(args) > 1 else _kw.get("junit_out")
        self.call += 1
        payload = _MINIMAL_JUNIT_EMPTY if self.call == 1 else _MINIMAL_JUNIT_ONE_FAILURE
        if junit_out is not None:
            Path(junit_out).write_text(payload, encoding="utf-8")
        return subprocess.CompletedProcess(
            args=[], returncode=(0 if self.call == 1 else 1),
            stdout="", stderr="pretend baseline diff",
        )


def _run_fail(*args, **_kw) -> subprocess.CompletedProcess[str]:
    """Legacy stub — kept for callers that don't care about baseline
    diff. Writes a junit with one failure so the diff shows +1 vs an
    (empty-junit) baseline call. In new tests prefer
    ``_PytestBaselineDiff()`` for explicit call ordering."""
    junit_out = args[1] if len(args) > 1 else _kw.get("junit_out")
    if junit_out is not None:
        Path(junit_out).write_text(_MINIMAL_JUNIT_ONE_FAILURE, encoding="utf-8")
    return subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="pretend pytest failed"
    )


def _init_temp_repo(tmp: Path) -> None:
    """Init a throwaway git repo with an initial commit so HEAD exists."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(tmp), check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=str(tmp), check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=str(tmp), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(tmp), check=True)
    # seed a file so HEAD exists and the tree is non-empty
    (tmp / "seed.txt").write_text("seed\n")
    (tmp / "tools").mkdir()
    (tmp / "tools" / "data_feeds").mkdir()
    subprocess.run(["git", "add", "-A"], cwd=str(tmp), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(tmp), check=True)


# --- precondition refusals --------------------------------------------------

@pytest.mark.asyncio
async def test_apply_refuses_wrong_status(tmp_path: Path) -> None:
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000001",
        "status": P.STATUS_SUBMITTED,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/x.py",
        "content": "",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(
        store, row["proposal_id"], repo_root=tmp_path
    )
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    # PURE-READ contract: refusal must NOT change the row's STATUS.
    assert store.update_status.await_count == 0


@pytest.mark.asyncio
async def test_apply_refuses_target_already_exists(tmp_path: Path) -> None:
    (tmp_path / "tools" / "data_feeds").mkdir(parents=True)
    (tmp_path / "tools" / "data_feeds" / "existing.py").write_text("# exists\n")
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000002",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/existing.py",
        "content": "# new\n",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(store, row["proposal_id"], repo_root=tmp_path)
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    assert store.update_status.await_count == 0


@pytest.mark.asyncio
async def test_apply_refuses_reclassified_red(tmp_path: Path) -> None:
    _init_temp_repo(tmp_path)
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000003",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "core/evil.py",  # red now
        "content": "# hi\n",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(store, row["proposal_id"], repo_root=tmp_path)
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    assert store.update_status.await_count == 0


@pytest.mark.asyncio
async def test_apply_refuses_dirty_tree(tmp_path: Path) -> None:
    _init_temp_repo(tmp_path)
    # make the tree dirty
    (tmp_path / "dirty.txt").write_text("uncommitted\n")
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000004",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/x.py",
        "content": "# hi\n",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(store, row["proposal_id"], repo_root=tmp_path)
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    assert store.update_status.await_count == 0


# --- pure-read contract: refusal on terminal rows leaves them byte-identical

@pytest.mark.asyncio
async def test_refusal_on_applied_row_is_byte_identical(tmp_path: Path) -> None:
    """The bug that destroyed 580d247c: a second ``morgoth apply`` on an
    already-applied row overwrote its status_reason with the refusal
    string. The row must be byte-identical after refusal."""
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000901",
        "status": P.STATUS_APPLIED,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/live.py",
        "content": "already applied\n",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(store, row["proposal_id"], repo_root=tmp_path)
    # Wrong-status refusal returns APPLY_REFUSED_PRECHECK (was
    # STATUS_APPLY_FAILED_ROLLED_BACK — misleading: a rerun on an
    # already-terminal row is not a rollback of that row).
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    assert store.update_status.await_count == 0, (
        "refusal must NOT change the row's STATUS — closes the "
        "580d247c/1735f617 data-destruction class"
    )


@pytest.mark.asyncio
async def test_refusal_on_rejected_row_is_byte_identical(tmp_path: Path) -> None:
    """Same class, other terminal: a rejected proposal's audit trail
    (rejection reason + timestamp) survives a stray ``morgoth apply``."""
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000902",
        "status": P.STATUS_REJECTED,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/nope.py",
        "content": "rejected\n",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(store, row["proposal_id"], repo_root=tmp_path)
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    assert store.update_status.await_count == 0


@pytest.mark.asyncio
async def test_precheck_refusal_appends_note_without_changing_status(
    tmp_path: Path,
) -> None:
    """2026-09-30: precheck refusal must call set_status_reason (append
    a diagnostic to status_reason) but must NOT call update_status
    (which moves the status column). Status stays approved_pending_apply
    so auto_approve.rollback_rate doesn't count this event."""
    _init_temp_repo(tmp_path)
    (tmp_path / "dirty.txt").write_text("uncommitted\n")
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000007",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/x.py",
        "content": "# hi\n",
    }
    store = _fake_store_with_row(row)
    store.set_status_reason = AsyncMock(return_value=True)
    result = await apply_mod.apply_proposal(
        store, row["proposal_id"], repo_root=tmp_path,
    )
    assert result == apply_mod.APPLY_REFUSED_PRECHECK
    # No status change.
    assert store.update_status.await_count == 0
    # Diagnostic note appended, GUARDED by require_status.
    assert store.set_status_reason.await_count == 1
    call = store.set_status_reason.await_args
    assert "[precheck-refused]" in call.args[1]
    assert "git tree is not clean" in call.args[1]
    assert call.kwargs.get("require_status") == P.STATUS_APPROVED_PENDING_APPLY


@pytest.mark.asyncio
async def test_apply_ignores_pre_existing_failures(tmp_path: Path) -> None:
    """46 legacy failures on the live tree must NOT block apply. Both
    baseline and proposal pytest runs return exit != 0 with the SAME
    failing set; new_failures = ∅ → the flow proceeds past pytest to
    the commit + restart + health-check happy path."""
    _init_temp_repo(tmp_path)
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000008",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/harmless.py",
        "content": "name = 'harmless'\n",
    }
    store = _fake_store_with_row(row)
    def _same_failing_junit(*args, **_kw) -> subprocess.CompletedProcess[str]:
        junit_out = args[1] if len(args) > 1 else _kw.get("junit_out")
        if junit_out is not None:
            Path(junit_out).write_text(
                _MINIMAL_JUNIT_ONE_FAILURE, encoding="utf-8",
            )
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="46 failed",
            stderr="pre-existing legacy failure",
        )
    result = await apply_mod.apply_proposal(
        store, row["proposal_id"], repo_root=tmp_path,
        _pytest_runner=_same_failing_junit,
        _restart_runner=_run_ok,
        _health_check=AsyncMock(return_value=True),
    )
    assert result == apply_mod.STATUS_APPLIED, (
        "pre-existing failures reproduced identically in the proposal "
        "run must NOT block apply — that's the whole point of baseline diff"
    )


def test_grep_lock_no_update_status_in_precheck_branch() -> None:
    """Structural guard: apply.py's precheck section must have ZERO
    update_status calls. A future edit that reintroduces a write on the
    refusal branch fails here before it can destroy another proposal."""
    import inspect
    src = inspect.getsource(apply_mod)
    lo = src.index("# ---- 1. preconditions")
    hi = src.index("# ---- 2a. BASELINE pytest")
    precheck_block = src[lo:hi]
    # Match the CALL, not the word (the section header/docstring
    # mentions update_status when explaining why it is banned here).
    assert "store.update_status(" not in precheck_block, (
        "precheck refusal branch must be pure-read; found store.update_status "
        "call inside the block that runs BEFORE apply has started"
    )


# --- pytest-fail rollback (file removed, no commit) -------------------------

@pytest.mark.asyncio
async def test_apply_deletes_file_on_pytest_new_failure(tmp_path: Path) -> None:
    """The write introduces a NEW failure vs the baseline captured
    before the write. 2026-09-30 baseline-diff: the OLD ``returncode
    != 0 → rollback`` would have fired on any pre-existing failure in
    the live suite (46 of them today). Now we only roll back when
    proposal_failures - baseline_failures is non-empty."""
    _init_temp_repo(tmp_path)
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000005",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/broken.py",
        "content": "raise SystemExit(1)\n",
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(
        store,
        row["proposal_id"],
        repo_root=tmp_path,
        _pytest_runner=_PytestBaselineDiff(),
        _restart_runner=_run_ok,
        _health_check=AsyncMock(return_value=True),
    )
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    reason = store.update_status.await_args.args[2]
    assert "NEW failure" in reason
    assert not (tmp_path / "tools" / "data_feeds" / "broken.py").exists(), \
        "file must be removed on new-failure detection"
    # And no commit was created — HEAD still at the seed commit.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=str(tmp_path), capture_output=True, text=True
    )
    assert log.stdout.count("\n") == 1  # one commit only (seed)


# --- health-fail rollback (commit created, then reset) ---------------------

@pytest.mark.asyncio
async def test_apply_resets_hard_on_health_fail(tmp_path: Path) -> None:
    """The critical rollback path: pytest passes, commit is created, then
    health check FAILS. We must ``git reset --hard HEAD~1`` and the tmp
    tree must be byte-identical to its pre-apply state.
    """
    _init_temp_repo(tmp_path)
    pre_snapshot = _snapshot_tree(tmp_path)
    pre_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True
    ).stdout.strip()

    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000006",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/dummy.py",
        "content": 'name = "some_new_tool"\n',
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(
        store,
        row["proposal_id"],
        repo_root=tmp_path,
        _pytest_runner=_run_ok,
        _restart_runner=_run_ok,
        _health_check=AsyncMock(return_value=False),  # forces rollback
    )
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    reason = store.update_status.await_args.args[2]
    assert "health check FAILED" in reason
    # File removed by the reset.
    assert not (tmp_path / "tools" / "data_feeds" / "dummy.py").exists()
    # HEAD is back to pre-apply.
    post_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True
    ).stdout.strip()
    assert post_head == pre_head, "HEAD must be reset to pre-apply commit"
    # Tree byte-identical.
    assert _snapshot_tree(tmp_path) == pre_snapshot, \
        "tmp tree drifted from pre-apply state"


# --- happy path (mocked) ----------------------------------------------------

@pytest.mark.asyncio
async def test_apply_success_path(tmp_path: Path) -> None:
    _init_temp_repo(tmp_path)
    row = {
        "proposal_id": "00000000-0000-0000-0000-000000000007",
        "status": P.STATUS_APPROVED_PENDING_APPLY,
        "change_type": "new_file",
        "target_path": "tools/data_feeds/happy.py",
        "content": 'name = "happy_tool"\n',
    }
    store = _fake_store_with_row(row)
    result = await apply_mod.apply_proposal(
        store,
        row["proposal_id"],
        repo_root=tmp_path,
        _pytest_runner=_run_ok,
        _restart_runner=_run_ok,
        _health_check=AsyncMock(return_value=True),
    )
    assert result == apply_mod.STATUS_APPLIED
    # The commit exists in tmp git history.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=str(tmp_path), capture_output=True, text=True
    ).stdout
    assert "[self-modify] apply proposal" in log
    # The file survives.
    assert (tmp_path / "tools" / "data_feeds" / "happy.py").exists()


# --- helpers ---------------------------------------------------------------

def _snapshot_tree(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        # Skip the git internals — they legitimately change during the run.
        if ".git" in p.parts:
            continue
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
    return out
