"""Consciousness aggregation API routes."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, Request


router = APIRouter(prefix="/api/consciousness", tags=["consciousness"])

_STOP_WORDS = {
    "about",
    "after",
    "agent",
    "all",
    "also",
    "and",
    "been",
    "before",
    "between",
    "build",
    "cache",
    "check",
    "code",
    "data",
    "error",
    "from",
    "into",
    "more",
    "morgoth",
    "need",
    "next",
    "that",
    "their",
    "them",
    "then",
    "they",
    "this",
    "tool",
    "update",
    "with",
}
_DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "code": {"code", "python", "refactor", "api", "endpoint", "schema", "test", "typescript", "frontend", "backend"},
    "research": {"research", "investigate", "learn", "analysis", "knowledge", "memory", "query", "topic", "signal"},
    "finance": {"market", "price", "trade", "trading", "token", "bitcoin", "eth", "btc", "finance", "macro"},
    "system": {"system", "runtime", "health", "agent", "scheduler", "process", "monitor", "websocket", "config"},
}


def _tokenize(content: str) -> list[str]:
    """Split free-form thought content into meaningful lowercase tokens."""

    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", content.lower())
    return [token for token in tokens if token not in _STOP_WORDS]


def _classify_domain(topic: str) -> str:
    """Map a topic token to a UI color domain."""

    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if topic in keywords:
            return domain
    return "other"


def _extract_topics(content: str, *, limit: int = 3) -> list[str]:
    """Return the strongest topic tokens for a single thought."""

    ranked = Counter(_tokenize(content))
    return [topic for topic, _count in ranked.most_common(limit)]


def build_clusters(logs: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    """Aggregate recent THOUGHT logs into topic clusters."""

    counts: Counter[str] = Counter()
    latest_thought: dict[str, str] = {}

    for log in logs:
        if str(log.get("level", "")).upper() != "THOUGHT":
            continue

        content = str(log.get("content", "")).strip()
        topics = _extract_topics(content)
        for topic in topics:
            counts[topic] += 1
            latest_thought[topic] = content

    clusters = [
        {
            "topic": topic,
            "count": count,
            "last_thought": latest_thought.get(topic, ""),
            "color_domain": _classify_domain(topic),
        }
        for topic, count in counts.most_common(limit)
    ]
    return clusters


def build_concept_graph(logs: list[dict[str, Any]], *, node_limit: int = 20) -> dict[str, list[dict[str, Any]]]:
    """Build a concept co-occurrence graph from recent THOUGHT logs."""

    counts: Counter[str] = Counter()
    edges: Counter[tuple[str, str]] = Counter()

    for log in logs:
        if str(log.get("level", "")).upper() != "THOUGHT":
            continue

        topics = list(dict.fromkeys(_extract_topics(str(log.get("content", "")), limit=4)))
        counts.update(topics)

        for index, source in enumerate(topics):
            for target in topics[index + 1 :]:
                edge = tuple(sorted((source, target)))
                edges[edge] += 1

    top_topics = {topic for topic, _count in counts.most_common(node_limit)}
    nodes = [
        {
            "id": topic,
            "label": topic.title(),
            "domain": _classify_domain(topic),
            "weight": count,
        }
        for topic, count in counts.most_common(node_limit)
    ]
    graph_edges = [
        {"source": source, "target": target, "weight": weight}
        for (source, target), weight in edges.most_common()
        if source in top_topics and target in top_topics
    ]
    return {"nodes": nodes, "edges": graph_edges}


@router.get("/clusters")
async def get_clusters(request: Request, limit: int = 12, log_limit: int = 250) -> dict[str, Any]:
    """Return recent thought clusters aggregated from log content."""

    logs = await request.app.state.persistent_memory.list_logs(limit=log_limit)
    return {"clusters": build_clusters(logs, limit=limit)}


@router.get("/concepts")
async def get_concepts(request: Request, log_limit: int = 250) -> dict[str, Any]:
    """Return a concept co-occurrence graph from recent thought logs."""

    logs = await request.app.state.persistent_memory.list_logs(limit=log_limit)
    return build_concept_graph(logs)
