"""Router argument validation at the boundary.

123 malformed calls in ChromaDB history burned cycles on KeyError:
- get_crypto_price called without required 'symbol' (115 hits)
- web_search called without required 'query' (8 hits)
Each declares its required-arg schema; the router now rejects the
call BEFORE execute so the model gets a usable retry hint.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.tool_router import ToolRouter, _validate_arguments


def _tool(name, parameters):
    t = MagicMock()
    t.name = name
    t.parameters = parameters
    t.execute = AsyncMock(return_value={"success": True, "result": "ok"})
    t.to_ollama_schema = MagicMock(return_value={})
    return t


class TestValidator:
    def test_missing_required_is_error(self):
        tool = _tool("get_crypto_price", {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        })
        err = _validate_arguments(tool, {})
        assert err is not None
        assert "missing required" in err
        assert "'symbol'" in err
        assert "Allowed parameters" in err

    def test_unknown_param_is_error(self):
        tool = _tool("get_x", {
            "type": "object",
            "properties": {"a": {"type": "string"}},
        })
        err = _validate_arguments(tool, {"b": 1})
        assert err is not None
        assert "unknown param(s)" in err
        assert "'b'" in err

    def test_missing_and_unknown_both_reported(self):
        tool = _tool("get_x", {
            "type": "object",
            "properties": {"a": {}}, "required": ["a"],
        })
        err = _validate_arguments(tool, {"b": 1})
        assert "missing required" in err
        assert "unknown param(s)" in err

    def test_valid_call_returns_none(self):
        tool = _tool("get_crypto_price", {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        })
        assert _validate_arguments(tool, {"symbol": "btc"}) is None

    def test_optional_arg_omitted_is_valid(self):
        tool = _tool("get_x", {
            "type": "object",
            "properties": {"a": {}, "b": {}}, "required": ["a"],
        })
        assert _validate_arguments(tool, {"a": 1}) is None

    def test_opaque_schema_short_circuits_to_none(self):
        # Tools that expose an empty properties dict are treated as
        # free-form; we can't validate without inventing rules.
        tool = _tool("get_x", {"type": "object", "properties": {}})
        assert _validate_arguments(tool, {"anything": 1}) is None

    def test_no_schema_at_all(self):
        tool = _tool("get_x", None)
        assert _validate_arguments(tool, {"anything": 1}) is None


@pytest.mark.asyncio
class TestRouterInterception:
    async def test_missing_required_returns_failure_never_raises(self):
        r = ToolRouter()
        r.register(_tool("get_crypto_price", {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        }))
        env = await r.execute_tool("get_crypto_price", {})
        assert env["success"] is False
        assert "invalid arguments" in env["error"]
        assert "'symbol'" in env["error"]
        # The live tool must NOT have been called on rejection.
        r._tools["get_crypto_price"].execute.assert_not_called()

    async def test_valid_call_still_executes(self):
        r = ToolRouter()
        r.register(_tool("get_crypto_price", {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        }))
        env = await r.execute_tool("get_crypto_price", {"symbol": "btc"})
        assert env["success"] is True
        r._tools["get_crypto_price"].execute.assert_awaited_once()

    async def test_unknown_param_rejected(self):
        r = ToolRouter()
        r.register(_tool("web_search", {
            "type": "object",
            "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            "required": ["query"],
        }))
        env = await r.execute_tool("web_search", {"query": "x", "func_name": "fetch"})
        assert env["success"] is False
        assert "unknown param" in env["error"]

    async def test_metadata_flag_set_on_rejection(self):
        r = ToolRouter()
        r.register(_tool("get_crypto_price", {
            "type": "object",
            "properties": {"symbol": {}},
            "required": ["symbol"],
        }))
        env = await r.execute_tool("get_crypto_price", {})
        assert env["metadata"]["validation"] is True
