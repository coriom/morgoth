"""Main orchestration loop for Morgoth."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from agents.agent_manager import AgentManager
from api.ws.handler import OutboundWebSocketMessage, WebSocketManager
from core.config import AppConfig
from core.llm_client import ChatMessage, OllamaLLMClient
from core.scheduler import Scheduler, Task, TaskPriority, TaskType
from core.tool_router import ToolRouter
from memory.episodic import EpisodicMemory
from memory.persistent import PersistentMemory
from notifications.telegram import TelegramNotifier


class LogEntry(BaseModel):
    """Log entry contract for disk, DB, and UI streaming."""

    timestamp: str
    level: str
    agent: str
    content: str
    tokens_used: int | None = None
    duration_ms: int | None = None
    user_id: str = "default"


class BrainResponse(BaseModel):
    """Normalized response returned by the brain."""

    message: str
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    model: str


class Brain:
    """Main orchestration service for Phase 1."""

    def __init__(
        self,
        config: AppConfig,
        llm_client: OllamaLLMClient,
        persistent_memory: PersistentMemory,
        episodic_memory: EpisodicMemory,
        scheduler: Scheduler,
        tool_router: ToolRouter,
        agent_manager: AgentManager,
        notifier: TelegramNotifier,
        websocket_manager: WebSocketManager | None = None,
    ) -> None:
        """Initialize the brain service and dependencies."""

        self._config = config
        self._llm_client = llm_client
        self._persistent_memory = persistent_memory
        self._episodic_memory = episodic_memory
        self._scheduler = scheduler
        self._tool_router = tool_router
        self._agent_manager = agent_manager
        self._notifier = notifier
        self._websocket_manager = websocket_manager
        self._ready = False
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def initialize(self) -> dict[str, Any]:
        """Run startup checks and initialize runtime dependencies."""

        if self._ready:
            return {"status": "READY"}

        await self._persistent_memory.initialize()
        await self._episodic_memory.initialize()
        await self._scheduler.initialize()
        awakening = await self.awaken()
        self._ready = awakening["status"] == "READY"
        return awakening

    async def awaken(self) -> dict[str, Any]:
        """Execute the AWAKENING protocol from the specification."""

        missing: list[str] = []
        ollama_ok = await self._llm_client.health_check()
        if not ollama_ok:
            missing.append("Ollama unreachable")
            model_status = {
                self._config.ollama_primary_model: False,
                self._config.ollama_agent_model: False,
            }
        else:
            model_status = await self._llm_client.ensure_models_available(
                [self._config.ollama_primary_model, self._config.ollama_agent_model]
            )
        for model_name, available in model_status.items():
            if not available:
                missing.append(f"Missing Ollama model: {model_name}")

        tool_results = await self.test_tools()
        if not all(result["success"] for result in tool_results.values()):
            missing.append("One or more Layer 1 tools failed self-test")

        await self.ensure_recurring_tasks()
        status = "READY" if not missing else "MISSING_DEPENDENCIES"
        await self.log(
            "SYSTEM",
            "morgoth_core",
            f"Awakening completed with status {status}",
            user_id="default",
        )
        return {"status": status, "missing": missing, "tool_results": tool_results, "models": model_status}

    async def test_tools(self) -> dict[str, dict[str, Any]]:
        """Run one lightweight self-test for each registered Layer 1 tool."""

        tests = {
            "web_search": {"query": "Morgoth system", "max_results": 1},
            "execute_python": {"code": "print('ok')", "timeout_seconds": 5},
            "read_file": {"path": "SPEC.md"},
            "write_file": {"path": "data/tool_test.txt", "content": "ok"},
            "get_crypto_price": {"symbol": "bitcoin"},
            "get_crypto_history": {"symbol": "bitcoin", "days": 1},
            "get_news": {"topic": "general", "limit": 1},
            "create_agent": {"name": "self_test_agent", "task": "reply with ok", "agent_type": "ephemeral"},
            "notify": {"level": "INFO", "content": "Phase 1 self-test"},
            "remember": {"collection": "decisions", "content": "tool self test", "category": "self_test"},
            "recall": {"collection": "decisions", "query": "self test", "limit": 1},
        }
        results: dict[str, dict[str, Any]] = {}
        for name, payload in tests.items():
            try:
                results[name] = await self._tool_router.execute_tool(name, payload)
            except Exception as exc:
                results[name] = {"success": False, "result": None, "error": str(exc), "metadata": {}}
        return results

    async def ensure_recurring_tasks(self) -> None:
        """Ensure at least one recurring task exists."""

        existing = await self._scheduler.list_tasks()
        recurring = [row for row in existing if row["type"] == TaskType.RECURRING.value]
        if recurring:
            return
        task = Task(
            type=TaskType.RECURRING,
            priority=TaskPriority.BACKGROUND,
            description="Monitor BTC price every day",
            created_by="morgoth",
            recurrence_cron="0 8 * * *",
        )
        await self._scheduler.schedule(task)

    async def enqueue_message(self, content: str, user_id: str = "default") -> None:
        """Queue an incoming chat message for asynchronous processing."""

        await self._message_queue.put({"content": content, "user_id": user_id})

    async def run(self) -> None:
        """Run the main orchestration loop."""

        await self.initialize()
        while True:
            if not self._message_queue.empty():
                message = await self._message_queue.get()
                response = await self.process_message(message["content"], message["user_id"])
                await self.broadcast("result", response.message, metadata={"tool_results": response.tool_results})

            task = await self._scheduler.get_next_task()
            if task is not None:
                await self.dispatch_task(task)

            await asyncio.sleep(0.1)

    async def process_message(self, content: str, user_id: str = "default") -> BrainResponse:
        """Process a user chat message and return the assistant response."""

        await self._episodic_memory.add_text(
            "conversations",
            content,
            category="chat_user",
            agent_id="human",
            user_id=user_id,
        )

        messages = [ChatMessage(role="user", content=content)]
        response = await self._llm_client.chat(messages, tools=self._tool_router.get_schemas())
        tool_results: list[dict[str, Any]] = []

        if response.message.tool_calls:
            for tool_call in response.message.tool_calls:
                tool_result = await self._tool_router.execute_tool(tool_call.function.name, tool_call.function.arguments)
                tool_results.append({"tool": tool_call.function.name, "result": tool_result})
                messages.append(ChatMessage(role="assistant", content=response.message.content, tool_calls=response.message.tool_calls))
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=json.dumps(tool_result),
                        name=tool_call.function.name,
                        tool_call_id=tool_call.id,
                    )
                )
            response = await self._llm_client.chat(messages)

        message = response.message.content or ""
        await self._episodic_memory.add_text(
            "conversations",
            message,
            category="chat_assistant",
            agent_id="morgoth_core",
            user_id=user_id,
        )
        await self.log("RESULT", "morgoth_core", message, user_id=user_id, tokens_used=response.eval_count)
        return BrainResponse(message=message, tool_results=tool_results, model=response.model)

    async def dispatch_task(self, task: Task) -> None:
        """Dispatch a scheduled task to the agent manager."""

        await self.log("ACTION", "morgoth_core", f"Dispatching task {task.task_id}", user_id=task.user_id)
        agent = await self._agent_manager.create(
            name=f"task_{task.task_id}",
            task=task.description,
            agent_type="ephemeral",
            tools=[],
            user_id=task.user_id,
        )
        completed = await self._scheduler.mark_complete(task, {"agent_id": agent["agent_id"]})
        await self.broadcast("agent_update", f"Task {completed.task_id} dispatched", agent_id=agent["agent_id"])

    async def log(
        self,
        level: str,
        agent: str,
        content: str,
        *,
        user_id: str,
        tokens_used: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Write a log entry to disk, PostgreSQL, and the UI stream."""

        entry = LogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            level=level,
            agent=agent,
            content=content,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            user_id=user_id,
        )
        await self._write_log_file(entry)
        await self._persistent_memory.insert_log(entry.model_dump())
        if level != "THOUGHT" or self._config.log_level_thought:
            await self.broadcast(level.lower(), content, agent_id=agent, metadata=entry.model_dump())

    async def broadcast(
        self,
        message_type: str,
        content: str,
        *,
        agent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Broadcast a WebSocket message if a manager is configured."""

        if self._websocket_manager is None:
            return
        message = OutboundWebSocketMessage(
            type=message_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            agent_id=agent_id,
            content=content,
            metadata=metadata or {},
        )
        await self._websocket_manager.broadcast(message)

    async def get_status(self) -> dict[str, Any]:
        """Return a compact brain status payload."""

        return {
            "ready": self._ready,
            "primary_model": self._config.ollama_primary_model,
            "agent_model": self._config.ollama_agent_model,
            "max_concurrent_agents": self._config.max_concurrent_agents,
        }

    async def get_logs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent log entries from PostgreSQL."""

        return await self._persistent_memory.list_logs(limit=limit)

    async def get_tasks(self) -> list[dict[str, Any]]:
        """Return all scheduled tasks."""

        return await self._scheduler.list_tasks()

    async def write_exploration_report(self) -> Path:
        """Write a minimal exploration report required by the bootstrap protocol."""

        path = self._config.data_dir / "exploration_report.md"
        content = f"# Exploration Report\n\nGenerated on {date.today().isoformat()}\n"
        await asyncio.to_thread(path.write_text, content, "utf-8")
        return path

    async def shutdown(self) -> None:
        """Close all managed resources."""

        await self._tool_router.close()
        await self._notifier.close()
        await self._llm_client.close()
        await self._persistent_memory.close()

    async def _write_log_file(self, entry: LogEntry) -> None:
        """Append a log entry to the daily JSONL file."""

        log_path = self._config.logs_dir / f"morgoth_{date.today().isoformat()}.log"
        payload = json.dumps(entry.model_dump()) + "\n"

        def _append() -> None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(payload)

        await asyncio.to_thread(_append)
