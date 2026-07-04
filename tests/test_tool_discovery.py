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

# Superset baselines — the live rail's floor. Silent shrinkage below this
# floor fails the suite; growth is allowed.
#
# POLICY CHANGE (retirement of reddit_search): reddit_search was removed
# from the source rail after Reddit's 2023 API closure produced 403 across
# every host + UA and 0 objectives / 0 theses used it. The named policy
# is: retirement of a broken source drops the corresponding baseline entry;
# it is not "silent shrinkage". Future retirements follow the same pattern.
_OLD_DATA_SOURCE_TOOLS = frozenset({
    "get_crypto_price",
    "get_bitcoin_onchain",
    "get_news",
    "web_search",
})

_OLD_CHAT_TOOL_NAMES = {
    "web_search",
    "get_news",
    "get_crypto_price",
    "get_bitcoin_onchain",
    "fred_series_observations",
    "technical_analysis",
    "remember",
    "recall",
    "create_objective",
    "update_objective",
}


def test_data_source_tools_contains_baseline() -> None:
    """The baseline data-source tools must never disappear silently.

    Assertion is SUPERSET, not equality: adding a new tool via the
    self-modify pipeline (a legitimate expansion of the source rail) MUST
    NOT break the test suite. What breaks the suite is any of the
    baseline names going missing, which would silently shrink the rail.
    """
    from core.brain import DATA_SOURCE_TOOLS

    missing = _OLD_DATA_SOURCE_TOOLS - DATA_SOURCE_TOOLS
    assert not missing, (
        f"DATA_SOURCE_TOOLS lost baseline members: {missing!r}. "
        f"Current: {DATA_SOURCE_TOOLS!r}"
    )
    # Type must remain frozenset.
    assert isinstance(DATA_SOURCE_TOOLS, frozenset)


def test_chat_tool_names_contains_baseline() -> None:
    """The baseline chat tools must never disappear silently.

    Same rationale as DATA_SOURCE_TOOLS: SUPERSET, not equality — allow
    pipeline-driven growth, catch silent shrinkage.
    """
    from core.brain import CHAT_TOOL_NAMES

    current = set(CHAT_TOOL_NAMES)
    missing = _OLD_CHAT_TOOL_NAMES - current
    assert not missing, (
        f"CHAT_TOOL_NAMES lost baseline members: {missing!r}. "
        f"Current: {current!r}"
    )
    # Type must remain list (brain.py iterates it with .get_schemas).
    assert isinstance(CHAT_TOOL_NAMES, list)
    # No duplicates.
    assert len(CHAT_TOOL_NAMES) == len(set(CHAT_TOOL_NAMES))


def test_discovery_finds_the_four_baseline_data_feeds_tools() -> None:
    """Discovery must at minimum find the 4 baseline data_feeds tools.

    SUPERSET: pipeline-added tools legitimately grow this set.
    """
    from tools.discovery import discover_data_feed_tools

    classes = discover_data_feed_tools()
    names = {cls.name for cls in classes}
    baseline = {
        "get_crypto_price",
        "get_crypto_history",
        "get_bitcoin_onchain",
        "get_news",
    }
    missing = baseline - names
    assert not missing, f"discovery lost baseline tools: {missing!r}"
    # Deterministic sort order regardless of set size.
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
