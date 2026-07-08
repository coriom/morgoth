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

    Pre-submit terminals (written DIRECTLY by ``submit_terminal``,
    never transit ``submitted``):

    malformed         [terminal]  Spec parsed but failed validation
                                  (bad tool_name, digest_fields, ...).
    rejected_name     [terminal]  tool_name's content tokens do not
                                  appear in description/endpoint —
                                  the name lies about what the tool
                                  fetches (e.g. "exchange_flows" on a
                                  mining-pool endpoint).
    rejected_endpoint [terminal]  api_base_url + endpoint_path (after
                                  normalization) duplicates an already-
                                  registered data_source tool's
                                  endpoint. Two sources on one endpoint
                                  make the distinct-sources rail
                                  fictional.
    rejected_smoke    [terminal]  URL was fetchable but returned non-2xx
                                  / network failure / non-JSON body.
    rejected_shape    [terminal]  Body was JSON but the template's
                                  extraction site does not yield the
                                  proposed digest_fields as scalars.
    rejected_stale    [terminal]  Body was a chronologically-ascending
                                  array; the template's [0] extraction
                                  site would return the OLDEST element
                                  on every call — silent staleness. The
                                  fix is a different endpoint, not the
                                  template.

    These rows carry the rejected SPEC as ``content`` (JSON) so a
    later reflect run can render them back into its prompt as a
    negative list — the model stops re-proposing already-judged
    candidates. Rows are never in ``submitted`` state; the transition
    is single-step, at reject time.

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
# Pre-submit reflect-time terminal statuses. Rows land here directly
# via ``submit_terminal`` (never transit ``submitted``); they carry the
# rejected SPEC as ``content`` (JSON) so the negative-list loader can
# render them back into the next reflect prompt as calibration data.
STATUS_MALFORMED = "malformed"
STATUS_REJECTED_NAME = "rejected_name"
STATUS_REJECTED_ENDPOINT = "rejected_endpoint"
STATUS_REJECTED_SMOKE = "rejected_smoke"
STATUS_REJECTED_SHAPE = "rejected_shape"
STATUS_REJECTED_STALE = "rejected_stale"
# Field-liveness gate — 4 GETs over 7.5 min, three rules (see
# self_modify.liveness). Written via update_status because the probe
# runs CONCURRENTLY with gate_tests (sandbox pytest) and the row is
# already submitted by the time the probe verdict lands. Still counts
# as a pre-submit-terminal for calibration purposes: carries the spec
# and lands on the negative list.
STATUS_REJECTED_STATIC = "rejected_static"
# Delegation regime — SHADOW authority WITH a mechanical trapdoor.
# The shadow verifier can auto-classify a proposal into
# shadow_rejected ONLY when the delegation flag is on AND ONLY on
# a REJECT verdict. APPROVE/FLAG stays in the operator queue (the
# dangerous direction remains 100% human). shadow_rejected is
# terminal and distinct from operator-rejected so the calibration
# axis stays intact.
STATUS_SHADOW_REJECTED = "shadow_rejected"
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
    STATUS_MALFORMED,
    STATUS_REJECTED_NAME,
    STATUS_REJECTED_ENDPOINT,
    STATUS_REJECTED_SMOKE,
    STATUS_REJECTED_SHAPE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_STATIC,
    STATUS_SHADOW_REJECTED,
    STATUS_APPLIED,
    STATUS_APPLY_FAILED_ROLLED_BACK,
)

# The statuses the reflect negative-list loader considers. Gate-3
# rejects (rejected) plus the pre-submit terminals that carry a
# rejected spec.
NEGATIVE_LIST_STATUSES: tuple[str, ...] = (
    STATUS_REJECTED,
    STATUS_MALFORMED,
    STATUS_REJECTED_NAME,
    STATUS_REJECTED_ENDPOINT,
    STATUS_REJECTED_SMOKE,
    STATUS_REJECTED_SHAPE,
    STATUS_REJECTED_STALE,
    STATUS_REJECTED_STATIC,
    STATUS_SHADOW_REJECTED,
)

