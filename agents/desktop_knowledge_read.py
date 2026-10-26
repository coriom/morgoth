"""Bounded, authenticated Desktop projection of managed Project Markdown vaults.

The caller supplies a canonical catalog Project, never a filesystem path. No
compiler, database, model, or research runtime is involved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from core.project import ProjectConfig
from project_manager import ProjectManager, ProjectManagerError

PREFIX = "/desktop-knowledge/v1"
SOURCE = "PROJECT_MARKDOWN_VAULT"
MAX_DOCUMENTS = 96
MAX_FILE = 16_384
MAX_LINKS = 32
LINK = re.compile(r"\[\[([^\[\]\n]{1,180})\]\]")
DOC_ID = re.compile(r"[0-9a-f]{64}\Z")


class KnowledgeUnavailable(Exception):
    """Fixed, non-sensitive source refusal."""


class LinkRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    target_id: str | None


class DocumentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    document_id: str
    project_id: str
    title: str
    recorded_at: str
    links: list[LinkRead]
    source: str = SOURCE
    content: str | None = None


class KnowledgePage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = 1
    project_id: str
    source: str = SOURCE
    read_at: str
    documents: list[DocumentRead]
    next_offset: int | None


def _id(relative: str) -> str:
    return hashlib.sha256(relative.encode("utf-8")).hexdigest()


def _open_root(project: ProjectConfig, manager: ProjectManager) -> int:
    if project.is_legacy or project.vault_dir != manager.home.projects_root / project.id / "vault":
        raise KnowledgeUnavailable("MANAGED_PROJECT_REQUIRED")
    # ProjectManager validates the catalog; held descriptors prevent a later
    # swap of Projects or the selected Project into a symlink.
    try:
        fd = os.open(manager.home.application_home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise KnowledgeUnavailable("SOURCE_UNAVAILABLE") from None
    except OSError:
        raise KnowledgeUnavailable("UNSAFE_SOURCE") from None
    try:
        for name in ("projects", project.id, "vault"):
            try:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                raise KnowledgeUnavailable("SOURCE_UNAVAILABLE") from None
            except OSError:
                raise KnowledgeUnavailable("UNSAFE_SOURCE") from None
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _walk(fd: int, prefix: str = "", depth: int = 0) -> list[tuple[str, bytes, float]]:
    result: list[tuple[str, bytes, float]] = []
    try:
        names = []
        with os.scandir(fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > 128:
                    raise KnowledgeUnavailable("SOURCE_TOO_LARGE")
        names.sort()
    except OSError:
        raise KnowledgeUnavailable("SOURCE_UNAVAILABLE") from None
    for name in names:
        if name.startswith(".") or len(name) > 100:
            continue
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise KnowledgeUnavailable("UNSAFE_SOURCE")
            if stat.S_ISDIR(info.st_mode):
                if depth >= 3:
                    raise KnowledgeUnavailable("SOURCE_TOO_LARGE")
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    result.extend(_walk(child, prefix + name + "/", depth + 1))
                finally:
                    os.close(child)
            elif name.endswith(".md"):
                if not stat.S_ISREG(info.st_mode):
                    raise KnowledgeUnavailable("UNSAFE_SOURCE")
                if info.st_size > MAX_FILE:
                    raise KnowledgeUnavailable("SOURCE_TOO_LARGE")
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    checked = os.fstat(file_fd)
                    if not stat.S_ISREG(checked.st_mode) or checked.st_size > MAX_FILE:
                        raise KnowledgeUnavailable("UNSAFE_SOURCE")
                    raw = os.read(file_fd, MAX_FILE + 1)
                    if len(raw) > MAX_FILE:
                        raise KnowledgeUnavailable("SOURCE_TOO_LARGE")
                    result.append((prefix + name, raw, checked.st_mtime))
                finally:
                    os.close(file_fd)
        except OSError:
            raise KnowledgeUnavailable("SOURCE_UNAVAILABLE") from None
        if len(result) > MAX_DOCUMENTS:
            raise KnowledgeUnavailable("SOURCE_TOO_LARGE")
    return result


def read_vault(project: ProjectConfig, manager: ProjectManager) -> list[DocumentRead]:
    """Read only regular UTF-8 Markdown under one managed Project vault."""
    fd = _open_root(project, manager)
    try:
        files = _walk(fd)
    finally:
        os.close(fd)
    paths = {path for path, _, _ in files}
    documents = []
    for path, raw, modified in files:
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            raise KnowledgeUnavailable("INCOMPATIBLE_SOURCE") from None
        title = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ")), Path(path).stem)[:160]
        links = []
        for match in LINK.finditer(content):
            if len(links) >= MAX_LINKS:
                raise KnowledgeUnavailable("SOURCE_TOO_LARGE")
            target, _, label = match.group(1).partition("|")
            target = target.strip().removesuffix(".md")
            candidate = PurePosixPath(target)
            # Wiki links are vault-root-relative. Never interpret ../, absolute,
            # URL, or backslash content as a traversable path.
            valid = (not candidate.is_absolute() and len(candidate.parts) <= 4
                     and all(part not in (".", "..") for part in candidate.parts)
                     and "\\" not in target and ":" not in target)
            relative = str(candidate) + ".md" if valid else ""
            links.append(LinkRead(label=(label.strip() or target)[:160],
                                  target_id=_id(relative) if relative in paths else None))
        documents.append(DocumentRead(document_id=_id(path), project_id=project.id, title=title,
                                      recorded_at=datetime.fromtimestamp(modified, timezone.utc).isoformat(),
                                      links=links, content=content))
    return documents


def install_desktop_knowledge_routes(app: FastAPI, manager: ProjectManager) -> None:
    """Mount fixed GET routes behind the Management token/loopback boundary."""
    def records(project_id: str) -> list[DocumentRead]:
        manager.get_project(project_id)
        project = next((item for item in manager._catalog() if item.id == project_id), None)
        if project is None:
            raise ProjectManagerError("UNKNOWN_PROJECT", "Project unavailable")
        return read_vault(project, manager)

    def refusal(exc: KnowledgeUnavailable) -> JSONResponse:
        code = str(exc)
        status = 422 if code == "MANAGED_PROJECT_REQUIRED" else 409 if code in {"UNSAFE_SOURCE", "INCOMPATIBLE_SOURCE", "SOURCE_TOO_LARGE"} else 503
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": "knowledge source unavailable"}})

    @app.get(f"{PREFIX}/projects/{{project_id}}/documents", response_model=KnowledgePage)
    async def list_documents(project_id: str, limit: int = Query(20, ge=1, le=20),
                       offset: int = Query(0, ge=0, le=95),
                       query: str = Query("", max_length=80)) -> KnowledgePage | JSONResponse:
        """List a bounded Project vault; optional casefolded content search."""
        try:
            all_docs = records(project_id)
        except KnowledgeUnavailable as exc:
            return refusal(exc)
        selected = [d for d in all_docs if query.casefold() in (d.title + "\n" + (d.content or "")).casefold()]
        page = selected[offset:offset + limit]
        return KnowledgePage(project_id=project_id, read_at=datetime.now(timezone.utc).isoformat(),
                             documents=[d.model_copy(update={"content": None}) for d in page],
                             next_offset=offset + limit if offset + limit < len(selected) else None)

    @app.get(f"{PREFIX}/projects/{{project_id}}/documents/{{document_id}}", response_model=DocumentRead)
    async def get_document(project_id: str, document_id: str) -> DocumentRead | JSONResponse:
        """Read one opaque document ID, never a caller-controlled path."""
        if not DOC_ID.fullmatch(document_id):
            return JSONResponse(status_code=422, content={"error": {"code": "INVALID_DOCUMENT", "message": "invalid document ID"}})
        try:
            found = next((d for d in records(project_id) if d.document_id == document_id), None)
        except KnowledgeUnavailable as exc:
            return refusal(exc)
        if found is None:
            return JSONResponse(status_code=404, content={"error": {"code": "UNKNOWN_DOCUMENT", "message": "document unavailable"}})
        return found
