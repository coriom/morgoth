"""Tests for the multi-source rail: add_source_used, get_sources_used, and DATA_SOURCE_TOOLS."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.brain import DATA_SOURCE_TOOLS
from memory.persistent import PersistentMemory


pytestmark = pytest.mark.asyncio


class _AsyncCtxManager:
    """Minimal async context manager wrapping a mock connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


async def test_add_source_used_appends_and_returns_list(app_config) -> None:
    """add_source_used should return the updated sources_used list from PostgreSQL."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"sources_used": ["get_crypto_price"]}
    )

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    result = await persistent_memory.add_source_used(
        "12345678-1234-5678-1234-567812345678", "get_crypto_price"
    )

    assert result == ["get_crypto_price"]
    mock_conn.fetchrow.assert_called_once()
    call_args = mock_conn.fetchrow.call_args
    sql = call_args[0][0]
    assert "DISTINCT" in sql, "SQL must dedup via DISTINCT to keep sources_used unique"
    assert call_args[0][1] == "12345678-1234-5678-1234-567812345678"
    assert call_args[0][2] == "get_crypto_price"


async def test_add_source_used_dedups_on_repeat(app_config) -> None:
    """Calling add_source_used with an already-present source should return the unchanged list."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"sources_used": ["get_crypto_price"]}
    )

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    first = await persistent_memory.add_source_used(
        "12345678-1234-5678-1234-567812345678", "get_crypto_price"
    )
    second = await persistent_memory.add_source_used(
        "12345678-1234-5678-1234-567812345678", "get_crypto_price"
    )

    assert first == ["get_crypto_price"]
    assert second == ["get_crypto_price"]


async def test_add_source_used_returns_empty_when_row_missing(app_config) -> None:
    """add_source_used should return [] when no objective matches the id."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    result = await persistent_memory.add_source_used(
        "00000000-0000-0000-0000-000000000000", "get_crypto_price"
    )

    assert result == []


async def test_get_sources_used_returns_empty_for_unknown_objective(app_config) -> None:
    """get_sources_used should return [] when the objective row does not exist."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=None)

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    result = await persistent_memory.get_sources_used(
        "00000000-0000-0000-0000-000000000000"
    )

    assert result == []


async def test_get_sources_used_parses_json_string_column(app_config) -> None:
    """get_sources_used should JSON-decode the sources_used column when it comes back as a string."""

    persistent_memory = PersistentMemory(app_config)

    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(
        return_value={"sources_used": '["get_crypto_price", "reddit_search"]'}
    )

    mock_pool = MagicMock()
    mock_pool.acquire = MagicMock(return_value=_AsyncCtxManager(mock_conn))
    persistent_memory._pool = mock_pool

    result = await persistent_memory.get_sources_used(
        "12345678-1234-5678-1234-567812345678"
    )

    assert result == ["get_crypto_price", "reddit_search"]


async def test_data_source_tools_membership() -> None:
    """DATA_SOURCE_TOOLS must contain the external data sources and exclude internal tools.

    SUPERSET assertion: the baseline five external sources must always be
    present (silent shrinkage of the source rail is a regression), but
    the auto-discovery pipeline is allowed to grow the set with new
    is_data_source=True tools. Internal / non-source tools must remain
    absent.

    fred_series_observations is intentionally NOT included: without a
    FRED_API_KEY in .env the tool always fails, so counting it as a
    source-rail option only wastes cycle slots. Re-add it if/when a key
    is provisioned.
    """

    baseline = frozenset({
        "get_crypto_price",
        "get_bitcoin_onchain",
        "get_news",
        "reddit_search",
        "web_search",
    })
    missing = baseline - DATA_SOURCE_TOOLS
    assert not missing, f"DATA_SOURCE_TOOLS lost baseline sources: {missing!r}"
    for excluded in (
        "recall",
        "remember",
        "technical_analysis",
        "create_objective",
        "update_objective",
        "fred_series_observations",
    ):
        assert excluded not in DATA_SOURCE_TOOLS, (
            f"{excluded!r} must not count toward the source rail"
        )
