"""Focused tests for Phase 2b backend route aggregation helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from api.routes.consciousness import build_clusters, build_concept_graph
from api.routes.evolution import build_timeline_points


def test_build_clusters_groups_recent_thoughts() -> None:
    """Thought clusters should count repeated extracted topics."""

    logs = [
        {"level": "THOUGHT", "content": "Refactor api endpoint and frontend schema alignment."},
        {"level": "THOUGHT", "content": "Frontend schema review for websocket endpoint."},
        {"level": "RESULT", "content": "ignore me"},
    ]

    clusters = build_clusters(logs)

    topics = {cluster["topic"] for cluster in clusters}
    assert {"frontend", "schema", "endpoint"} & topics
    assert any(cluster["color_domain"] == "code" for cluster in clusters)
    assert all(cluster["count"] >= 1 for cluster in clusters)


def test_build_concept_graph_tracks_cooccurrence() -> None:
    """Co-occurring thought topics should produce graph edges."""

    logs = [
        {"level": "THOUGHT", "content": "Research knowledge graph connections and research signals."},
        {"level": "THOUGHT", "content": "Knowledge graph update for research memory."},
    ]

    graph = build_concept_graph(logs)

    assert any(node["id"] == "research" for node in graph["nodes"])
    assert any(
        {edge["source"], edge["target"]} == {"research", "knowledge"}
        for edge in graph["edges"]
    )


def test_build_timeline_points_is_cumulative() -> None:
    """Timeline points should accumulate counts over days."""

    points = build_timeline_points(
        self_modifications=[
            {"timestamp": datetime(2026, 4, 14, tzinfo=timezone.utc), "file_path": "tools/new_tool.py"},
            {"timestamp": datetime(2026, 4, 15, tzinfo=timezone.utc), "file_path": "agents/new_agent.py"},
        ],
        agents=[{"created_at": datetime(2026, 4, 15, tzinfo=timezone.utc)}],
        knowledge=[{"created_at": datetime(2026, 4, 14, tzinfo=timezone.utc), "updated_at": None}],
    )

    assert points[0]["self_mods"] == 1
    assert points[0]["tools"] == 1
    assert points[0]["knowledge"] == 1
    assert points[1]["self_mods"] == 2
    assert points[1]["agents"] == 1
