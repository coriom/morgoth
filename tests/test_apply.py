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


def _run_ok(*_a, **_kw) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="OK", stderr="")


def _run_fail(*_a, **_kw) -> subprocess.CompletedProcess[str]:
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
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    reason = store.update_status.await_args.args[2]
    assert "apply refused: status is" in reason


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
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    assert "already exists" in store.update_status.await_args.args[2]


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
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    assert "re-classify at apply time" in store.update_status.await_args.args[2]


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
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    assert "not clean" in store.update_status.await_args.args[2]


# --- pytest-fail rollback (file removed, no commit) -------------------------

@pytest.mark.asyncio
async def test_apply_deletes_file_on_pytest_fail(tmp_path: Path) -> None:
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
        _pytest_runner=_run_fail,
        _restart_runner=_run_ok,
        _health_check=AsyncMock(return_value=True),
    )
    assert result == apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK
    assert not (tmp_path / "tools" / "data_feeds" / "broken.py").exists(), \
        "file must be removed on pytest failure"
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
