"""Evolution metrics and timeline API routes."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/evolution", tags=["evolution"])


def build_timeline_points(
    self_modifications: list[dict[str, Any]],
    agents: list[dict[str, Any]],
    knowledge: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build cumulative daily growth points across the tracked datasets."""

    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "self_mods": 0,
            "tools": 0,
            "agents": 0,
            "knowledge": 0,
        }
    )

    for item in self_modifications:
        timestamp = item.get("timestamp")
        if not isinstance(timestamp, datetime):
            continue
        key = timestamp.date().isoformat()
        buckets[key]["self_mods"] += 1
        if str(item.get("file_path", "")).startswith("tools/"):
            buckets[key]["tools"] += 1

    for item in agents:
        timestamp = item.get("created_at")
        if not isinstance(timestamp, datetime):
            continue
        key = timestamp.date().isoformat()
        buckets[key]["agents"] += 1

    for item in knowledge:
        timestamp = item.get("created_at") or item.get("updated_at")
        if not isinstance(timestamp, datetime):
            continue
        key = timestamp.date().isoformat()
        buckets[key]["knowledge"] += 1

    totals = {"self_mods": 0, "tools": 0, "agents": 0, "knowledge": 0}
    points: list[dict[str, Any]] = []
    for day in sorted(buckets):
        day_counts = buckets[day]
        for key in totals:
            totals[key] += day_counts[key]
        points.append(
            {
                "timestamp": f"{day}T00:00:00+00:00",
                "self_mods": totals["self_mods"],
                "tools": totals["tools"],
                "agents": totals["agents"],
                "knowledge": totals["knowledge"],
            }
        )

    return points


@router.get("/metrics")
async def get_metrics(request: Request) -> dict[str, int]:
    """Return aggregate growth counters for the evolution page."""

    persistent_memory = request.app.state.persistent_memory
    self_modifications = await persistent_memory.fetch("SELECT file_path FROM self_modifications")
    agents = await persistent_memory.fetch("SELECT agent_id FROM agents")
    knowledge = await persistent_memory.fetch("SELECT fact_id FROM knowledge")

    return {
        "self_modifications": len(self_modifications),
        "tools_created": sum(1 for row in self_modifications if str(row["file_path"]).startswith("tools/")),
        "agents_spawned": len(agents),
        "knowledge_nodes": len(knowledge),
    }


@router.get("/timeline")
async def get_timeline(request: Request) -> dict[str, Any]:
    """Return cumulative daily growth data."""

    persistent_memory = request.app.state.persistent_memory
    self_modifications = [dict(row) for row in await persistent_memory.fetch("SELECT timestamp, file_path FROM self_modifications")]
    agents = [dict(row) for row in await persistent_memory.fetch("SELECT created_at FROM agents")]
    knowledge = [dict(row) for row in await persistent_memory.fetch("SELECT created_at, updated_at FROM knowledge")]
    return {"points": build_timeline_points(self_modifications, agents, knowledge)}
