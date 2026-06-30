"""Tests for POST /api/wiki/compile.

The endpoint delegates to compile_wiki(pm, llm) from scripts/compile_wiki.py.
We mock compile_wiki itself so the tests never touch Ollama, the filesystem,
or the embedding model.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.routes import wiki


def _build_test_app(pm: Any, llm: Any) -> FastAPI:
    """Minimal FastAPI app with only the wiki router and mocked deps."""
    app = FastAPI()
    app.state.persistent_memory = pm
    app.state.llm_client = llm
    app.include_router(wiki.router)
    return app


def test_compile_endpoint_returns_counts() -> None:
    """POST /api/wiki/compile returns the counts dict from compile_wiki."""
    fake_counts = {
        "theses_read": 29,
        "entities_written": 13,
        "contradictions": 1,
        "vault_path": "/home/corio/Morgoth/vault",
    }
    with patch(
        "api.routes.wiki.compile_wiki",
        new=AsyncMock(return_value=fake_counts),
    ) as compile_mock:
        pm = MagicMock()
        llm = MagicMock()
        client = TestClient(_build_test_app(pm, llm))

        resp = client.post("/api/wiki/compile")

    assert resp.status_code == 200
    assert resp.json() == fake_counts
    # compile_wiki was called with pm and llm from app.state
    compile_mock.assert_awaited_once_with(pm, llm)


def test_compile_endpoint_returns_500_on_failure() -> None:
    """A failure inside compile_wiki maps to HTTP 500 with a clean detail string."""
    with patch(
        "api.routes.wiki.compile_wiki",
        new=AsyncMock(side_effect=RuntimeError("Ollama is down")),
    ):
        pm = MagicMock()
        llm = MagicMock()
        client = TestClient(_build_test_app(pm, llm))

        resp = client.post("/api/wiki/compile")

    assert resp.status_code == 500
    assert "wiki compile failed" in resp.json()["detail"]


def test_compile_endpoint_only_accepts_post() -> None:
    """GET on /api/wiki/compile is rejected (the call writes files)."""
    with patch(
        "api.routes.wiki.compile_wiki",
        new=AsyncMock(return_value={}),
    ):
        client = TestClient(_build_test_app(MagicMock(), MagicMock()))

        resp = client.get("/api/wiki/compile")

    assert resp.status_code == 405
