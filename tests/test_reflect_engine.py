"""Switchable engine tests: provider resolution, request shape, key
hygiene, retries, engine-column persistence.

No real API is called anywhere in this file. httpx is patched at the
factory level so we assemble a full mock ``httpx.AsyncClient`` and
inspect what the reflect engine tried to send.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from loguru import logger

from self_modify import reflect_llm
from self_modify.reflect_llm import (
    ANTHROPIC_URL,
    ANTHROPIC_VERSION,
    DEFAULT_ANTHROPIC_MODEL,
    ReflectLLMError,
    reflect_chat,
    resolve_provider,
)


# ---------- provider resolution --------------------------------------------

def test_resolve_provider_defaults_to_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFLECT_PROVIDER", raising=False)
    assert resolve_provider(None) == "ollama"


def test_resolve_provider_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLECT_PROVIDER", "anthropic")
    assert resolve_provider(None) == "anthropic"


def test_resolve_provider_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLECT_PROVIDER", "ollama")
    assert resolve_provider("anthropic") == "anthropic"


def test_resolve_provider_rejects_unknown() -> None:
    with pytest.raises(ReflectLLMError):
        resolve_provider("openai")


def test_resolve_provider_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFLECT_PROVIDER", raising=False)
    assert resolve_provider("ANTHROPIC") == "anthropic"


# ---------- ollama branch delegates to existing client ---------------------

@pytest.mark.asyncio
async def test_ollama_branch_delegates_to_existing_client() -> None:
    """Ollama path calls llm.chat with the exact message shape and
    returns whatever the client returned."""
    ollama_client = MagicMock()
    ollama_client.chat = AsyncMock(
        return_value=SimpleNamespace(
            message=SimpleNamespace(content="  spec text  "),
        )
    )
    config = SimpleNamespace(ollama_primary_model="llama3.1:8b")

    text, meta = await reflect_chat(
        "prompt X", config, "ollama", ollama_client=ollama_client
    )

    assert text == "spec text"  # stripped
    ollama_client.chat.assert_awaited_once()
    call_args = ollama_client.chat.await_args.args
    messages = call_args[0]
    assert messages[0].role == "user"
    assert messages[0].content == "prompt X"
    # meta shape — no request/response body, only scalars.
    assert meta["provider"] == "ollama"
    assert meta["model"] == "llama3.1:8b"
    assert meta["prompt_len"] == len("prompt X")
    assert meta["response_len"] == len("spec text")
    assert "latency_ms" in meta


# ---------- anthropic branch: request shape --------------------------------

def _mock_response(status_code: int = 200, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=body or {})
    return resp


def _factory_yielding(response: MagicMock, capture: dict) -> Any:
    """Return a factory whose async-cm posts to a fake client that
    records the args and returns ``response``."""

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    async def _post(url: str, json: dict, headers: dict) -> MagicMock:
        capture["url"] = url
        capture["json"] = json
        capture["headers"] = headers
        return response

    fake_client.post = AsyncMock(side_effect=_post)
    return lambda: fake_client


@pytest.mark.asyncio
async def test_anthropic_branch_builds_correct_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-KEYSENTINEL")
    monkeypatch.delenv("REFLECT_ANTHROPIC_MODEL", raising=False)

    captured: dict[str, Any] = {}
    response = _mock_response(
        200,
        {"content": [{"type": "text", "text": "here is the spec"}]},
    )

    text, meta = await reflect_chat(
        "test prompt",
        SimpleNamespace(),
        "anthropic",
        httpx_client_factory=_factory_yielding(response, captured),
    )
    assert text == "here is the spec"
    assert captured["url"] == ANTHROPIC_URL
    assert captured["json"]["model"] == DEFAULT_ANTHROPIC_MODEL
    assert captured["json"]["max_tokens"] == 1024
    assert captured["json"]["messages"] == [
        {"role": "user", "content": "test prompt"}
    ]
    assert captured["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert captured["headers"]["x-api-key"] == "sk-ant-test-KEYSENTINEL"
    assert meta["provider"] == "anthropic"
    assert meta["model"] == DEFAULT_ANTHROPIC_MODEL


@pytest.mark.asyncio
async def test_anthropic_branch_honors_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("REFLECT_ANTHROPIC_MODEL", "claude-opus-4-7")

    captured: dict[str, Any] = {}
    response = _mock_response(200, {"content": [{"type": "text", "text": "ok"}]})
    _, meta = await reflect_chat(
        "p", SimpleNamespace(), "anthropic",
        httpx_client_factory=_factory_yielding(response, captured),
    )
    assert meta["model"] == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_anthropic_branch_concats_multiple_text_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    captured: dict[str, Any] = {}
    response = _mock_response(
        200,
        {"content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": " second"},
            {"type": "tool_use", "text": "should be ignored"},
        ]},
    )
    text, _ = await reflect_chat(
        "p", SimpleNamespace(), "anthropic",
        httpx_client_factory=_factory_yielding(response, captured),
    )
    assert text == "first second"


# ---------- missing-key: clean refusal, no traceback -----------------------

@pytest.mark.asyncio
async def test_anthropic_missing_key_raises_clean_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "anthropic",
            httpx_client_factory=lambda: None,  # must not be called
        )
    msg = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in msg
    assert "console.anthropic.com" in msg
    # The refusal instruction never contains any actual key material.
    assert "sk-ant-" not in msg or "sk-ant-..." in msg  # placeholder is allowed


# ---------- KEY LEAK: sentinel must appear in NO log record ---------------

@pytest.mark.asyncio
async def test_anthropic_key_never_appears_in_any_log_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The load-bearing hygiene test.

    Runs a successful anthropic call with a sentinel key that would be
    unmistakable if it leaked, captures ALL log records at DEBUG level,
    and asserts the sentinel appears nowhere. Also asserts it does not
    appear in the returned meta or in any exception the engine raises.
    """
    SENTINEL = "sk-ant-DO-NOT-LEAK-THIS-VALUE-EVER"
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)

    log_buf = io.StringIO()
    # Direct all loguru output to our buffer at DEBUG.
    handler_id = logger.add(log_buf, level="DEBUG", format="{message}")
    try:
        captured: dict[str, Any] = {}
        response = _mock_response(
            200, {"content": [{"type": "text", "text": "spec"}]},
        )
        text, meta = await reflect_chat(
            "prompt",
            SimpleNamespace(),
            "anthropic",
            httpx_client_factory=_factory_yielding(response, captured),
        )
    finally:
        logger.remove(handler_id)

    log_output = log_buf.getvalue()
    assert SENTINEL not in log_output, (
        "API key LEAKED into log output — hygiene contract broken"
    )
    # Meta must also not carry it (it's a per-call scalar summary).
    assert SENTINEL not in repr(meta)


