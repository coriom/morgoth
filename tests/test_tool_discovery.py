"""Non-regression tests for the auto-discovery refactor.

The computed sets from ``tools.discovery`` + brain.py's static merge MUST
equal the OLD hand-written literals byte-for-byte (as sets). If a change
under tools/data_feeds/ ever silently widens or narrows the source rail
or the chat schema, this suite fails so it can't slip through.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

# The OLD literal values — copied verbatim from the pre-refactor brain.py.
# These are the non-regression baseline. Do NOT change them casually.
_OLD_DATA_SOURCE_TOOLS = frozenset({
    "get_crypto_price",
    "get_bitcoin_onchain",
    "get_news",
    "reddit_search",
    "web_search",
})

_OLD_CHAT_TOOL_NAMES = {
    "web_search",
    "get_news",
    "get_crypto_price",
    "get_bitcoin_onchain",
    "fred_series_observations",
    "reddit_search",
    "technical_analysis",
    "remember",
    "recall",
    "create_objective",
    "update_objective",
}


def test_computed_data_source_tools_matches_baseline() -> None:
    from core.brain import DATA_SOURCE_TOOLS

    assert DATA_SOURCE_TOOLS == _OLD_DATA_SOURCE_TOOLS, (
        f"DATA_SOURCE_TOOLS drift: got {DATA_SOURCE_TOOLS!r}, "
        f"expected {_OLD_DATA_SOURCE_TOOLS!r}"
    )
    # Type must remain frozenset.
    assert isinstance(DATA_SOURCE_TOOLS, frozenset)


def test_computed_chat_tool_names_matches_baseline_as_set() -> None:
    from core.brain import CHAT_TOOL_NAMES

    assert set(CHAT_TOOL_NAMES) == _OLD_CHAT_TOOL_NAMES, (
        f"CHAT_TOOL_NAMES drift: got {set(CHAT_TOOL_NAMES)!r}, "
        f"expected {_OLD_CHAT_TOOL_NAMES!r}"
    )
    # Type must remain list (brain.py iterates it with .get_schemas).
    assert isinstance(CHAT_TOOL_NAMES, list)
    # No duplicates.
    assert len(CHAT_TOOL_NAMES) == len(set(CHAT_TOOL_NAMES))


def test_discovery_finds_all_data_feeds_tools() -> None:
    from tools.discovery import discover_data_feed_tools

    classes = discover_data_feed_tools()
    names = {cls.name for cls in classes}
    # The four currently-shipped data_feeds tools.
    assert names == {
        "get_crypto_price",
        "get_crypto_history",
        "get_bitcoin_onchain",
        "get_news",
    }
    # Deterministic sort order.
    assert [cls.name for cls in classes] == sorted(names)


def test_discovery_flags_are_correct_per_tool() -> None:
    from tools.discovery import discover_data_feed_tools

    by_name = {cls.name: cls for cls in discover_data_feed_tools()}
    # Data source status
    assert by_name["get_crypto_price"].is_data_source is True
    assert by_name["get_bitcoin_onchain"].is_data_source is True
    assert by_name["get_news"].is_data_source is True
    assert by_name["get_crypto_history"].is_data_source is False
    # Chat inclusion
    assert by_name["get_crypto_price"].is_chat_tool is True
    assert by_name["get_bitcoin_onchain"].is_chat_tool is True
    assert by_name["get_news"].is_chat_tool is True
    assert by_name["get_crypto_history"].is_chat_tool is False


def test_fred_not_discovered_but_flagged_false_by_default() -> None:
    """FRED lives under tools/connectors, so discovery must skip it.

    Baseline: fred was deliberately excluded from the source rail;
    the STATIC_DATA_SOURCES set excludes it too. Nothing under
    tools/data_feeds/ is named 'fred_series_*'.
    """
    from tools.discovery import discover_data_feed_tools

    names = {cls.name for cls in discover_data_feed_tools()}
    assert not any(n.startswith("fred_") for n in names), (
        "fred tools live under tools/connectors/ and must NOT be discovered"
    )


def test_api_tools_endpoint_returns_registered_inventory() -> None:
    """GET /api/tools returns name + is_data_source + is_chat_tool."""
    from api.routes import tools as tools_route

    # Fake a router with a couple of registered tools.
    class _FakeTool:
        def __init__(self, name: str, ds: bool, ct: bool) -> None:
            self.name = name
            self.is_data_source = ds
            self.is_chat_tool = ct

    class _FakeRouter:
        def __init__(self) -> None:
            self._tools = {
                "get_news": _FakeTool("get_news", True, True),
                "get_crypto_history": _FakeTool("get_crypto_history", False, False),
            }

    app = FastAPI()
    app.state.tool_router = _FakeRouter()
    app.include_router(tools_route.router)
    client = TestClient(app)

    resp = client.get("/api/tools")
    assert resp.status_code == 200
    body = resp.json()
    # Deterministic name-sorted order
    assert [t["name"] for t in body] == ["get_crypto_history", "get_news"]
    # Shape check
    assert body[0] == {
        "name": "get_crypto_history",
        "is_data_source": False,
        "is_chat_tool": False,
    }
    assert body[1] == {
        "name": "get_news",
        "is_data_source": True,
        "is_chat_tool": True,
    }
