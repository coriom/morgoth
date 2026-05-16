"""Tests for brain status extensions and cycle-feed ring buffer."""

from __future__ import annotations

import collections
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.brain import Brain


def _make_brain(**kwargs: Any) -> Brain:
    """Build a Brain with all dependencies stubbed out."""
    config = MagicMock()
    config.ollama_primary_model = "model-a"
    config.ollama_agent_model = "model-b"
    config.max_concurrent_agents = 3
    config.ollama_base_url = "http://localhost:11434"
    config.log_level_thought = False
    for k, v in kwargs.items():
        setattr(config, k, v)

    brain = Brain(
        config=config,
        llm_client=MagicMock(),
        persistent_memory=MagicMock(),
        episodic_memory=MagicMock(),
        scheduler=MagicMock(),
        tool_router=MagicMock(),
        agent_manager=MagicMock(),
        notifier=MagicMock(),
        websocket_manager=None,
    )
    return brain


# ---------------------------------------------------------------------------
# uptime_seconds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uptime_seconds_is_non_negative() -> None:
    brain = _make_brain()
    with patch.object(brain, "_fetch_vram_used_mb", new=AsyncMock(return_value=None)):
        status = await brain.get_status()
    assert isinstance(status["uptime_seconds"], int)
    assert status["uptime_seconds"] >= 0


# ---------------------------------------------------------------------------
# vram fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vram_total_is_6144() -> None:
    brain = _make_brain()
    with patch.object(brain, "_fetch_vram_used_mb", new=AsyncMock(return_value=512)):
        status = await brain.get_status()
    assert status["vram_total_mb"] == 6144
    assert status["vram_used_mb"] == 512


@pytest.mark.asyncio
async def test_vram_used_is_null_when_ollama_unreachable() -> None:
    brain = _make_brain()
    with patch.object(brain, "_fetch_vram_used_mb", new=AsyncMock(return_value=None)):
        status = await brain.get_status()
    assert status["vram_used_mb"] is None


@pytest.mark.asyncio
async def test_fetch_vram_used_mb_sums_size_vram() -> None:
    brain = _make_brain()
    fake_payload = {
        "models": [
            {"name": "model-a", "size_vram": 1024 * 1024 * 1024},  # 1024 MB
            {"name": "model-b", "size_vram": 512 * 1024 * 1024},   # 512 MB
        ]
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = fake_payload
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("core.brain.httpx.AsyncClient", return_value=mock_client):
        result = await brain._fetch_vram_used_mb()

    assert result == 1536


@pytest.mark.asyncio
async def test_fetch_vram_returns_none_on_exception() -> None:
    brain = _make_brain()
    with patch("core.brain.httpx.AsyncClient", side_effect=Exception("no conn")):
        result = await brain._fetch_vram_used_mb()
    assert result is None


# ---------------------------------------------------------------------------
# cycle_feed ring buffer
# ---------------------------------------------------------------------------

def test_feed_append_adds_entry() -> None:
    brain = _make_brain()
    brain._feed_append("ACTION", "calling web_search", tool="web_search", duration_ms=42)
    assert len(brain._cycle_feed) == 1
    entry = brain._cycle_feed[0]
    assert entry["level"] == "ACTION"
    assert entry["tool"] == "web_search"
    assert entry["duration_ms"] == 42
    assert "ts" in entry


def test_feed_append_maxlen_respected() -> None:
    brain = _make_brain()
    for i in range(250):
        brain._feed_append("OK", f"msg {i}")
    assert len(brain._cycle_feed) == 200


def test_get_cycle_feed_newest_first() -> None:
    brain = _make_brain()
    brain._feed_append("SYSTEM", "first")
    brain._feed_append("OK", "second")
    brain._feed_append("ERROR", "third")

    feed = brain.get_cycle_feed(limit=3)
    assert feed[0]["message"] == "third"
    assert feed[1]["message"] == "second"
    assert feed[2]["message"] == "first"


def test_get_cycle_feed_limit_respected() -> None:
    brain = _make_brain()
    for i in range(20):
        brain._feed_append("OK", f"msg {i}")
    feed = brain.get_cycle_feed(limit=5)
    assert len(feed) == 5


def test_get_cycle_feed_limit_capped_at_200() -> None:
    brain = _make_brain()
    for i in range(200):
        brain._feed_append("OK", f"msg {i}")
    feed = brain.get_cycle_feed(limit=999)
    assert len(feed) == 200


# ---------------------------------------------------------------------------
# status includes all required keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_contains_all_required_keys() -> None:
    brain = _make_brain()
    with patch.object(brain, "_fetch_vram_used_mb", new=AsyncMock(return_value=0)):
        status = await brain.get_status()
    for key in ("uptime_seconds", "vram_used_mb", "vram_total_mb",
                "total_cycles_completed", "last_cycle_at", "last_cycle_action"):
        assert key in status, f"missing key: {key}"
