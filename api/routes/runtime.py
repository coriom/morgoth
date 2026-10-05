"""Authenticated desktop research lifecycle and Project-profile controls."""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from api.routes.proposals import require_token
from core.llm.environment import detect_environment
from core.llm.profiles import (
    ProfileError, current_profile, list_profiles, profile_status,
    recommended_profile, resolve_task_route, switch_profile,
)
from core.llm.tasks import all_tasks
from core.project import current_project
from core.runtime import ENGINE_ROOT


router = APIRouter(
    prefix="/api/runtime/v1", tags=["runtime"],
    dependencies=[Depends(require_token)],
)


class ProfileSelection(BaseModel):
    """The only operator-controlled profile mutation input."""

    model_config = ConfigDict(extra="forbid")
    profile: str


class ProfileView(BaseModel):
    """Credential-free profile readiness and effective provider map."""

    id: str
    status: Literal["READY", "BLOCKED", "UNAVAILABLE"]
    reason: str
    providers: dict[str, str]


class ProfilesResponse(BaseModel):
    """Project profile catalog and current/recommended selections."""

    schema_version: int = 1
    current: str
    recommended: str | None
    profiles: list[ProfileView]


class RuntimeStatus(BaseModel):
    """Safe runtime identity, readiness and owned-task status."""

    schema_version: int = 1
    project: str
    domain: str
    code_sha: str | None
    initialized: bool
    awakening_ready: bool
    research_state: str
    autonomous_task_alive: bool
    profile: str
    profile_status: Literal["READY", "BLOCKED", "UNAVAILABLE"]


def _desktop(request: Request) -> None:
    if not getattr(request.app.state, "desktop_runtime", False):
        raise HTTPException(status_code=404, detail="desktop research runtime unavailable")


def _code_sha(root: Path = ENGINE_ROOT) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 and all(c in "0123456789abcdef" for c in value) else None


async def _profiles() -> ProfilesResponse:
    project = current_project()
    try:
        environment = await detect_environment()
        selected = current_profile(project)
        profiles = []
        for profile in list_profiles(project):
            status, reason = profile_status(project, profile, environment)
            profiles.append(ProfileView(
                id=profile.id, status=status, reason=reason,
                providers={task: resolve_task_route(project, task, profile=profile)[0]
                           for task in all_tasks()},
            ))
        return ProfilesResponse(
            current=selected.id,
            recommended=recommended_profile(project, environment),
            profiles=profiles,
        )
    except (ProfileError, ValueError, KeyError):
        raise HTTPException(status_code=409, detail="Project profile state unavailable") from None


@router.get("/status", response_model=RuntimeStatus, operation_id="desktopRuntimeStatus")
async def runtime_status(request: Request) -> RuntimeStatus:
    """Return identity and readiness without equating process liveness to research."""
    _desktop(request)
    project = current_project()
    profiles = await _profiles()
    brain = request.app.state.brain
    active = next(item for item in profiles.profiles if item.id == profiles.current)
    return RuntimeStatus(
        project=project.id, domain=project.domain,
        code_sha=getattr(request.app.state, "code_sha", None),
        initialized=brain.initialized,
        awakening_ready=brain.awakening_ready,
        research_state=brain.research_state,
        autonomous_task_alive=brain.autonomous_task_alive,
        profile=profiles.current, profile_status=active.status,
    )


@router.get("/profiles", response_model=ProfilesResponse, operation_id="desktopRuntimeProfiles")
async def runtime_profiles(request: Request) -> ProfilesResponse:
    """List effective profiles using the canonical Project-scoped resolver."""
    _desktop(request)
    return await _profiles()


@router.post("/profile", response_model=ProfilesResponse, operation_id="desktopRuntimeSelectProfile")
async def select_profile(request: Request, input: ProfileSelection) -> ProfilesResponse:
    """Atomically switch a qualified Project profile while research is paused."""
    _desktop(request)
    async with request.app.state.runtime_control_lock:
        brain = request.app.state.brain
        if brain.research_state != "PAUSED":
            raise HTTPException(status_code=409, detail="research must be initialized and paused")
        try:
            await switch_profile(current_project(), input.profile)
        except ProfileError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return await _profiles()


@router.post("/start", response_model=RuntimeStatus, operation_id="desktopRuntimeStart")
async def start_research(request: Request) -> RuntimeStatus:
    """Start one owned autonomous loop only with ready dependencies/profile."""
    _desktop(request)
    async with request.app.state.runtime_control_lock:
        brain = request.app.state.brain
        if not brain.initialized or not brain.awakening_ready:
            raise HTTPException(status_code=409, detail="research dependencies are not ready")
        if brain.research_state != "PAUSED":
            raise HTTPException(status_code=409, detail="research is not paused")
        profiles = await _profiles()
        active = next(item for item in profiles.profiles if item.id == profiles.current)
        if active.status != "READY":
            raise HTTPException(status_code=409, detail=f"profile {active.status.lower()}; research not started")
        try:
            started = await brain.start_autonomous_research()
        except RuntimeError:
            raise HTTPException(status_code=409, detail="research start unavailable") from None
        if not started:
            raise HTTPException(status_code=409, detail="research already started")
        return await runtime_status(request)
