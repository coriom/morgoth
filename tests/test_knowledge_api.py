"""Tests for the /api/theses and /api/contradictions read-only endpoints."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.routes import knowledge
from memory.persistent import PersistentMemory


class _AsyncCtxManager:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


def _build_test_app(persistent_memory: Any) -> FastAPI:
    """Minimal FastAPI app with only the knowledge router and a mocked PM."""
    app = FastAPI()
    app.state.persistent_memory = persistent_memory
    app.include_router(knowledge.router)
    return app


def _thesis_row(
    thesis_id: str = "11111111-1111-1111-1111-111111111111",
    subject: str = "BTC volume",
    claim: str = "declining",
    status: str = "active",
    objective_id: str = "obj-1",
) -> dict[str, Any]:
    return {
        "thesis_id": thesis_id,
        "subject": subject,
        "claim": claim,
        "confidence": "high",
        "evidence": [{"source": "get_crypto_price", "detail": "down 24h"}],
        "status": status,
        "objective_id": objective_id,
        "created_at": datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
    }


# ---------------------- GET /api/theses ----------------------


def test_list_theses_returns_items_shape() -> None:
    pm = MagicMock()
    pm.get_theses = AsyncMock(return_value=[
        _thesis_row(),
        _thesis_row(thesis_id="22222222-2222-2222-2222-222222222222",
                    subject="BTC fees", claim="increasing"),
    ])
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/theses")

    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert len(body["items"]) == 2
    assert body["items"][0]["subject"] == "BTC volume"
    assert body["items"][0]["claim"] == "declining"
    # evidence preserved
    assert body["items"][0]["evidence"] == [
        {"source": "get_crypto_price", "detail": "down 24h"}
    ]


def test_list_theses_status_filter_passes_through() -> None:
    pm = MagicMock()
    pm.get_theses = AsyncMock(return_value=[_thesis_row(status="contradicted")])
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/theses?status=contradicted")

    assert resp.status_code == 200
    pm.get_theses.assert_awaited_once()
    kw = pm.get_theses.call_args.kwargs
    assert kw["status"] == "contradicted"
    assert kw["subject"] is None


def test_list_theses_subject_filter_passes_through() -> None:
    pm = MagicMock()
    pm.get_theses = AsyncMock(return_value=[_thesis_row(subject="BTC volume")])
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/theses?subject=BTC")

    assert resp.status_code == 200
    kw = pm.get_theses.call_args.kwargs
    assert kw["subject"] == "BTC"


def test_list_theses_db_failure_returns_500_clean_message() -> None:
    pm = MagicMock()
    pm.get_theses = AsyncMock(side_effect=RuntimeError("boom"))
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/theses")

    assert resp.status_code == 500
    assert "theses read failed" in resp.json()["detail"]


# ---------------------- GET /api/contradictions ----------------------


def _contradiction_join_row(
    contradiction_id: str = "cccccccc-cccc-cccc-cccc-cccccccccccc",
    a_id: str | None = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    b_id: str | None = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    subject_a: str | None = "BTC volume",
    subject_b: str | None = "Bitcoin volume",
) -> dict[str, Any]:
    return {
        "contradiction_id": contradiction_id,
        "subject_group": subject_a,
        "detected_at": datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc),
        "thesis_id_a": a_id,
        "subject_a": subject_a,
        "claim_a": "declining" if a_id else None,
        "confidence_a": "high" if a_id else None,
        "status_a": "contradicted" if a_id else None,
        "evidence_a": [{"source": "x", "detail": "y"}] if a_id else None,
        "objective_id_a": "obj-A" if a_id else None,
        "created_at_a": datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc) if a_id else None,
        "thesis_id_b": b_id,
        "subject_b": subject_b,
        "claim_b": "increasing" if b_id else None,
        "confidence_b": "medium" if b_id else None,
        "status_b": "contradicted" if b_id else None,
        "evidence_b": [{"source": "x", "detail": "z"}] if b_id else None,
        "objective_id_b": "obj-B" if b_id else None,
        "created_at_b": datetime(2026, 6, 29, 13, 0, tzinfo=timezone.utc) if b_id else None,
    }


def test_list_contradictions_returns_resolved_pair() -> None:
    pm = MagicMock()
    pm.get_contradictions = AsyncMock(return_value=[_contradiction_join_row()])
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/contradictions")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["subject_group"] == "BTC volume"
    # Both sides resolved to full theses, NOT bare ids
    assert item["thesis_a"]["subject"] == "BTC volume"
    assert item["thesis_a"]["claim"] == "declining"
    assert item["thesis_a"]["status"] == "contradicted"
    assert item["thesis_b"]["subject"] == "Bitcoin volume"
    assert item["thesis_b"]["claim"] == "increasing"


def test_list_contradictions_handles_deleted_thesis_with_null() -> None:
    """If a referenced thesis was deleted, that side is null — endpoint does not 500."""
    pm = MagicMock()
    pm.get_contradictions = AsyncMock(return_value=[
        _contradiction_join_row(b_id=None, subject_b=None),
    ])
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/contradictions")

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["thesis_a"]["subject"] == "BTC volume"
    assert items[0]["thesis_b"] is None


def test_list_contradictions_db_failure_returns_500_clean_message() -> None:
    pm = MagicMock()
    pm.get_contradictions = AsyncMock(side_effect=RuntimeError("kaboom"))
    client = TestClient(_build_test_app(pm))

    resp = client.get("/api/contradictions")

    assert resp.status_code == 500
    assert "contradictions read failed" in resp.json()["detail"]


# ---------------------- get_contradictions JOIN method ----------------------


@pytest.mark.asyncio
async def test_get_contradictions_method_emits_left_join_shape(app_config) -> None:
    """The persistence method runs ONE LEFT JOIN query and returns dict rows."""
    pm = PersistentMemory(app_config)
    fake_row = _contradiction_join_row()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[fake_row])
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm._pool = pool

    result = await pm.get_contradictions(limit=25)

    assert result == [fake_row]
    conn.fetch.assert_awaited_once()
    sql, *params = conn.fetch.call_args[0]
    assert "FROM contradictions c" in sql
    assert "LEFT JOIN theses ta ON ta.thesis_id = c.thesis_id_a" in sql
    assert "LEFT JOIN theses tb ON tb.thesis_id = c.thesis_id_b" in sql
    assert "ORDER BY c.detected_at DESC" in sql
    assert params == [25]
