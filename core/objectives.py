"""Autonomous objective generation and tracking for Morgoth."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig
from core.llm_client import ChatMessage, OllamaLLMClient
from memory.persistent import PersistentMemory


class ObjectiveCategory(str, Enum):
    """Supported objective categories."""

    RESEARCH = "research"
    CAPABILITY = "capability"
    MONITORING = "monitoring"
    OPTIMIZATION = "optimization"


class ObjectiveStatus(str, Enum):
    """Supported objective lifecycle states."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class ObjectiveEvidence(BaseModel):
    """Evidence item that triggered an objective."""

    trigger: str
    summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Objective(BaseModel):
    """Tracked objective persisted in PostgreSQL."""

    objective_id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str
    category: ObjectiveCategory
    priority: int = 2
    generated_by: str = "morgoth"
    status: ObjectiveStatus = ObjectiveStatus.PENDING
    evidence: list[ObjectiveEvidence] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    user_id: str = "default"

    def to_record(self) -> dict[str, Any]:
        """Serialize the objective for PostgreSQL writes."""

        return {
            "objective_id": self.objective_id,
            "title": self.title,
            "description": self.description,
            "category": self.category.value,
            "priority": self.priority,
            "generated_by": self.generated_by,
            "status": self.status.value,
            "evidence": [item.model_dump(mode="json") for item in self.evidence],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "user_id": self.user_id,
        }


class ObjectiveSuggestion(BaseModel):
    """Structured suggestion produced by the generator."""

    title: str
    description: str
    category: ObjectiveCategory
    priority: int = 2


