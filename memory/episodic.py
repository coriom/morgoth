"""Async wrapper around ChromaDB for episodic memory."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import chromadb
from chromadb.api.models.Collection import Collection
from loguru import logger
from pydantic import BaseModel, Field


DEFAULT_COLLECTIONS = (
    "conversations",
    "research",
    "decisions",
    "market_patterns",
    "code_archive",
)


class EpisodicMetadata(BaseModel):
    """Required metadata for every episodic memory document."""

    timestamp: str
    agent_id: str
    user_id: str = "default"
    category: str


class EpisodicDocument(BaseModel):
    """Document written to a ChromaDB collection."""

    document_id: str = Field(default_factory=lambda: str(uuid4()))
    content: str
    metadata: EpisodicMetadata


class QueryMatch(BaseModel):
    """Single document returned from a similarity query."""

    document_id: str
    content: str
    metadata: EpisodicMetadata
    distance: float | None = None


class EpisodicMemory:
    """Persistent ChromaDB-backed episodic memory service."""

    def __init__(self, persist_directory: str | Path = "data/chroma_db") -> None:
        """Initialize the memory wrapper."""

        self._persist_directory = Path(persist_directory)
        self._client: chromadb.PersistentClient | None = None
        self._collections: dict[str, Collection] = {}

    @property
    def collections(self) -> tuple[str, ...]:
        """Return the collection names managed by this wrapper."""

        return DEFAULT_COLLECTIONS

    async def initialize(self) -> None:
        """Create the persistent client and ensure all collections exist."""

        await asyncio.to_thread(self._persist_directory.mkdir, parents=True, exist_ok=True)
        self._client = await asyncio.to_thread(chromadb.PersistentClient, path=str(self._persist_directory))

        for collection_name in self.collections:
            collection = await asyncio.to_thread(self._client.get_or_create_collection, collection_name)
            self._collections[collection_name] = collection

        logger.info("Initialized ChromaDB with {} collections", len(self._collections))

    async def add_document(self, collection_name: str, document: EpisodicDocument) -> str:
        """Insert a document into one of the managed collections."""

        collection = self._get_collection(collection_name)
        await asyncio.to_thread(
            collection.add,
            ids=[document.document_id],
            documents=[document.content],
            metadatas=[document.metadata.model_dump()],
        )
        logger.debug("Stored episodic document '{}' in '{}'", document.document_id, collection_name)
        return document.document_id

    async def add_text(
        self,
        collection_name: str,
        content: str,
        *,
        category: str,
        agent_id: str,
        user_id: str = "default",
    ) -> str:
        """Create and store an episodic document from raw text."""

        document = EpisodicDocument(
            content=content,
            metadata=EpisodicMetadata(
                timestamp=datetime.now(timezone.utc).isoformat(),
                agent_id=agent_id,
                user_id=user_id,
                category=category,
            ),
        )
        return await self.add_document(collection_name, document)

    async def query(
        self,
        collection_name: str,
        query_text: str,
        *,
        limit: int = 5,
    ) -> list[QueryMatch]:
        """Query similar documents from a collection."""

        collection = self._get_collection(collection_name)
        result = await asyncio.to_thread(collection.query, query_texts=[query_text], n_results=limit)
        return self._parse_query_result(result)

    async def get_document(self, collection_name: str, document_id: str) -> QueryMatch | None:
        """Fetch a document by id from a collection."""

        collection = self._get_collection(collection_name)
        result = await asyncio.to_thread(collection.get, ids=[document_id], include=["documents", "metadatas"])

        ids = result.get("ids", [])
        if not ids:
            return None

        return QueryMatch(
            document_id=ids[0],
            content=result["documents"][0],
            metadata=EpisodicMetadata.model_validate(result["metadatas"][0]),
        )

    async def list_recent(self, collection_name: str, limit: int = 20) -> list[QueryMatch]:
        """Return the most recently stored documents from a collection."""

        collection = self._get_collection(collection_name)
        result = await asyncio.to_thread(collection.get, include=["documents", "metadatas"])

        ids = result.get("ids", [])
        documents = result.get("documents", [])
        metadatas = result.get("metadatas", [])
        rows = [
            QueryMatch(
                document_id=document_id,
                content=documents[index],
                metadata=EpisodicMetadata.model_validate(metadatas[index]),
            )
            for index, document_id in enumerate(ids)
        ]
        rows.sort(key=lambda item: item.metadata.timestamp, reverse=True)
        return rows[:limit]

    def _get_collection(self, collection_name: str) -> Collection:
        """Return a managed collection or raise a descriptive error."""

        if collection_name not in self._collections:
            raise ValueError(f"Unknown episodic collection: {collection_name}")
        return self._collections[collection_name]

    def _parse_query_result(self, result: dict[str, Any]) -> list[QueryMatch]:
        """Normalize a ChromaDB query response."""

        ids = result.get("ids", [[]])[0]
        documents = result.get("documents", [[]])[0]
        metadatas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0] if result.get("distances") else []

        matches: list[QueryMatch] = []
        for index, document_id in enumerate(ids):
            metadata = EpisodicMetadata.model_validate(metadatas[index])
            distance = distances[index] if index < len(distances) else None
            matches.append(
                QueryMatch(
                    document_id=document_id,
                    content=documents[index],
                    metadata=metadata,
                    distance=distance,
                )
            )
        return matches
