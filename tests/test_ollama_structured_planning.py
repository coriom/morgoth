"""Protocol and privacy tests for opt-in Ollama structured planning."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from loguru import logger
import pytest

from agents.generalist_mission_planner import PlanningProposal
from core.llm_client import ChatMessage, OllamaLLMClient


def _client(handler):
    config = SimpleNamespace(ollama_base_url="http://127.0.0.1:11434",
                             ollama_primary_model="fixture-model", ollama_num_ctx=4096)
    client = OllamaLLMClient(config, timeout=1)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                       base_url=config.ollama_base_url)
    return client


def _reply() -> httpx.Response:
    return httpx.Response(200, json={"model": "fixture-model", "done": True,
                                     "done_reason": "stop", "message": {
                                         "role": "assistant", "content": "{}"}})


@pytest.mark.asyncio
async def test_schema_request_and_ordinary_chat_payloads_are_separate() -> None:
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payloads.append(json.loads(request.content))
        return _reply()

    client = _client(handler)
    try:
        message = [ChatMessage(role="user", content="synthetic mission")]
        schema = PlanningProposal.model_json_schema()
        await client.chat(message, model="fixture-model", tools=None, stream=False,
                          options={"temperature": 0, "num_predict": 640}, format=schema)
        await client.chat(message, model="fixture-model", tools=None, stream=False)
    finally:
        await client.close()
    assert payloads[0]["format"] == schema
    assert payloads[0]["stream"] is False
    assert "tools" not in payloads[0] and "think" not in payloads[0]
    assert payloads[0]["options"] == {"num_ctx": 4096, "temperature": 0,
                                      "num_predict": 640}
    assert "format" not in payloads[1] and "think" not in payloads[1]
    assert payloads[1]["options"] == {"num_ctx": 4096}


@pytest.mark.asyncio
async def test_structured_request_and_http_error_logs_never_contain_private_text() -> None:
    captured = []
    sink = logger.add(lambda message: captured.append(str(message)), level="DEBUG")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"TOP_SECRET_DO_NOT_PERSIST" in request.content
        return httpx.Response(400, json={"error": "TOP_SECRET_DO_NOT_PERSIST"})

    client = _client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat([ChatMessage(role="user", content="TOP_SECRET_DO_NOT_PERSIST")],
                              format=PlanningProposal.model_json_schema())
    finally:
        await client.close()
        logger.remove(sink)
    assert "TOP_SECRET_DO_NOT_PERSIST" not in "".join(captured)


def test_proposal_schema_bounds_every_field() -> None:
    schema = PlanningProposal.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(PlanningProposal.model_fields)
    criterion = schema["properties"]["completion_criteria"]
    assert criterion["minItems"] == 1 and criterion["maxItems"] == 4
    assert criterion["items"]["minLength"] == 8
    assert criterion["items"]["maxLength"] == 120
    for field in ("outcome", "uncertainty"):
        assert schema["properties"][field]["maxLength"] == 160
