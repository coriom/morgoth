"""Invariant: is_data_source label sources from DATA_SOURCE_TOOLS everywhere.

The reddit / web_search bug had two roots: their classes did not carry
is_data_source=True (they live outside tools/data_feeds/ so discovery
never touches them), while brain.py's _STATIC_DATA_SOURCES did include
them in the runtime rail. Reading the class flag anywhere produced a
label that disagreed with rail membership.

Fix: every code path that surfaces a data-source label reads
core.brain.DATA_SOURCE_TOOLS directly. This file locks that invariant
in three places (API endpoint, wiki compiler, reflect context builder)
and against every current static-set tool.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes import tools as tools_route
from core.brain import DATA_SOURCE_TOOLS


# ---------- runtime rail composition --------------------------------------

def test_reddit_search_no_longer_in_data_source_tools() -> None:
    """Named-policy retirement: reddit_search must be absent from the rail."""
    assert "reddit_search" not in DATA_SOURCE_TOOLS


def test_web_search_is_still_in_data_source_tools() -> None:
    """web_search remains in the rail after the labeling fix — nothing about
    the fix should reduce the rail; it just makes the label consistent."""
    assert "web_search" in DATA_SOURCE_TOOLS


def test_rail_baseline_intact_post_retirement() -> None:
    """The floor after reddit_search retirement: four live baseline sources."""
    baseline = frozenset({
        "get_crypto_price",
        "get_bitcoin_onchain",
        "get_news",
        "web_search",
    })
    missing = baseline - DATA_SOURCE_TOOLS
    assert not missing, (
        f"post-retirement baseline lost {missing!r}; only reddit_search "
        "should have been removed"
    )


# ---------- /api/tools reports from DATA_SOURCE_TOOLS ---------------------

class _FakeTool:
    def __init__(self, name: str, class_flag: bool = False) -> None:
        self.name = name
        self.is_data_source = class_flag
        self.is_chat_tool = True
        self.description = ""


def _app_with_tools(*names: str) -> FastAPI:
    class _FakeRouter:
        def __init__(self) -> None:
            self._tools = {n: _FakeTool(n, class_flag=False) for n in names}

    app = FastAPI()
    app.state.tool_router = _FakeRouter()
    app.include_router(tools_route.router)
    return app


def test_api_tools_labels_web_search_as_data_source() -> None:
    """web_search has class_flag=False but IS in DATA_SOURCE_TOOLS —
    the endpoint must report True regardless of the class flag."""
    client = TestClient(_app_with_tools("web_search", "recall"))
    body = {t["name"]: t for t in client.get("/api/tools").json()}
    assert body["web_search"]["is_data_source"] is True
    assert body["recall"]["is_data_source"] is False


def test_api_tools_labels_static_set_tools_correctly() -> None:
    """Parametrize-in-body over the current DATA_SOURCE_TOOLS set."""
    all_names = list(DATA_SOURCE_TOOLS) + ["recall", "remember", "notify"]
    client = TestClient(_app_with_tools(*all_names))
    body = {t["name"]: t for t in client.get("/api/tools").json()}
    for name in DATA_SOURCE_TOOLS:
        assert body[name]["is_data_source"] is True, (
            f"/api/tools mislabels {name!r} — must be True by DATA_SOURCE_TOOLS "
            f"membership regardless of the class flag"
        )
    for excluded in ("recall", "remember", "notify"):
        assert body[excluded]["is_data_source"] is False


# ---------- wiki compiler reads DATA_SOURCE_TOOLS -------------------------

def test_wiki_tool_page_labels_web_search_as_data_source() -> None:
    """The system-vault tool page must show data_source: True for
    web_search — historically its class flag was False.
    """
    from scripts.compile_wiki import _tool_page

    tool = _FakeTool("web_search", class_flag=False)
    page = _tool_page(
        tool=tool, provenance=None, objectives_count=0, theses_fed=[],
        is_data_source=True,
    )
    assert "data_source: **True**" in page


def test_wiki_tool_page_labels_non_source_correctly() -> None:
    from scripts.compile_wiki import _tool_page

    tool = _FakeTool("recall", class_flag=False)
    page = _tool_page(
        tool=tool, provenance=None, objectives_count=0, theses_fed=[],
        is_data_source=False,
    )
    assert "data_source: **False**" in page


# ---------- reflect context reads DATA_SOURCE_TOOLS -----------------------

@pytest.mark.asyncio
async def test_reflect_context_labels_web_search_as_data_source() -> None:
    """The reflect context builder must label web_search as
    data_source (its class flag says False; the runtime set says True)."""
    from self_modify import reflect

    fake_tools = [
        _FakeTool("web_search", class_flag=False),
        _FakeTool("recall", class_flag=False),
    ]
    pm = MagicMock()
    pm.get_objectives = AsyncMock(return_value=[])
    pm.get_theses = AsyncMock(return_value=[])
    config = SimpleNamespace()

    with patch("scripts.compile_wiki._registered_tools_offline",
               return_value=fake_tools), \
         patch("scripts.compile_wiki._load_tool_usage",
               AsyncMock(return_value=({}, {}))):
        ctx = await reflect._build_context(pm, config)

    lines = ctx["tools_block"].splitlines()
    web_line = next(line for line in lines if line.startswith("- web_search "))
    recall_line = next(line for line in lines if line.startswith("- recall "))
    assert "(data_source)" in web_line, (
        f"reflect context mislabels web_search: {web_line!r}"
    )
    assert "(chat/util)" in recall_line


# ---------- parametrize the invariant over the whole static rail ---------

@pytest.mark.parametrize("name", sorted(DATA_SOURCE_TOOLS))
def test_every_data_source_tool_gets_true_label_from_api(name: str) -> None:
    """For every name in DATA_SOURCE_TOOLS, the API labels it True."""
    client = TestClient(_app_with_tools(name))
    body = client.get("/api/tools").json()
    assert body[0]["is_data_source"] is True, (
        f"API mislabels {name!r}: expected is_data_source=True"
    )
