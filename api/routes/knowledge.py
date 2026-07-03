"""Read-only API exposing accumulated theses and detected contradictions.

Theses and contradictions are TRANSVERSAL to objectives (a contradiction
links two theses from different objectives), so they live under their own
endpoints rather than nested under /api/objectives.

Both endpoints are GET-only. Response models are permissive (extra fields
tolerated, optional where the LLM-produced data may vary) so a schema drift
in the underlying tables does not surface as a 500 — that lesson was learned
from the prior /api/objectives ObjectiveEvidence bug.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


router = APIRouter(prefix="/api", tags=["knowledge"])


class Thesis(BaseModel):
    """A single extracted thesis. Permissive on extras."""

    model_config = ConfigDict(extra="allow")

    thesis_id: str | None = None
    subject: str | None = None
    claim: str | None = None
    confidence: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    status: str | None = None
    objective_id: str | None = None
    created_at: datetime | None = None


class Contradiction(BaseModel):
    """A contradiction with its two theses pre-resolved.

    thesis_a / thesis_b may be None if the referenced thesis row has been
    deleted — the LEFT JOIN preserves the contradiction record itself.
    """

    model_config = ConfigDict(extra="allow")

    contradiction_id: str | None = None
    subject_group: str | None = None
    detected_at: datetime | None = None
    thesis_a: Thesis | None = None
    thesis_b: Thesis | None = None


def _normalize_thesis_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw asyncpg row dict into Thesis-friendly shape.

    asyncpg returns JSONB columns as a Python str, not a parsed list — same
    handling pattern as memory/persistent.py uses for sources_used.
    """
    out = dict(row)
    if out.get("thesis_id") is not None:
        out["thesis_id"] = str(out["thesis_id"])
    if out.get("objective_id") is not None:
        out["objective_id"] = str(out["objective_id"])
    evidence = out.get("evidence")
    if evidence is None:
        out["evidence"] = []
    elif isinstance(evidence, str):
        try:
            out["evidence"] = json.loads(evidence)
        except (ValueError, TypeError):
            out["evidence"] = []
    return out


def _row_to_contradiction(row: dict[str, Any]) -> Contradiction:
    """Build a Contradiction from a JOINed row produced by get_contradictions."""

    def _side(suffix: str) -> Thesis | None:
        # If the JOINed thesis was deleted, all _a/_b fields will be None.
        if row.get(f"thesis_id_{suffix}") is None and row.get(f"subject_{suffix}") is None:
            return None
        side_row = {
            "thesis_id": row.get(f"thesis_id_{suffix}"),
            "subject": row.get(f"subject_{suffix}"),
            "claim": row.get(f"claim_{suffix}"),
            "confidence": row.get(f"confidence_{suffix}"),
            "evidence": row.get(f"evidence_{suffix}") or [],
            "status": row.get(f"status_{suffix}"),
            "objective_id": row.get(f"objective_id_{suffix}"),
            "created_at": row.get(f"created_at_{suffix}"),
        }
        return Thesis.model_validate(_normalize_thesis_dict(side_row))

    return Contradiction(
        contradiction_id=str(row["contradiction_id"]) if row.get("contradiction_id") else None,
        subject_group=row.get("subject_group"),
        detected_at=row.get("detected_at"),
        thesis_a=_side("a"),
        thesis_b=_side("b"),
    )


@router.get("/theses")
async def list_theses(
    request: Request,
    status: str | None = None,
    subject: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return theses (newest first), optionally filtered by status and/or subject substring."""
    try:
        rows = await request.app.state.persistent_memory.get_theses(
            status=status, subject=subject, limit=limit
        )
    except Exception as exc:
        logger.exception("GET /api/theses failed")
        raise HTTPException(status_code=500, detail=f"theses read failed: {type(exc).__name__}")
    items = [Thesis.model_validate(_normalize_thesis_dict(r)).model_dump(mode="json") for r in rows]
    return {"items": items}


@router.get("/contradictions")
async def list_contradictions(
    request: Request,
    limit: int = 100,
    include_resolved: bool = False,
) -> dict[str, Any]:
    """Return contradictions (newest first) with their two theses pre-resolved.

    Default: only UNRESOLVED pairs (resolution IS NULL). Pass
    ``include_resolved=true`` to see every row including those voided by
    the remediation script.
    """
    try:
        rows = await request.app.state.persistent_memory.get_contradictions(
            limit=limit, unresolved_only=not include_resolved
        )
    except Exception as exc:
        logger.exception("GET /api/contradictions failed")
        raise HTTPException(
            status_code=500, detail=f"contradictions read failed: {type(exc).__name__}"
        )
    items = [_row_to_contradiction(r).model_dump(mode="json") for r in rows]
    return {"items": items}
