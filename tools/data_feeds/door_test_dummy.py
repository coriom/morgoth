"""Door-test dummy — trivial no-op tool for step 2 validation.

is_data_source=False so it never joins the source rail. is_chat_tool
defaults True so it appears in /api/tools + CHAT_TOOL_NAMES for the few
minutes this tool exists before being reverted.
"""

from __future__ import annotations

from typing import Any

from tools.base_tool import BaseTool


class DoorTestDummyTool(BaseTool):
    """No-op tool used only by the step-2 door test."""

    name = "door_test_dummy"
    is_data_source = False
    description = "no-op tool used by the self-modify door test"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, config: Any) -> None:
        self._config = config

    async def execute(self, **kwargs: Any) -> dict[str, Any]:
        return self.success({"ping": "pong"})
