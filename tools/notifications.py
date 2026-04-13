"""Notification tool wrappers."""

from __future__ import annotations

from typing import Any, Protocol

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


class Notifier(Protocol):
    """Protocol for notification backends."""

    async def send(self, level: str, content: str) -> bool:
        """Send a notification message."""


class NotifyTool(BaseTool):
    """Send notifications through the configured notifier."""

    name = "notify"
    description = "Send a notification through the configured channels."
    parameters = {
        "type": "object",
        "properties": {
            "level": {"type": "string", "default": "INFO"},
            "content": {"type": "string"},
        },
        "required": ["content"],
    }

    def __init__(self, config: AppConfig, notifier: Notifier) -> None:
        """Initialize the tool with config and notifier."""

        self._config = config
        self._notifier = notifier

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Send a notification if enabled by permissions."""

        if not self._config.permissions.permissions.can_send_notifications:
            raise PermissionDeniedError("Notifications are disabled by permissions")

        level = str(kwargs.get("level", "INFO")).upper()
        content = str(kwargs["content"])
        delivered = await self._notifier.send(level, content)
        return self.success({"delivered": delivered, "level": level, "content": content})
