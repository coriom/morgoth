"""Reddit public API connector tools."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


REDDIT_BASE_URL = "https://www.reddit.com"
USER_AGENT = "MorgothIntelligence/3.0"


class RedditPost(BaseModel):
    """Normalized Reddit post."""

    id: str
    subreddit: str
    title: str
    author: str | None = None
    score: int
    comments: int
    url: str
    permalink: str
    created_utc: float | None = None
    selftext: str = ""


class RedditSearchTool(BaseTool):
    """Search public Reddit posts by query."""

    name = "reddit_search"
    description = "Search public Reddit posts for social sentiment, narratives, and emerging topics."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "subreddit": {"type": "string"},
            "sort": {"type": "string", "enum": ["relevance", "hot", "top", "new", "comments"]},
            "time": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "all"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
        },
        "required": ["query"],
    }

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the tool with app configuration."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0, headers={"User-Agent": USER_AGENT})

    async def close(self) -> None:
        """Close the shared HTTP client."""

        await self._client.aclose()

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Search Reddit and return normalized posts."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        query = str(kwargs["query"]).strip()
        subreddit = self._clean_subreddit(kwargs.get("subreddit"))
        sort = str(kwargs.get("sort", "relevance"))
        time_filter = str(kwargs.get("time", "week"))
        limit = int(kwargs.get("limit", 10))
        path = f"/r/{subreddit}/search.json" if subreddit else "/search.json"
        response = await self._client.get(
            f"{REDDIT_BASE_URL}{path}",
            params={
                "q": query,
                "sort": sort,
                "t": time_filter,
                "limit": limit,
                "restrict_sr": "1" if subreddit else "0",
            },
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        posts = self._extract_posts(response.json(), limit=limit)
        return self.success([post.model_dump() for post in posts], source="reddit", query=query, subreddit=subreddit)

    def _clean_subreddit(self, value: Any) -> str:
        """Normalize subreddit input."""

        if not value:
            return ""
        return str(value).strip().removeprefix("r/").strip("/")

    def _extract_posts(self, payload: dict[str, Any], limit: int) -> list[RedditPost]:
        """Extract normalized posts from a Reddit listing payload."""

        children = payload.get("data", {}).get("children", [])
        posts: list[RedditPost] = []
        for child in children[:limit]:
            data = child.get("data", {})
            posts.append(self._normalize_post(data))
        return posts

    def _normalize_post(self, data: dict[str, Any]) -> RedditPost:
        """Normalize one Reddit post payload."""

        permalink = str(data.get("permalink", ""))
        return RedditPost(
            id=str(data.get("id", "")),
            subreddit=str(data.get("subreddit", "")),
            title=str(data.get("title", "")),
            author=data.get("author"),
            score=int(data.get("score") or 0),
            comments=int(data.get("num_comments") or 0),
            url=str(data.get("url", "")),
            permalink=f"{REDDIT_BASE_URL}{permalink}" if permalink.startswith("/") else permalink,
            created_utc=self._optional_float(data.get("created_utc")),
            selftext=str(data.get("selftext", ""))[:2000],
        )

    def _optional_float(self, value: Any) -> float | None:
        """Convert a value to float when possible."""

        try:
            return float(value)
        except (TypeError, ValueError):
            return None


class RedditSubredditPostsTool(RedditSearchTool):
    """Fetch hot public posts from a subreddit."""

    name = "reddit_subreddit_posts"
    description = "Fetch hot public posts from a subreddit for social monitoring."
    parameters = {
        "type": "object",
        "properties": {
            "subreddit": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
        },
        "required": ["subreddit"],
    }

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Fetch hot subreddit posts and return normalized posts."""

        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        subreddit = self._clean_subreddit(kwargs["subreddit"])
        limit = int(kwargs.get("limit", 10))
        response = await self._client.get(
            f"{REDDIT_BASE_URL}/r/{subreddit}/hot.json",
            params={"limit": limit},
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        posts = self._extract_posts(response.json(), limit=limit)
        return self.success([post.model_dump() for post in posts], source="reddit", subreddit=subreddit)
