"""Morgoth-authored data-feed tool: get_deribit_btc_perpetual.

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
from self_modify.digest_path import resolve_digest_fields


_BASE_URL = 'https://www.deribit.com'
_ENDPOINT_PATH = '/api/v2/public/ticker?instrument_name=BTC-PERPETUAL'
_SOURCE_LABEL = 'www.deribit.com'
_TOOL_DESCRIPTION = "Deribit BTC-PERPETUAL ticker (inverse perp: contracts priced in USD, settled in BTC). Prices (mark, index, last, bid/ask, 24h high/low) in USD per BTC; open_interest_usd in USD notional; volume_24h_btc in BTC (base currency); funding_current_rate and funding_8h_rate are decimal fractions (0.0001 = 0.01 percent), as reported by Deribit's current_funding and funding_8h fields."
# ONE EXTRACTOR (2026-09-29): _DIGEST_FIELDS carries the full
# {name, path} entries so runtime resolution goes through
# self_modify.digest_path.resolve_digest_fields — the SAME resolver
# the liveness probe and the shadow sampler use. Never re-implement.
_DIGEST_FIELDS = [{'name': 'mark_price_usd', 'path': 'result.mark_price'}, {'name': 'index_price_usd', 'path': 'result.index_price'}, {'name': 'open_interest_usd', 'path': 'result.open_interest'}, {'name': 'funding_current_rate', 'path': 'result.current_funding'}, {'name': 'funding_8h_rate', 'path': 'result.funding_8h'}, {'name': 'last_price_usd', 'path': 'result.last_price'}, {'name': 'best_bid_usd', 'path': 'result.best_bid_price'}, {'name': 'best_ask_usd', 'path': 'result.best_ask_price'}, {'name': 'volume_24h_btc', 'path': 'result.stats.volume'}, {'name': 'high_24h_usd', 'path': 'result.stats.high'}, {'name': 'low_24h_usd', 'path': 'result.stats.low'}]
# Keyed-API block: populated when the spec declared a requires_key.
# The env var NAME is baked into the module; the VALUE is fetched
# via os.getenv AT RUNTIME so a key rotation needs no redeploy and
# the value never appears in git, logs, or LLM context.
_REQUIRES_KEY_ENV = None   # None if the tool is keyless
_KEY_IN = None                       # "query" | "header" | None
_KEY_PARAM = None                 # e.g. "api_key" or "X-API-Key" | None


class GetDeribitBtcPerpetualTool(BaseTool):
    __doc__ = _TOOL_DESCRIPTION

    name = 'get_deribit_btc_perpetual'
    is_data_source = True
    # Declared endpoints for the duplication gate on FUTURE reflect
    # runs. Normalized form: host+path, no scheme, no query, no
    # trailing slash. Derived deterministically from the spec so the
    # next model can see this tool's endpoint in the reflect registry.
    api_endpoints = ('www.deribit.com/api/v2/public/ticker',)
    # Class attribute stays a tuple of NAMES for identity consumers
    # (learned_served_phrases, _registered_digest_fields, `morgoth
    # show`). Path info lives in _DIGEST_FIELDS at module scope and
    # is used only by execute()'s resolver call — the class attribute
    # is the identity surface, the module var is the extraction surface.
    digest_fields = tuple(_e["name"] if isinstance(_e, dict) else _e
                          for _e in _DIGEST_FIELDS)
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
        # 2026-09-29: ONE EXTRACTOR. The tool, the liveness probe, and
        # the shadow sampler share this resolver — no re-implementation.
        # 2026-09-30 RESILIENT PARTIAL: a SINGLE missing field must not
        # fail the whole call — return the resolved fields with an
        # explicit `missing` list in metadata. Fail hard only when
        # NOTHING resolves. Design-time strictness lives at the
        # liveness gate (a field null across every hit still rejects
        # the proposal at review time).
        record, errors, _meta = resolve_digest_fields(_DIGEST_FIELDS, data)
        missing = [n for n, _ in errors]
        if not record:
            head = "; ".join(f"{n}: {m}" for n, m in errors[:3]) or "no fields resolved"
            return self.failure(
                f"{_SOURCE_LABEL}: digest resolve failed — {head}",
                source=_SOURCE_LABEL,
            )

        fetched_at = datetime.now(timezone.utc).isoformat()
        return self.success(
            record, source=_SOURCE_LABEL, fetched_at=fetched_at,
            missing=missing,
        )
