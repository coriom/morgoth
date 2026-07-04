"""Proposal store — DB-backed lifecycle for self-modify proposals.

Reuses the connection pool owned by ``PersistentMemory``. The table itself
is created by ``PersistentMemory.initialize`` using the established
non-fatal ``CREATE TABLE IF NOT EXISTS`` pattern.

Lifecycle
---------
::

    submitted
      ├─ (zone gate)  → zone_rejected            [terminal]
      └─ (zone gate ok)
           ├─ (tests gate) → tests_failed        [terminal]
           └─ (tests gate ok) → pending_approval
                  ├─ approve → approved_pending_apply [terminal in step 1]
                  └─ reject  → rejected               [terminal]

    rejected_shape                                    [terminal, pre-submit]
        Reflect-time reject when the API response does not carry the
        proposed digest_fields at the template's extraction site (or
        those fields are array/dict-valued and would break the compact
        digest contract). Not written by the pipeline gates; produced
        by ``self_modify.reflect`` before ``submit()`` is called, so
        the row never actually exists — the constant lives here for
        callers that want to reason over the reflect outcome uniformly.

Terminology note: ``approved_pending_apply`` is deliberately terminal in
this step. APPLY DOES NOT EXIST yet. An approved proposal sits inert; the
apply/commit/rollback machinery is scoped for a later step.
"""

from __future__ import annotations

import uuid as _uuid
from typing import Any

from memory.persistent import PersistentMemory


# Explicit lifecycle enumeration — anywhere in this module or the gates
# that assigns a status must use one of these constants.
STATUS_SUBMITTED = "submitted"
STATUS_ZONE_REJECTED = "zone_rejected"
STATUS_TESTS_FAILED = "tests_failed"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_APPROVED_PENDING_APPLY = "approved_pending_apply"
STATUS_REJECTED = "rejected"
# Pre-submit reflect-time reject when the endpoint's response does not
# structurally support the proposed digest fields. See module docstring.
STATUS_REJECTED_SHAPE = "rejected_shape"
# Apply-time statuses (step 2 — the door).
STATUS_APPLIED = "applied"
STATUS_APPLY_FAILED_ROLLED_BACK = "apply_failed_rolled_back"

ALL_STATUSES: tuple[str, ...] = (
    STATUS_SUBMITTED,
    STATUS_ZONE_REJECTED,
    STATUS_TESTS_FAILED,
    STATUS_PENDING_APPROVAL,
    STATUS_APPROVED_PENDING_APPLY,
    STATUS_REJECTED,
    STATUS_REJECTED_SHAPE,
    STATUS_APPLIED,
    STATUS_APPLY_FAILED_ROLLED_BACK,
)


class ProposalStore:
    """Thin CRUD wrapper around ``self_modify_proposals``.

    Owns no connection lifecycle — takes a live ``PersistentMemory`` and
    reuses its pool. Callers are responsible for ``pm.initialize()`` /
    ``pm.close()``.
    """

    def __init__(self, pm: PersistentMemory) -> None:
        self._pm = pm

    async def submit(
        self,
        target_path: str,
        change_type: str,
        content: str,
        rationale: str,
        proposed_by: str = "human",
        engine: str = "ollama",
    ) -> str:
        """Insert a new proposal in ``submitted`` state; return its id.

        ``proposed_by`` records provenance ('human' for operator-injected
        rows, 'morgoth' for reflection-born ones). ``engine`` records
        which LLM the spec came from ('ollama' | 'anthropic'). Together
        they are the calibration axis: proposal quality can be sliced by
        ``proposed_by × engine`` for the future security-agent gate.
        """
        proposal_id = str(_uuid.uuid4())
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO self_modify_proposals
                    (proposal_id, target_path, change_type, content, rationale,
                     status, proposed_by, engine)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                _uuid.UUID(proposal_id),
                target_path,
                change_type,
                content,
                rationale,
                STATUS_SUBMITTED,
                proposed_by,
                engine,
            )
        return proposal_id

    async def count_by_status_and_author(
        self,
        status: str,
        proposed_by: str,
    ) -> int:
        """Count proposals matching (status, proposed_by).

        Used by the reflect job's cap: refuses to submit another if
        pending_approval + proposed_by='morgoth' >= 3.
        """
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS n FROM self_modify_proposals "
                "WHERE status = $1 AND proposed_by = $2",
                status,
                proposed_by,
            )
        return int(row["n"]) if row else 0

    async def get(self, proposal_id: str) -> dict[str, Any] | None:
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM self_modify_proposals WHERE proposal_id = $1",
                _uuid.UUID(proposal_id),
            )
        return dict(row) if row else None

    async def list_pending(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM self_modify_proposals "
                "WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
                STATUS_PENDING_APPROVAL,
                limit,
            )
        return [dict(r) for r in rows]

    async def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM self_modify_proposals "
                "ORDER BY created_at DESC LIMIT $1",
                limit,
            )
        return [dict(r) for r in rows]

    async def update_status(
        self,
        proposal_id: str,
        status: str,
        status_reason: str | None = None,
    ) -> bool:
        if status not in ALL_STATUSES:
            raise ValueError(f"Unknown status: {status!r}")
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE self_modify_proposals "
                "SET status = $1, status_reason = $2, updated_at = NOW() "
                "WHERE proposal_id = $3",
                status,
                status_reason,
                _uuid.UUID(proposal_id),
            )
        return result.endswith(" 1")
