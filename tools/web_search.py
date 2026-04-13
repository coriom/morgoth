"""DuckDuckGo-backed web search tool."""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel, Field

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


class SearchItem(BaseModel):
    """One web search result."""

    title: str
    snippet: str
    url: str


class WebSearchTool(BaseTool):
    """Search the web using DuckDuckGo's public endpoint."""

    name = "web_search"
    description = "Search the web for general information using DuckDuckGo."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Search the web and return normalized results."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        query = str(kwargs["query"]).strip()
        max_results = int(kwargs.get("max_results", 5))
        logger.debug("Running web search for '{}'", query)

        response = await self._client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
        )
        response.raise_for_status()
        payload = response.json()

        items = self._extract_results(payload, limit=max_results)
        return self.success([item.model_dump() for item in items], query=query, source="duckduckgo")

    def _extract_results(self, payload: dict[str, Any], limit: int) -> list[SearchItem]:
        """Extract normalized results from the DuckDuckGo payload."""

        items: list[SearchItem] = []
        abstract_text = payload.get("AbstractText")
        abstract_url = payload.get("AbstractURL")
        heading = payload.get("Heading")
        if abstract_text and abstract_url:
            items.append(SearchItem(title=heading or "DuckDuckGo", snippet=abstract_text, url=abstract_url))

        for topic in payload.get("RelatedTopics", []):
            if "Topics" in topic:
                nested = topic["Topics"]
            else:
                nested = [topic]
            for nested_topic in nested:
                text = nested_topic.get("Text")
                url = nested_topic.get("FirstURL")
                if text and url:
                    items.append(SearchItem(title=text.split(" - ")[0], snippet=text, url=url))
                if len(items) >= limit:
                    return items[:limit]

        return items[:limit]