class ObjectivesManager:
    """Generate and track autonomous objectives with graceful fallback behavior."""

    def __init__(
        self,
        config: AppConfig,
        persistent_memory: PersistentMemory,
        llm_client: OllamaLLMClient | None = None,
    ) -> None:
        """Store runtime dependencies."""

        self._config = config
        self._persistent_memory = persistent_memory
        self._llm_client = llm_client

    async def create_objective(self, objective: Objective) -> Objective:
        """Persist an objective and return it."""

        record = objective.to_record()
        query = """
        INSERT INTO objectives (
            objective_id, title, description, category, priority, generated_by,
            status, evidence, created_at, completed_at, user_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
        ON CONFLICT (objective_id) DO UPDATE
        SET title = EXCLUDED.title,
            description = EXCLUDED.description,
            category = EXCLUDED.category,
            priority = EXCLUDED.priority,
            generated_by = EXCLUDED.generated_by,
            status = EXCLUDED.status,
            evidence = EXCLUDED.evidence,
            created_at = EXCLUDED.created_at,
            completed_at = EXCLUDED.completed_at,
            user_id = EXCLUDED.user_id
        """
        await self._persistent_memory.execute(
            query,
            record["objective_id"],
            record["title"],
            record["description"],
            record["category"],
            record["priority"],
            record["generated_by"],
            record["status"],
            json.dumps(record["evidence"]),
            record["created_at"],
            record["completed_at"],
            record["user_id"],
        )
        logger.info("Objective '{}' stored with status '{}'", objective.title, objective.status.value)
        return objective

    async def list_objectives(
        self,
        *,
        status: ObjectiveStatus | None = None,
        limit: int = 100,
    ) -> list[Objective]:
        """Return recent objectives, optionally filtered by status."""

        if status is None:
            rows = await self._persistent_memory.fetch(
                "SELECT * FROM objectives ORDER BY priority ASC, created_at DESC LIMIT $1",
                limit,
            )
        else:
            rows = await self._persistent_memory.fetch(
                """
                SELECT * FROM objectives
                WHERE status = $1
                ORDER BY priority ASC, created_at DESC
                LIMIT $2
                """,
                status.value,
                limit,
            )
        return [self._row_to_objective(dict(row)) for row in rows]

    async def update_status(
        self,
        objective_id: str,
        status: ObjectiveStatus,
        *,
        completed_at: datetime | None = None,
    ) -> bool:
        """Update an objective status."""

        completion_time = completed_at
        if status == ObjectiveStatus.COMPLETED and completion_time is None:
            completion_time = datetime.now(timezone.utc)
        result = await self._persistent_memory.execute(
            "UPDATE objectives SET status = $1, completed_at = $2 WHERE objective_id = $3",
            status.value,
            completion_time,
            objective_id,
        )
        return result.rows_affected > 0

    async def generate_objective(
        self,
        observation: str,
        *,
        evidence: list[ObjectiveEvidence] | None = None,
        category_hint: ObjectiveCategory | None = None,
        user_id: str = "default",
    ) -> Objective:
        """Generate an objective from an observation and store it."""

        evidence_items = evidence or [
            ObjectiveEvidence(
                trigger="observation",
                summary=observation[:400],
            )
        ]
        suggestion = await self._suggest_objective(observation, evidence_items, category_hint=category_hint)
        objective = Objective(
            title=suggestion.title,
            description=suggestion.description,
            category=suggestion.category,
            priority=max(0, min(3, suggestion.priority)),
            evidence=evidence_items,
            user_id=user_id,
        )
        return await self.create_objective(objective)

    async def _suggest_objective(
        self,
        observation: str,
        evidence: list[ObjectiveEvidence],
        *,
        category_hint: ObjectiveCategory | None = None,
    ) -> ObjectiveSuggestion:
        """Use the LLM when available, with heuristic fallback on failure."""

        if self._llm_client is None:
            return self._fallback_suggestion(observation, evidence, category_hint=category_hint)

        system_prompt = (
            "You generate one autonomous objective for Morgoth. "
            "Return strict JSON with keys: title, description, category, priority. "
            "category must be one of research, capability, monitoring, optimization. "
            "priority must be an integer 0-3 where 0 is highest."
        )
        user_prompt = json.dumps(
            {
                "observation": observation,
                "category_hint": category_hint.value if category_hint else None,
                "evidence": [item.model_dump(mode="json") for item in evidence],
            },
            default=str,
        )

        try:
            response = await self._llm_client.chat(
                [
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=user_prompt),
                ],
                model=self._config.choose_model_for_task("self_modify"),
                options={"temperature": 0.2},
            )
            content = (response.message.content or "").strip()
            suggestion = ObjectiveSuggestion.model_validate(json.loads(self._strip_code_fences(content)))
            return suggestion
        except Exception:
            logger.exception("LLM objective generation failed; using heuristic fallback")
            return self._fallback_suggestion(observation, evidence, category_hint=category_hint)

    def _fallback_suggestion(
        self,
        observation: str,
        evidence: list[ObjectiveEvidence],
        *,
        category_hint: ObjectiveCategory | None = None,
    ) -> ObjectiveSuggestion:
        """Generate an objective with simple deterministic heuristics."""

        lowered = observation.lower()
        if category_hint is not None:
            category = category_hint
        elif "error" in lowered or "failed" in lowered or "timeout" in lowered:
            category = ObjectiveCategory.OPTIMIZATION
        elif "missing" in lowered or "need" in lowered or "lack" in lowered:
            category = ObjectiveCategory.CAPABILITY
        elif "price" in lowered or "anomaly" in lowered or "monitor" in lowered:
            category = ObjectiveCategory.MONITORING
        else:
            category = ObjectiveCategory.RESEARCH

        title_prefix = {
            ObjectiveCategory.RESEARCH: "Investigate",
            ObjectiveCategory.CAPABILITY: "Build",
            ObjectiveCategory.MONITORING: "Monitor",
            ObjectiveCategory.OPTIMIZATION: "Stabilize",
        }[category]
        summary = evidence[0].summary if evidence else observation
        return ObjectiveSuggestion(
            title=f"{title_prefix} {summary[:72].strip()}",
            description=observation.strip(),
            category=category,
            priority=1 if category in {ObjectiveCategory.OPTIMIZATION, ObjectiveCategory.CAPABILITY} else 2,
        )

    def _row_to_objective(self, row: dict[str, Any]) -> Objective:
        """Convert a PostgreSQL row into an objective model."""

        raw_evidence = row.get("evidence") or []
        if isinstance(raw_evidence, str):
            raw_evidence = json.loads(raw_evidence)
        return Objective(
            objective_id=str(row["objective_id"]),
            title=row["title"],
            description=row["description"],
            category=ObjectiveCategory(row["category"]),
            priority=row["priority"],
            generated_by=row["generated_by"],
            status=ObjectiveStatus(row["status"]),
            evidence=[ObjectiveEvidence.model_validate(item) for item in raw_evidence],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            user_id=row.get("user_id", "default"),
        )

    def _strip_code_fences(self, content: str) -> str:
        """Remove optional Markdown fences from LLM JSON output."""

        cleaned = content.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned
