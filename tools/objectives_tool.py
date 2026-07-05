"""Objective creation tool for Morgoth.

Side-door policy (mid-cycle spawns): ``create_objective`` is in
``CHAT_TOOL_NAMES``, which means the 8B can invoke it WHILE working
another objective — not just from the empty-queue generation branch.
That is INTENTIONAL: the model finding a real gap while investigating
a subject is legitimate. The control is not a spawn ban; it is the
semantic dedup gate below. Divergence context from
``core.objective_gen_context`` is NOT injected into work cycles
because it would pollute the work prompt with generation-time
scaffolding; keeping generation and work prompts separate is the
design intent, and dedup is what prevents the basin from re-entering
through the side door.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any, Callable

from loguru import logger

from memory.persistent import PersistentMemory
from tools.base_tool import BaseTool


# How many words of the description form the derived title when the
# LLM omits ``title`` from its ``create_objective`` tool-call args.
# llama3.1:8b was measured to drop the title 2/5 dry-runs — those
# calls used to KeyError and silently waste the cycle slot; now they
# land as writable rows with a derived title.
_TITLE_DERIVATION_WORDS = 8


# Semantic dedup threshold, calibrated against real DB pairs:
#   near-dup real DB    → 0.86 (must match)
#   case-variant        → 1.00 (must match)
#   near-dup vs unrelated targets → ≤0.55 (must not match)
# 0.75 sits in the middle of that 0.31-wide gap: comfortable margin
# on both sides. Env-overridable so an operator can tighten/loosen
# without a redeploy.
DEFAULT_OBJECTIVE_DEDUP_THRESHOLD: float = 0.75


def _resolve_dedup_threshold() -> float:
    """Env override → validated float. Silent-fallback on parse error."""
    raw = os.environ.get("OBJECTIVE_DEDUP_THRESHOLD")
    if raw is None:
        return DEFAULT_OBJECTIVE_DEDUP_THRESHOLD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "OBJECTIVE_DEDUP_THRESHOLD={!r} not a float; using default", raw,
        )
        return DEFAULT_OBJECTIVE_DEDUP_THRESHOLD
    if not 0.0 < value <= 1.0:
        logger.warning(
            "OBJECTIVE_DEDUP_THRESHOLD={!r} out of (0,1]; using default", raw,
        )
        return DEFAULT_OBJECTIVE_DEDUP_THRESHOLD
    return value


# Non-terminal statuses — objectives in these states are actively
# being worked or queued for work. Comparisons against terminal
# rows (``done``, ``completed``) would false-positive an obvious
# "we already investigated this" case as duplication of live work.
_NON_TERMINAL_STATUSES: tuple[str, ...] = ("pending", "in_progress")


def _compose_for_embedding(title: str, description: str) -> str:
    """Match calibration harness: ``"title. description"``, trimmed.

    When description is empty (legacy rows created before the
    write-side description fallback landed), embed just the title —
    no trailing ``". "``. The old ``.strip(". ")`` also stripped
    legitimate trailing periods from titles; the explicit-branch
    form here preserves them.
    """
    t = (title or "").strip()
    d = (description or "").strip()
    if not d:
        return t or "(empty)"
    return f"{t}. {d}"


async def _find_semantic_duplicate(
    pm: PersistentMemory,
    new_title: str,
    new_description: str,
    threshold: float,
    embed_fn: Callable[[list[str]], list[list[float]]] | None = None,
) -> dict[str, Any] | None:
    """Return an existing non-terminal objective if the new one is a
    semantic duplicate; else ``None``.

    Fetches non-terminal rows directly from the pool (read-only),
    batch-embeds the new proposal + every candidate, and returns the
    FIRST match at or above the threshold. First-match rather than
    best-match because the operator-visible signal is "which row
    should you work instead" — for a small non-terminal set the
    first candidate is fine and keeps the render deterministic.

    ``embed_fn`` is injectable for tests (mirrors the pattern used by
    ``core.contradictions.group_theses_by_subject``).

    Fail-open contract is the CALLER's responsibility — any exception
    here must propagate so the caller's ``try/except`` runs the
    write anyway.
    """
    from core.contradictions import _cosine, _get_embedding_fn

    pool = pm._require_pool()  # noqa: SLF001
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT objective_id, title, description, status FROM objectives "
            "WHERE status = ANY($1::text[]) ORDER BY created_at ASC",
            list(_NON_TERMINAL_STATUSES),
        )
    if not rows:
        return None

    new_text = _compose_for_embedding(new_title, new_description)
    corpus = [new_text] + [
        _compose_for_embedding(
            r.get("title") or "", r.get("description") or "",
        )
        for r in rows
    ]
    fn = embed_fn or _get_embedding_fn()
    embeddings = fn(corpus)
    new_emb = list(embeddings[0])
    for row, emb in zip(rows, embeddings[1:]):
        if _cosine(new_emb, list(emb)) >= threshold:
            return dict(row)
    return None


def derive_title_from_description(description: str) -> str:
    """Deterministic title fallback for missing/empty ``title`` args.

    - First ``_TITLE_DERIVATION_WORDS`` words of the description.
    - Ellipsis appended if the description had more words than that.
    - Empty description → ``"(untitled investigation)"`` sentinel so
      the row remains queryable + human-readable.

    No LLM, no round-trip, no writes; purely a string operation.
    """
    if not isinstance(description, str):
        description = "" if description is None else str(description)
    words = description.split()
    if not words:
        return "(untitled investigation)"
    head = words[: _TITLE_DERIVATION_WORDS]
    ellipsis = "…" if len(words) > _TITLE_DERIVATION_WORDS else ""
    return " ".join(head) + ellipsis


class CreateObjectiveTool(BaseTool):
    """Create a persistent objective in the PostgreSQL objectives table."""

    name = "create_objective"
    description = (
        "Create a persistent objective in Morgoth's goal queue. "
        "Use this when identifying a knowledge gap or task to pursue. "
        "Objectives have lifecycle: pending → in_progress → done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short title, max 100 chars"},
            "description": {"type": "string", "description": "What to accomplish"},
            "priority": {"type": "integer", "description": "1=highest, 5=lowest", "default": 3},
        },
        "required": ["title", "description"],
    }

    def __init__(self, persistent_memory: PersistentMemory) -> None:
        """Initialize with persistent memory for PostgreSQL access."""

        self._persistent_memory = persistent_memory

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Insert a new objective row and return the created record."""

        description = str(kwargs.get("description") or "")
        # Missing / empty / whitespace-only title → derive from
        # description. The prior handler KeyError'd here and returned
        # failure, silently wasting the cycle slot; measured 2/5 on
        # the dry-run. Deterministic fallback keeps the row writable.
        raw_title = kwargs.get("title")
        title = str(raw_title).strip() if raw_title is not None else ""
        if not title:
            title = derive_title_from_description(description)
        title = title[:100]
        # Symmetric description fallback — the first production else-
        # branch generation (cefaeb94) landed with a real title and
        # an EMPTY description. Empty descriptions degrade the dedup
        # gate's embedding and leave the operator staring at a blank
        # detail field. Copy the (already fallback-resolved) title.
        # Degenerate case: BOTH title and description missing →
        # title derives to "(untitled investigation)" from the empty
        # description, and description then copies that same sentinel.
        # The row remains writable, queryable, and readable — the
        # contract the title fallback established.
        if not description.strip():
            description = title
        priority = int(kwargs.get("priority", 3))

        # Semantic dedup gate — see module docstring for the side-door
        # policy. Any error → warn + proceed with creation (fail-open):
        # a slipped duplicate costs redundant cycles; a blocked
        # creation path costs the whole generation capability.
        try:
            threshold = _resolve_dedup_threshold()
            duplicate = await _find_semantic_duplicate(
                self._persistent_memory, title, description, threshold,
            )
        except Exception as exc:
            logger.warning(
                "objective dedup gate failed (fail-open): {}", exc,
            )
            duplicate = None
        if duplicate is not None:
            existing_id = str(duplicate.get("objective_id") or "")
            existing_title = str(duplicate.get("title") or "").strip()
            short_id = existing_id[:8]
            return self.failure(
                f"duplicate of active objective {existing_title!r} "
                f"({short_id}) — work that objective instead of "
                f"spawning a variant"
            )

        try:
            row = await self._persistent_memory.create_objective(
                title=title,
                description=description,
                priority=priority,
            )
            return self.success(self._serialize(row))
        except Exception as exc:
            return self.failure(str(exc))

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        """Convert asyncpg non-JSON-serializable types before returning to the LLM."""

        result: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, uuid.UUID):
                result[key] = str(value)
            elif isinstance(value, datetime):
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result


