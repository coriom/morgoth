"""Shared test fixtures for Morgoth."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import (
    AppConfig,
    MorgothPermissions,
    NotificationLevels,
    PermissionFlags,
    TaskLimits,
)
from memory.episodic import QueryMatch


class DummyResponse:
    """Minimal async HTTP response test double."""

    def __init__(self, payload: dict[str, Any] | None = None, text: str = "") -> None:
        """Store the payload returned by the double."""

        self._payload = payload or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        """Return the configured JSON payload."""

        return self._payload

    def raise_for_status(self) -> None:
        """Simulate a successful HTTP response."""


class DummyHTTPClient:
    """Simple async HTTP client test double."""

    def __init__(self, response: DummyResponse) -> None:
        """Store a fixed response."""

        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> DummyResponse:
        """Record a GET call and return the fixed response."""

        self.calls.append(("GET", url, {"params": params or {}, "headers": headers or {}}))
        return self.response

    async def post(self, url: str, json: dict[str, Any] | None = None) -> DummyResponse:
        """Record a POST call and return the fixed response."""

        self.calls.append(("POST", url, {"json": json or {}}))
        return self.response

    async def aclose(self) -> None:
        """Simulate client shutdown."""


class DummyPersistentMemory:
    """Minimal persistent memory test double."""

    def __init__(self) -> None:
        """Initialize captured writes."""

        self.snapshots: list[dict[str, Any]] = []

    async def insert_market_snapshot(self, payload: dict[str, Any]) -> None:
        """Capture a market snapshot write."""

        self.snapshots.append(payload)


class DummyAgentManager:
    """Minimal agent manager test double."""

    async def create(self, name: str, task: str, agent_type: str, tools: list[str], user_id: str) -> dict[str, Any]:
        """Return a predictable created agent payload."""

        return {
            "agent_id": "agent-123",
            "name": name,
            "task": task,
            "agent_type": agent_type,
            "tools": tools,
            "user_id": user_id,
        }


class DummyNotifier:
    """Minimal notifier test double."""

    async def send(self, level: str, content: str) -> bool:
        """Pretend the notification was sent."""

        return True


class DummyEpisodicMemory:
    """Minimal episodic memory test double."""

    async def add_text(
        self,
        collection_name: str,
        content: str,
        *,
        category: str,
        agent_id: str,
        user_id: str = "default",
    ) -> str:
        """Return a predictable document id."""

        return "doc-123"

    async def query(self, collection_name: str, query_text: str, *, limit: int = 5) -> list[QueryMatch]:
        """Return a predictable query result."""

        return [
            QueryMatch(
                document_id="doc-123",
                content="remembered content",
                metadata={
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "agent_id": "morgoth_core",
                    "user_id": "default",
                    "category": "test",
                },
                distance=0.1,
            )
        ][:limit]


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    """Create a fully-populated application config for tests."""

    permissions = MorgothPermissions(
        version="1.0",
        last_updated_by="human",
        permissions=PermissionFlags(
            can_create_ephemeral_agents=True,
            can_create_persistent_agents=False,
            can_self_modify=False,
            can_store_secrets=False,
            can_pull_ollama_models=False,
            can_execute_code=True,
            can_write_files=True,
            can_send_notifications=True,
            can_access_internet=True,
            can_place_real_orders=False,
        ),
        evolvable_zone_paths=["tools/", "agents/", "data/", "tests/", "notifications/"],
        immutable_zone_paths=["core/", "api/", "memory/episodic.py", "memory/persistent.py", ".env"],
        notification_levels=NotificationLevels(INFO=["ui"], WARNING=["ui"], CRITICAL=["ui", "telegram"]),
        task_limits=TaskLimits(max_concurrent_agents=3, max_recurring_tasks=10),
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    return AppConfig(
        POSTGRES_URL="postgresql://user:pass@localhost:5432/morgoth",
        OLLAMA_BASE_URL="http://localhost:11434",
        OLLAMA_PRIMARY_MODEL="deepseek-r1:14b-qwen-distill-q4_K_M",
        OLLAMA_AGENT_MODEL="llama3.1:8b",
        COINGECKO_API_KEY="",
        TELEGRAM_BOT_TOKEN="token",
        TELEGRAM_CHAT_ID="chat-id",
        SECRET_KEY="secret",
        MAX_CONCURRENT_AGENTS=3,
        LOG_RETENTION_DAYS=30,
        LOG_LEVEL_THOUGHT=True,
        root_dir=tmp_path,
        data_dir=tmp_path / "data",
        logs_dir=tmp_path / "data" / "logs",
        chroma_dir=tmp_path / "data" / "chroma_db",
        perms_path=tmp_path / "MORGOTH_PERMS.json",
        permissions=permissions,
    )


@pytest.fixture
def DummyResponseFixture() -> type[DummyResponse]:
    """Expose the dummy response class as a fixture."""

    return DummyResponse


@pytest.fixture
def DummyHTTPClientFixture() -> type[DummyHTTPClient]:
    """Expose the dummy HTTP client class as a fixture."""

    return DummyHTTPClient


@pytest.fixture
def DummyPersistentMemoryFixture() -> type[DummyPersistentMemory]:
    """Expose the dummy persistent memory class as a fixture."""

    return DummyPersistentMemory


@pytest.fixture
def DummyAgentManagerFixture() -> type[DummyAgentManager]:
    """Expose the dummy agent manager class as a fixture."""

    return DummyAgentManager


@pytest.fixture
def DummyNotifierFixture() -> type[DummyNotifier]:
    """Expose the dummy notifier class as a fixture."""

    return DummyNotifier


@pytest.fixture
def DummyEpisodicMemoryFixture() -> type[DummyEpisodicMemory]:
    """Expose the dummy episodic memory class as a fixture."""

    return DummyEpisodicMemory
