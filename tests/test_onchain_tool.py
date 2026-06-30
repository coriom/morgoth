"""Tests for the get_bitcoin_onchain tool (mempool.space)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.brain import DATA_SOURCE_TOOLS
from tools.data_feeds.onchain import GetBitcoinOnchainTool


class _Response:
    """Async response double matching DummyResponse semantics."""

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        should_raise: bool = False,
        status_code: int = 200,
    ) -> None:
        self._payload = payload or {}
        self.status_code = status_code
        self._should_raise = should_raise

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self._should_raise:
            request = httpx.Request("GET", "https://mempool.space/api/v1/test")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("dummy status error", request=request, response=response)


class _MultiResponseClient:
    """Async client double that returns different payloads keyed by URL substring."""

    def __init__(
        self,
        responses: dict[str, _Response],
        *,
        raise_on_call: Exception | None = None,
    ) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self._raise_on_call = raise_on_call

    async def get(self, url: str, params: Any = None, headers: Any = None) -> _Response:
        self.calls.append(url)
        if self._raise_on_call is not None:
            raise self._raise_on_call
        for key, response in self.responses.items():
            if key in url:
                return response
        raise AssertionError(f"unexpected URL in test: {url}")

    async def aclose(self) -> None:
        pass


# Real shapes captured from mempool.space (Phase B), trimmed to the keys the tool reads.
_HASHRATE_PAYLOAD = {
    "hashrates": [],
    "difficulty": [],
    "currentHashrate": 9.696e20,
    "currentDifficulty": 133869853540305.4,
}

_DIFFICULTY_PAYLOAD = {
    "progressPercent": 24.9,
    "difficultyChange": -1.78,
    "estimatedRetargetDate": 1783760043120,
    "remainingBlocks": 1514,
    "remainingTime": 926689120,
    "previousRetarget": 7.15,
    "previousTime": 1782525607,
    "nextRetargetHeight": 957600,
    "timeAvg": 613041,
    "adjustedTimeAvg": 612080,
    "timeOffset": 0,
    "expectedBlocks": 512.91,
}

_FEES_PAYLOAD = {
    "fastestFee": 6,
    "halfHourFee": 5,
    "hourFee": 4,
    "economyFee": 2,
    "minimumFee": 1,
}

_MEMPOOL_PAYLOAD = {
    "count": 110318,
    "vsize": 45805097,
    "total_fee": 18538011,
    "fee_histogram": [],
}


def _ok_responses() -> dict[str, _Response]:
    return {
        "/api/v1/mining/hashrate/3d": _Response(_HASHRATE_PAYLOAD),
        "/api/v1/difficulty-adjustment": _Response(_DIFFICULTY_PAYLOAD),
        "/api/v1/fees/recommended": _Response(_FEES_PAYLOAD),
        "/api/mempool": _Response(_MEMPOOL_PAYLOAD),
    }


@pytest.mark.asyncio
async def test_execute_returns_compact_digest(app_config) -> None:
    """execute() returns success with the expected result keys against real mempool.space shapes."""

    client = _MultiResponseClient(_ok_responses())
    tool = GetBitcoinOnchainTool(app_config, client=client)

    result = await tool.execute()

    assert result["success"] is True
    assert result["error"] is None
    assert result["metadata"]["source"] == "mempool.space"

    body = result["result"]
    # top-level keys
    assert set(body.keys()) == {
        "hash_rate",
        "difficulty",
        "next_difficulty_adjustment",
        "fees_sat_vb",
        "mempool_tx_count",
        "mempool_vsize",
    }
    # values map correctly from each endpoint
    assert body["hash_rate"] == _HASHRATE_PAYLOAD["currentHashrate"]
    assert body["difficulty"] == _HASHRATE_PAYLOAD["currentDifficulty"]
    assert body["next_difficulty_adjustment"]["estimated_change_percent"] == -1.78
    assert body["next_difficulty_adjustment"]["remaining_blocks"] == 1514
    assert body["fees_sat_vb"]["fastest"] == 6
    assert body["fees_sat_vb"]["minimum"] == 1
    assert body["mempool_tx_count"] == 110318
    assert body["mempool_vsize"] == 45805097
    # all four endpoints were called
    assert len(client.calls) == 4


@pytest.mark.asyncio
async def test_execute_returns_failure_on_http_error(app_config) -> None:
    """execute() returns success=False with an error when httpx raises."""

    client = _MultiResponseClient(
        _ok_responses(),
        raise_on_call=httpx.ConnectError("network down"),
    )
    tool = GetBitcoinOnchainTool(app_config, client=client)

    result = await tool.execute()

    assert result["success"] is False
    assert result["error"] is not None
    assert "mempool.space" in result["error"]
    assert result["metadata"]["source"] == "mempool.space"


@pytest.mark.asyncio
async def test_execute_propagates_http_status_error_as_failure(app_config) -> None:
    """A 4xx/5xx response (raise_for_status) is caught and returned as a failure."""

    bad_responses = _ok_responses()
    bad_responses["/api/v1/mining/hashrate/3d"] = _Response(
        {}, should_raise=True, status_code=503
    )
    client = _MultiResponseClient(bad_responses)
    tool = GetBitcoinOnchainTool(app_config, client=client)

    result = await tool.execute()

    assert result["success"] is False
    assert "mempool.space" in result["error"]


def test_to_ollama_schema_is_well_formed(app_config) -> None:
    """The tool's schema is structurally valid for Ollama function calling."""

    tool = GetBitcoinOnchainTool(app_config)
    schema = tool.to_ollama_schema()

    assert schema["type"] == "function"
    assert schema["function"]["name"] == "get_bitcoin_onchain"
    assert schema["function"]["description"]
    params = schema["function"]["parameters"]
    assert params["type"] == "object"
    # no required args
    assert "required" not in params or not params["required"]


def test_get_bitcoin_onchain_is_in_data_source_tools() -> None:
    """The tool counts as a distinct external data source for the 3-source rail."""

    assert "get_bitcoin_onchain" in DATA_SOURCE_TOOLS
