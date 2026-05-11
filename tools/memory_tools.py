"""Episodic memory tools."""

from __future__ import annotations

from typing import Any

from memory.episodic import EpisodicMemory
from tools.base_tool import BaseTool

VALID_COLLECTIONS = {"conversations", "research", "decisions", "market_patterns", "code_archive"}

COLLECTION_ALIASES = {
    "self": "decisions",
    "autobiographical": "decisions",
    "identity": "decisions",
    "self-description": "decisions",
    "chat": "conversations",
    "history": "conversations",
    "memory": "conversations",
    "code": "code_archive",
    "market": "market_patterns",
}


def resolve_collection(name: str) -> str:
    """Map any LLM-invented collection name to a valid ChromaDB collection."""
    if not name or name.startswith("<"):
        return "conversations"
    if name in VALID_COLLECTIONS:
        return name
    normalized = name.lower().replace("_", "-").replace(" ", "-")
    for alias, target in COLLECTION_ALIASES.items():
        if alias in normalized:
            return target
    return "conversations"


class RememberTool(BaseTool):
    """Store a memory in ChromaDB."""

    name = "remember"
    description = (
        "Store a piece of information in episodic memory. "
        "collection must be one of: conversations, research, decisions, "
        "market_patterns, code_archive"
    )
    parameters = {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "default": "conversations"},
            "content": {"type": "string"},
            "category": {"type": "string"},
            "agent_id": {"type": "string", "default": "morgoth_core"},
            "user_id": {"type": "string", "default": "default"},
        },
        "required": ["content", "category"],
    }

    def __init__(self, episodic_memory: EpisodicMemory) -> None:
        """Initialize the tool with episodic memory."""

        self._episodic_memory = episodic_memory

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Store text in the configured episodic memory collection."""

        collection = resolve_collection(str(kwargs.get("collection", "conversations")))
        document_id = await self._episodic_memory.add_text(
            collection,
            str(kwargs["content"]),
            category=str(kwargs["category"]),
            agent_id=str(kwargs.get("agent_id", "morgoth_core")),
            user_id=str(kwargs.get("user_id", "default")),
        )
        return self.success({"document_id": document_id})


class RecallTool(BaseTool):
    """Recall similar memories from ChromaDB."""

    name = "recall"
    description = (
        "Search episodic memory for relevant entries. "
        "collection must be one of: conversations, research, decisions, "
        "market_patterns, code_archive. Leave empty to search all."
    )
    parameters = {
        "type": "object",
        "properties": {
            "collection": {"type": "string", "default": "conversations"},
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, episodic_memory: EpisodicMemory) -> None:
        """Initialize the tool with episodic memory."""

        self._episodic_memory = episodic_memory

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Search episodic memory for semantically similar entries."""

        collection = resolve_collection(str(kwargs.get("collection", "conversations")))
        matches = await self._episodic_memory.query(
            collection,
            str(kwargs["query"]),
            limit=int(kwargs.get("limit", 5)),
        )
        return self.success([match.model_dump() for match in matches])
