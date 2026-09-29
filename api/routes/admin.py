"""Admin API routes."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.config import MorgothPermissions, load_permissions


router = APIRouter(prefix="/api/admin", tags=["admin"])


class PermissionsPatchRequest(BaseModel):
    """Request body for permissions updates."""

    payload: MorgothPermissions


@router.get("/permissions")
async def get_permissions(request: Request) -> dict[str, Any]:
    """Return the current permissions file."""

    from core.project import current_project
    if not current_project().is_legacy:
        return request.app.state.config.permissions.model_dump()
    permissions = await load_permissions(request.app.state.config.perms_path)
    return permissions.model_dump()


@router.patch("/permissions")
async def patch_permissions(request_body: PermissionsPatchRequest, request: Request) -> dict[str, Any]:
    """Update the permissions file through the human admin API."""

    from core.project import current_project
    if not current_project().is_legacy:
        raise HTTPException(status_code=409, detail="Project permissions are read-only in V1")
    perms_path = request.app.state.config.perms_path
    content = json.dumps(request_body.payload.model_dump(), indent=2) + "\n"
    await asyncio.to_thread(perms_path.write_text, content, "utf-8")
    return {"success": True}
