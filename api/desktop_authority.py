"""Desktop-only ASGI boundary for the historical research application.

The native client owns the Project token. The WebView has no direct HTTP or
WebSocket authority over a Desktop research engine.
"""
from __future__ import annotations

import asyncio
import hmac
from typing import Any, Awaitable, Callable

from starlette.datastructures import State
from starlette.responses import PlainTextResponse

from api.token import HEADER_NAME, read_ui_token


ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[Any]]], Awaitable[None]]


class DesktopAuthorityMiddleware:
    """Authenticate every Desktop HTTP request before CORS or route dispatch."""

    def __init__(self, app: ASGIApp, *, runtime_state: State) -> None:
        self.app = app
        self.runtime_state = runtime_state

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Awaitable[Any]],
                       send: Callable[..., Awaitable[Any]]) -> None:
        kind = scope["type"]
        if not getattr(self.runtime_state, "desktop_runtime", False) or kind not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if kind == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return

        headers = scope.get("headers", ())
        values: dict[bytes, list[bytes]] = {}
        for key, value in headers:
            lowered = key.lower()
            if lowered in {b"host", b"origin", HEADER_NAME.lower().encode("ascii")}:
                values.setdefault(lowered, []).append(value)

        port = getattr(self.runtime_state, "desktop_expected_port", None)
        expected_host = f"127.0.0.1:{port}".encode("ascii") if type(port) is int and 1 <= port <= 65535 else None
        if (expected_host is None or values.get(b"host") != [expected_host]
                or b"origin" in values):
            await PlainTextResponse("Forbidden", status_code=403, headers={"Cache-Control": "no-store"})(scope, receive, send)
            return

        supplied = values.get(HEADER_NAME.lower().encode("ascii"), [])
        if len(supplied) != 1 or not supplied[0] or len(supplied[0]) > 256:
            await self._unauthorized(scope, receive, send)
            return
        try:
            expected = await asyncio.to_thread(read_ui_token)
        except (OSError, UnicodeError, ValueError):
            expected = None
        if not expected or not hmac.compare_digest(supplied[0], expected.encode("utf-8")):
            await self._unauthorized(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _unauthorized(scope: dict[str, Any], receive: Callable[..., Awaitable[Any]],
                            send: Callable[..., Awaitable[Any]]) -> None:
        await PlainTextResponse("Unauthorized", status_code=401, headers={"Cache-Control": "no-store"})(scope, receive, send)
