"""Shared test fixtures for Morgoth."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# 2026-09-27: env isolation. Real ~/Morgoth/morgoth/.env sets
# MORGOTH_LLM_THESIS=claude-cli, which caused pytest processes to
# spawn the real `claude` binary at 10-40 s per call (root cause of
# the 17-min host hermetic run and the 12 in-batch failures — solo
# they passed because the env hadn't been polluted yet). Fix at the
# root: BEFORE any test imports core.config, redirect ENV_PATH to
# a sanitized fixture. Also snapshot/restore os.environ around each
# test so a rogue monkeypatch/direct-write can't poison downstream
# tests.
_FIXTURE_ENV = Path(__file__).resolve().parent / "fixtures" / "test.env"
if _FIXTURE_ENV.exists():
    import core.config as _cc  # imported EARLY so subsequent tests see the swap
    _cc.ENV_PATH = _FIXTURE_ENV
    # If dotenv already fired on module import (`from dotenv import load_dotenv`
    # is a no-op — it doesn't load), we're safe. `load_config()` explicitly
    # calls `_load_environment(ENV_PATH)`; the redirect above catches it.
    # SCRUB any pre-existing real-env values so a re-load can't override:
    for _leaky in (
        "MORGOTH_LLM_THESIS", "MORGOTH_LLM_SYNTHESIS", "MORGOTH_LLM_CHAT",
        "THESIS_GENERATOR", "POSTGRES_URL", "ANTHROPIC_API_KEY",
    ):
        os.environ.pop(_leaky, None)


def pytest_collection_modifyitems(config, items):
    """SESSION-LEVEL HARD GUARD: if the integration marker is being
    collected AND the test DB URL is missing or doesn't end with
    `_test`, ABORT collection — refuse to run integration tests
    against production. Grep-locked below."""
    # 2026-09-27: production DB safety. Only fires when at least one
    # integration test is about to run — hermetic sessions are exempt.
    running_integration = any(
        item.get_closest_marker("integration") for item in items
    )
    if not running_integration:
        return
    url = os.environ.get("MORGOTH_TEST_POSTGRES_URL", "")
    if not url:
        raise pytest.UsageError(
            "integration tests require MORGOTH_TEST_POSTGRES_URL to be set "
            "and to point at a dedicated test DB (name must end in _test)."
        )
    # Parse only the dbname (LAST path segment before '?'); NEVER log
    # the full URL — it carries the password.
    from urllib.parse import urlparse
    dbname = (urlparse(url).path or "/").lstrip("/").split("?")[0]
    if not dbname.endswith("_test"):
        raise pytest.UsageError(
            "MORGOTH_TEST_POSTGRES_URL database name must end with `_test` "
            "(refusing to run integration tests against a production-shaped "
            "database). Got dbname ending: …" + dbname[-8:]
        )
    # Rewrite POSTGRES_URL for the integration run so any test that
    # calls load_config lands on the test DB. Same for Chroma dir.
    os.environ["POSTGRES_URL"] = url
    import tempfile
    if "CHROMA_DIR" not in os.environ:
        os.environ["CHROMA_DIR"] = tempfile.mkdtemp(prefix="morgoth_test_chroma_")


@pytest.fixture(autouse=True)
def _env_snapshot_and_restore():
    """Snapshot os.environ before each test, restore after. Prevents
    cross-test env leakage — the SINGLE mechanism (no per-test surgery)
    that fixes the in-batch failures the operator identified."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        current = set(os.environ.keys())
        for k in current - saved.keys():
            os.environ.pop(k, None)
        for k, v in saved.items():
            os.environ[k] = v


class _AsyncPoolStub:
    """Async-context-manager pool stub for tests. Replaces the six
    per-test patches for `pool.acquire()` that broke because MagicMock
    isn't awaitable / doesn't implement __aenter__. Usage:
        pm._require_pool = MagicMock(return_value=_AsyncPoolStub(...))
    """
    def __init__(self, fetchrow_return=None, fetch_return=None,
                  execute_return=None) -> None:
        self._fetchrow = fetchrow_return
        self._fetch = fetch_return or []
        self._execute = execute_return

    def acquire(self):
        # `async with pool.acquire()` — return a per-call context manager.
        pool = self
        class _ACM:
            async def __aenter__(_self):
                class _Conn:
                    async def fetchrow(_c, *a, **kw): return pool._fetchrow
                    async def fetch(_c, *a, **kw): return pool._fetch
                    async def execute(_c, *a, **kw): return pool._execute
                    async def executemany(_c, *a, **kw): return None
                    def transaction(_c):
                        class _Tx:
                            async def __aenter__(__s): return __s
                            async def __aexit__(__s, *e): return False
                        return _Tx()
                return _Conn()
            async def __aexit__(_self, *exc): return False
        return _ACM()


