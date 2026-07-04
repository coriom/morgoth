"""Unit tests for the self-modify pipeline: proposals CRUD, gates, cli.

Every DB touch is mocked using the ``_AsyncCtxManager`` pattern the rest
of the test suite already uses (see tests/test_persistent_memory.py).
Subprocess calls in ``gate_tests`` are patched — no real pytest runs
here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from self_modify import gates, proposals as P


class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _make_store_with_mock_pool(conn: AsyncMock) -> tuple[P.ProposalStore, MagicMock]:
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)
    return P.ProposalStore(pm), pool


# ---------- ProposalStore ---------------------------------------------------

@pytest.mark.asyncio
async def test_submit_inserts_and_returns_uuid() -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    store, _ = _make_store_with_mock_pool(conn)

    pid = await store.submit(
        target_path="tools/data_feeds/x.py",
        change_type="new_file",
        content="x",
        rationale="why",
    )
    assert isinstance(pid, str) and len(pid) == 36  # uuid4 string
    # Was the INSERT called with the right lifecycle-start status?
    args = conn.execute.await_args.args
    # (pid, target_path, change_type, content, rationale, status, proposed_by, engine)
    assert args[-3] == P.STATUS_SUBMITTED
    # Default proposed_by is 'human' when not overridden.
    assert args[-2] == "human"
    # Default engine is 'ollama' when not overridden.
    assert args[-1] == "ollama"


@pytest.mark.asyncio
async def test_get_returns_dict_or_none() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"proposal_id": "x", "status": "submitted"})
    store, _ = _make_store_with_mock_pool(conn)
    row = await store.get("00000000-0000-0000-0000-000000000001")
    assert row["status"] == "submitted"

    conn.fetchrow = AsyncMock(return_value=None)
    assert await store.get("00000000-0000-0000-0000-000000000002") is None


@pytest.mark.asyncio
async def test_update_status_rejects_unknown_status() -> None:
    conn = AsyncMock()
    store, _ = _make_store_with_mock_pool(conn)
    with pytest.raises(ValueError):
        await store.update_status(
            "00000000-0000-0000-0000-000000000001",
            "not_a_real_status",
        )


@pytest.mark.asyncio
async def test_update_status_returns_true_on_one_row_updated() -> None:
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    store, _ = _make_store_with_mock_pool(conn)
    ok = await store.update_status(
        "00000000-0000-0000-0000-000000000001",
        P.STATUS_REJECTED,
        "test",
    )
    assert ok is True


# ---------- gate_zone -------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_zone_marks_red_as_zone_rejected() -> None:
    store = MagicMock()
    store.update_status = AsyncMock(return_value=True)
    proposal = {
        "proposal_id": "id-1",
        "target_path": "core/brain.py",
        "change_type": "edit",
    }
    result = await gates.gate_zone(store, proposal)
    assert result == P.STATUS_ZONE_REJECTED
    store.update_status.assert_awaited_once()
    _, called_status, reason = store.update_status.await_args.args
    assert called_status == P.STATUS_ZONE_REJECTED
    assert "core/brain.py" in reason
    assert "zone=red" in reason


@pytest.mark.asyncio
async def test_gate_zone_returns_green_for_green_proposal_without_persisting() -> None:
    """A green proposal must NOT be persisted by gate_zone — it advances."""
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "id-2",
        "target_path": "tools/data_feeds/new.py",
        "change_type": "new_file",
    }
    result = await gates.gate_zone(store, proposal)
    assert result == "green"
    store.update_status.assert_not_awaited()


# ---------- gate_tests ------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_tests_records_pending_approval_on_pytest_zero(tmp_path: Path) -> None:
    """A zero-exit pytest → pending_approval (real filesystem, mocked pytest)."""
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "data_feeds").mkdir()
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "id-3",
        "target_path": "tools/data_feeds/dummy.py",
        "change_type": "new_file",
        "content": "# harmless\n",
    }

    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="1 passed", stderr=""
    )
    with patch.object(gates, "_run_pytest_in_sandbox", return_value=fake_completed):
        result = await gates.gate_tests(store, proposal, repo_root=tmp_path)

    assert result == P.STATUS_PENDING_APPROVAL
    called_status = store.update_status.await_args.args[1]
    assert called_status == P.STATUS_PENDING_APPROVAL


@pytest.mark.asyncio
async def test_gate_tests_records_tests_failed_on_pytest_nonzero(tmp_path: Path) -> None:
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "data_feeds").mkdir()
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "id-4",
        "target_path": "tools/data_feeds/dummy.py",
        "change_type": "new_file",
        "content": "raise SystemExit(1)\n",
    }
    fake_completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="E   ImportError"
    )
    with patch.object(gates, "_run_pytest_in_sandbox", return_value=fake_completed):
        result = await gates.gate_tests(store, proposal, repo_root=tmp_path)

    assert result == P.STATUS_TESTS_FAILED
    reason = store.update_status.await_args.args[2]
    assert "exit=1" in reason
    assert "ImportError" in reason


@pytest.mark.asyncio
async def test_gate_tests_rejects_non_new_file_change_types(tmp_path: Path) -> None:
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "id-5",
        "target_path": "tools/data_feeds/x.py",
        "change_type": "edit",
        "content": "--- diff ---",
    }
    result = await gates.gate_tests(store, proposal, repo_root=tmp_path)
    assert result == P.STATUS_TESTS_FAILED
    reason = store.update_status.await_args.args[2]
    assert "unsupported change_type" in reason


# ---------- run_pipeline ----------------------------------------------------

@pytest.mark.asyncio
async def test_run_pipeline_short_circuits_on_red() -> None:
    """Red proposal must not reach gate_tests."""
    store = MagicMock()
    store.update_status = AsyncMock()
    proposal = {
        "proposal_id": "id-6",
        "target_path": "core/brain.py",
        "change_type": "edit",
        "content": "",
    }
    # If gate_tests were called, patching it to raise catches that.
    with patch.object(gates, "gate_tests", side_effect=RuntimeError("must not be called")):
        result = await gates.run_pipeline(store, proposal)
    assert result == P.STATUS_ZONE_REJECTED


# ---------- cli -------------------------------------------------------------

@pytest.mark.asyncio
async def test_cli_approve_refuses_wrong_lifecycle() -> None:
    """approve on a not-pending proposal must not transition it."""
    from self_modify.cli import _cmd_approve

    store = MagicMock()
    store.get = AsyncMock(
        return_value={"proposal_id": "id-7", "status": P.STATUS_SUBMITTED}
    )
    store.update_status = AsyncMock()

    args = MagicMock()
    args.proposal_id = "id-7"
    rc = await _cmd_approve(store, args)
    assert rc == 1
    store.update_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_approve_transitions_pending_to_approved() -> None:
    from self_modify.cli import _cmd_approve

    store = MagicMock()
    store.get = AsyncMock(
        return_value={"proposal_id": "id-8", "status": P.STATUS_PENDING_APPROVAL}
    )
    store.update_status = AsyncMock()

    args = MagicMock()
    args.proposal_id = "id-8"
    rc = await _cmd_approve(store, args)
    assert rc == 0
    called_status = store.update_status.await_args.args[1]
    assert called_status == P.STATUS_APPROVED_PENDING_APPLY


@pytest.mark.asyncio
async def test_cli_reject_marks_rejected() -> None:
    from self_modify.cli import _cmd_reject

    store = MagicMock()
    store.get = AsyncMock(
        return_value={"proposal_id": "id-9", "status": P.STATUS_PENDING_APPROVAL}
    )
    store.update_status = AsyncMock()

    args = MagicMock()
    args.proposal_id = "id-9"
    args.reason = "not needed"
    rc = await _cmd_reject(store, args)
    assert rc == 0
    called_status = store.update_status.await_args.args[1]
    assert called_status == P.STATUS_REJECTED
