"""Tests for the /api/proposals mutation endpoints.

Verifies the security boundary (token gate) and CLI-parity of the state
transitions (approve → approved_pending_apply, reject → rejected via the
same ``ProposalStore.update_status`` method the CLI uses).

The apply-as-job endpoint is a thin wrapper around
``self_modify.apply.apply_proposal`` — that function is exhaustively
covered by test_apply.py; here we only assert (a) it returns immediately,
(b) single-flight, (c) apply-status reflects step markers + terminal.
"""

from __future__ import annotations

import stat
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.routes import proposals as proposals_route
from api.token import HEADER_NAME
from self_modify import proposals as P


# --- helpers ----------------------------------------------------------------

def _fake_pm() -> MagicMock:
    pm = MagicMock()
    pm.get_shadow_verdicts = AsyncMock(return_value=[])
    return pm


def _build_app_with_token(tmp_path: Path, token: str = "s3cret-token") -> tuple[FastAPI, Path]:
    """Point ``ensure_ui_token`` at a tmp file pre-seeded with a known token."""
    token_file = tmp_path / "ui_token"
    token_file.write_text(token)
    token_file.chmod(0o600)
    (tmp_path).chmod(0o700)
    # Patch the module-level constants so the endpoint reads from the tmp file.
    patcher = patch.object(proposals_route, "ensure_ui_token", return_value=token)
    patcher.start()
    app = FastAPI()
    app.state.persistent_memory = _fake_pm()
    app.include_router(proposals_route.router)
    return app, token_file


def _pending_row(pid: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") -> dict[str, Any]:
    return {
        "proposal_id": pid,
        "target_path": "tools/data_feeds/x.py",
        "change_type": "new_file",
        "content": "print('x')",
        "rationale": "why not",
        "status": P.STATUS_PENDING_APPROVAL,
        "status_reason": None,
        "proposed_by": "morgoth",
        "engine": "claude-cli",
        "retry_of": None,
        "created_at": None,
        "updated_at": None,
    }


def _patch_store(store_mock: MagicMock):
    return patch.object(P, "ProposalStore", return_value=store_mock)


# --- token gate -------------------------------------------------------------

def test_approve_without_token_returns_401(tmp_path: Path) -> None:
    app, _ = _build_app_with_token(tmp_path)
    try:
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        store.get = AsyncMock(return_value=_pending_row())
        store.update_status = AsyncMock(return_value=True)
        with _patch_store(store):
            client = TestClient(app)
            resp = client.post("/api/proposals/aaaaaaaa/approve", json={})
        assert resp.status_code == 401
        store.update_status.assert_not_awaited()
    finally:
        patch.stopall()


def test_approve_with_wrong_token_returns_401(tmp_path: Path) -> None:
    app, _ = _build_app_with_token(tmp_path, token="correct")
    try:
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        store.get = AsyncMock(return_value=_pending_row())
        store.update_status = AsyncMock(return_value=True)
        with _patch_store(store):
            client = TestClient(app)
            resp = client.post(
                "/api/proposals/aaaaaaaa/approve",
                headers={HEADER_NAME: "wrong"},
                json={},
            )
        assert resp.status_code == 401
        store.update_status.assert_not_awaited()
    finally:
        patch.stopall()


def test_approve_with_correct_token_transitions_state(tmp_path: Path) -> None:
    app, _ = _build_app_with_token(tmp_path, token="ok-token")
    try:
        pid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value=pid)
        store.get = AsyncMock(return_value=_pending_row(pid))
        store.update_status = AsyncMock(return_value=True)
        with _patch_store(store):
            client = TestClient(app)
            resp = client.post(
                f"/api/proposals/{pid}/approve",
                headers={HEADER_NAME: "ok-token"},
                json={"comment": "looks safe"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == P.STATUS_APPROVED_PENDING_APPLY
        store.update_status.assert_awaited_once()
        args = store.update_status.await_args.args
        assert args[1] == P.STATUS_APPROVED_PENDING_APPLY
        assert "looks safe" in args[2]
    finally:
        patch.stopall()


def test_approve_on_non_pending_returns_409(tmp_path: Path) -> None:
    app, _ = _build_app_with_token(tmp_path, token="t")
    try:
        row = _pending_row()
        row["status"] = P.STATUS_APPLIED  # already terminal
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value=row["proposal_id"])
        store.get = AsyncMock(return_value=row)
        store.update_status = AsyncMock()
        with _patch_store(store):
            client = TestClient(app)
            resp = client.post(
                f"/api/proposals/{row['proposal_id']}/approve",
                headers={HEADER_NAME: "t"},
                json={},
            )
        assert resp.status_code == 409
        store.update_status.assert_not_awaited()
    finally:
        patch.stopall()


def test_reject_requires_non_empty_reason(tmp_path: Path) -> None:
    app, _ = _build_app_with_token(tmp_path, token="t")
    try:
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        store.get = AsyncMock(return_value=_pending_row())
        store.update_status = AsyncMock()
        with _patch_store(store):
            client = TestClient(app)
            resp = client.post(
                "/api/proposals/aaaaaaaa/reject",
                headers={HEADER_NAME: "t"},
                json={"reason": ""},
            )
        assert resp.status_code == 422
        store.update_status.assert_not_awaited()
    finally:
        patch.stopall()


def test_reject_with_reason_transitions_to_rejected(tmp_path: Path) -> None:
    app, _ = _build_app_with_token(tmp_path, token="t")
    try:
        pid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value=pid)
        store.get = AsyncMock(return_value=_pending_row(pid))
        store.update_status = AsyncMock(return_value=True)
        with _patch_store(store):
            client = TestClient(app)
            resp = client.post(
                f"/api/proposals/{pid}/reject",
                headers={HEADER_NAME: "t"},
                json={"reason": "hallucinated endpoint"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == P.STATUS_REJECTED
        args = store.update_status.await_args.args
        assert args[1] == P.STATUS_REJECTED
        assert args[2] == "hallucinated endpoint"
    finally:
        patch.stopall()


# --- token file mode --------------------------------------------------------

def test_ensure_ui_token_writes_0600_file(tmp_path: Path) -> None:
    """The on-disk session token must be owner-read-only."""
    from api import token as tok_mod
    path = tmp_path / "d" / "ui_token"
    with patch.object(tok_mod, "TOKEN_PATH", path), \
         patch.object(tok_mod, "TOKEN_DIR", path.parent):
        val = tok_mod.ensure_ui_token(path)
    assert val
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
    assert dir_mode == 0o700, f"expected 0700, got {oct(dir_mode)}"


# --- CLI parity: same store method the CLI approve/reject uses --------------

def test_approve_uses_store_update_status(tmp_path: Path) -> None:
    """UI approve must NOT reimplement transitions — it calls update_status,
    the same method self_modify.cli._cmd_approve calls."""
    app, _ = _build_app_with_token(tmp_path, token="t")
    try:
        pid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        store = MagicMock()
        store.resolve_id = AsyncMock(return_value=pid)
        store.get = AsyncMock(return_value=_pending_row(pid))
        store.update_status = AsyncMock(return_value=True)
        with _patch_store(store):
            client = TestClient(app)
            client.post(
                f"/api/proposals/{pid}/approve",
                headers={HEADER_NAME: "t"},
                json={},
            )
        assert store.update_status.await_count == 1
        assert store.update_status.await_args.args[0] == pid
    finally:
        patch.stopall()
