"""Independent, authenticated local API for offline Project configuration."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hmac
import re
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.types import ASGIApp, Message, Receive, Scope, Send
import yaml

from core.domain import DomainPackError, _DOMAINS_ROOT, _load
from project_manager import ProjectManager, ProjectManagerError


API_PREFIX = "/management/v1"
TOKEN_HEADER = "x-morgoth-management-token"
MAX_BODY_BYTES = 8192
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_ERROR_STATUS = {
    "INVALID_REQUEST": 422,
    "UNKNOWN_PROJECT": 404,
    "UNKNOWN_DOMAIN": 404,
    "ALREADY_EXISTS": 409,
    "INVALID_CATALOG": 409,
    "NAMESPACE_CONFLICT": 409,
    "PATH_CONFLICT": 409,
    "PUBLICATION_FAILED": 500,
}


@dataclass(frozen=True)
class ManagementSecurity:
    """Explicit local authority; the token is never displayed or serialized."""

    token: str = field(repr=False)
    port: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not _TOKEN_PATTERN.fullmatch(self.token):
            raise ValueError("management token must be URL-safe and at least 256 bits")
        try:
            decoded = base64.urlsafe_b64decode(self.token + "=" * (-len(self.token) % 4))
        except (ValueError, base64.binascii.Error):
            raise ValueError("invalid management token encoding") from None
        if len(decoded) < 32 or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError("invalid management security settings")


class ManagementError(BaseModel):
    """Safe, stable error payload."""

    code: str
    message: str


class ErrorEnvelope(BaseModel):
    """Error response wrapper."""

    error: ManagementError


class ProjectView(BaseModel):
    """Configuration only; runtime and provider readiness are unchecked."""

    id: str
    name: str
    domain: str
    legacy: bool
    manifest_path: str | None
    workspace_root: str | None
    postgres_schema: str
    chroma_prefix: str
    vault_dir: str
    runtime_dir: str
    configuration_valid: bool
    runtime_checked: bool


class ProjectList(BaseModel):
    """Offline catalog listing."""

    projects: list[ProjectView]
    configuration_valid: bool
    runtime_checked: bool


class ProjectValidation(BaseModel):
    """Canonical Project validation result."""

    project: ProjectView
    configuration_valid: bool
    runtime_checked: bool


class ProjectCreation(BaseModel):
    """Published local configuration result, including durability status."""

    project: ProjectView
    created: bool
    durability_confirmed: bool
    engine_started: bool
    storage_initialized: bool


class CreateProjectRequest(BaseModel):
    """Only identity, display name and installed Domain are client-controlled."""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    domain: str


class DomainView(BaseModel):
    """Installed Domain pack inspection, without readiness claims."""

    id: str
    tagline: str | None
    configuration_valid: bool
    diagnostic: str | None = None


class DomainList(BaseModel):
    """Installed Domain summaries, including invalid packs."""

    domains: list[DomainView]
    runtime_checked: bool


class ManagementStatus(BaseModel):
    """Management transport availability only."""

    api_version: str
    management_available: bool


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class LocalAuthorityBoundary:
    """Reject unauthorised, nonlocal-origin and oversized HTTP before routing."""

    def __init__(self, app: ASGIApp, security: ManagementSecurity) -> None:
        self.app = app
        self.security = security

    async def _serve_safely(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Absorb unexpected route failures before server traceback logging."""
        started = False

        async def safe_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception:
            if not started:
                await _error(500, "INTERNAL_ERROR", "management operation failed")(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            return
        headers: dict[bytes, list[bytes]] = {}
        for key, value in scope.get("headers", []):
            headers.setdefault(key.lower(), []).append(value)
        if headers.get(b"host") != [f"127.0.0.1:{self.security.port}".encode("ascii")]:
            await _error(400, "INVALID_HOST", "local Host required")(scope, receive, send)
            return
        if b"origin" in headers:
            await _error(403, "ORIGIN_NOT_ALLOWED", "Origin requests are unavailable")(scope, receive, send)
            return
        tokens = headers.get(TOKEN_HEADER.encode("ascii"), [])
        if len(tokens) != 1 or not hmac.compare_digest(tokens[0], self.security.token.encode("ascii")):
            await _error(401, "UNAUTHORIZED", "management token required")(scope, receive, send)
            return
        if scope["method"] == "POST" and headers.get(b"content-type") not in (
            [b"application/json"], [b"application/json; charset=utf-8"]
        ):
            await _error(415, "UNSUPPORTED_MEDIA_TYPE", "application/json required")(scope, receive, send)
            return
        body = bytearray()
        chunks = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await _error(400, "INVALID_REQUEST", "incomplete request body")(scope, receive, send)
                return
            chunks += 1
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES or chunks > 256:
                await _error(413, "BODY_TOO_LARGE", "request body exceeds limit")(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        if scope["method"] != "POST" and body:
            await _error(400, "INVALID_REQUEST", "request body is not supported")(scope, receive, send)
            return
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._serve_safely(scope, replay, send)


def _installed_domains() -> DomainList:
    """Read installed packs through the canonical loader, including invalid ones."""
    if not _DOMAINS_ROOT.is_dir():
        raise ProjectManagerError("INVALID_CATALOG", "installed Domain catalog is unavailable")
    domains: list[DomainView] = []
    for entry in sorted(_DOMAINS_ROOT.iterdir(), key=lambda path: path.name):
        if not entry.is_dir() and not entry.is_symlink():
            continue
        try:
            if entry.is_symlink():
                raise DomainPackError("symbolic Domain pack")
            pack = _load(entry.name)
            domains.append(DomainView(id=entry.name, tagline=pack.tagline, configuration_valid=True))
        except (DomainPackError, OSError, ValueError, yaml.YAMLError):
            domains.append(DomainView(id=entry.name, tagline=None, configuration_valid=False,
                                      diagnostic="INVALID_DOMAIN_PACK"))
    return DomainList(domains=domains, runtime_checked=False)


def create_management_app(manager: ProjectManager, security: ManagementSecurity) -> FastAPI:
    """Build a separate, offline management ASGI application with explicit authority."""
    app = FastAPI(title="Morgoth Management API", version="1.0.0",
                  docs_url=None, redoc_url=None, openapi_url=None, debug=False)
    app.add_middleware(LocalAuthorityBoundary, security=security)

    @app.exception_handler(ProjectManagerError)
    async def manager_error(_request: Request, exc: ProjectManagerError) -> JSONResponse:
        return _error(_ERROR_STATUS.get(exc.code, 500), exc.code, str(exc))

    @app.exception_handler(RequestValidationError)
    async def request_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return _error(422, "INVALID_REQUEST", "invalid management request")

    errors: dict[int, dict[str, Any]] = {code: {"model": ErrorEnvelope} for code in
                                        (400, 401, 403, 404, 409, 413, 415, 422, 500)}

    @app.get(f"{API_PREFIX}/status", response_model=ManagementStatus,
             operation_id="management_status_v1", responses=errors)
    async def status() -> ManagementStatus:
        """Report only management API availability."""
        return ManagementStatus(api_version="1", management_available=True)

    @app.get(f"{API_PREFIX}/domains", response_model=DomainList,
             operation_id="management_list_domains_v1", responses=errors)
    def domains() -> DomainList:
        """Inspect installed Domain configuration, without starting a runtime."""
        return _installed_domains()

    @app.get(f"{API_PREFIX}/projects", response_model=ProjectList,
             operation_id="management_list_projects_v1", responses=errors)
    def projects() -> dict[str, Any]:
        """List the explicit home's Project catalog."""
        return manager.list_projects()

    @app.get(f"{API_PREFIX}/projects/{{project_id}}", response_model=ProjectView,
             operation_id="management_get_project_v1", responses=errors)
    def project(project_id: str) -> dict[str, Any]:
        """Inspect one Project configuration."""
        return manager.get_project(project_id)

    @app.get(f"{API_PREFIX}/projects/{{project_id}}/validation", response_model=ProjectValidation,
             operation_id="management_validate_project_v1", responses=errors)
    def validation(project_id: str) -> dict[str, Any]:
        """Validate a Project through the existing manager."""
        return manager.validate_project(project_id)

    @app.post(f"{API_PREFIX}/projects", response_model=ProjectCreation, status_code=201,
              operation_id="management_create_project_v1", responses=errors)
    def create(request: CreateProjectRequest) -> dict[str, Any]:
        """Publish local configuration atomically through the existing manager."""
        return manager.create_project(request.id, name=request.name, domain=request.domain)

    original_openapi: Callable[[], dict[str, Any]] = app.openapi

    def management_openapi() -> dict[str, Any]:
        """Generate the real route schema, describing header authority only."""
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["ManagementToken"] = {
            "type": "apiKey", "in": "header", "name": "X-Morgoth-Management-Token"}
        schema["security"] = [{"ManagementToken": []}]
        return schema

    app.openapi = management_openapi
    return app
