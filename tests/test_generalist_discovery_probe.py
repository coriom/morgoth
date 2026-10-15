"""No-inference operator probe checks and bounded public evidence."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agents import generalist_discovery_probe as probe
from test_generalist_discovery import intent


class FakeClient:
    def __init__(self, response, choice=None):
        self.response, self.choice, self.calls, self.closed = response, choice, 0, False

    async def list_models(self):
        return [SimpleNamespace(name="fixture-model")]

    async def chat(self, messages, **kwargs):
        self.calls += 1
        assert kwargs["tools"] is None and kwargs["stream"] is False
        assert "get_work" not in str(messages)
        assert "openlibrary" not in str(messages).lower()
        content = self.response if self.calls == 1 else self.choice
        return SimpleNamespace(done=True, done_reason="stop",
                               message=SimpleNamespace(content=content, tool_calls=[]))

    async def close(self):
        self.closed = True


def install(monkeypatch, client):
    monkeypatch.setattr(probe, "_local_model_config", lambda: SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434", ollama_primary_model="fixture-model",
        ollama_num_ctx=4096))
    monkeypatch.setattr(probe, "OllamaLLMClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(probe.logger, "remove", lambda: None)


@pytest.mark.asyncio
async def test_declined_consent_uses_zero_model_calls(monkeypatch):
    client = FakeClient(intent().model_dump_json())
    install(monkeypatch, client)
    monkeypatch.setattr(probe, "_foreground_consent", lambda: False)
    result = await probe._evaluate()
    assert result["FAILURE_CLASS"] == "LOCAL_CONSENT_REQUIRED"
    assert result["MODEL_CALL_COUNT"] == 0 and client.calls == 0 and client.closed
    assert result["PRODUCTION_QUALIFIED"] is False


@pytest.mark.asyncio
async def test_one_model_intent_discovers_nonlexical_top_without_authority(monkeypatch):
    client = FakeClient(intent(terms=("book", "bibliographic", "catalog")).model_dump_json(),
                        json.dumps({"catalog_id": "books", "abstain": False,
                                    "uncertainty": "Only index metadata has been inspected"}))
    install(monkeypatch, client)
    monkeypatch.setattr(probe, "_foreground_consent", lambda: True)
    result = await probe._evaluate()
    assert result["VERDICT"] == "MODEL_DISCOVERY_VALIDATED_PENDING_APPROVAL"
    assert all(result[key] is True for key in ("INTENT_PROPOSED", "DOCUMENT_DISCOVERED",
                                                "SOURCE_GROUNDED", "CONTRACT_VALIDATED",
                                                "ISOLATED_TEST_PASSED", "APPROVAL_PENDING"))
    assert result["LEXICAL_TOP_MATCHES_SELECTED"] is False
    assert result["DOCUMENT_CHOICE_PROPOSED"] is True
    assert result["MODEL_CALL_COUNT"] == client.calls == 2
    assert result["AUTHORIZATION_GRANTED"] is False
    assert result["EXECUTED_CAPABILITY"] is False
    assert "Synthetic work" not in json.dumps(result)
    assert client.closed


def test_unverified_checkout_blocks_before_evaluation(monkeypatch, capsys):
    monkeypatch.setattr(probe, "_checkout_verified", lambda: False)
    monkeypatch.setattr(probe.sys, "argv", ["probe"])
    monkeypatch.setattr(probe, "_evaluate", lambda: pytest.fail("must not evaluate"))
    assert probe.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result == {"VERDICT": "CHECKOUT_REJECTED", "PRODUCTION_QUALIFIED": False}
