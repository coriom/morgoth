"""Optional versioned Desktop read routes, never mounted by normal Management startup."""
from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from agents.desktop_read_models import DesktopReadStore, MissionPage, MissionSummary, CapabilityPage, ReadUnavailable
from core.project import ProjectConfig
from project_manager import ProjectManager, ProjectManagerError

PREFIX = "/desktop-read/v1"


def install_desktop_read_routes(app: FastAPI, manager: ProjectManager, reader: DesktopReadStore) -> None:
    """Mount only GET routes behind the Management app's existing token boundary."""
    def project(project_id: str) -> ProjectConfig:
        manager.get_project(project_id)  # validates identifier and catalog membership
        found = next((item for item in manager._catalog() if item.id == project_id), None)
        if found is None:
            raise ProjectManagerError("UNKNOWN_PROJECT", "Project is not in the catalog")
        return found

    def refusal(exc: ReadUnavailable) -> JSONResponse:
        status = 422 if str(exc) in {"INVALID_PAGE", "MANAGED_PROJECT_REQUIRED"} else 409 if str(exc) == "INCOMPATIBLE_RECORD" else 503
        return JSONResponse(status_code=status, content={"error": {"code": str(exc), "message": "read model unavailable"}})

    @app.get(f"{PREFIX}/projects/{{project_id}}/missions", response_model=MissionPage)
    async def list_missions(project_id: str, limit: int = Query(10, ge=1, le=10),
                            offset: int = Query(0, ge=0, le=1000)) -> MissionPage | JSONResponse:
        """Read at most ten historical checkpoints in a managed test Project."""
        try:
            return await reader.missions(project(project_id), limit=limit, offset=offset)
        except ReadUnavailable as exc:
            return refusal(exc)

    @app.get(f"{PREFIX}/projects/{{project_id}}/missions/{{mission_id}}", response_model=MissionSummary)
    async def get_mission(project_id: str, mission_id: UUID) -> MissionSummary | JSONResponse:
        """Read one exact Project-scoped checkpoint; foreign IDs return 404."""
        try:
            found = await reader.mission(project(project_id), mission_id)
        except ReadUnavailable as exc:
            return refusal(exc)
        if found is None:
            return JSONResponse(status_code=404, content={"error": {"code": "UNKNOWN_MISSION", "message": "mission unavailable"}})
        return found

    @app.get(f"{PREFIX}/projects/{{project_id}}/capabilities", response_model=CapabilityPage)
    async def list_capabilities(project_id: str, limit: int = Query(10, ge=1, le=10),
                                offset: int = Query(0, ge=0, le=1000)) -> CapabilityPage | JSONResponse:
        """Read capability validation facts; never issue or infer an approval."""
        try:
            return await reader.capabilities(project(project_id), limit=limit, offset=offset)
        except ReadUnavailable as exc:
            return refusal(exc)
