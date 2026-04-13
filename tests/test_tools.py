"""Unit tests for Phase 1 Layer 1 tools."""

from __future__ import annotations

import pytest

from tools.agent_control import CreateAgentTool
from tools.code_executor import ExecutePythonTool
from tools.data_feeds.crypto import GetCryptoHistoryTool, GetCryptoPriceTool
from tools.data_feeds.news import GetNewsTool
from tools.file_manager import ReadFileTool, WriteFileTool
from tools.memory_tools import RecallTool, RememberTool
from tools.notifications import NotifyTool
from tools.web_search import WebSearchTool


pytestmark = pytest.mark.asyncio


async def test_web_search_tool_returns_normalized_results(app_config, DummyHTTPClientFixture, DummyResponseFixture) -> None:
    """Web search tool should normalize DuckDuckGo responses."""

    client = DummyHTTPClientFixture(
        DummyResponseFixture(
            {
                "AbstractText": "Result summary",
                "AbstractURL": "https://example.com",
                "Heading": "Example",
            }
        )
    )
    tool = WebSearchTool(app_config, client=client)
    result = await tool.execute(query="morgoth", max_results=1)
    assert result["success"] is True
    assert result["result"][0]["title"] == "Example"


async def test_execute_python_tool_runs_code(app_config, monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute Python tool should return subprocess output."""

    tool = ExecutePythonTool(app_config)
    async def _fake_run_code(code: str, timeout_seconds: int) -> tuple[str, str, int]:
        return ("ok\n", "", 0)

    monkeypatch.setattr(tool, "_run_code", _fake_run_code)
    result = await tool.execute(code="print('ok')", timeout_seconds=5)
    assert result["success"] is True
    assert result["result"]["stdout"].strip() == "ok"


async def test_read_file_tool_reads_repo_file(app_config) -> None:
    """Read file tool should return file contents."""

    file_path = app_config.root_dir / "README.md"
    file_path.write_text("hello", encoding="utf-8")
    tool = ReadFileTool(app_config)
    result = await tool.execute(path="README.md")
    assert result["success"] is True
    assert result["result"]["content"] == "hello"


async def test_write_file_tool_writes_in_evolvable_zone(app_config) -> None:
    """Write file tool should write inside the evolvable zone only."""

    tool = WriteFileTool(app_config)
    result = await tool.execute(path="data/output.txt", content="saved")
    assert result["success"] is True
    assert (app_config.root_dir / "data" / "output.txt").read_text(encoding="utf-8") == "saved"


async def test_get_crypto_price_tool_parses_payload(
    app_config,
    DummyPersistentMemoryFixture,
    DummyHTTPClientFixture,
    DummyResponseFixture,
) -> None:
    """Crypto price tool should normalize CoinGecko price payloads."""

    persistent_memory = DummyPersistentMemoryFixture()
    client = DummyHTTPClientFixture(
        DummyResponseFixture(
            {
                "bitcoin": {
                    "usd": 100000.0,
                    "usd_24h_change": 1.5,
                    "usd_24h_vol": 12345.0,
                }
            }
        )
    )
    tool = GetCryptoPriceTool(app_config, persistent_memory=persistent_memory, client=client)
    result = await tool.execute(symbol="bitcoin")
    assert result["success"] is True
    assert result["result"]["symbol"] == "BITCOIN"
    assert persistent_memory.snapshots


async def test_get_crypto_history_tool_returns_prices(app_config, DummyHTTPClientFixture, DummyResponseFixture) -> None:
    """Crypto history tool should return normalized price points."""

    client = DummyHTTPClientFixture(DummyResponseFixture({"prices": [[1, 10.0], [2, 11.0]]}))
    tool = GetCryptoHistoryTool(app_config, client=client)
    result = await tool.execute(symbol="bitcoin", days=2)
    assert result["success"] is True
    assert len(result["result"]["prices"]) == 2


async def test_get_news_tool_parses_rss(app_config, DummyHTTPClientFixture, DummyResponseFixture) -> None:
    """News tool should parse RSS feed entries."""

    rss = """
    <rss><channel><item><title>Headline</title><link>https://example.com</link><description>Summary</description></item></channel></rss>
    """
    client = DummyHTTPClientFixture(DummyResponseFixture(text=rss))
    tool = GetNewsTool(app_config, client=client)
    result = await tool.execute(topic="general", limit=1)
    assert result["success"] is True
    assert result["result"][0]["title"] == "Headline"


async def test_create_agent_tool_delegates_to_manager(app_config, DummyAgentManagerFixture) -> None:
    """Create agent tool should delegate to the agent manager."""

    tool = CreateAgentTool(app_config, DummyAgentManagerFixture())
    result = await tool.execute(name="researcher", task="summarize")
    assert result["success"] is True
    assert result["result"]["agent_id"] == "agent-123"


async def test_notify_tool_sends_notification(app_config, DummyNotifierFixture) -> None:
    """Notify tool should use the configured notifier."""

    tool = NotifyTool(app_config, DummyNotifierFixture())
    result = await tool.execute(level="INFO", content="hello")
    assert result["success"] is True
    assert result["result"]["delivered"] is True


async def test_remember_tool_stores_memory(DummyEpisodicMemoryFixture) -> None:
    """Remember tool should store text in episodic memory."""

    tool = RememberTool(DummyEpisodicMemoryFixture())
    result = await tool.execute(content="memory", category="note")
    assert result["success"] is True
    assert result["result"]["document_id"] == "doc-123"


async def test_recall_tool_queries_memory(DummyEpisodicMemoryFixture) -> None:
    """Recall tool should query episodic memory."""

    tool = RecallTool(DummyEpisodicMemoryFixture())
    result = await tool.execute(query="memory")
    assert result["success"] is True
    assert result["result"][0]["document_id"] == "doc-123"
