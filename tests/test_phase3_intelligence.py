"""Tests for Phase 3 intelligence expansion agents and tools."""

from __future__ import annotations

from typing import Any

import pytest

from agents.agent_manager import AgentManager
from core.llm_client import ChatMessage, ChatResponse
from tools.analysis.technical import TechnicalAnalysisTool
from tools.connectors.fred import FredSeriesObservationsTool, FredSeriesSearchTool
from tools.connectors.reddit import RedditSearchTool, RedditSubredditPostsTool


pytestmark = pytest.mark.asyncio


class DummyLLMClient:
    """Minimal LLM client test double."""

    async def chat(self, messages: list[ChatMessage], *, model: str | None = None, **_: Any) -> ChatResponse:
        """Return a predictable chat response."""

        return ChatResponse(
            model=model or "llama3.1:8b",
            message=ChatMessage(role="assistant", content=f"synthesis: {messages[-1].content}"),
            done=True,
        )


class DummyAgentMemory:
    """Minimal persistent memory test double for agent manager tests."""

    def __init__(self) -> None:
        """Initialize captured agent writes."""

        self.agents: list[dict[str, Any]] = []

    async def save_agent(self, payload: dict[str, Any]) -> None:
        """Capture an agent payload."""

        self.agents.append(payload)


async def test_fred_search_tool_normalizes_series(app_config, DummyHTTPClientFixture, DummyResponseFixture, monkeypatch) -> None:
    """FRED search should normalize series metadata."""

    monkeypatch.setenv("FRED_API_KEY", "test-key")
    client = DummyHTTPClientFixture(
        DummyResponseFixture(
            {
                "seriess": [
                    {
                        "id": "UNRATE",
                        "title": "Unemployment Rate",
                        "frequency_short": "M",
                        "units_short": "Percent",
                        "seasonal_adjustment_short": "SA",
                        "popularity": "95",
                    }
                ]
            }
        )
    )
    result = await FredSeriesSearchTool(app_config, client=client).execute(query="unemployment", limit=1)

    assert result["success"] is True
    assert result["result"][0]["series_id"] == "UNRATE"
    assert client.calls[0][2]["params"]["api_key"] == "test-key"


async def test_fred_observations_tool_normalizes_missing_values(
    app_config,
    DummyHTTPClientFixture,
    DummyResponseFixture,
    monkeypatch,
) -> None:
    """FRED observations should convert missing '.' values to null."""

    monkeypatch.setenv("FRED_API_KEY", "test-key")
    client = DummyHTTPClientFixture(
        DummyResponseFixture(
            {
                "observations": [
                    {"date": "2026-02-01", "value": ".", "realtime_start": "2026-02-02", "realtime_end": "2026-02-02"},
                    {"date": "2026-01-01", "value": "4.0", "realtime_start": "2026-01-02", "realtime_end": "2026-01-02"},
                ]
            }
        )
    )
    result = await FredSeriesObservationsTool(app_config, client=client).execute(series_id="unrate", limit=2)

    assert result["success"] is True
    assert result["result"]["series_id"] == "UNRATE"
    assert result["result"]["observations"][0]["value"] == 4.0
    assert result["result"]["observations"][1]["value"] is None


async def test_reddit_search_tool_normalizes_posts(app_config, DummyHTTPClientFixture, DummyResponseFixture) -> None:
    """Reddit search should normalize listing children."""

    client = DummyHTTPClientFixture(
        DummyResponseFixture(
            {
                "data": {
                    "children": [
                        {
                            "data": {
                                "id": "abc",
                                "subreddit": "test",
                                "title": "Macro sentiment",
                                "author": "user",
                                "score": 42,
                                "num_comments": 7,
                                "url": "https://example.com",
                                "permalink": "/r/test/comments/abc",
                                "created_utc": 1770000000,
                                "selftext": "details",
                            }
                        }
                    ]
                }
            }
        )
    )
    result = await RedditSearchTool(app_config, client=client).execute(query="macro", subreddit="r/test", limit=1)

    assert result["success"] is True
    assert result["result"][0]["title"] == "Macro sentiment"
    assert result["metadata"]["subreddit"] == "test"


async def test_reddit_subreddit_posts_tool_uses_hot_listing(app_config, DummyHTTPClientFixture, DummyResponseFixture) -> None:
    """Subreddit posts should use the hot listing endpoint."""

    client = DummyHTTPClientFixture(DummyResponseFixture({"data": {"children": []}}))
    result = await RedditSubredditPostsTool(app_config, client=client).execute(subreddit="technology", limit=3)

    assert result["success"] is True
    assert client.calls[0][1].endswith("/r/technology/hot.json")


async def test_technical_analysis_tool_returns_indicators() -> None:
    """Technical analysis should calculate RSI, MACD, and moving averages."""

    prices = [float(index) for index in range(1, 61)]
    result = await TechnicalAnalysisTool().execute(prices=prices, short_window=5, long_window=20, rsi_period=14)

    assert result["success"] is True
    assert result["result"]["sample_size"] == 60
    assert result["result"]["rsi"] == 100.0
    assert result["result"]["moving_averages"]["short_sma"] == 58.0
    assert result["result"]["trend"] == "bullish"


@pytest.mark.parametrize(
    ("name", "task", "tools", "expected_specialization", "expected_tool"),
    [
        ("research-alpha", "Investigate the current liquidity regime.", ["web_search"], "research", "reddit_search"),
        ("sentiment-alpha", "Analyze social mood around AI infrastructure.", ["reddit_search"], "sentiment", "get_news"),
        ("macro-alpha", "Analyze unemployment and inflation indicators.", ["fred_series_search"], "macro", "fred_series_observations"),
    ],
)
async def test_agent_manager_selects_specialist_agents(
    app_config,
    name: str,
    task: str,
    tools: list[str],
    expected_specialization: str,
    expected_tool: str,
) -> None:
    """Agent manager should register and instantiate Phase 3 specialists automatically."""

    manager = AgentManager(app_config, DummyLLMClient(), DummyAgentMemory())  # type: ignore[arg-type]
    agent = await manager.create(
        name=name,
        task=task,
        agent_type="ephemeral",
        tools=tools,
    )

    assert agent["specialization"] == expected_specialization
    assert agent["current_task"] == task
    assert expected_tool in agent["tools"]
