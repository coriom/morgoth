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
from core.contradictions import (
    CONTRADICTION_WINDOW_HOURS,
    claims_oppose,
    group_theses_by_subject,
    subjects_timeframe_conflict,
    window_for,
)
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

MIN_DISTINCT_SOURCES: int = 3

# Objectives older than this transition to ``stale_timeout`` at
# Brain.initialize(). Rationale: MAX_CYCLES bounds an objective's
# ACTIVE lifetime to well under an hour of cycling, so a
# non-terminal row days old is by construction abandoned (either
# the selector never re-visits ``in_progress`` rows — it filters on
# ``pending`` only — or a process restart lost the in-flight id).
# Resuming decades-stale market context would be worse than
# terminating it (markets moved; the evidence is dated), so the
# timeout is the right shape for BOTH orphaning modes.
# Env-overridable via ``OBJECTIVE_STALE_DAYS`` (float; invalid ->
# warn + default).
OBJECTIVE_STALE_DAYS: float = 7.0


def _resolve_stale_days() -> float:
    """Env override → validated float; silent-fallback on parse error."""
    import os
    raw = os.environ.get("OBJECTIVE_STALE_DAYS")
    if raw is None:
        return OBJECTIVE_STALE_DAYS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "OBJECTIVE_STALE_DAYS={!r} not a float; using default", raw,
        )
        return OBJECTIVE_STALE_DAYS
    if value <= 0:
        logger.warning(
            "OBJECTIVE_STALE_DAYS={!r} must be > 0; using default", raw,
        )
        return OBJECTIVE_STALE_DAYS
    return value

# Non-data_feeds source-classified tools: they don't live under
# tools/data_feeds/ so auto-discovery doesn't find them, but they ARE
# valid sources in the multi-source rail. Kept as an explicit constant.
_STATIC_DATA_SOURCES: frozenset[str] = frozenset({
    "web_search",
    # FRED joins the source rail (07-25). Live-verified against CPIAUCSL
    # before the join — the reddit_search precedent (source rail slot
    # burned for months on a dead API) is the reason for that gate.
    "fred_series_observations",
})

# Non-data_feeds chat-schema tools: the LLM sees these on every turn.
# Same rationale as above — they aren't discovered because they don't
# live under tools/data_feeds/.
_STATIC_CHAT_TOOL_NAMES: tuple[str, ...] = (
    "web_search",
    "fred_series_observations",
    # Symbol discovery for the FRED source: observations without search
    # forces the model to guess series IDs.
    "fred_series_search",
    "technical_analysis",
    "remember",
    "recall",
    "create_objective",
    "update_objective",
)


def _compute_tool_sets() -> tuple[frozenset[str], list[str]]:
    """Merge STATIC_* with tools discovered under tools/data_feeds/.

    Computed once at module import time. A new file under
    tools/data_feeds/ that declares ``is_data_source = True`` joins the
    source rail on the next process start; ``is_chat_tool = True``
    (the BaseTool default) puts it in the chat schema.
    """
    from tools.discovery import discover_data_feed_tools

    discovered = discover_data_feed_tools()
    data_sources = _STATIC_DATA_SOURCES | frozenset(
        cls.name for cls in discovered if cls.is_data_source
    )
    chat_names = list(_STATIC_CHAT_TOOL_NAMES) + [
        cls.name for cls in discovered if cls.is_chat_tool
    ]
    return data_sources, chat_names


DATA_SOURCE_TOOLS, CHAT_TOOL_NAMES = _compute_tool_sets()


