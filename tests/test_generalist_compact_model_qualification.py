"""Hermetic gates for one selected-model probe and one conditional browser trial."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from agents import generalist_compact_model_qualification as compact
from agents import generalist_ollama_cold_load_r2 as r2
from agents.generalist_browser_lab import BrowserRefusal


def config() -> SimpleNamespace:
    return SimpleNamespace(ollama_base_url="http://127.0.0.1:11434",
                           ollama_primary_model="llama3.1:8b", ollama_num_ctx=4096)


def ready() -> dict[str, object]:
    return {"VERDICT": "NON_GENERATIVE_PREFLIGHT_COMPLETE", "FAILURE_CLASS": "NONE",
            "MODEL_RESIDENCY": "NOT_LOADED", "PRODUCTION_QUALIFIED": False,
            "INFERENCE_ATTEMPTED": False, "STAGE_A_QUALIFIED": False,
            "STAGE_B_ATTEMPTED": False}


def passed_action(elapsed: int = 12000) -> dict[str, object]:
    return {"MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
            "COMPLETE_RESPONSE": True, "STRUCTURED_ACTION_VALID": True,
            "CLIENT_ELAPSED_MS": elapsed, "FAILURE_CLASS": "NONE",
            "INFERENCE_ATTEMPTED": True, "CLASSIFICATIONS": [
                "COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE"],
            "MODEL_RESIDENCY_BEFORE": "NOT_LOADED",
            "MODEL_LOAD_DURATION_MS": 7000, "RESOURCE_SUMMARY": {
                "GPU_USED_MIB_MAX_OBSERVED": 2800}}


def test_checkout_is_exact_local_only_and_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    calls: list[tuple[str, ...]] = []
    values = {("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
              ("branch", "--show-current"): compact.BRANCH,
              ("rev-parse", "HEAD"): sha,
              ("rev-parse", f"origin/{compact.BRANCH}"): sha,
              ("status", "--porcelain=v1", "--untracked-files=all"): ""}

    def git(*args: str) -> str:
        calls.append(args)
        return values[args]

    monkeypatch.setattr(compact.operator, "_git", git)
    compact.verify_checkout(sha)
    assert all("ls-remote" not in call for call in calls)
    values[("status", "--porcelain=v1", "--untracked-files=all")] = "?? unknown"
    with pytest.raises(BrowserRefusal, match="PREFLIGHT_CHECKOUT"):
        compact.verify_checkout(sha)


def test_resource_gate_requires_measured_size_and_headroom() -> None:
    metadata = {"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
                "MODEL_RESIDENCY": "NOT_LOADED", "OTHER_LOADED_MODEL_COUNT": 0,
                "MODEL_SIZE_MIB": 2500}
    resources = {"HOST_MEMORY_AVAILABLE_MIB": 4000,
                 "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED"}
    assert compact._resource_ready(metadata, resources,
                                   {"GPU_MEMORY_FREE_MIB": 2500})
    assert not compact._resource_ready(metadata, {**resources,
               "HOST_MEMORY_AVAILABLE_MIB": 3000}, {"GPU_MEMORY_FREE_MIB": 2500})
    assert not compact._resource_ready(metadata, resources,
                                       {"GPU_MEMORY_FREE_MIB": None})
    assert not compact._resource_ready({**metadata,
               "OTHER_LOADED_MODEL_COUNT": 1}, resources,
                                       {"GPU_MEMORY_FREE_MIB": 2500})


@pytest.mark.asyncio
async def test_inventory_requires_exact_installed_digest_and_quantization(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def tags(*_args):
        return {"models": [{"name": "qwen3:4b-instruct", "digest": "b" * 64,
                            "details": {"quantization_level": "Q4_K_M"}}]}, 1
    monkeypatch.setattr(compact, "_bounded_metadata_get", tags)
    assert await compact._inventory(config().ollama_base_url,
                                    "qwen3:4b-instruct") == (1, "Q4_K_M", "b" * 64)
    assert await compact._inventory(config().ollama_base_url,
                                    "not-installed") == (1, None, None)

    async def malicious(*_args):
        return {"models": [{"name": "qwen3:4b-instruct", "digest": "not-a-digest",
                            "details": {"quantization_level": "Q4_K_M"}}]}, 1
    monkeypatch.setattr(compact, "_bounded_metadata_get", malicious)
    with pytest.raises(BrowserRefusal, match="MODEL_IDENTITY_UNVERIFIED"):
        await compact._inventory(config().ollama_base_url, "qwen3:4b-instruct")


@pytest.mark.asyncio
async def test_missing_model_and_unavailable_inventory_block_before_consent(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compact, "verify_checkout", lambda _sha: None)
    monkeypatch.setattr(compact, "_local_model_config", config)
    monkeypatch.setattr(compact, "_inventory", lambda _base, _model: asyncio.sleep(
        0, result=(1, None, None)))
    monkeypatch.setattr(compact, "_consented", lambda _word: (_ for _ in ()).throw(
        AssertionError("no consent without installed candidate")))
    absent = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
    assert absent["FAILURE_CLASS"] == "MODEL_NOT_INSTALLED"
    assert absent["INFERENCE_ATTEMPTED"] is False
    async def unavailable(_base: str, _model: str) -> tuple[int, str | None, str | None]:
        raise httpx.ConnectError("private text")
    monkeypatch.setattr(compact, "_inventory", unavailable)
    blocked = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
    assert blocked["FAILURE_CLASS"] == "LOCAL_METADATA_UNAVAILABLE"
    assert "private text" not in str(blocked)

    monkeypatch.setattr(compact, "_inventory", lambda _base, _model: asyncio.sleep(
        0, result=(1, "Q8_0", "b" * 64)))
    mismatch = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
    assert mismatch["FAILURE_CLASS"] == "MODEL_QUANTIZATION_MISMATCH"
    assert mismatch["INFERENCE_ATTEMPTED"] is False


@pytest.mark.asyncio
async def test_preflight_only_and_refused_stage_a_make_no_model_call(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(*_args):
        return ready(), config()
    monkeypatch.setattr(compact, "_preflight", preflight)
    monkeypatch.setattr(compact.r2, "run_one_request", lambda *_args, **_kwargs: (
        _ for _ in ()).throw(AssertionError("inference forbidden")))
    inspected = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M",
                                       preflight_only=True)
    assert inspected["INFERENCE_ATTEMPTED"] is False
    monkeypatch.setattr(compact, "_consented", lambda _word: False)
    declined = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
    assert declined["FAILURE_CLASS"] == "STAGE_A_CONSENT_REQUIRED"
    assert declined["STAGE_B_ATTEMPTED"] is False


@pytest.mark.asyncio
async def test_stage_a_invalid_slow_or_pressured_never_starts_browser(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(*_args):
        return ready(), config()
    monkeypatch.setattr(compact, "_preflight", preflight)
    monkeypatch.setattr(compact, "_consented", lambda word: word == "MODEL ONCE COMPACT")
    monkeypatch.setattr(compact.operator, "_evaluate", lambda **_kwargs: (
        _ for _ in ()).throw(AssertionError("browser stage forbidden")))
    for trial in (passed_action(65000), {**passed_action(),
                 "STRUCTURED_ACTION_VALID": False, "FAILURE_CLASS": "MODEL_INVALID_JSON"},
                  {**passed_action(), "CLASSIFICATIONS": ["RESOURCE_PRESSURE_OBSERVED"]}):
        async def stage_a(*_args, **_kwargs):
            return trial
        monkeypatch.setattr(compact.r2, "run_one_request", stage_a)
        result = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
        assert result["STAGE_A_QUALIFIED"] is False
        assert result["STAGE_B_ATTEMPTED"] is False


@pytest.mark.asyncio
async def test_valid_stage_a_hands_off_selected_model_to_separately_consenting_p1(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(*_args):
        return ready(), config()
    async def stage_a(_report, supplied, **kwargs):
        assert supplied.ollama_primary_model == "llama3.1:8b"
        assert kwargs["selected_model"] == "qwen3:4b-instruct"
        return passed_action()
    async def browser(**kwargs):
        assert kwargs == {"branch": compact.BRANCH, "fixture": "heldout",
                          "objective": compact.operator.OBJECTIVE,
                          "target": compact.operator.TARGET,
                          "expected": compact.operator.EXPECTED,
                          "model_override": "qwen3:4b-instruct", "hierarchical": True}
        return {"VERDICT": "MODEL_BROWSER_QUALIFIED", "FAILURE_CLASS": "NONE",
                "MODEL_CALL_COUNT": 5, "ACTION_COUNT": 4,
                "TASK_ACCEPTANCE_PASSED": True,
                "SYNTHETIC_ACTION_TRACE": [{"CONTROL": "C02", "UI_CHANGED": True}]}
    monkeypatch.setattr(compact, "_preflight", preflight)
    monkeypatch.setattr(compact, "_consented", lambda word: word == "MODEL ONCE COMPACT")
    monkeypatch.setattr(compact.r2, "run_one_request", stage_a)
    monkeypatch.setattr(compact, "_inventory", lambda _base, _model: asyncio.sleep(
        0, result=(2, "Q4_K_M", "a" * 64)))
    async def preflight_with_digest(*_args):
        return {**ready(), "MODEL_DIGEST_SHA256": "a" * 64}, config()
    monkeypatch.setattr(compact, "_preflight", preflight_with_digest)
    monkeypatch.setattr(compact.operator, "_evaluate", browser)
    result = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
    assert result["STAGE_A_QUALIFIED"] is True
    assert result["STAGE_B_ATTEMPTED"] is True
    assert result["VERDICT"] == "HIERARCHICAL_TASK_COMPLETED"
    assert result["TASK_ACCEPTANCE_PASSED"] is True
    assert result["PRODUCTION_QUALIFIED"] is False


@pytest.mark.asyncio
async def test_model_digest_drift_blocks_browser_stage(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(*_args):
        return {**ready(), "MODEL_DIGEST_SHA256": "a" * 64}, config()
    async def stage_a(*_args, **_kwargs):
        return passed_action()
    monkeypatch.setattr(compact, "_preflight", preflight)
    monkeypatch.setattr(compact, "_consented", lambda _word: True)
    monkeypatch.setattr(compact.r2, "run_one_request", stage_a)
    monkeypatch.setattr(compact, "_inventory", lambda _base, _model: asyncio.sleep(
        0, result=(2, "Q4_K_M", "b" * 64)))
    monkeypatch.setattr(compact.operator, "_evaluate", lambda **_kwargs: (
        _ for _ in ()).throw(AssertionError("browser stage must not run")))
    result = await compact.evaluate("qwen3:4b-instruct", "a" * 40, "Q4_K_M")
    assert result["STAGE_A_QUALIFIED"] is True
    assert result["STAGE_B_ATTEMPTED"] is False
    assert result["FAILURE_CLASS"] == "MODEL_IDENTITY_CHANGED_OR_UNAVAILABLE"


@pytest.mark.asyncio
async def test_selected_model_uses_same_single_request_schema_with_mock_http() -> None:
    sent: list[dict[str, object]] = []
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat" and request.url.host == "127.0.0.1"
        sent.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={
            "model": "qwen3:4b-instruct", "message": {"role": "assistant",
            "content": '{"revision":1,"choice_id":"c1"}'}, "done": True,
            "done_reason": "stop"})
    client = r2.DiagnosticChatClient(config(), selected_model="qwen3:4b-instruct",
                                     transport=httpx.MockTransport(handler))
    source = r2.ExtendedActionSource(client, "qwen3:4b-instruct", loaded=False)
    try:
        action = await source.propose("Check synthetic stock", "SKU-44",
                                      r2.reliability._synthetic_observation())
        assert action.kind == "click"
        assert source.calls == source.responses == 1
    finally:
        await client.close()
    assert len(sent) == 1 and sent[0]["model"] == "qwen3:4b-instruct"
    assert sent[0]["stream"] is False and "tools" not in sent[0]
    assert sent[0]["format"]["properties"]["revision"]["enum"] == [1]


@pytest.mark.asyncio
async def test_selected_model_refuses_unexpected_response_identity() -> None:
    client = r2.DiagnosticChatClient(
        config(), selected_model="qwen3:4b-instruct",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={
            "model": "another-model", "message": {"role": "assistant",
            "content": '{"revision":1,"choice_id":"c1"}'}, "done": True})))
    source = r2.ExtendedActionSource(client, "qwen3:4b-instruct", loaded=False)
    try:
        with pytest.raises(BrowserRefusal, match="MODEL_ENVELOPE_INVALID"):
            await source.propose("Check stock", "SKU-44",
                                 r2.reliability._synthetic_observation())
    finally:
        await client.close()
    assert source.calls == 1 and source.responses == 0


def test_stage_a_qualification_distinguishes_format_and_latency() -> None:
    assert compact._stage_a_qualified(passed_action())
    assert not compact._stage_a_qualified(passed_action(60001))
    assert not compact._stage_a_qualified({**passed_action(),
               "MODEL_RESPONSE_COUNT": 0})
    assert compact.STAGE_B_MAX_CALLS == 6
    assert compact.STAGE_A_DEADLINE_SECONDS == 120
