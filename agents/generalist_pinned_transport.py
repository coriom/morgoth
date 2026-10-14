"""One-host HTTPS transport for reviewed, disposable public GETs.

The HTTP origin remains the reviewed hostname for Host and TLS verification.
Only the TCP dial is replaced with a previously validated public IP, avoiding
the DNS preflight-to-connect rebinding gap. This is not a generic web client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from typing import Iterable

import certifi
import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


_HTTPX_VERSION = "0.28.1"
_HTTPCORE_VERSION = "1.0.9"


def _public_addresses(rows: list[tuple[object, ...]]) -> tuple[str, ...]:
    """Reject the entire DNS answer if any address is not globally routable."""
    addresses: list[str] = []
    for row in rows:
        try:
            address = str(ipaddress.ip_address(row[4][0]))
            if not ipaddress.ip_address(address).is_global:
                raise ValueError("nonpublic destination")
        except (IndexError, TypeError, ValueError):
            raise httpcore.ConnectError("destination refused") from None
        if address not in addresses:
            addresses.append(address)
    if not addresses or len(addresses) > 32:
        raise httpcore.ConnectError("destination refused")
    return tuple(addresses)


class _PublicAddressBackend(AutoBackend):
    """Resolve once and dial one checked IP while httpcore retains hostname SNI."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.dial_count = 0
        self.attempt_count = 0

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int | bytes]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        """Refuse unreviewed hosts, ports and any private DNS answer."""
        if host != self.host or port != 443 or local_address is not None or self.attempt_count:
            raise httpcore.ConnectError("destination refused")
        self.attempt_count += 1
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port, 0,
                                  socket.SOCK_STREAM, socket.IPPROTO_TCP), timeout=2)
            addresses = _public_addresses(rows)
        except (OSError, TimeoutError):
            raise httpcore.ConnectError("public DNS unavailable") from None
        self.dial_count += 1
        return await super().connect_tcp(addresses[0], port, timeout=timeout,
                                         socket_options=socket_options)

    async def connect_unix_socket(self, *args: object, **kwargs: object) -> httpcore.AsyncNetworkStream:
        """The reviewed read never uses Unix sockets."""
        raise httpcore.ConnectError("destination refused")


class PinnedPublicTransport(httpx.AsyncHTTPTransport):
    """Fail-closed exact-host GET transport pinned to reviewed library versions."""

    def __init__(self, host: str) -> None:
        if httpx.__version__ != _HTTPX_VERSION or httpcore.__version__ != _HTTPCORE_VERSION:
            raise RuntimeError("HTTP transport version not reviewed")
        if not host or host.lower() != host or "." not in host:
            raise ValueError("invalid reviewed host")
        super().__init__(verify=ssl.create_default_context(cafile=certifi.where()),
                         trust_env=False, http2=False, retries=0,
                         limits=httpx.Limits(max_connections=1, max_keepalive_connections=0))
        self.host = host
        self._backend = _PublicAddressBackend(host)
        self.request_count = 0
        # httpx 0.28.1 delegates to this httpcore 1.0.9 pool. No proxy path,
        # alternate transport, connection reuse or second TCP attempt exists.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(cafile=certifi.where()),
            max_connections=1, max_keepalive_connections=0, http1=True,
            http2=False, retries=0, network_backend=self._backend)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Enforce one unauthenticated HTTPS GET to the exact reviewed origin."""
        if (request.method != "GET" or request.url.scheme != "https"
                or request.url.host != self.host or request.url.port not in {None, 443}
                or request.headers.get("authorization") or request.headers.get("cookie")
                or request.headers.get("proxy-authorization")
                or request.extensions.get("sni_hostname") not in {None, self.host}
                or self._backend.attempt_count):
            raise httpx.ConnectError("destination refused", request=request)
        self.request_count += 1
        return await super().handle_async_request(request)
