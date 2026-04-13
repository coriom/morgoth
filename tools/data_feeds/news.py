"""RSS news tool."""

from __future__ import annotations

import asyncio
from typing import Any

import feedparser
import httpx
from pydantic import BaseModel

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


DEFAULT_NEWS_FEEDS = {
    "crypto": "https://cointelegraph.com/rss",
    "finance": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "general": "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
}


class NewsItem(BaseModel):
    """Normalized RSS news item."""

    title: str
    link: str
    summary: str


class GetNewsTool(BaseTool):
    """Fetch the latest news entries from RSS feeds."""

    name = "get_news"
    description = "Fetch news items from RSS feeds by topic."
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "default": "general"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": [],
    }

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch and parse RSS feed items."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        topic = str(kwargs.get("topic", "general")).lower()
        limit = int(kwargs.get("limit", 5))
        feed_url = DEFAULT_NEWS_FEEDS.get(topic, DEFAULT_NEWS_FEEDS["general"])

        response = await self._client.get(feed_url)
        response.raise_for_status()
        feed = await asyncio.to_thread(feedparser.parse, response.text)
        items = [
            NewsItem(title=entry.get("title", ""), link=entry.get("link", ""), summary=entry.get("summary", ""))
            for entry in feed.entries[:limit]
        ]
        return self.success([item.model_dump() for item in items], source=feed_url, topic=topic)
