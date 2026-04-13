"""Centralized configuration loading and permission helpers for Morgoth."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError


ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
PERMS_PATH = ROOT_DIR / "MORGOTH_PERMS.json"

REASONING_TASK_TYPES = {"reasoning", "finance", "code_review", "self_modify"}
AGENT_TASK_TYPES = {"quick_lookup", "agent_subtask", "summarize"}


class PermissionDeniedError(Exception):
    """Raised when a runtime action violates Morgoth permissions."""


class PermissionFlags(BaseModel):
    """Boolean capability flags loaded from ``MORGOTH_PERMS.json``."""

    can_create_ephemeral_agents: bool
    can_create_persistent_agents: bool
    can_self_modify: bool
    can_store_secrets: bool
    can_pull_ollama_models: bool
    can_execute_code: bool
    can_write_files: bool
    can_send_notifications: bool
    can_access_internet: bool
    can_place_real_orders: bool


class NotificationLevels(BaseModel):
    """Notification routing rules per severity."""

    INFO: list[str]
    WARNING: list[str]
    CRITICAL: list[str]


class TaskLimits(BaseModel):
    """Task and agent execution limits."""

    max_concurrent_agents: int
    max_recurring_tasks: int


class MorgothPermissions(BaseModel):
    """Full permissions document schema."""

    version: str
    last_updated_by: str
    permissions: PermissionFlags
    evolvable_zone_paths: list[str]
    immutable_zone_paths: list[str]
    notification_levels: NotificationLevels
    task_limits: TaskLimits


class AppConfig(BaseModel):
    """Application settings loaded from environment and permission files."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    postgres_url: str = Field(alias="POSTGRES_URL")
    ollama_base_url: HttpUrl = Field(alias="OLLAMA_BASE_URL")
    ollama_primary_model: str = Field(alias="OLLAMA_PRIMARY_MODEL")
    ollama_agent_model: str = Field(alias="OLLAMA_AGENT_MODEL")
    coingecko_api_key: str = Field(default="", alias="COINGECKO_API_KEY")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    secret_key: str = Field(alias="SECRET_KEY")
    max_concurrent_agents: int = Field(alias="MAX_CONCURRENT_AGENTS")
    log_retention_days: int = Field(alias="LOG_RETENTION_DAYS")
    log_level_thought: bool = Field(alias="LOG_LEVEL_THOUGHT")
    root_dir: Path = ROOT_DIR
    data_dir: Path = ROOT_DIR / "data"
    logs_dir: Path = ROOT_DIR / "data" / "logs"
    chroma_dir: Path = ROOT_DIR / "data" / "chroma_db"
    perms_path: Path = PERMS_PATH
    permissions: MorgothPermissions

    def choose_model_for_task(self, task_type: str) -> str:
        """Select the model defined by the specification for a task type."""

        if task_type in REASONING_TASK_TYPES:
            return self.ollama_primary_model
        if task_type in AGENT_TASK_TYPES:
            return self.ollama_agent_model
        return self.ollama_primary_model

    def resolve_path(self, raw_path: str | Path) -> Path:
        """Resolve an application path relative to the repository root."""

        path = Path(raw_path)
        if path.is_absolute():
            return path.resolve()
        return (self.root_dir / path).resolve()

    def is_path_in_zone(self, raw_path: str | Path, zone_paths: list[str]) -> bool:
        """Return whether a path belongs to one of the configured zone roots."""

        resolved_path = self.resolve_path(raw_path)
        for zone in zone_paths:
            zone_path = self.resolve_path(zone)
            if resolved_path == zone_path or zone_path in resolved_path.parents:
                return True
        return False

    def ensure_path_readable(self, raw_path: str | Path) -> Path:
        """Validate that the path is inside the repository before reading."""

        resolved_path = self.resolve_path(raw_path)
        if self.root_dir != resolved_path and self.root_dir not in resolved_path.parents:
            raise PermissionDeniedError(f"Read path outside repository: {resolved_path}")
        return resolved_path

    def ensure_path_writable(self, raw_path: str | Path) -> Path:
        """Validate that the path can be written under current permissions."""

        if not self.permissions.permissions.can_write_files:
            raise PermissionDeniedError("File writing is disabled by permissions")

        resolved_path = self.resolve_path(raw_path)
        if self.is_path_in_zone(resolved_path, self.permissions.immutable_zone_paths):
            raise PermissionDeniedError(f"Immutable path cannot be written: {resolved_path}")
        if not self.is_path_in_zone(resolved_path, self.permissions.evolvable_zone_paths):
            raise PermissionDeniedError(f"Path outside evolvable zone: {resolved_path}")
        return resolved_path


async def _read_json_file(path: Path) -> dict[str, Any]:
    """Read a JSON file asynchronously and return its content."""

    def _reader() -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    return await asyncio.to_thread(_reader)


def _load_environment(env_path: Path = ENV_PATH) -> None:
    """Load environment variables from ``.env`` if present."""

    load_dotenv(dotenv_path=env_path, override=False)


async def load_permissions(path: Path = PERMS_PATH) -> MorgothPermissions:
    """Load and validate the Morgoth permissions file."""

    payload = await _read_json_file(path)
    permissions = MorgothPermissions.model_validate(payload)
    logger.debug("Loaded permissions from {}", path)
    return permissions


async def load_config() -> AppConfig:
    """Load the complete application configuration."""

    await asyncio.to_thread(_load_environment)
    permissions = await load_permissions()

    env_values = {
        "POSTGRES_URL": os.getenv("POSTGRES_URL"),
        "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL"),
        "OLLAMA_PRIMARY_MODEL": os.getenv("OLLAMA_PRIMARY_MODEL"),
        "OLLAMA_AGENT_MODEL": os.getenv("OLLAMA_AGENT_MODEL"),
        "COINGECKO_API_KEY": os.getenv("COINGECKO_API_KEY", ""),
        "TELEGRAM_BOT_TOKEN": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "TELEGRAM_CHAT_ID": os.getenv("TELEGRAM_CHAT_ID", ""),
        "SECRET_KEY": os.getenv("SECRET_KEY"),
        "MAX_CONCURRENT_AGENTS": os.getenv("MAX_CONCURRENT_AGENTS"),
        "LOG_RETENTION_DAYS": os.getenv("LOG_RETENTION_DAYS"),
        "LOG_LEVEL_THOUGHT": os.getenv("LOG_LEVEL_THOUGHT"),
        "permissions": permissions,
    }

    try:
        config = AppConfig.model_validate(env_values)
    except ValidationError:
        logger.exception("Failed to validate application configuration")
        raise

    await asyncio.to_thread(config.logs_dir.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(config.chroma_dir.mkdir, parents=True, exist_ok=True)

    logger.info(
        "Configuration loaded with primary model '{}' and agent model '{}'",
        config.ollama_primary_model,
        config.ollama_agent_model,
    )
    return config