class UpdateObjectiveTool(BaseTool):
    """Update an existing objective's status or add evidence."""

    name = "update_objective"
    description = (
        "Update an existing objective's status or add evidence. "
        "Use to mark progress: in_progress when starting work, "
        "done when complete. Add evidence describing what was found."
    )
    parameters = {
        "type": "object",
        "properties": {
            "objective_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "done"]},
            "evidence_summary": {
                "type": "string",
                "description": "What was found or accomplished",
            },
        },
        "required": ["objective_id"],
    }

    def __init__(self, persistent_memory: PersistentMemory) -> None:
        """Initialize with persistent memory for PostgreSQL access."""

        self._persistent_memory = persistent_memory

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Update the objective row and return the updated record."""

        objective_id = str(kwargs["objective_id"])
        status = kwargs.get("status")
        evidence_summary = kwargs.get("evidence_summary")
        evidence = {"summary": evidence_summary} if evidence_summary else None

        try:
            row = await self._persistent_memory.update_objective(
                objective_id=objective_id,
                status=status,
                evidence=evidence,
            )
            return self.success(self._serialize(row))
        except Exception as exc:
            return self.failure(str(exc))

    def _serialize(self, row: dict[str, Any]) -> dict[str, Any]:
        """Convert asyncpg non-JSON-serializable types before returning to the LLM."""

        result: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, uuid.UUID):
                result[key] = str(value)
            elif isinstance(value, datetime):
                result[key] = value.isoformat()
            else:
                result[key] = value
        return result
