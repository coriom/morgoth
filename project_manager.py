"""Offline, Project-scoped catalog management; no engine or storage startup."""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import stat
from typing import Any

from pydantic import ValidationError
import yaml

from core.domain import DomainPackError, _load
from core.project import (
    DEFAULT_PROJECT, ProjectConfig, ProjectConfigError, load_projects,
    validate_projects,
)
from core.runtime import RuntimeHome
from core.storage_namespace import overlap, validate_identifier


class ProjectManagerError(ValueError):
    """Safe, structured management failure for CLI or later desktop adapters."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _reject_symlink_components(path: Path) -> None:
    """Reject any existing symlink in a write or catalog path."""
    for component in (path, *path.parents):
        try:
            if component.is_symlink():
                raise ProjectManagerError("PATH_CONFLICT", "catalog path contains a symbolic link")
        except OSError:
            raise ProjectManagerError("PATH_CONFLICT", "catalog path cannot be inspected") from None


def _open_directory(path: Path) -> int:
    """Open a real directory without following its final component."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


class ProjectManager:
    """Manage one explicit RuntimeHome without rebinding the engine Project."""

    def __init__(self, home: RuntimeHome) -> None:
        """Bind an explicit, safe catalog path without reading or creating it."""
        if (not home.application_home.is_absolute()
                or home.application_home == Path(home.application_home.anchor)
                or home.projects_root != home.application_home / "projects"
                or overlap(home.projects_root, home.engine_root)
                or overlap(home.projects_root, (Path.home() / ".morgoth").resolve())):
            raise ProjectManagerError("PATH_CONFLICT", "unsafe Project catalog location")
        self.home = home

    def _catalog(self) -> tuple[ProjectConfig, ...]:
        """Load every manifest through the canonical loader, failing closed."""
        root = self.home.projects_root
        _reject_symlink_components(root)
        if root.exists():
            if not root.is_dir():
                raise ProjectManagerError("PATH_CONFLICT", "Project catalog is not a directory")
            try:
                for entry in root.iterdir():
                    if entry.is_symlink() or (entry.is_dir() and (entry / "project.yaml").is_symlink()):
                        raise ProjectManagerError("PATH_CONFLICT", "Project catalog contains a symbolic link")
            except OSError:
                raise ProjectManagerError("INVALID_CATALOG", "Project catalog cannot be inspected") from None
        try:
            return load_projects(root)
        except (ProjectConfigError, DomainPackError, OSError, ValueError, yaml.YAMLError):
            raise ProjectManagerError("INVALID_CATALOG", "Project catalog contains an invalid manifest or collision") from None

    def list_projects(self) -> dict[str, Any]:
        """List configured Projects only; never check engine readiness."""
        projects = self._catalog()
        return {"projects": [self._view(project) for project in projects],
                "configuration_valid": True, "runtime_checked": False}

    def get_project(self, project_id: str) -> dict[str, Any]:
        """Inspect one Project without changing the process-bound selection."""
        project_id = self._checked_id(project_id)
        project = next((item for item in self._catalog() if item.id == project_id), None)
        if project is None:
            raise ProjectManagerError("UNKNOWN_PROJECT", "Project is not in the catalog")
        return self._view(project)

    def validate_project(self, project_id: str) -> dict[str, Any]:
        """Validate the entire catalog and one selected manifest canonically."""
        view = self.get_project(project_id)
        return {"project": view, "configuration_valid": True, "runtime_checked": False}

    def create_project(self, project_id: str, *, name: str, domain: str) -> dict[str, Any]:
        """Provision owned local configuration, then publish its manifest atomically."""
        project_id = self._checked_id(project_id)
        if project_id == DEFAULT_PROJECT:
            raise ProjectManagerError("INVALID_REQUEST", "default is a reserved Project identifier")
        if (not isinstance(name, str) or not name.strip() or len(name) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ProjectManagerError("INVALID_REQUEST", "Project name must be 1–128 printable characters")
        try:
            validate_identifier(domain)
            _load(domain)
        except (ValueError, DomainPackError, OSError, yaml.YAMLError):
            raise ProjectManagerError("UNKNOWN_DOMAIN", "Domain is not an installed valid pack") from None
        digest = hashlib.sha256(project_id.encode("ascii")).hexdigest()
        project_dir = self.home.projects_root / project_id
        try:
            project = ProjectConfig(
                id=project_id, name=name.strip(), domain=domain,
                postgres_schema=f"p_{digest[:60]}", chroma_prefix=f"p_{digest[:28]}_",
                vault_dir=project_dir / "vault", runtime_dir=project_dir / "runtime",
                workspace_root=project_dir / "workspace",
            )
        except (ValidationError, ValueError):
            raise ProjectManagerError("INVALID_REQUEST", "generated Project configuration is invalid") from None
        self._check_candidate(project)
        return self._create_locked(project)

    @staticmethod
    def _checked_id(project_id: str) -> str:
        try:
            return validate_identifier(project_id)
        except ValueError:
            raise ProjectManagerError("INVALID_REQUEST", "invalid Project identifier") from None

    def _check_candidate(self, project: ProjectConfig) -> None:
        existing = self._catalog()
        if any(item.id == project.id for item in existing):
            raise ProjectManagerError("ALREADY_EXISTS", "Project identifier already exists")
        try:
            validate_projects((*existing, project))
        except ProjectConfigError:
            raise ProjectManagerError("NAMESPACE_CONFLICT", "Project paths or namespaces conflict") from None

    def _ensure_catalog_root(self) -> None:
        """Create only owned home/catalog directories; refuse symlink traversal."""
        home, root = self.home.application_home, self.home.projects_root
        _reject_symlink_components(root)
        if not home.exists():
            try:
                parent_fd = _open_directory(home.parent)
                try:
                    try:
                        os.mkdir(home.name, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        # A cooperating creator won the race; open it nofollow below.
                        pass
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                raise ProjectManagerError("PUBLICATION_FAILED", "application home cannot be created") from None
        try:
            home_fd = _open_directory(home)
            try:
                try:
                    os.mkdir(root.name, mode=0o700, dir_fd=home_fd)
                except FileExistsError:
                    pass
                os.fsync(home_fd)
            finally:
                os.close(home_fd)
            _reject_symlink_components(root)
        except OSError:
            raise ProjectManagerError("PATH_CONFLICT", "Project catalog cannot be opened safely") from None

    def _create_locked(self, project: ProjectConfig) -> dict[str, Any]:
        self._ensure_catalog_root()
        try:
            root_fd = _open_directory(self.home.projects_root)
            try:
                try:
                    lock_fd = os.open(".project-manager.lock",
                                      os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                                      0o600, dir_fd=root_fd)
                except OSError:
                    raise ProjectManagerError("PATH_CONFLICT", "unsafe Project manager lock") from None
                try:
                    mode = os.fstat(lock_fd).st_mode
                    if not stat.S_ISREG(mode) or mode & 0o077:
                        raise ProjectManagerError("PATH_CONFLICT", "unsafe Project manager lock")
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    self._check_candidate(project)
                    return self._create_tree(root_fd, project)
                finally:
                    os.close(lock_fd)
            finally:
                os.close(root_fd)
        except ProjectManagerError:
            raise
        except OSError:
            raise ProjectManagerError("PUBLICATION_FAILED", "Project publication failed") from None

    def _create_tree(self, root_fd: int, project: ProjectConfig) -> dict[str, Any]:
        """The final directory is undiscoverable until the manifest link exists."""
        try:
            os.mkdir(project.id, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            raise ProjectManagerError("ALREADY_EXISTS", "Project directory already exists") from None
        except OSError:
            raise ProjectManagerError("PUBLICATION_FAILED", "Project directory cannot be created") from None
        try:
            project_fd = _open_directory(self.home.projects_root / project.id)
        except OSError:
            try:
                os.rmdir(project.id, dir_fd=root_fd)
            except OSError:
                pass
            raise ProjectManagerError("PUBLICATION_FAILED", "Project directory cannot be opened safely") from None
        published = False
        try:
            for child in ("vault", "runtime", "workspace"):
                os.mkdir(child, mode=0o700, dir_fd=project_fd)
            manifest = project.model_dump(mode="json", exclude={"llm_overrides", "metadata"})
            data = yaml.safe_dump(manifest, sort_keys=True).encode("utf-8")
            fd = os.open("project.yaml.pending", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=project_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Validate exactly the bytes that will be linked into the catalog.
            ProjectConfig.model_validate(yaml.safe_load(data))
            os.fsync(project_fd)
            self._publish_manifest(project_fd)
            published = True
            durability_confirmed = True
            try:
                os.unlink("project.yaml.pending", dir_fd=project_fd)
                os.fsync(project_fd)
                os.fsync(root_fd)
            except OSError:
                # Publication already succeeded; report its durability limit
                # instead of falsely reporting that no Project was created.
                durability_confirmed = False
            return {"project": self._view(project), "created": True,
                    "engine_started": False, "storage_initialized": False,
                    "durability_confirmed": durability_confirmed}
        except ProjectManagerError:
            raise
        except (OSError, ValueError, ValidationError, yaml.YAMLError):
            raise ProjectManagerError("PUBLICATION_FAILED", "Project publication failed") from None
        finally:
            if not published:
                self._cleanup_unpublished(root_fd, project_fd, project.id)
            os.close(project_fd)

    @staticmethod
    def _publish_manifest(project_fd: int) -> None:
        """One non-overwriting link is the catalog publication point."""
        os.link("project.yaml.pending", "project.yaml",
                src_dir_fd=project_fd, dst_dir_fd=project_fd, follow_symlinks=False)

    @staticmethod
    def _cleanup_unpublished(root_fd: int, project_fd: int, project_id: str) -> None:
        """Remove only our empty staged tree; never recurse into user content."""
        try:
            if os.stat(project_id, dir_fd=root_fd, follow_symlinks=False).st_ino != os.fstat(project_fd).st_ino:
                return
            for name in ("project.yaml.pending",):
                try:
                    os.unlink(name, dir_fd=project_fd)
                except FileNotFoundError:
                    pass
            for name in ("workspace", "runtime", "vault"):
                try:
                    os.rmdir(name, dir_fd=project_fd)
                except FileNotFoundError:
                    pass
            os.rmdir(project_id, dir_fd=root_fd)
        except OSError:
            # Foreign/new content prevents empty-dir removal; never delete it.
            pass

    def _view(self, project: ProjectConfig) -> dict[str, Any]:
        """Expose configuration, never metadata, credentials or readiness claims."""
        return {
            "id": project.id, "name": project.name, "domain": project.domain,
            "legacy": project.is_legacy,
            "manifest_path": (str(self.home.projects_root / project.id / "project.yaml")
                              if not project.is_legacy else None),
            "workspace_root": str(project.workspace_root) if project.workspace_root else None,
            "postgres_schema": project.postgres_schema,
            "chroma_prefix": project.chroma_prefix,
            "vault_dir": str(project.vault_dir), "runtime_dir": str(project.runtime_dir),
            "configuration_valid": True, "runtime_checked": False,
        }
