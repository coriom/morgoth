"""Telegram notification backend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from loguru import logger

from core.config import AppConfig


class TelegramNotifier:
    """Telegram notifier with per-level rate limiting."""

    def __init__(self, config: AppConfig, client: httpx.AsyncClient | None = None) -> None:
        """Initialize the notifier."""

        self._config = config
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._last_sent: dict[str, datetime] = {}

    async def close(self) -> None:
        """Close the underlying HTTP client."""

        await self._client.aclose()

    async def send(self, level: str, content: str) -> bool:
        """Send a notification if credentials are configured and rate limit allows it."""

        if not self._config.telegram_bot_token or not self._config.telegram_chat_id:
            logger.warning("Telegram credentials not configured; notification skipped")
            return False

        level = level.upper()
        now = datetime.now(timezone.utc)
        previous = self._last_sent.get(level)
        if previous is not None and now - previous < timedelta(minutes=1):
            logger.debug("Telegram notification for level '{}' skipped due to rate limit", level)
            return False

        payload = {
            "chat_id": self._config.telegram_chat_id,
            "text": f"[MORGOTH/{level}] {content}",
        }
        response = await self._client.post(
            f"https://api.telegram.org/bot{self._config.telegram_bot_token}/sendMessage",
            json=payload,
        )
        response.raise_for_status()
        self._last_sent[level] = now
        return True
