"""Proposals API — in-UI approve / reject / list.

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
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from api.token import HEADER_NAME, ensure_ui_token
from memory.persistent import PersistentMemory
from self_modify import proposals as P


router = APIRouter(prefix="/api/proposals", tags=["proposals"])


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