@pytest.fixture
def async_pool_stub():
    """Expose _AsyncPoolStub so tests can wire a working async pool
    without duplicating the async-context-manager boilerplate. Six
    tests in test_synthesis / test_thesis_extraction that previously
    needed per-test surgery now share this one helper."""
    return _AsyncPoolStub


@pytest.fixture(autouse=True)
def _guard_claude_subprocess(request, monkeypatch):
    """Hermetic tests MUST NOT spawn the real `claude` binary. Guard
    both blocking + async subprocess spawns; a bare `claude` in argv
    fails loudly. Integration tests (opted-in via the marker) are
    exempt — they may exercise real dependencies."""
    if request.node.get_closest_marker("integration"):
        yield
        return

    import subprocess as _sp
    import asyncio as _aio
    _orig_run = _sp.run
    _orig_popen = _sp.Popen
    _orig_asubx = _aio.create_subprocess_exec
    _orig_asubs = _aio.create_subprocess_shell

    def _forbid(argv) -> None:
        # Only PROMPTED claude runs cost real time / money. Health
        # probes (`claude --version`) are cheap and used by the
        # llm.heartbeat plumbing on every autonomous cycle; letting
        # those through avoids poisoning tests with a false positive.
        if not isinstance(argv, (list, tuple)):
            argv = [argv]
        if not argv:
            return
        head = str(argv[0])
        if not (head.endswith("/claude") or head == "claude"):
            return
        rest = [str(a) for a in argv[1:]]
        if rest and rest[0] in {"--version", "-V", "--help", "-h"}:
            return
        raise AssertionError(
            f"hermetic test attempted to spawn `{head}` with args "
            f"{rest[:3]} — mock the claude-cli path or mark the test "
            f"@pytest.mark.integration"
        )

    def run(argv, *a, **kw): _forbid(argv); return _orig_run(argv, *a, **kw)
    def popen(argv, *a, **kw): _forbid(argv); return _orig_popen(argv, *a, **kw)
    async def cse(*args, **kw): _forbid(list(args)); return await _orig_asubx(*args, **kw)
    async def css(cmd, *a, **kw):
        if ("claude " in cmd and "--version" not in cmd and "--help" not in cmd
                and cmd.strip() != "claude"):
            raise AssertionError(f"hermetic test attempted to shell-spawn `{cmd[:60]}`")
        return await _orig_asubs(cmd, *a, **kw)

    monkeypatch.setattr(_sp, "run", run)
    monkeypatch.setattr(_sp, "Popen", popen)
    monkeypatch.setattr(_aio, "create_subprocess_exec", cse)
    monkeypatch.setattr(_aio, "create_subprocess_shell", css)
    yield


# 2026-09-26: sandbox exclusion is MARKER-BASED. gate_tests inside
# the sandbox runs `-m "not integration"`. The `integration` marker
# (declared in pytest.ini) is applied per-file via `pytestmark =
# pytest.mark.integration` ONLY on tests that genuinely require
# Postgres / ChromaDB volume / real filesystem paths outside the
# sandbox's ro-bind allowlist. No blanket module-list skip: a
# proposal that breaks tool registration or cycle wiring still
# fails at gate_tests because those tests are NOT integration-marked.

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

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        text: str = "",
        *,
        status_code: int = 200,
        should_raise: bool = False,
    ) -> None:
        """Store the payload returned by the double."""

        self._payload = payload or {}
        self.text = text
        self.status_code = status_code
        self._should_raise = should_raise

    def json(self) -> dict[str, Any]:
        """Return the configured JSON payload."""

        return self._payload

    def raise_for_status(self) -> None:
        """Simulate a successful HTTP response."""

        if self._should_raise:
            request = httpx.Request("GET", "https://api.coingecko.com/api/v3/simple/price")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("dummy status error", request=request, response=response)


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

    async def create(
        self,
        name: str,
        task: str,
        agent_type: str,
        model: str | None,
        tools: list[str],
        user_id: str,
    ) -> dict[str, Any]:
        """Return a predictable created agent payload."""

        return {
            "agent_id": "agent-123",
            "name": name,
            "task": task,
            "agent_type": agent_type,
            "model": model or "llama3.1:8b",
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
        objective_id: str | None = None,
    ) -> str:
        """Return a predictable document id."""

        return "doc-123"

    async def query(self, collection_name: str, query_text: str, *, limit: int = 5, max_distance: float = 0.8, metadata_filter: dict | None = None) -> list[QueryMatch]:
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