# The statuses that ``submit_terminal`` is allowed to write. Anything
# outside this set MUST transit ``submitted`` and go through the normal
# pipeline — the guard keeps ``submit_terminal`` from being repurposed
# to short-circuit gates it was never meant to bypass.
_PRE_SUBMIT_TERMINAL_STATUSES: tuple[str, ...] = (
    STATUS_MALFORMED,
    STATUS_REJECTED_NAME,
    STATUS_REJECTED_ENDPOINT,
    STATUS_REJECTED_SMOKE,
    STATUS_REJECTED_SHAPE,
    STATUS_REJECTED_STALE,
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
        retry_of: str | None = None,
    ) -> str:
        """Insert a new proposal in ``submitted`` state; return its id.

        ``proposed_by`` records provenance ('human' for operator-injected
        rows, 'morgoth' for reflection-born ones). ``engine`` records
        which LLM the spec came from ('ollama' | 'anthropic'). Together
        they are the calibration axis: proposal quality can be sliced by
        ``proposed_by × engine`` for the future security-agent gate.

        ``retry_of`` points at the proposal_id of a preceding pre-submit
        reject that this attempt is correcting (see ``reflect``'s
        retry-with-feedback loop). NULL for first attempts.
        """
        proposal_id = str(_uuid.uuid4())
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO self_modify_proposals
                    (proposal_id, target_path, change_type, content, rationale,
                     status, proposed_by, engine, retry_of)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                _uuid.UUID(proposal_id),
                target_path,
                change_type,
                content,
                rationale,
                STATUS_SUBMITTED,
                proposed_by,
                engine,
                _uuid.UUID(retry_of) if retry_of else None,
            )
        return proposal_id

    async def submit_terminal(
        self,
        *,
        target_path: str,
        change_type: str,
        content: str,
        rationale: str | None,
        status: str,
        status_reason: str,
        proposed_by: str,
        engine: str,
        retry_of: str | None = None,
    ) -> str:
        """Insert a proposal DIRECTLY into a terminal pre-submit status.

        Used only for calibration-worthy pre-submit rejects: ``malformed``,
        ``rejected_smoke``, ``rejected_shape``. The row never transits
        ``submitted`` — this is a single-step write. ``content`` should
        be the rejected spec serialized as JSON so a later reflect run's
        negative-list loader can render it back.

        Guarded to statuses declared in ``PRE_SUBMIT_TERMINAL_STATUSES``
        so it can't be misused to short-circuit the normal pipeline.
        """
        if status not in _PRE_SUBMIT_TERMINAL_STATUSES:
            raise ValueError(
                f"submit_terminal: status {status!r} is not one of "
                f"{_PRE_SUBMIT_TERMINAL_STATUSES}"
            )
        proposal_id = str(_uuid.uuid4())
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO self_modify_proposals
                    (proposal_id, target_path, change_type, content, rationale,
                     status, status_reason, proposed_by, engine, retry_of)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                _uuid.UUID(proposal_id),
                target_path,
                change_type,
                content,
                rationale,
                status,
                status_reason,
                proposed_by,
                engine,
                _uuid.UUID(retry_of) if retry_of else None,
            )
        return proposal_id

    async def list_recent_rejections(
        self,
        *,
        proposed_by: str = "morgoth",
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return recent rejections for the reflect negative-list loader.

        Includes rejects across the four statuses in
        ``NEGATIVE_LIST_STATUSES``: gate-3 operator rejects
        (``rejected``) plus the three pre-submit terminals
        (``malformed``, ``rejected_smoke``, ``rejected_shape``).
        Ordered by ``updated_at`` DESC — most recent judgment first —
        so the negative list stays fresh even when the DB grows.
        """
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM self_modify_proposals "
                "WHERE proposed_by = $1 AND status = ANY($2::text[]) "
                "ORDER BY updated_at DESC LIMIT $3",
                proposed_by,
                list(NEGATIVE_LIST_STATUSES),
                limit,
            )
        return [dict(r) for r in rows]

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

    async def list_shadow_rejected(self, limit: int = 50) -> list[dict[str, Any]]:
        """Rows the delegation hook auto-rejected — audit-only view.
        The default ``morgoth proposals`` view excludes these; the
        ``--all`` flag surfaces them tagged ``[shadow]``."""
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM self_modify_proposals "
                "WHERE status = $1 ORDER BY created_at DESC LIMIT $2",
                STATUS_SHADOW_REJECTED,
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

    async def resolve_id(self, ref: str) -> str:
        """Resolve a proposal reference (full UUID or hex prefix ≥4)
        to the full UUID string.

        The CLI displays 8-char short IDs (``morgoth proposals``).
        Every ID-consuming subcommand used to pass the operator's
        input verbatim to ``uuid.UUID(...)``, which raised
        ``ValueError`` on any short form — six sessions of stalled
        verdicts trace to this. The resolver accepts any hex prefix
        ≥4 chars and disambiguates against the current DB.

        Contract:
          - 0 rows          → ``LookupError("no proposal matching <ref>")``
          - >1 rows         → ``LookupError("ambiguous: <id1>, <id2>, ...")``
          - 1 row           → return the full UUID string
          - <4-char prefix  → ``ValueError`` (too short — an ambiguity
                              risk that grows with the DB)
          - full UUID (36)  → return unchanged (no DB roundtrip needed)

        Uppercase / mixed-case input is normalized to lowercase, and
        surrounding whitespace is stripped, so operator paste from
        the ``morgoth proposals`` table Just Works.
        """
        cleaned = (ref or "").strip().lower()
        if not cleaned:
            raise ValueError("proposal id is empty")
        if len(cleaned) == 36 and cleaned.count("-") == 4:
            return cleaned  # already a full UUID
        # Bare-hex short form.
        if len(cleaned) < 4:
            raise ValueError(
                f"proposal id prefix must be ≥4 hex chars (got {cleaned!r})"
            )
        # Only hex digits + dashes allowed.
        for c in cleaned:
            if c not in "0123456789abcdef-":
                raise ValueError(
                    f"proposal id must be hex/dash (got {cleaned!r})"
                )
        pool = self._pm._require_pool()  # noqa: SLF001
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT proposal_id::text AS pid FROM self_modify_proposals "
                "WHERE proposal_id::text LIKE $1 ORDER BY created_at DESC",
                cleaned + "%",
            )
        if not rows:
            raise LookupError(f"no proposal matching {ref!r}")
        if len(rows) > 1:
            ids = ", ".join(r["pid"][:8] for r in rows[:6])
            more = "" if len(rows) <= 6 else f", ... (+{len(rows) - 6} more)"
            raise LookupError(f"ambiguous: {ids}{more}")
        return rows[0]["pid"]

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
