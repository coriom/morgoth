"""Proposals API — in-UI approve / reject / apply.

**Security**: mutations require the ``X-Morgoth-Token`` header, matched
against the local UI session token (``~/.morgoth/ui_token``, mode 0600
— see ``api.token``). Reads (list/detail) are unauthenticated: they
carry no side effect and are already visible to a local operator via
the CLI or the DB.

The mutation handlers deliberately reuse the SAME ``ProposalStore``
methods as the CLI (``update_status``). This keeps the two surfaces
converging on identical state transitions — a divergence bug in the
UI path could otherwise corrupt shadow calibration or the reflect
negative list.

Apply is BACKGROUND-DISPATCHED — POST /apply returns immediately, and
the long pytest → commit → restart → health sequence runs off the
request loop. The frontend polls /apply-status for step markers +
terminal outcome. A single-flight lock (in-process; single uvicorn
worker) prevents concurrent applies from racing on the git tree.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from api.token import HEADER_NAME, ensure_ui_token
from memory.persistent import PersistentMemory
from self_modify import apply as apply_mod
from self_modify import proposals as P


router = APIRouter(prefix="/api/proposals", tags=["proposals"])


# --- shared apply-progress state (in-process) -------------------------------

_apply_lock: asyncio.Lock = asyncio.Lock()
_apply_running: str | None = None
_apply_progress: dict[str, dict[str, Any]] = {}


def _record_progress(pid: str, step: str, terminal: str | None = None) -> None:
    _apply_progress[pid] = {"step": step, "terminal": terminal}


# --- token gate -------------------------------------------------------------

def require_token(
    x_morgoth_token: str | None = Header(default=None, alias=HEADER_NAME),
) -> None:
    """Compare the header against the on-disk session token in constant time.

    Fresh-read the file each call — cheap (small file, on-page cache)
    and correct if the operator rotated it manually.
    """
    import hmac
    expected = ensure_ui_token()
    if not x_morgoth_token or not hmac.compare_digest(x_morgoth_token, expected):
        raise HTTPException(status_code=401, detail="invalid or missing X-Morgoth-Token")


# --- helpers ----------------------------------------------------------------

async def _get_store(request: Request) -> P.ProposalStore:
    pm: PersistentMemory = request.app.state.persistent_memory
    return P.ProposalStore(pm)


async def _dossier(store: P.ProposalStore, row: dict[str, Any]) -> dict[str, Any]:
    """Merge the row with its shadow verdicts + surface UUID/timestamps as strings."""
    d = dict(row)
    pid = str(d["proposal_id"])
    d["proposal_id"] = pid
    if d.get("retry_of") is not None:
        d["retry_of"] = str(d["retry_of"])
    for k in ("created_at", "updated_at"):
        v = d.get(k)
        if v is not None and hasattr(v, "isoformat"):
            d[k] = v.isoformat()
    verdicts = await store._pm.get_shadow_verdicts(pid)  # noqa: SLF001
    for v in verdicts:
        v["id"] = str(v["id"])
        v["proposal_id"] = str(v["proposal_id"])
        ca = v.get("created_at")
        if ca is not None and hasattr(ca, "isoformat"):
            v["created_at"] = ca.isoformat()
    d["shadow_verdicts"] = verdicts
    return d


# --- read endpoints (no token) ----------------------------------------------

@router.get("")
async def list_proposals(request: Request, limit: int = 50) -> dict[str, Any]:
    """List recent proposals (all statuses)."""
    store = await _get_store(request)
    rows = await store.list_recent(limit=limit)
    return {"items": [await _dossier(store, r) for r in rows]}


@router.get("/pending-count")
async def pending_count(request: Request) -> dict[str, int]:
    """Cross-page alert badge source. Cheap enough for global polling."""
    store = await _get_store(request)
    rows = await store.list_pending(limit=200)
    return {"count": len(rows)}


@router.get("/{proposal_id}")
async def get_proposal(proposal_id: str, request: Request) -> dict[str, Any]:
    store = await _get_store(request)
    try:
        pid = await store.resolve_id(proposal_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row = await store.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id}")
    return await _dossier(store, row)


# --- mutation endpoints (token-gated) ---------------------------------------

class ApproveRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class RejectRequest(BaseModel):
    # min_length=1 gives the 422 the operator asked for on empty reason.
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/{proposal_id}/approve", dependencies=[Depends(require_token)])
async def approve_proposal(
    proposal_id: str, payload: ApproveRequest, request: Request,
) -> dict[str, Any]:
    store = await _get_store(request)
    try:
        pid = await store.resolve_id(proposal_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row = await store.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id}")
    if row["status"] != P.STATUS_PENDING_APPROVAL:
        raise HTTPException(
            status_code=409,
            detail=f"proposal is {row['status']!r}, not {P.STATUS_PENDING_APPROVAL!r}",
        )
    reason = "approved via morgoth ui"
    if payload.comment:
        reason = f"{reason}: {payload.comment}"
    await store.update_status(pid, P.STATUS_APPROVED_PENDING_APPLY, reason)
    logger.info(f"ui.approve: {pid} → approved_pending_apply")
    return {"proposal_id": pid, "status": P.STATUS_APPROVED_PENDING_APPLY}


@router.post("/{proposal_id}/reject", dependencies=[Depends(require_token)])
async def reject_proposal(
    proposal_id: str, payload: RejectRequest, request: Request,
) -> dict[str, Any]:
    store = await _get_store(request)
    try:
        pid = await store.resolve_id(proposal_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row = await store.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id}")
    await store.update_status(pid, P.STATUS_REJECTED, payload.reason)
    logger.info(f"ui.reject: {pid} → rejected ({payload.reason[:60]})")
    return {"proposal_id": pid, "status": P.STATUS_REJECTED}


# --- apply as async job -----------------------------------------------------

async def _run_apply(store: P.ProposalStore, pid: str) -> None:
    """Background task: drive apply_proposal, publish step markers.

    apply_proposal already updates status_reason at each step (precheck
    / write / pytest / commit / restart / health), so /apply-status can
    surface the latest reason without a second reporting channel. This
    helper also records a coarse ``step`` label + terminal outcome that
    stays queryable after the DB row's status_reason is overwritten by
    the final result.
    """
    global _apply_running  # noqa: PLW0603
    try:
        _record_progress(pid, "running")
        final = await apply_mod.apply_proposal(store, pid)
        _record_progress(pid, "done", terminal=final)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"ui.apply {pid} crashed: {exc!r}")
        _record_progress(
            pid, "crashed", terminal=apply_mod.STATUS_APPLY_FAILED_ROLLED_BACK,
        )
    finally:
        async with _apply_lock:
            _apply_running = None


@router.post("/{proposal_id}/apply", dependencies=[Depends(require_token)])
async def apply_proposal_endpoint(
    proposal_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Kick off apply as a background task; return immediately.

    Single-flight: refuses a second apply while one is already running.
    The precheck inside apply_proposal is now pure-read (969cb52), so a
    stray concurrent call can't corrupt state — but the lock keeps two
    pytest runs from stepping on the same git tree simultaneously.
    """
    global _apply_running  # noqa: PLW0603
    store = await _get_store(request)
    try:
        pid = await store.resolve_id(proposal_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row = await store.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id}")
    async with _apply_lock:
        if _apply_running is not None:
            raise HTTPException(
                status_code=409,
                detail=f"another apply is running ({_apply_running})",
            )
        _apply_running = pid
        _record_progress(pid, "queued")
    background_tasks.add_task(_run_apply, store, pid)
    logger.info(f"ui.apply: {pid} queued")
    return {"proposal_id": pid, "queued": True}


@router.get("/{proposal_id}/apply-status")
async def apply_status(proposal_id: str, request: Request) -> dict[str, Any]:
    """Progress + terminal outcome for the given proposal's apply run.

    Combines the in-memory step marker (queued/running/done/crashed)
    with the DB's authoritative ``status`` + ``status_reason`` so the
    frontend can surface the fine-grained step (from apply_proposal's
    per-step update_status writes) alongside the coarse lifecycle.
    """
    store = await _get_store(request)
    try:
        pid = await store.resolve_id(proposal_id)
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    row = await store.get(pid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no proposal {proposal_id}")
    progress = _apply_progress.get(pid, {"step": "idle", "terminal": None})
    return {
        "proposal_id": pid,
        "step": progress["step"],
        "terminal": progress["terminal"],
        "status": row["status"],
        "status_reason": row.get("status_reason") or "",
    }
