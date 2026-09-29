"""Immutable project selection and the sole effective storage namespace resolver.

One Project per process. Domain packs supply semantics; their legacy namespace
fields are consulted ONLY by the built-in default-project migration adapter.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator
import yaml

from core.runtime import ENGINE_ROOT, runtime_home
from core.storage_namespace import collection_names, overlap, storage_path, validate_identifier, validate_namespace

DEFAULT_PROJECT = "default"


class ProjectConfigError(ValueError):
    """Invalid project selection/catalog; refuse before opening storage."""


class ProjectConfig(BaseModel):
    """Project instance configuration, deeply immutable and independent of Domain."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    name: str
    domain: str
    workspace_root: Path | None = None
    postgres_schema: str
    chroma_prefix: str
    vault_dir: Path
    runtime_dir: Path
    llm_overrides: Mapping[str, str] = Field(default_factory=dict)
    metadata: Mapping[str, str] = Field(default_factory=dict)

    @field_validator("id", "domain")
    @classmethod
    def identifier(cls, value: str) -> str:
        """Project and domain IDs cannot select paths outside the catalog."""
        return validate_identifier(value)

    @field_validator("name")
    @classmethod
    def readable_name(cls, value: str) -> str:
        """Require a nonempty human-facing name."""
        if not value.strip():
            raise ValueError("project name must not be empty")
        return value

    @field_validator("vault_dir", "runtime_dir", "workspace_root", mode="before")
    @classmethod
    def canonical_path(cls, value: str | Path | None) -> Path | None:
        """Normalize storage identity before checking catalog collisions."""
        return None if value is None else storage_path(value)

    @model_validator(mode="after")
    def namespaces(self) -> "ProjectConfig":
        """Reserve legacy values; validate routes and freeze nested mappings."""
        validate_namespace(self.postgres_schema, self.chroma_prefix, legacy=self.id == DEFAULT_PROJECT)
        if self.id == DEFAULT_PROJECT and (self.domain != "crypto" or self.postgres_schema != "public" or self.chroma_prefix != ""):
            raise ValueError("default project is reserved for the historical crypto namespace")
        if overlap(self.vault_dir, self.runtime_dir):
            raise ValueError("vault and runtime directories must be disjoint")
        for task, spec in self.llm_overrides.items():
            # Pure validation, no provider initialization / inference.
            from core.llm.tasks import DEFAULTS
            from core.llm.registry import _parse_spec
            if task not in DEFAULTS:
                raise ValueError("unknown project LLM task")
            _parse_spec(spec)
        object.__setattr__(self, "llm_overrides", MappingProxyType(dict(self.llm_overrides)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        return self

    @field_serializer("llm_overrides", "metadata")
    def serialize_mapping(self, value: Mapping[str, str]) -> dict[str, str]:
        """Keep JSON/YAML-facing mappings ordinary objects."""
        return dict(value)

    @property
    def is_legacy(self) -> bool:
        """The reserved built-in project alone retains historical paths."""
        return self.id == DEFAULT_PROJECT

    @property
    def chroma_dir(self) -> Path:
        """Project-local Chroma state as well as collection-level namespacing."""
        return self.runtime_dir / "chroma_db"

    @property
    def ui_token_dir(self) -> Path:
        """Retain the legacy UI token location, isolate all new projects."""
        return Path.home() / ".morgoth" if self.is_legacy else self.runtime_dir / "auth"


# Conceptual name and config name refer to the same immutable object.
Project = ProjectConfig


def legacy_project() -> ProjectConfig:
    """Single migration boundary: import crypto's historical storage fields."""
    from core.domain import _load, DEFAULT_DOMAIN
    pack = _load(DEFAULT_DOMAIN)
    return ProjectConfig(id=DEFAULT_PROJECT, name="Morgoth", domain=pack.name,
                         postgres_schema=pack.postgres_schema, chroma_prefix=pack.chroma_prefix,
                         vault_dir=pack.vault_dir, runtime_dir=ENGINE_ROOT / "data")


def _writable_roots(project: ProjectConfig) -> tuple[Path, ...]:
    roots = (project.vault_dir, project.runtime_dir)
    return roots + ((project.workspace_root,) if project.workspace_root else ())


def validate_projects(projects: tuple[ProjectConfig, ...]) -> None:
    """Reject duplicate IDs, schema/name collisions and overlapping writable roots."""
    for index, project in enumerate(projects):
        if not project.is_legacy:
            for path in _writable_roots(project):
                if overlap(path, ENGINE_ROOT) or overlap(path, (Path.home() / ".morgoth").resolve()):
                    raise ProjectConfigError("project storage overlaps reserved installation state")
        for other in projects[:index]:
            if project.id == other.id:
                raise ProjectConfigError("duplicate project identifier")
            if project.postgres_schema == other.postgres_schema:
                raise ProjectConfigError("projects share a PostgreSQL schema")
            if set(collection_names(project.chroma_prefix)) & set(collection_names(other.chroma_prefix)):
                raise ProjectConfigError("projects share physical Chroma collections")
            for a in _writable_roots(project):
                for b in _writable_roots(other):
                    if overlap(a, b):
                        raise ProjectConfigError("project writable directories overlap")


def load_projects(root: Path | None = None) -> tuple[ProjectConfig, ...]:
    """Read the complete local catalog; invalid neighbours fail closed too."""
    root = root if root is not None else runtime_home().projects_root
    projects = [legacy_project()]
    for path in sorted(root.glob("*/project.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            project = ProjectConfig.model_validate(raw)
        except (OSError, ValueError, yaml.YAMLError):
            raise ProjectConfigError("invalid project manifest (required fields, types or namespaces)") from None
        if project.id != path.parent.name or project.is_legacy:
            raise ProjectConfigError("project manifest ID must match its directory; default is reserved")
        from core.domain import _load
        _load(project.domain)  # Domain existence/type validation, no runtime storage.
        projects.append(project)
    result = tuple(projects)
    validate_projects(result)
    return result


@lru_cache(maxsize=1)
def current_project() -> ProjectConfig:
    """Bind MORGOTH_PROJECT once; unknown IDs never fall back to default."""
    identifier = os.environ.get("MORGOTH_PROJECT", DEFAULT_PROJECT)
    try:
        validate_identifier(identifier)
    except ValueError:
        raise ProjectConfigError("invalid MORGOTH_PROJECT identifier") from None
    selected = next((p for p in load_projects() if p.id == identifier), None)
    if selected is None:
        raise ProjectConfigError(f"unknown project: {identifier}")
    domain_override = os.environ.get("MORGOTH_DOMAIN")
    if domain_override and domain_override.strip() != selected.domain:
        raise ProjectConfigError("MORGOTH_DOMAIN conflicts with Project.domain; select a matching project")
    return selected


def current_namespace() -> ProjectConfig:
    """Canonical effective namespace: storage consumers must use this resolver."""
    return current_project()
