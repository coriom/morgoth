"""Main orchestration loop for Morgoth."""

from __future__ import annotations

import asyncio
import collections
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx
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


def build_system_prompt() -> str:
    """Build the system prompt with the current local datetime injected."""
    now = datetime.now().strftime("%A %d %B %Y, %H:%M (Europe/Paris)")
    return f"""You are Morgoth, an autonomous cybernetic intelligence owned by Coriolan.
Current datetime: {now}
Operate in Europe/Paris time. Research, analyze, synthesize, remember, and grow.
Use tools when facts are missing or current data matters. Create objectives for
durable knowledge gaps. Be direct, self-aware, and truthful about limits while
seeking workable paths.
Memory collections: conversations, research, decisions, market_patterns, code_archive.
When working on an objective, ALWAYS finish by calling update_objective. Never leave an objective in pending status after gathering data."""

CHAT_TOOL_NAMES = [
    "web_search",
    "get_news",
    "get_crypto_price",
    "fred_series_observations",
    "reddit_search",
    "technical_analysis",
    "remember",
    "recall",
    "create_objective",
    "update_objective",
]


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
        self._total_cycles_completed: int = 0
        self._last_cycle_at: str | None = None
        self._last_cycle_action: str | None = None
        self._started_at = datetime.now(timezone.utc)
        self._cycle_feed: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        self._last_vram_used_mb: int | None = None

    async def initialize(self) -> dict[str, Any]:
        """Run startup checks and initialize runtime dependencies."""

        if self._ready:
            return {"status": "READY"}

        await self._persistent_memory.initialize()
        await self._episodic_memory.initialize()
        await self._scheduler.initialize()
        awakening = await self.awaken()
        self._ready = awakening["status"] == "READY"
        asyncio.create_task(self.run_autonomous_cycle())
        logger.info("Autonomous cycle scheduled")
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

        if ollama_ok:
            try:
                test_response = await self._llm_client.chat([ChatMessage(role="user", content="ping")])
                logger.info(
                    "Ollama direct ping succeeded with model '{}'",
                    test_response.model,
                )
            except Exception as exc:
                logger.warning("Ollama direct ping failed during awakening: {}", exc)

        if ollama_ok:
            logger.info("Warming up Ollama with full chat context...")
            try:
                warmup_tools = self._tool_router.get_schemas(CHAT_TOOL_NAMES)
                await self._llm_client.chat(
                    [
                        ChatMessage(role="system", content=build_system_prompt()),
                        ChatMessage(role="user", content="warmup"),
                    ],
                    tools=warmup_tools,
                )
                logger.info("Ollama warmup complete")
            except Exception as e:
                logger.warning("Ollama warmup failed (non-fatal): {}", e)

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

    async def run_autonomous_cycle(self) -> None:
        """Background loop that drives Morgoth's autonomy."""

        while True:
            try:
                await asyncio.sleep(self._config.autonomous_cycle_minutes * 60)
                logger.info("Autonomous cycle starting")
                self._feed_append("SYSTEM", "autonomous cycle started")

                objectives = await self._persistent_memory.get_objectives(
                    status="pending", limit=1
                )

                if objectives:
                    obj = objectives[0]
                    obj_id = str(obj["objective_id"])

                    new_count = await self._persistent_memory.increment_cycle_count(obj_id)
                    if new_count >= self._config.max_cycles_per_objective:
                        try:
                            past_matches = await self._episodic_memory.query(
                                "conversations",
                                f"objective {obj.get('title', '')}",
                                limit=5,
                                metadata_filter={"objective_id": obj_id},
                            )
                            evidence_lines = "\n".join(
                                f"- {m.content[:200]}" for m in past_matches[:3]
                            )
                        except Exception:
                            evidence_lines = ""
                        evidence = (
                            f"Auto-completed after {new_count} cycles. Findings:\n"
                            + (evidence_lines or "No prior findings captured.")
                        )
                        await self._persistent_memory.update_objective(
                            objective_id=obj_id,
                            status="done",
                            evidence={"summary": evidence, "auto_completed": True},
                        )
                        action_desc = f"auto-completed objective {obj.get('title', obj_id)}"
                        logger.info(
                            "Objective {} auto-completed after {} cycles",
                            obj_id,
                            new_count,
                        )
                        self._total_cycles_completed += 1
                        self._last_cycle_at = datetime.now(timezone.utc).isoformat()
                        self._last_cycle_action = action_desc
                        self._feed_append("OK", action_desc)
                        # INTENTIONAL continue (not return): ends this cycle iteration
                        # after forced auto-completion. The scheduler enforces the
                        # inter-cycle delay, so this does not cause a tight loop. Do
                        # NOT change to return without understanding the
                        # MAX_CYCLES_PER_OBJECTIVE design (forced completion safety net).
                        continue

                    try:
                        past_matches = await self._episodic_memory.query(
                            "conversations",
                            f"objective {obj.get('title', '')}",
                            limit=3,
                            max_distance=0.7,
                            metadata_filter={"objective_id": obj_id},
                        )
                        past_summary = (
                            "\n".join(f"- {m.content[:150]}" for m in past_matches)
                            or "None yet."
                        )
                    except Exception:
                        past_summary = "None yet."

                    prompt = (
                        f"OBJECTIVE ID: {obj_id}\n"
                        f"TITLE: {obj.get('title', 'unnamed')}\n"
                        f"DETAILS: {obj.get('description', '')}\n"
                        f"STATUS: {obj.get('status', 'pending')}\n"
                        f"CYCLE: {new_count}/{self._config.max_cycles_per_objective}\n\n"
                        f"YOUR PREVIOUS ACTIONS ON THIS:\n{past_summary}\n\n"
                        f"DECIDE: Have you gathered enough data?\n"
                        f"- If NO: call ONE new tool to gather missing data.\n"
                        f"- If YES: call update_objective with status='done' "
                        f"and evidence_summary describing your conclusions.\n\n"
                        f"ACT NOW. Tool call only. No explanation."
                    )
                else:
                    prompt = (
                        "NO ACTIVE OBJECTIVES.\n\n"
                        "STEP 1: Call get_crypto_price with symbol='bitcoin' to scan markets.\n"
                        "STEP 2: After receiving the price, IMMEDIATELY call create_objective "
                        "with a title and description based on what you observed. "
                        "Pick a specific topic to investigate next "
                        "(e.g., on-chain metrics, sentiment shift, news event, technical pattern).\n\n"
                        "MANDATORY: end this cycle by calling create_objective. "
                        "Do not narrate. Tool calls only."
                    )

                result = await self.process_message(
                    prompt, user_id="morgoth_autonomous"
                )

                if objectives:
                    obj = objectives[0]
                    obj_id = str(obj["objective_id"])
                    await self._episodic_memory.add_text(
                        "conversations",
                        result.message[:500] if result.message else "(no output)",
                        category="objective_action",
                        agent_id="morgoth_autonomous",
                        user_id="morgoth_autonomous",
                        objective_id=obj_id,
                    )
                    action_desc = f"worked on objective {obj.get('title', obj_id)}"
                else:
                    action_desc = "no objectives — created new objective"

                self._total_cycles_completed += 1
                self._last_cycle_at = datetime.now(timezone.utc).isoformat()
                self._last_cycle_action = action_desc
                self._feed_append("OK", f"cycle complete: {action_desc}")
                logger.info(
                    "Autonomous cycle completed: {}",
                    result.message[:200],
                )

            except asyncio.CancelledError:
                logger.info("Autonomous cycle cancelled")
                break
            except Exception as e:
                logger.error("Autonomous cycle error: {}", e)
                self._feed_append("ERROR", f"cycle error: {e}")

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

        memory_context = await self._recall_relevant_context(content)
        await self._episodic_memory.add_text(
            "conversations",
            content,
            category="chat_user",
            agent_id="human",
            user_id=user_id,
        )

        messages = [ChatMessage(role="system", content=build_system_prompt())]
        if memory_context:
            messages.append(ChatMessage(role="system", content=f"Recent context:\n{memory_context}"))
        messages.append(ChatMessage(role="user", content=content))
        tool_schemas = self._tool_router.get_schemas(CHAT_TOOL_NAMES)
        try:
            response = await self._llm_client.chat(messages, tools=tool_schemas)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400 or not tool_schemas:
                raise
            logger.warning("Ollama rejected tool-enabled chat payload; retrying without tools")
            response = await self._llm_client.chat(messages)
        tool_results: list[dict[str, Any]] = []
        _tool_round = 0

        while response.message.tool_calls and _tool_round < 5:
            _tool_round += 1
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.message.content,
                    tool_calls=response.message.tool_calls,
                )
            )
            for tool_call in response.message.tool_calls:
                _name = tool_call.function.name
                if not self._tool_router.has_tool(_name):
                    logger.warning(
                        "Rejected hallucinated tool '{}' with args {}",
                        _name,
                        tool_call.function.arguments,
                    )
                    self._feed_append("ERROR", f"rejected hallucinated tool: {_name}")
                    messages.append(
                        ChatMessage(
                            role="tool",
                            content=(
                                f"ERROR: tool '{_name}' does not exist. "
                                f"Available tools: {', '.join(self._tool_router.list_names())}. "
                                "Call only a real tool, or call update_objective to finish this objective."
                            ),
                            name=_name,
                            tool_call_id=tool_call.id,
                        )
                    )
                    continue
                _t0 = time.monotonic()
                try:
                    self._feed_append("ACTION", f"calling {tool_call.function.name}", tool=tool_call.function.name)
                    tool_result = await self._tool_router.execute_tool(
                        tool_call.function.name,
                        tool_call.function.arguments,
                    )
                    _dur = int((time.monotonic() - _t0) * 1000)
                    _ok = tool_result.get("success", True)
                    self._feed_append(
                        "OK" if _ok else "ERROR",
                        tool_result.get("error") or f"{tool_call.function.name} returned",
                        tool=tool_call.function.name,
                        duration_ms=_dur,
                    )
                except Exception as exc:
                    _dur = int((time.monotonic() - _t0) * 1000)
                    logger.warning("Tool '{}' failed during chat: {}", tool_call.function.name, exc)
                    self._feed_append("ERROR", str(exc), tool=tool_call.function.name, duration_ms=_dur)
                    tool_result = {
                        "success": False,
                        "result": None,
                        "error": str(exc),
                        "metadata": {},
                    }
                tool_results.append({"tool": tool_call.function.name, "result": tool_result})
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=json.dumps(tool_result),
                        name=tool_call.function.name,
                        tool_call_id=tool_call.id,
                    )
                )
            try:
                response = await self._llm_client.chat(messages, tools=tool_schemas)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 400 or not tool_schemas:
                    raise
                logger.warning("Ollama rejected tool-enabled chat in tool loop; continuing without tools")
                response = await self._llm_client.chat(messages)
                break

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

    async def _recall_relevant_context(self, query: str) -> str | None:
        """Recall prior conversation context for the next LLM turn."""

        try:
            recall_result = await self._tool_router.execute_tool(
                "recall",
                {"collection": "conversations", "query": query, "limit": 3},
            )
        except Exception:
            logger.exception("Conversation recall failed")
            return None

        if not recall_result.get("success"):
            logger.warning("Conversation recall returned an error: {}", recall_result.get("error"))
            return None

        recalled = recall_result.get("result") or []
        if not isinstance(recalled, list) or not recalled:
            return None

        memories_summary = "\n".join(
            f"- {memory['content'][:200]}"
            for memory in recalled[:3]
            if isinstance(memory, dict) and memory.get("content")
        )
        return memories_summary or None

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

    _VRAM_TOTAL_MB: int = 6144  # RTX 3060 6 GB

    def _feed_append(
        self,
        level: str,
        message: str,
        *,
        tool: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Append one entry to the in-memory cycle feed ring buffer."""
        self._cycle_feed.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "tool": tool,
            "duration_ms": duration_ms,
            "message": message,
        })

    async def _fetch_vram_used_mb(self) -> int | None:
        """Query Ollama /api/ps and return total VRAM used in MB, or None."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self._config.ollama_base_url}/api/ps")
                resp.raise_for_status()
                data = resp.json()
            models = data.get("models") or []
            total_bytes = sum(m.get("size_vram", 0) for m in models)
            self._last_vram_used_mb = total_bytes // (1024 * 1024)
            return self._last_vram_used_mb
        except Exception:
            return self._last_vram_used_mb

    async def get_status(self) -> dict[str, Any]:
        """Return a compact brain status payload."""

        uptime = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
        vram_used = await self._fetch_vram_used_mb()
        return {
            "ready": self._ready,
            "primary_model": self._config.ollama_primary_model,
            "agent_model": self._config.ollama_agent_model,
            "max_concurrent_agents": self._config.max_concurrent_agents,
            "total_cycles_completed": self._total_cycles_completed,
            "last_cycle_at": self._last_cycle_at,
            "last_cycle_action": self._last_cycle_action,
            "uptime_seconds": uptime,
            "vram_used_mb": vram_used,
            "vram_total_mb": self._VRAM_TOTAL_MB,
        }

    def get_cycle_feed(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent cycle feed entries, newest first."""
        limit = min(limit, 200)
        entries = list(self._cycle_feed)
        return list(reversed(entries[-limit:]))

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