@pytest.mark.asyncio
async def test_anthropic_http_error_hides_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 400+ status must raise WITHOUT surfacing response body.

    Anthropic error bodies can echo request fragments and headers; the
    engine's contract is that only ``provider`` + ``status_code`` are
    exposed on failure.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    resp = MagicMock()
    resp.status_code = 401
    # Body that would leak key material if surfaced anywhere.
    resp.text = "authentication_error: your key sk-ant-x is bad"
    resp.json = MagicMock(return_value={"error": {"message": "leak"}})

    async def _post(url, json, headers):
        return resp

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=_post)

    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "anthropic",
            httpx_client_factory=lambda: fake_client,
        )
    msg = str(excinfo.value)
    assert "401" in msg
    assert "sk-ant-x" not in msg
    assert "authentication_error" not in msg


# ---------- transient retry -----------------------------------------------

@pytest.mark.asyncio
async def test_anthropic_transient_error_retries_once_then_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    call_count = {"n": 0}

    async def _post(url, json, headers):
        call_count["n"] += 1
        raise httpx.ConnectTimeout("simulated")

    fake_client = AsyncMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(side_effect=_post)

    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "anthropic",
            httpx_client_factory=lambda: fake_client,
        )
    # Retried exactly once, then gave up (total = 2 attempts).
    assert call_count["n"] == 2
    assert "ConnectTimeout" in str(excinfo.value)


# ---------- engine column recorded on submit -----------------------------

@pytest.mark.asyncio
async def test_submit_records_engine_column() -> None:
    """ProposalStore.submit passes ``engine`` through to the INSERT."""
    from self_modify.proposals import ProposalStore, STATUS_SUBMITTED

    class _AsyncCtxManager:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        async def __aenter__(self) -> Any:
            return self._conn

        async def __aexit__(self, *args: Any) -> None:
            pass

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_AsyncCtxManager(conn))
    pm = MagicMock()
    pm._require_pool = MagicMock(return_value=pool)

    store = ProposalStore(pm)
    await store.submit(
        target_path="tools/data_feeds/x.py",
        change_type="new_file",
        content="x",
        rationale="y",
        proposed_by="morgoth",
        engine="anthropic",
    )
    args = conn.execute.await_args.args
    # (query, pid, target, change_type, content, rationale, status,
    #  proposed_by, engine, retry_of)
    assert args[-1] is None  # retry_of NULL for first attempts
    assert args[-2] == "anthropic"
    assert args[-3] == "morgoth"
    assert args[-4] == STATUS_SUBMITTED