def _looks_like_unemitted_tool_intent(text: str | None) -> bool:
    """Return True when terminal-turn text announces a tool action without emitting a tool_call.

    Narrow detector: triggers when text either (a) explicitly references update_objective,
    or (b) uses an intent phrase ("I will call", "I'll use", "calling ", ...) combined
    with the name of a registered chat tool. Pure analytical answers with no tool-related
    vocabulary do not match.
    """

    if not text:
        return False
    lower = text.lower()
    if "update_objective" in lower:
        return True
    call_phrases = (
        "i will call",
        "i'll call",
        "i will now call",
        "let me call",
        "i will use",
        "i'll use",
        "calling ",
    )
    if not any(phrase in lower for phrase in call_phrases):
        return False
    return any(name in lower for name in CHAT_TOOL_NAMES)


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
        self._current_objective_id: str | None = None

    async def initialize(self) -> dict[str, Any]:
        """Run startup checks and initialize runtime dependencies."""

        if self._ready:
            return {"status": "READY"}

        if MIN_DISTINCT_SOURCES >= self._config.max_cycles_per_objective:
            logger.warning(
                "Multi-source rail unsatisfiable: MIN_DISTINCT_SOURCES={} >= "
                "MAX_CYCLES_PER_OBJECTIVE={}. The model has zero slack cycles "
                "to call update_objective after meeting the source minimum; "
                "MAX_CYCLES will force-complete every objective.",
                MIN_DISTINCT_SOURCES,
                self._config.max_cycles_per_objective,
            )

        await self._persistent_memory.initialize()
        # Stale-objective sweep: transition non-terminal rows older
        # than OBJECTIVE_STALE_DAYS (env-overridable) to
        # ``stale_timeout``. Non-blocking: any failure warns and
        # startup proceeds — losing the sweep costs a stuck row, not
        # the whole brain init. See OBJECTIVE_STALE_DAYS docstring
        # for the orphaning modes this covers.
        try:
            stale_days = _resolve_stale_days()
            terminated = await self._persistent_memory.timeout_stale_objectives(
                stale_days,
            )
            if terminated:
                now = datetime.now(timezone.utc)
                for row in terminated:
                    created = row.get("created_at")
                    if isinstance(created, datetime):
                        age_days = (now - created).total_seconds() / 86400.0
                        age_repr = f"{age_days:.1f}d"
                    else:
                        age_repr = "unknown"
                    logger.info(
                        "stale objective terminated: id={} title={!r} "
                        "age={} cycle_count={}",
                        str(row.get("objective_id", ""))[:8],
                        (row.get("title") or "")[:80],
                        age_repr,
                        row.get("cycle_count", 0),
                    )
            else:
                logger.info(
                    "stale-objective sweep: no rows older than {} days",
                    stale_days,
                )
        except Exception as exc:
            logger.warning(
                "stale-objective sweep failed (non-blocking): {}", exc,
            )
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
                                limit=10,
                                max_distance=2.0,
                                metadata_filter={"objective_id": obj_id},
                            )
                            findings = [m.content for m in past_matches if m.content]
                            evidence_lines = "\n".join(
                                f"- {m.content[:200]}" for m in past_matches[:3]
                            )
                        except Exception:
                            findings = []
                            evidence_lines = ""
                        evidence = (
                            f"Auto-completed after {new_count} cycles. Findings:\n"
                            + (evidence_lines or "No prior findings captured.")
                        )
                        try:
                            sources_used_done = await self._persistent_memory.get_sources_used(obj_id)
                        except Exception:
                            sources_used_done = []
                        synthesis_text = await self._synthesize_objective(
                            obj, sources_used_done, findings
                        )
                        await self._persistent_memory.update_objective(
                            objective_id=obj_id,
                            status="done",
                            evidence={"summary": evidence, "auto_completed": True},
                        )
                        if synthesis_text is not None:
                            try:
                                await self._persistent_memory.update_objective(
                                    objective_id=obj_id,
                                    evidence={
                                        "type": "synthesis",
                                        "content": synthesis_text,
                                        "sources": sorted(set(sources_used_done)),
                                    },
                                )
                            except Exception as exc:
                                logger.warning(
                                    "Synthesis storage failed, objective already done: {}",
                                    exc,
                                )
                                self._feed_append(
                                    "ERROR",
                                    f"synthesis storage failed: {type(exc).__name__}",
                                )
                        # Thesis extraction: structured beliefs derived from the
                        # synthesis, persisted for future contradiction detection.
                        # Skipped when there is no synthesis (None/empty). The whole
                        # block is non-blocking — objective is already done above.
                        if synthesis_text:
                            try:
                                theses = await self._extract_theses(
                                    obj, synthesis_text, sources_used_done
                                )
                                for t in theses:
                                    await self._persistent_memory.add_thesis(
                                        subject=t["subject"],
                                        claim=t["claim"],
                                        confidence=t.get("confidence", "medium"),
                                        evidence=t.get("evidence", []),
                                        objective_id=obj_id,
                                    )
                                if theses:
                                    self._feed_append(
                                        "OK",
                                        f"extracted {len(theses)} thesis/theses",
                                    )
                                else:
                                    self._feed_append(
                                        "INFO",
                                        "no testable theses extracted",
                                    )
                            except Exception as exc:
                                logger.warning(
                                    "Thesis extraction failed, objective already done: {}",
                                    exc,
                                )
                                self._feed_append(
                                    "ERROR",
                                    f"thesis extraction failed: {type(exc).__name__}",
                                )
                        # Contradiction detection: scan the active thesis set
                        # for newly-introduced opposed claims. Non-blocking — a
                        # detector failure must NOT prevent objective completion.
                        try:
                            contradictions = await self.detect_contradictions()
                            if contradictions:
                                self._feed_append(
                                    "WARN",
                                    f"detected {len(contradictions)} contradiction(s)",
                                )
                        except Exception as exc:
                            logger.warning(
                                "Contradiction detection failed, objective already done: {}",
                                exc,
                            )
                            self._feed_append(
                                "ERROR",
                                f"contradiction detection failed: {type(exc).__name__}",
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

                    try:
                        sources_used = await self._persistent_memory.get_sources_used(obj_id)
                    except Exception:
                        sources_used = []
                    _remaining = sorted(DATA_SOURCE_TOOLS - set(sources_used))
                    _source_count = len(set(sources_used))
                    _source_rail = (
                        f"DATA SOURCES USED: {', '.join(sources_used) or 'none'} "
                        f"({_source_count}/{MIN_DISTINCT_SOURCES} minimum).\n"
                        + (
                            f"You MUST gather from a DIFFERENT source not yet used. "
                            f"Unused sources: {', '.join(_remaining)}. Call ONE of them now.\n"
                            if _source_count < MIN_DISTINCT_SOURCES else
                            "Minimum sources met. You may call update_objective to finish, or gather more.\n"
                        )
                    )

                    prompt = (
                        f"OBJECTIVE ID: {obj_id}\n"
                        f"TITLE: {obj.get('title', 'unnamed')}\n"
                        f"DETAILS: {obj.get('description', '')}\n"
                        f"STATUS: {obj.get('status', 'pending')}\n"
                        f"CYCLE: {new_count}/{self._config.max_cycles_per_objective}\n\n"
                        f"YOUR PREVIOUS ACTIONS ON THIS:\n{past_summary}\n\n"
                        f"{_source_rail}"
                        f"DECIDE: Have you gathered enough data?\n"
                        f"- If NO: call ONE new tool to gather missing data.\n"
                        f"- If YES: call update_objective with status='done' "
                        f"and evidence_summary describing your conclusions.\n\n"
                        f"ACT NOW. Tool call only. No explanation."
                    )
                else:
                    # Knowledge-grounded generation. The context block
                    # renders recent titles (with a DIVERGE instruction),
                    # data-source tool usage (a 0-usage tool is
                    # unexplored territory), active thesis subjects,
                    # and open contradictions. Non-blocking: if the
                    # builder returns "" (empty DB or transient
                    # failure) we fall back to the historical
                    # STEP-1 price-scan bootstrap so a zero-knowledge
                    # Morgoth still generates.
                    from core.objective_gen_context import (
                        build_generation_context,
                    )
                    generation_ctx = await build_generation_context(
                        self._persistent_memory, self._config,
                    )
                    if generation_ctx:
                        prompt = (
                            f"{generation_ctx}"
                            "NO ACTIVE OBJECTIVES.\n\n"
                            "Pick ONE specific investigable topic "
                            "grounded in the state above:\n"
                            "- an unexplored data source's territory "
                            "(a 0-usage count signals unexplored ground),\n"
                            "- an open contradiction (a live research lead),\n"
                            "- or a thesis subject that needs deeper evidence.\n"
                            "DIVERGE from the recent titles above.\n\n"
                            "MANDATORY: end this cycle by calling create_objective. "
                            "Do not narrate. Tool calls only."
                        )
                    else:
                        # Bootstrap fallback — zero-knowledge state
                        # (empty DB) or builder failure. Kept
                        # byte-identical to the pre-context prompt so
                        # first-run behavior is preserved.
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
                    # Operator steering: append the active focus directive if
                    # one exists. Read at generation time (no cache) so a new
                    # directive takes effect on the NEXT cycle without a
                    # restart. Non-blocking: any DB error logs and falls
                    # through — the prompt then matches the no-directive
                    # baseline byte-for-byte.
                    try:
                        focus_row = await self._persistent_memory.get_active_focus()
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "focus directive read failed (non-blocking): {}", exc
                        )
                        focus_row = None
                    if focus_row and focus_row.get("directive"):
                        prompt += (
                            "\n\nOPERATOR FOCUS DIRECTIVE (steers topic choice only):\n"
                            f"{focus_row['directive']}\n"
                            "This directive influences WHICH subjects you "
                            "investigate. It does not change your identity, "
                            "constraints, methods, or permissions."
                        )

                self._current_objective_id = obj_id if objectives else None
                try:
                    result = await self.process_message(
                        prompt, user_id="morgoth_autonomous"
                    )
                finally:
                    self._current_objective_id = None

                if objectives:
                    obj = objectives[0]
                    obj_id = str(obj["objective_id"])
                    await self._episodic_memory.add_text(
                        "conversations",
                        self._format_cycle_finding(result)[:1500],
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

    def _format_cycle_finding(self, result: BrainResponse) -> str:
        """Combine model narrative and tool-result data into a stored finding.

        The autonomous-objective prompt instructs the model to emit tool calls
        only and "No explanation", so result.message is routinely empty after a
        successful tool call. Persisting only result.message therefore loses the
        actual fetched payload (price, news, search snippets) and downstream
        synthesis sees "(no output)". Including a compact tool-result digest
        keeps the data the cycle just fetched in the finding.
        """
        narrative = (result.message or "").strip()
        tool_lines: list[str] = []
        for tr in result.tool_results or []:
            name = tr.get("tool")
            inner = tr.get("result") or {}
            if inner.get("success"):
                payload_str = json.dumps(inner.get("result"), default=str)[:300]
                tool_lines.append(f"{name}: {payload_str}")
            else:
                tool_lines.append(f"{name} FAILED: {inner.get('error') or 'unknown'}")
        parts: list[str] = []
        # tool results first so they survive downstream 300-char truncation in
        # the synthesis prompt even when narrative also exists
        if tool_lines:
            parts.append("TOOL RESULTS:\n" + "\n".join(f"- {ln}" for ln in tool_lines))
        if narrative:
            parts.append(narrative)
        return "\n\n".join(parts) or "(no output)"

    async def _synthesize_objective(
        self,
        obj: dict[str, Any],
        sources_used: list[str],
        findings: list[str],
    ) -> str | None:
        """Produce a cross-source analysis at objective end. Returns None to skip.

        Distinct from a per-source summary: the prompt explicitly forbids
        summarizing each source separately and demands agreements, contradictions,
        and correlations BETWEEN sources. Inherits transient-error resilience by
        going through _chat_with_transient_retry. On any failure the function
        returns a short fallback string so completion still proceeds.
        """
        distinct = sorted(set(sources_used or []))
        if len(distinct) < 2:
            logger.info(
                "Synthesis skipped for objective {}: only {} distinct source(s)",
                obj.get("objective_id"),
                len(distinct),
            )
            self._feed_append(
                "INFO",
                f"synthesis skipped: only {len(distinct)} distinct source(s)",
            )
            return None

        findings_block = (
            "\n".join(f"- {(f or '').strip()[:300]}" for f in findings if f)
            or "(no findings captured)"
        )
        sources_block = ", ".join(distinct)
        prompt = (
            f"OBJECTIVE: {obj.get('title', 'unnamed')}\n"
            f"DETAILS: {obj.get('description', '')}\n\n"
            f"DISTINCT SOURCES CONSULTED: {sources_block}\n\n"
            f"FINDINGS GATHERED ACROSS CYCLES:\n{findings_block}\n\n"
            "Write the cross-source analysis ONLY. Identify agreements, "
            "contradictions, and correlations BETWEEN these sources. Do not "
            "summarize each source separately. State what the combination "
            "reveals that no single source shows. If sources conflict, name "
            "the conflict.\n\n"
            "OUTPUT RULES:\n"
            "- No identity preamble or self-reference (do not start with "
            "'I am Morgoth', 'I will', 'As an AI', etc.).\n"
            "- No tool-call narration. This is a written analysis, not a "
            "tool call: do NOT emit 'UPDATE_OBJECTIVE', status lines, or "
            "any 'I will now call X' phrasing.\n"
            "- Begin directly with the analysis."
        )
        messages = [
            ChatMessage(role="system", content=build_system_prompt()),
            ChatMessage(role="user", content=prompt),
        ]
        try:
            response = await self._chat_with_transient_retry(messages)
            text = (response.message.content or "").strip()
            return text or "(synthesis produced no content)"
        except Exception as exc:
            logger.warning(
                "Synthesis chat failed for objective {}: {}",
                obj.get("objective_id"),
                exc,
            )
            self._feed_append("ERROR", f"synthesis failed: {type(exc).__name__}: {exc}")
            return f"(synthesis failed: {type(exc).__name__})"

    async def _extract_theses(
        self,
        obj: dict[str, Any],
        synthesis_text: str,
        sources: list[str],
    ) -> list[dict[str, Any]]:
        """Extract testable directional theses from a synthesis. Returns possibly-empty list.

        One dedicated Ollama call via _chat_with_transient_retry (no tools) so it
        inherits the transient-retry resilience. The prompt instructs the model to
        emit a JSON array of {subject, claim, confidence, evidence}. Defensive
        parsing: any failure (chat error, malformed JSON, prose despite instructions)
        logs a warning and returns []. An empty list is a VALID result — a synthesis
        with no testable directional claim must not be coerced into inventing one.
        """
        if not synthesis_text or not synthesis_text.strip():
            return []
        prompt = (
            f"OBJECTIVE: {obj.get('title', 'unnamed')}\n"
            f"DISTINCT SOURCES: {', '.join(sources)}\n\n"
            f"SYNTHESIS:\n{synthesis_text}\n\n"
            "Extract testable directional beliefs from the synthesis as a JSON "
            "array. Each element MUST have:\n"
            '  "subject" (str): a normalized topic, e.g. "BTC short-term price"\n'
            '  "claim" (str): directional, e.g. "bearish" or "declining"; not vague\n'
            '  "confidence" (str): "low" | "medium" | "high"\n'
            '  "evidence" (array): [{"source": "<tool name>", "detail": "<excerpt>"}]\n\n'
            "RULES:\n"
            "- subject must be a SHORT normalized noun phrase — a topic "
            "(e.g. 'BTC short-term price', 'Ethereum hash rate'). NOT a full "
            "sentence, NOT a news headline, NOT a phrase naming a specific "
            "person or event. If the underlying topic cannot be reduced to "
            "a short noun phrase, do NOT emit a thesis about it.\n"
            "- claim MUST be a single directional trend or state that could "
            "be agreed with or contradicted (e.g. 'declining', 'bullish', "
            "'increasing', 'stable'). If you cannot state a directional "
            "claim, do NOT emit that thesis. Never emit claims like "
            "'unclear', 'mixed', 'unknown', 'complex', 'unrelated', "
            "'no correlation', 'inaccurate' — these are not testable; "
            "omit them.\n"
            "- Every thesis MUST have at least one evidence item drawn from "
            "the synthesis. If you cannot cite evidence for a claim, do NOT "
            "emit it.\n"
            "- evidence must come from the synthesis text; do not invent.\n"
            "- If the synthesis contains no testable directional claim, "
            "return [].\n"
            "EXAMPLES OF WHAT TO REJECT (do NOT emit):\n"
            "  · subject='Market volatility caused by Justice X's dissent' "
            "(fragment sentence, not a topic)\n"
            "  · claim='unclear or unrelated to BTC itself' (hedge)\n"
            "  · claim='no correlation' (non-directional)\n"
            "Output ONLY the JSON array. No prose, no preamble, no code fences."
        )
        messages = [
            ChatMessage(role="system", content=build_system_prompt()),
            ChatMessage(role="user", content=prompt),
        ]
        try:
            response = await self._chat_with_transient_retry(messages)
        except Exception as exc:
            logger.warning(
                "Thesis extraction chat failed for objective {}: {}",
                obj.get("objective_id"),
                exc,
            )
            return []
        return self._parse_thesis_json((response.message.content or "").strip())

    @staticmethod
    def _parse_thesis_json(text: str) -> list[dict[str, Any]]:
        """Defensively parse a model-emitted thesis JSON array; return [] on any failure."""
        if not text:
            return []
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        # Slice out the first JSON array in case prose leaked in around it
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start == -1 or end == -1 or end < start:
            logger.warning(
                "Thesis JSON parse: no array delimiters found in: {!r}",
                text[:200],
            )
            return []
        candidate = stripped[start : end + 1]
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Thesis JSON parse error: {}; raw: {!r}",
                exc,
                text[:200],
            )
            return []
        if not isinstance(data, list):
            return []
        # Deterministic backstop to the prompt: the 8B sometimes emits
        # non-directional pseudo-theses or claims without evidence despite
        # the RULES. Both make a thesis uncomparable for the contradiction
        # detector, so drop them at parse time.
        # Substring match (not exact) so phrases like "unclear or unrelated
        # to BTC itself" are caught. Safety: none of these tokens appears as
        # a substring of any directional word in DIRECTION_LEXICON
        # (declining/decreasing/falling/bearish/increasing/rising/bullish/...).
        non_directional_stoplist = {
            "unclear",
            "mixed",
            "unknown",
            "complex",
            "uncertain",
            "unrelated",
            "n/a",
        }
        valid: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            subject = item.get("subject")
            claim = item.get("claim")
            if not (
                isinstance(subject, str)
                and subject.strip()
                and isinstance(claim, str)
                and claim.strip()
            ):
                continue
            confidence = (
                item.get("confidence")
                if item.get("confidence") in ("low", "medium", "high")
                else "medium"
            )
            evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
            # Drop fragment-sentence subjects (headline copy escaping into
            # the subject field). Word-count only, conservative cutoff.
            # Legitimate observed subjects max at 6 words ("24-hour change
            # rate of BTC price", "BTC short-term price change accuracy");
            # the Sotomayor headline that motivated this filter was 12.
            # >10 words is well outside the legit distribution.
            if len(subject.strip().split()) > 10:
                logger.debug(
                    "Dropping fragment-subject thesis (>10 words): subject={!r}",
                    subject,
                )
                continue
            # Drop non-directional pseudo-theses (claim contains a hedge word)
            claim_lower = claim.strip().lower()
            if any(word in claim_lower for word in non_directional_stoplist):
                logger.debug(
                    "Dropping non-directional thesis: subject={!r} claim={!r}",
                    subject,
                    claim,
                )
                continue
            # Drop theses with no evidence — they cannot be contradiction-checked
            if not evidence:
                logger.debug(
                    "Dropping thesis with empty evidence: subject={!r} claim={!r}",
                    subject,
                    claim,
                )
                continue
            valid.append(
                {
                    "subject": subject.strip(),
                    "claim": claim.strip(),
                    "confidence": confidence,
                    "evidence": evidence,
                }
            )
        return valid

    async def detect_contradictions(self) -> list[dict[str, Any]]:
        """Scan active theses; classify opposed pairs into three buckets.

        Three-way outcome per opposing pair:

        1. **Timeframe conflict** — subjects on different horizons
           ("long-term" vs "short-term") are non-comparable; the pair is
           SKIPPED (no contradiction row, no status flip). See
           ``subjects_timeframe_conflict``.
        2. **Cross-window** — gap between the two theses' created_at is
           ≥ ``CONTRADICTION_WINDOW_HOURS`` (default 6h). This is a
           REVISION on a rolling metric, not a live contradiction. The
           OLDER thesis is flipped to ``superseded`` with ``superseded_by``
           pointing at the newer thesis. The newer thesis stays untouched
           (still active). No contradiction row.
        3. **Same-window** — gap < window. CURRENT behavior: both flip to
           ``contradicted`` and a contradiction row is recorded.

        Non-blocking on any DB/embedding failure — returns the partial list
        so the cycle's forced-completion path reaches its INTENTIONAL
        continue.
        """
        from datetime import datetime, timezone

        try:
            active = await self._persistent_memory.get_theses(status="active", limit=500)
        except Exception as exc:
            logger.warning("detect_contradictions: failed to load theses: {}", exc)
            self._feed_append("ERROR", f"contradiction load failed: {type(exc).__name__}")
            return []
        if len(active) < 2:
            return []
        try:
            groups = group_theses_by_subject(active)
        except Exception as exc:
            logger.warning("detect_contradictions: subject grouping failed: {}", exc)
            self._feed_append("ERROR", f"contradiction grouping failed: {type(exc).__name__}")
            return []

        # Per-pair window resolved inside the loop (window_for takes
        # both subjects). CONTRADICTION_WINDOW_HOURS retained as the
        # non-price default; window_for tightens to
        # CONTRADICTION_WINDOW_HOURS_PRICE for price-class pairs.
        found: list[dict[str, Any]] = []
        flipped: set[str] = set()          # IDs already flipped to contradicted this pass
        superseded_ids: set[str] = set()   # IDs already flipped to superseded this pass

        def _created_at(thesis: dict[str, Any]) -> datetime | None:
            raw = thesis.get("created_at")
            if isinstance(raw, datetime):
                return raw
            return None

        for group in groups:
            if len(group) < 2:
                continue
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    ta, tb = group[i], group[j]
                    if not claims_oppose(ta.get("claim", ""), tb.get("claim", "")):
                        continue

                    # Guard 1: timeframe mismatch — non-comparable subjects.
                    if subjects_timeframe_conflict(
                        ta.get("subject", ""), tb.get("subject", "")
                    ):
                        logger.debug(
                            "detect_contradictions: timeframe guard blocked pair "
                            "{!r} vs {!r}",
                            ta.get("subject"), tb.get("subject"),
                        )
                        continue

                    id_a = str(ta.get("thesis_id"))
                    id_b = str(tb.get("thesis_id"))
                    pair_subject = ta.get("subject")

                    # Guard 2: temporal window — cross-window pairs are
                    # supersessions on a rolling metric, not contradictions.
                    # Per-subject-class window: price-class pairs get
                    # the tighter 2h window; others keep 6h.
                    ca, cb = _created_at(ta), _created_at(tb)
                    if ca is not None and cb is not None:
                        # Both timestamps are timezone-aware in the schema.
                        gap = abs((ca - cb).total_seconds())
                        window_hours = window_for(
                            ta.get("subject", ""), tb.get("subject", ""),
                        )
                        window_seconds = window_hours * 3600.0
                        if gap >= window_seconds:
                            # Older → superseded, newer → untouched.
                            if ca <= cb:
                                older_id, newer_id = id_a, id_b
                            else:
                                older_id, newer_id = id_b, id_a
                            if older_id in superseded_ids:
                                continue
                            try:
                                await self._persistent_memory.mark_thesis_superseded(
                                    older_thesis_id=older_id,
                                    newer_thesis_id=newer_id,
                                )
                                superseded_ids.add(older_id)
                            except Exception as exc:
                                logger.warning(
                                    "Failed to mark thesis {} superseded: {}",
                                    older_id, exc,
                                )
                                continue
                            logger.debug(
                                "detect_contradictions: supersession — {} superseded by {} "
                                "(gap {:.1f}h ≥ window {:.1f}h)",
                                older_id[:8], newer_id[:8],
                                gap / 3600.0, window_hours,
                            )
                            continue

                    # Same-window: original contradiction path.
                    try:
                        await self._persistent_memory.record_contradiction(
                            thesis_id_a=id_a,
                            thesis_id_b=id_b,
                            subject_group=pair_subject,
                        )
                        if id_a not in flipped:
                            await self._persistent_memory.update_thesis_status(id_a, "contradicted")
                            flipped.add(id_a)
                        if id_b not in flipped:
                            await self._persistent_memory.update_thesis_status(id_b, "contradicted")
                            flipped.add(id_b)
                    except Exception as exc:
                        logger.warning(
                            "Failed to persist contradiction ({}, {}): {}",
                            id_a, id_b, exc,
                        )
                        continue
                    found.append({
                        "thesis_id_a": id_a,
                        "thesis_id_b": id_b,
                        "subject_group": pair_subject,
                        "claim_a": ta.get("claim"),
                        "claim_b": tb.get("claim"),
                    })
                    self._feed_append(
                        "WARN",
                        f"contradiction: '{ta.get('claim')}' vs '{tb.get('claim')}' "
                        f"on {(pair_subject or '')[:80]}",
                    )
        return found

    async def _chat_with_transient_retry(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Call Ollama once, retrying a single time on transient infra errors.

        Why: a per-cycle Ollama timeout (httpx.ReadTimeout etc.) propagates out
        of the cycle and burns a MAX_CYCLES slot because increment_cycle_count
        runs before the work. A bounded single retry inside the same cycle
        absorbs transient infra blips without changing budget semantics.
        Only httpx.TimeoutException and httpx.NetworkError are retried;
        HTTPStatusError is left to existing callers (e.g. the 400 fallback).
        Non-httpx errors propagate so genuine bugs are not masked.
        """
        try:
            return await self._llm_client.chat(messages, tools=tools)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            logger.warning(
                "Ollama chat hit transient {}; retrying once",
                type(exc).__name__,
            )
            self._feed_append(
                "ERROR",
                f"transient Ollama error ({type(exc).__name__}); retrying once",
            )
            return await self._llm_client.chat(messages, tools=tools)

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
            response = await self._chat_with_transient_retry(messages, tools=tool_schemas)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 400 or not tool_schemas:
                raise
            logger.warning("Ollama rejected tool-enabled chat payload; retrying without tools")
            # NOTE: 400-fallback chat not wrapped by _chat_with_transient_retry;
            # a ReadTimeout here burns the cycle slot. Low-probability (needs a
            # 400 then a timeout same cycle). Revisit if Ollama timeouts get frequent.
            response = await self._llm_client.chat(messages)
        tool_results: list[dict[str, Any]] = []
        _tool_round = 0
        _corrective_attempted = False

        while _tool_round < 5:
            if not response.message.tool_calls:
                if (
                    not _corrective_attempted
                    and self._current_objective_id
                    and _looks_like_unemitted_tool_intent(response.message.content)
                ):
                    _corrective_attempted = True
                    _tool_round += 1
                    messages.append(
                        ChatMessage(
                            role="assistant",
                            content=response.message.content,
                        )
                    )
                    messages.append(
                        ChatMessage(
                            role="system",
                            content=(
                                "You described an action but did not emit a tool call. "
                                "Emit the actual tool_call now with all required "
                                "arguments, or nothing. Do not narrate."
                            ),
                        )
                    )
                    try:
                        response = await self._chat_with_transient_retry(
                            messages, tools=tool_schemas
                        )
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 400 or not tool_schemas:
                            raise
                        logger.warning(
                            "Ollama rejected tool-enabled corrective retry; continuing without tools"
                        )
                        response = await self._llm_client.chat(messages)
                    continue
                break
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
                    if _ok and _name in DATA_SOURCE_TOOLS and self._current_objective_id:
                        try:
                            await self._persistent_memory.add_source_used(
                                self._current_objective_id, _name
                            )
                        except Exception as _src_exc:
                            logger.warning("Could not record source {}: {}", _name, _src_exc)
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
                response = await self._chat_with_transient_retry(messages, tools=tool_schemas)
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
