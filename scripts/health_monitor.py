"""Self-monitoring loop for Morgoth runtime dependencies."""

from __future__ import annotations

import asyncio
import resource
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig, load_config
from core.llm_client import OllamaLLMClient
from core.objectives import ObjectiveCategory, ObjectiveEvidence, ObjectivesManager
from memory.episodic import EpisodicMemory
from memory.persistent import PersistentMemory
from notifications.telegram import TelegramNotifier


CHECK_INTERVAL_SECONDS = 60
CONSECUTIVE_FAILURE_THRESHOLD = 3
MAX_MEMORY_MB = 2048


class ComponentHealth(BaseModel):
    """Health check result for one component."""

    name: str
    healthy: bool
    severity: str = "INFO"
    details: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class HealthSnapshot(BaseModel):
    """Grouped health check result."""

    overall_healthy: bool
    components: list[ComponentHealth]
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RuntimeHandles:
    """Opened resources used during one health check."""

    llm_client: OllamaLLMClient
    persistent_memory: PersistentMemory
    episodic_memory: EpisodicMemory
    notifier: TelegramNotifier
    objectives: ObjectivesManager


class HealthMonitor:
    """Continuously probe runtime dependencies and alert on degradation."""

    def __init__(self, config: AppConfig) -> None:
        """Store configuration and initialize failure tracking."""

        self._config = config
        self._consecutive_failures: dict[str, int] = {}

    async def run_forever(self, interval_seconds: int = CHECK_INTERVAL_SECONDS) -> None:
        """Run the monitoring loop until interrupted."""

        while True:
            handles = await self._open_handles()
            try:
                snapshot = await self.check_once(handles)
                await self._handle_snapshot(snapshot, handles)
            except Exception:
                logger.exception("Health monitor iteration failed")
            finally:
                await self._close_handles(handles)
            await asyncio.sleep(interval_seconds)

    async def check_once(self, handles: RuntimeHandles) -> HealthSnapshot:
        """Execute one full health-check round."""

        components = [
            await self._check_ollama(handles.llm_client),
            await self._check_postgres(handles.persistent_memory),
            await self._check_chromadb(handles.episodic_memory),
            await self._check_agents(handles.persistent_memory),
            self._check_memory_usage(),
            await self._check_task_queue(handles.persistent_memory),
        ]
        return HealthSnapshot(
            overall_healthy=all(component.healthy for component in components),
            components=components,
        )

    async def _handle_snapshot(self, snapshot: HealthSnapshot, handles: RuntimeHandles) -> None:
        """Log and escalate unhealthy snapshots."""

        for component in snapshot.components:
            failure_count = 0 if component.healthy else self._consecutive_failures.get(component.name, 0) + 1
            self._consecutive_failures[component.name] = failure_count
            log_message = f"{component.name}: {component.details or 'healthy'}"
            if component.healthy:
                logger.info(log_message)
                continue

            logger.warning(log_message)
            if component.severity.upper() == "CRITICAL":
                await handles.notifier.send("CRITICAL", log_message)

            if failure_count >= CONSECUTIVE_FAILURE_THRESHOLD:
                try:
                    await handles.objectives.generate_objective(
                        f"Repeated health monitor failure for {component.name}: {component.details}",
                        evidence=[
                            ObjectiveEvidence(
                                trigger="health_monitor",
                                summary=component.details or component.name,
                                metadata=component.metadata,
                            )
                        ],
                        category_hint=ObjectiveCategory.MONITORING,
                    )
                except Exception:
                    logger.exception("Failed to create monitoring objective for '{}'", component.name)
                self._consecutive_failures[component.name] = 0

    async def _check_ollama(self, llm_client: OllamaLLMClient) -> ComponentHealth:
        """Verify Ollama responds to the health endpoint."""

        healthy = await llm_client.health_check()
        return ComponentHealth(
            name="ollama",
            healthy=healthy,
            severity="CRITICAL" if not healthy else "INFO",
            details="Ollama reachable" if healthy else "Ollama did not respond to /api/tags",
        )

    async def _check_postgres(self, persistent_memory: PersistentMemory) -> ComponentHealth:
        """Verify PostgreSQL connectivity."""

        try:
            await persistent_memory.initialize()
            row = await persistent_memory.fetchrow("SELECT 1 AS ok")
            healthy = bool(row and row["ok"] == 1)
            return ComponentHealth(
                name="postgresql",
                healthy=healthy,
                severity="CRITICAL" if not healthy else "INFO",
                details="Database query succeeded" if healthy else "Database query returned unexpected result",
            )
        except Exception as exc:
            logger.exception("PostgreSQL health check failed")
            return ComponentHealth(
                name="postgresql",
                healthy=False,
                severity="CRITICAL",
                details=str(exc),
            )

    async def _check_chromadb(self, episodic_memory: EpisodicMemory) -> ComponentHealth:
        """Verify ChromaDB initialization and collection access."""

        try:
            await episodic_memory.initialize()
            healthy = len(episodic_memory.collections) > 0
            return ComponentHealth(
                name="chromadb",
                healthy=healthy,
                severity="CRITICAL" if not healthy else "INFO",
                details=f"{len(episodic_memory.collections)} collections available" if healthy else "No collections available",
            )
        except Exception as exc:
            logger.exception("ChromaDB health check failed")
            return ComponentHealth(
                name="chromadb",
                healthy=False,
                severity="CRITICAL",
                details=str(exc),
            )

    async def _check_agents(self, persistent_memory: PersistentMemory) -> ComponentHealth:
        """Verify persistent agent rows are not stuck in failed states."""

        try:
            rows = await persistent_memory.fetch(
                """
                SELECT agent_id, name, agent_type, status
                FROM agents
                WHERE stopped_at IS NULL
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
            active_agents = [dict(row) for row in rows if row["agent_type"] == "persistent"]
            failed_agents = [row for row in active_agents if row["status"] == "failed"]
            healthy = not failed_agents
            details = (
                f"{len(active_agents)} persistent agents healthy"
                if healthy
                else f"{len(failed_agents)} persistent agents in failed state"
            )
            return ComponentHealth(
                name="agents",
                healthy=healthy,
                severity="CRITICAL" if failed_agents else "INFO",
                details=details,
                metadata={"active_agents": len(active_agents), "failed_agents": len(failed_agents)},
            )
        except Exception as exc:
            logger.exception("Agent health check failed")
            return ComponentHealth(
                name="agents",
                healthy=False,
                severity="CRITICAL",
                details=str(exc),
            )

    def _check_memory_usage(self) -> ComponentHealth:
        """Verify the current process stays within a conservative memory bound."""

        usage_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        usage_mb = usage_kb / 1024
        healthy = usage_mb < MAX_MEMORY_MB
        return ComponentHealth(
            name="memory_usage",
            healthy=healthy,
            severity="WARNING" if not healthy else "INFO",
            details=f"RSS {usage_mb:.1f} MB",
            metadata={"rss_mb": round(usage_mb, 2), "limit_mb": MAX_MEMORY_MB},
        )

    async def _check_task_queue(self, persistent_memory: PersistentMemory) -> ComponentHealth:
        """Detect an obviously stalled pending task queue."""

        try:
            row = await persistent_memory.fetchrow(
                """
                SELECT COUNT(*) AS pending_count
                FROM tasks
                WHERE status = 'pending'
                  AND created_at < NOW() - INTERVAL '15 minutes'
                """
            )
            pending_count = int(row["pending_count"]) if row else 0
            healthy = pending_count == 0
            return ComponentHealth(
                name="task_queue",
                healthy=healthy,
                severity="WARNING" if pending_count else "INFO",
                details="Task queue flowing normally" if healthy else f"{pending_count} pending tasks older than 15 minutes",
                metadata={"stale_pending_tasks": pending_count},
            )
        except Exception as exc:
            logger.exception("Task queue health check failed")
            return ComponentHealth(
                name="task_queue",
                healthy=False,
                severity="WARNING",
                details=str(exc),
            )

    async def _open_handles(self) -> RuntimeHandles:
        """Create fresh runtime handles for one monitoring iteration."""

        llm_client = OllamaLLMClient(self._config)
        persistent_memory = PersistentMemory(self._config)
        episodic_memory = EpisodicMemory(self._config.chroma_dir)
        notifier = TelegramNotifier(self._config)
        objectives = ObjectivesManager(self._config, persistent_memory, llm_client)
        return RuntimeHandles(
            llm_client=llm_client,
            persistent_memory=persistent_memory,
            episodic_memory=episodic_memory,
            notifier=notifier,
            objectives=objectives,
        )

    async def _close_handles(self, handles: RuntimeHandles) -> None:
        """Close all opened resources."""

        await handles.notifier.close()
        await handles.llm_client.close()
        await handles.persistent_memory.close()


async def main() -> None:
    """Entry point for the health monitor."""

    config = await load_config()
    monitor = HealthMonitor(config)
    await monitor.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
