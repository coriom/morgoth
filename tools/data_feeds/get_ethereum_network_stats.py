"""Morgoth-authored data-feed tool: get_ethereum_network_stats.

Auto-generated from a spec via self_modify.reflect. See
self_modify_proposals for the proposal row (proposed_by='morgoth').
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from core.config import AppConfig, PermissionDeniedError
from tools.base_tool import BaseTool


_BASE_URL = 'https://api.blockcypher.com'
_ENDPOINT_PATH = '/v1/eth/main'
_SOURCE_LABEL = 'api.blockcypher.com'
_TOOL_DESCRIPTION = 'Fetch current Ethereum mainnet stats from BlockCypher including block height, mempool backlog (unconfirmed_count), and tiered gas prices (low/medium/high) plus EIP-1559 base_fee to gauge ETH network congestion and fee pressure.'
_DIGEST_FIELDS = ['height', 'unconfirmed_count', 'high_gas_price', 'medium_gas_price', 'low_gas_price', 'base_fee']


class GetEthereumNetworkStatsTool(BaseTool):
    __doc__ = _TOOL_DESCRIPTION

    name = 'get_ethereum_network_stats'
    is_data_source = True
    # Declared endpoints for the duplication gate on FUTURE reflect
    # runs. Normalized form: host+path, no scheme, no query, no
    # trailing slash. Derived deterministically from the spec so the
    # next model can see this tool's endpoint in the reflect registry.
    api_endpoints = ('api.blockcypher.com/v1/eth/main',)
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

    async def execute(self, **_kwargs: Any) -> dict[str, Any]:
        if not self._config.permissions.permissions.can_access_internet:
            raise PermissionDeniedError("Internet access is disabled by permissions")

        try:
            resp = await self._client.get(_BASE_URL + _ENDPOINT_PATH)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            return self.failure(
                f"{_SOURCE_LABEL} request failed: {exc}",
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
