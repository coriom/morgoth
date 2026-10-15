"""No-network protocol and failure attribution for the opt-in Ollama probe."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agents import generalist_ollama_plan_probe as probe
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_planner import PlanningProposal, ProposalEvidence


def _response(content: str | None, *, done: bool = True,
              reason: str | None = "stop", tool_calls: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(done=done, done_reason=reason,
                           message=SimpleNamespace(content=content, tool_calls=tool_calls))


def _proposal(catalog_id: str = "books", operation_id: str = "get_work") -> str:
    return json.dumps({"catalog_id": catalog_id, "operation_id": operation_id,
                       "outcome": "Provide a bounded public catalog record",
                       "completion_criteria": ["Required result fields pass schema validation"],
                       "uncertainty": "No external read has been authorized"})


class FakeClient:
    """Record local-only calls; never construct an HTTP transport."""

    def __init__(self, *, models: tuple[str, ...] = ("fixture-model:latest",),
                 response: object = None, error: Exception | None = None) -> None:
        self.models, self.response, self.error = models, response, error
        self.calls = 0
        self.closed = False
        self.options = None
        self.messages = None

    async def list_models(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=name) for name in self.models]

    async def chat(self, messages, **kwargs):
        self.calls += 1
        self.options = kwargs
        self.messages = messages
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, client: FakeClient) -> None:
    monkeypatch.setattr(probe, "_local_model_config", lambda: SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434", ollama_primary_model="fixture-model",
        ollama_num_ctx=4096))
    monkeypatch.setattr(probe, "OllamaLLMClient", lambda *args, **kwargs: client)
    monkeypatch.setattr(probe.logger, "remove", lambda: None)


@pytest.mark.asyncio
async def test_one_valid_proposal_uses_digest_verified_operation_choices(
        monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(response=_response(_proposal()))
    _install(monkeypatch, client)
    evidence = ProposalEvidence()
    await probe._one_proposal(evidence)
    assert evidence == ProposalEvidence(True, True, True, True, True, True)
    assert client.calls == 1 and client.closed
    assert client.options["tools"] is None and client.options["stream"] is False
    assert client.options["options"] == {"temperature": 0, "num_predict": 640}
    assert client.options["format"] == PlanningProposal.model_json_schema()
    assert client.options["format"]["additionalProperties"] is False
    assert set(client.options["format"]["required"]) == set(PlanningProposal.model_fields)
    assert client.options["format"]["properties"]["completion_criteria"]["maxItems"] == 4
    prompt = client.messages[1].content
    assert '"operation_id": "get_work"' in prompt
    assert '"operation_id": "get_route"' in prompt
    assert "execute POST /admin" not in prompt
    assert "https://" not in prompt


@pytest.mark.asyncio
async def test_absent_model_blocks_before_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(models=())
    _install(monkeypatch, client)
    evidence = ProposalEvidence()
    with pytest.raises(CapabilityRefusal, match="MODEL_NOT_INSTALLED"):
        await probe._one_proposal(evidence)
    assert evidence == ProposalEvidence()
    assert client.calls == 0 and client.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("response,error,expected", [
    (_response("", done=True), None, "RESPONSE_MISSING_OR_INCOMPLETE"),
    (_response(None, done=True), None, "RESPONSE_MISSING_OR_INCOMPLETE"),
    (_response("", done=False), None, "RESPONSE_MISSING_OR_INCOMPLETE"),
    (_response(_proposal(), reason="length"), None, "GENERATION_BUDGET_EXHAUSTED"),
    (_response(_proposal(), tool_calls=(object(),)), None, "FORBIDDEN_TOOL_ACTIVITY"),
    (_response("{invalid"), None, "INVALID_JSON"),
    (_response(_proposal().replace('"catalog_id": "books",',
                                  '"catalog_id": "books", "catalog_id": "transit",')),
     None, "DUPLICATE_JSON_KEY"),
    (_response("```json\n" + _proposal() + "\n```"), None, "INVALID_JSON"),
    (_response(""), None, "RESPONSE_MISSING_OR_INCOMPLETE"),
    (_response(_proposal()[:-1] + ', "extra": "TOP_SECRET_DO_NOT_PERSIST"}'),
     None, "INVALID_PROPOSAL_SCHEMA"),
    (_response(_proposal("invented")), None, "UNKNOWN_CATALOG_OR_OPERATION"),
    (_response(_proposal(operation_id="delete_work")), None, "UNKNOWN_CATALOG_OR_OPERATION"),
    (_response(_proposal("transit", "get_route")), None, "MISSION_CAPABILITY_INCOMPATIBLE"),
    (None, httpx.ConnectError("TOP_SECRET_DO_NOT_PERSIST"), "OLLAMA_UNREACHABLE"),
    (None, httpx.ReadTimeout("TOP_SECRET_DO_NOT_PERSIST"), "INFERENCE_TIMEOUT"),
    (None, httpx.HTTPStatusError("TOP_SECRET_DO_NOT_PERSIST", request=httpx.Request(
        "POST", "http://127.0.0.1/api/chat"), response=httpx.Response(500)),
     "MODEL_SERVICE_ERROR"),
    (None, httpx.HTTPStatusError("TOP_SECRET_DO_NOT_PERSIST", request=httpx.Request(
        "POST", "http://127.0.0.1/api/chat"), response=httpx.Response(400, json={
            "error": 'invalid format: expected "json" or a JSON schema'})),
     "MODEL_FORMAT_UNSUPPORTED"),
    (None, httpx.HTTPStatusError("TOP_SECRET_DO_NOT_PERSIST", request=httpx.Request(
        "POST", "http://127.0.0.1/api/chat"), response=httpx.Response(400, json={
            "error": 'invalid format: expected "json" or a JSON schema TOP_SECRET_DO_NOT_PERSIST'})),
     "MODEL_SERVICE_ERROR"),
])
async def test_model_failure_is_fixed_class_without_private_content(
        monkeypatch: pytest.MonkeyPatch, response: object, error: Exception | None,
        expected: str) -> None:
    client = FakeClient(response=response, error=error)
    _install(monkeypatch, client)
    evidence = ProposalEvidence()
    with pytest.raises(CapabilityRefusal) as caught:
        await probe._one_proposal(evidence)
    assert caught.value.code == expected
    assert "TOP_SECRET_DO_NOT_PERSIST" not in str(caught.value)
    assert evidence.inference_attempted and client.calls == 1 and client.closed
    assert evidence.response_received is (error is None)


@pytest.mark.asyncio
@pytest.mark.parametrize("content,expected,stages", [
    ("not json", "INVALID_JSON", (False, False, False, False)),
    (json.dumps({"catalog_id": "books"}), "INVALID_PROPOSAL_SCHEMA",
     (True, False, False, False)),
    (_proposal("invented"), "UNKNOWN_CATALOG_OR_OPERATION",
     (True, True, False, False)),
    (_proposal("transit", "get_route"), "MISSION_CAPABILITY_INCOMPATIBLE",
     (True, True, True, False)),
])
async def test_acceptance_stages_are_independent(monkeypatch: pytest.MonkeyPatch,
                                                  content: str, expected: str,
                                                  stages: tuple[bool, ...]) -> None:
    client = FakeClient(response=_response(content))
    _install(monkeypatch, client)
    evidence = ProposalEvidence()
    with pytest.raises(CapabilityRefusal, match=expected):
        await probe._one_proposal(evidence)
    assert (evidence.json_valid, evidence.proposal_validated,
            evidence.catalog_operation_validated, evidence.mission_compatible) == stages
    assert client.calls == 1 and client.closed


def test_public_report_never_emits_private_exception(monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """Even an unexpected private failure becomes a fixed non-disclosing code."""
    monkeypatch.setattr(probe, "_foreground_consent", lambda: True)
    monkeypatch.setattr(probe.sys, "argv", ["probe"])

    async def fail(_: ProposalEvidence) -> None:
        raise CapabilityRefusal("TOP_SECRET_DO_NOT_PERSIST")

    monkeypatch.setattr(probe, "_one_proposal", fail)
    assert probe.main() == 2
    output = capsys.readouterr().out
    assert "TOP_SECRET_DO_NOT_PERSIST" not in output
    assert json.loads(output) == {
        "VERDICT": "MODEL_PROPOSAL_BLOCKED", "FAILURE_CLASS": "PLANNING_FAILURE_UNKNOWN",
        "INFERENCE_ATTEMPTED": False, "RESPONSE_RECEIVED": False,
        "VALID_JSON": False, "VALID_PROPOSAL_SCHEMA": False,
        "CATALOG_OPERATION_VALIDATED": False, "MISSION_COMPATIBLE": False,
        "AUTHORIZATION_GRANTED": False,
        "MODEL_PROPOSAL": False, "INDEPENDENT_CONTRACT_VALIDATION": False,
        "EXECUTED_CAPABILITY": False, "MISSION_COMPLETED": False,
        "PRODUCTION_QUALIFIED": False}


@pytest.mark.asyncio
async def test_preflight_service_failure_does_not_attempt_inference(
        monkeypatch: pytest.MonkeyPatch) -> None:
    class Unreachable(FakeClient):
        async def list_models(self):
            raise httpx.ConnectError("TOP_SECRET_DO_NOT_PERSIST")

    client = Unreachable()
    _install(monkeypatch, client)
    evidence = ProposalEvidence()
    with pytest.raises(CapabilityRefusal, match="OLLAMA_UNREACHABLE"):
        await probe._one_proposal(evidence)
    assert evidence == ProposalEvidence()
    assert client.calls == 0 and client.closed


def test_invalid_local_config_is_fixed_and_no_model_is_created(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def reject():
        raise CapabilityRefusal("PRIVATE_CONFIG_PATH")

    monkeypatch.setattr(probe, "_local_model_config", reject)
    monkeypatch.setattr(probe, "OllamaLLMClient", lambda *args, **kwargs:
                        pytest.fail("client constructed before config validation"))
    with pytest.raises(CapabilityRefusal, match="LOCAL_CONFIG_REJECTED"):
        asyncio.run(probe._one_proposal(ProposalEvidence()))


def test_successful_public_report_does_not_claim_execution(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeClient(response=_response(_proposal()))
    _install(monkeypatch, client)
    monkeypatch.setattr(probe, "_foreground_consent", lambda: True)
    monkeypatch.setattr(probe.sys, "argv", ["probe"])
    assert probe.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["FAILURE_CLASS"] == "VALID_PROPOSAL"
    assert report["INFERENCE_ATTEMPTED"] is True
    assert report["RESPONSE_RECEIVED"] is True
    assert report["INDEPENDENT_CONTRACT_VALIDATION"] is True
    assert all(report[key] is True for key in (
        "VALID_JSON", "VALID_PROPOSAL_SCHEMA", "CATALOG_OPERATION_VALIDATED",
        "MISSION_COMPATIBLE"))
    assert report["AUTHORIZATION_GRANTED"] is False
    assert report["EXECUTED_CAPABILITY"] is False
    assert report["PRODUCTION_QUALIFIED"] is False


@pytest.mark.asyncio
async def test_cold_model_timeout_is_bounded_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    class Slow(FakeClient):
        async def chat(self, messages, **kwargs):
            self.calls += 1
            await asyncio.sleep(999)

    client = Slow()
    _install(monkeypatch, client)
    original = asyncio.wait_for

    async def quick(awaitable, timeout):
        if timeout == 90:
            return await original(awaitable, 0.01)
        return await original(awaitable, timeout)

    monkeypatch.setattr(probe.asyncio, "wait_for", quick)
    with pytest.raises(CapabilityRefusal, match="INFERENCE_TIMEOUT"):
        await probe._one_proposal(ProposalEvidence())
    assert client.calls == 1 and client.closed
