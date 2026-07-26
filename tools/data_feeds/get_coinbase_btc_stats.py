"""Morgoth-authored data-feed tool: get_coinbase_btc_stats.

Auto-generated from a spec via self_modify.reflect. See
self_modify_proposals for the proposal row (proposed_by='morgoth').
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


_BASE_URL = 'https://api.exchange.coinbase.com'
_ENDPOINT_PATH = '/products/BTC-USD/stats'
_SOURCE_LABEL = 'api.exchange.coinbase.com'
_TOOL_DESCRIPTION = 'Fetch Coinbase Exchange 24h open/high/low/last plus 24h and 30-day trading volume for BTC-USD to track US-regulated spot exchange activity distinct from Binance-based feeds.'
_DIGEST_FIELDS = ['open', 'high', 'low', 'last', 'volume', 'volume_30day']
# Keyed-API block: populated when the spec declared a requires_key.
# The env var NAME is baked into the module; the VALUE is fetched
# via os.getenv AT RUNTIME so a key rotation needs no redeploy and
# the value never appears in git, logs, or LLM context.
_REQUIRES_KEY_ENV = None   # None if the tool is keyless
_KEY_IN = None                       # "query" | "header" | None
_KEY_PARAM = None                 # e.g. "api_key" or "X-API-Key" | None


class GetCoinbaseBtcStatsTool(BaseTool):
    __doc__ = _TOOL_DESCRIPTION

    name = 'get_coinbase_btc_stats'
    is_data_source = True
    # Declared endpoints for the duplication gate on FUTURE reflect
    # runs. Normalized form: host+path, no scheme, no query, no
    # trailing slash. Derived deterministically from the spec so the
    # next model can see this tool's endpoint in the reflect registry.
    api_endpoints = ('api.exchange.coinbase.com/products/btc-usd/stats',)
    digest_fields = tuple(_DIGEST_FIELDS)
    description = _TOOL_DESCRIPTION
    parameters = {"type": "object", "properties": {}}

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _key_kwargs(self) -> tuple[dict[str, Any], str | None]:
        """Build the params/headers kwargs for the request. Returns
        ({params_or_headers_kwargs}, key_value) so the caller can
        scrub the key from any error message before returning it.
        Empty dict + None for keyless tools."""
        if not _REQUIRES_KEY_ENV:
            return {}, None
        key = os.getenv(_REQUIRES_KEY_ENV, "").strip()
        if not key:
            return {}, ""  # sentinel: env var declared but not set
        if _KEY_IN == "query":
            return {"params": {_KEY_PARAM: key}}, key
        return {"headers": {_KEY_PARAM: key}}, key

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        req_kwargs, key_val = self._key_kwargs()
        if _REQUIRES_KEY_ENV and key_val == "":
            return self.failure(
                f"env var {_REQUIRES_KEY_ENV} is required but not set",
                source=_SOURCE_LABEL,
            )

        try:
            resp = await self._client.get(_BASE_URL + _ENDPOINT_PATH, **req_kwargs)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            msg = str(exc)
            if key_val:
                # Redact the key from any surfaced error string. httpx's
                # HTTPStatusError embeds request.url which includes query
                # params; the key must never appear in logs.
                msg = msg.replace(key_val, "***REDACTED***")
            return self.failure(
                f"{_SOURCE_LABEL} request failed: {msg}",
                source=_SOURCE_LABEL,
            )

        data = resp.json()
        # Best-effort digest: pick the requested fields from a top-level dict
        # OR from the first entry of a top-level list (mirrors the fear_greed
        # pattern for {"data": [...]} shaped responses).
        record: dict[str, Any] = {}
        if isinstance(data, dict):
            for key in _DIGEST_FIELDS:
                if key in data:
                    record[key] = data[key]
            if not record and isinstance(data.get("data"), list) and data["data"]:
                first = data["data"][0]
                if isinstance(first, dict):
                    for key in _DIGEST_FIELDS:
                        if key in first:
                            record[key] = first[key]
        elif isinstance(data, list) and data and isinstance(data[0], dict):
            for key in _DIGEST_FIELDS:
                if key in data[0]:
                    record[key] = data[0][key]
        if not record:
            return self.failure(
                "response did not contain any of the expected digest fields",
                source=_SOURCE_LABEL,
            )

        fetched_at = datetime.now(timezone.utc).isoformat()
        return self.success(record, source=_SOURCE_LABEL, fetched_at=fetched_at)
