"""No-inference tests for the isolated R2 cold-load experiment."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from agents import generalist_browser_lab as browser
from agents import generalist_ollama_cold_load_r2 as r2
from agents import generalist_ollama_runner_diagnostic as runner


def config() -> SimpleNamespace:
    return SimpleNamespace(ollama_base_url="http://127.0.0.1:11434",
                           ollama_primary_model="llama3.1:8b", ollama_num_ctx=4096)


def ready() -> dict[str, object]:
    return {"VERDICT": "NON_GENERATIVE_PREFLIGHT_COMPLETE",
            "MODEL_INSTALLED": True, "MODEL_RESIDENCY": "NOT_LOADED",
            "OTHER_LOADED_MODEL_COUNT": 0, "HOST_MEMORY_AVAILABLE_MIB": 4096,
            "GPU_MEMORY_FREE_MIB": 2000,
            "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED", "INFERENCE_ATTEMPTED": False,
            "PRODUCTION_QUALIFIED": False}


def valid_chat_response() -> dict[str, object]:
    return {"model": "llama3.1:8b", "message": {"role": "assistant",
            "content": '{"revision":1,"choice_id":"c1"}'}, "done": True,
            "done_reason": "stop", "total_duration": 80_000_000,
            "load_duration": 50_000_000, "prompt_eval_duration": 10_000_000,
            "eval_duration": 20_000_000, "prompt_eval_count": 50, "eval_count": 12}


@pytest.mark.asyncio
async def test_exact_one_tool_free_structured_loopback_request() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "127.0.0.1" and request.url.path == "/api/chat"
        assert request.method == "POST"
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json=valid_chat_response())

    client = r2.DiagnosticChatClient(config(), transport=httpx.MockTransport(handler))
    source = r2.ExtendedActionSource(client, config().ollama_primary_model, loaded=False)
    try:
        action = await source.propose("Check stock item SKU-44", "SKU-44",
                                      r2.reliability._synthetic_observation())
        assert action.kind == "click" and source.responses == 1
        with pytest.raises(browser.BrowserRefusal, match="PROBE_REQUEST_LIMIT"):
            await client.chat([], model="llama3.1:8b", tools=None, stream=False,
                              options={"temperature": 0, "num_predict": 384},
                              format={"required": ["revision", "choice_id"]})
    finally:
        await client.close()
    assert len(requests) == 1
    payload = requests[0]
    assert payload["stream"] is False and "tools" not in payload
    assert payload["options"] == {"num_ctx": 4096, "temperature": 0,
                                  "num_predict": 384}
    assert set(payload["format"]["required"]) == {"revision", "choice_id"}
    assert payload["format"]["properties"]["revision"]["enum"] == [1]
    assert r2.DIAGNOSTIC_REQUEST_SECONDS < r2.DIAGNOSTIC_OUTER_SECONDS < r2.HTTP_PHASE_TIMEOUT_SECONDS < r2.SHELL_TIMEOUT_SECONDS
    assert browser.FIRST_COLD_MODEL_SECONDS == r2.NORMAL_COLD_SECONDS == 60
    assert client.client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,expected", [
    (302, b"private redirect", "MODEL_HTTP_UNEXPECTED"),
    (500, b"private server body", "MODEL_HTTP_5XX"),
    (200, b"x" * (r2.MAX_CHAT_RESPONSE_BYTES + 1), "MODEL_ENVELOPE_INVALID"),
    (200, b'{"model":"x","model":"y"}', "MODEL_ENVELOPE_INVALID"),
])
async def test_transport_refuses_redirect_status_oversize_and_duplicate_keys(
        status: int, body: bytes, expected: str) -> None:
    response = httpx.Response(status, content=body, headers={"location": "http://example.com"})
    client = r2.DiagnosticChatClient(config(), transport=httpx.MockTransport(lambda _r: response))
    source = r2.ExtendedActionSource(client, config().ollama_primary_model, loaded=False)
    try:
        with pytest.raises(browser.BrowserRefusal, match=expected):
            await source.propose("Check stock item SKU-44", "SKU-44",
                                 r2.reliability._synthetic_observation())
    finally:
        await client.close()
    assert source.calls == 1 and source.responses == 0
    assert client.http_response_received is True


def test_preflight_rejects_loaded_missing_or_low_resource_states() -> None:
    assert r2._preflight_sufficient(ready(), config(), {"SERVICE_STATE": "ACTIVE"}) is None
    for change in ({"MODEL_RESIDENCY": "LOADED"}, {"MODEL_INSTALLED": False},
                   {"OTHER_LOADED_MODEL_COUNT": 1},
                   {"HOST_MEMORY_AVAILABLE_MIB": None},
                   {"HOST_MEMORY_AVAILABLE_MIB": 1000},
                   {"GPU_MEMORY_FREE_MIB": None},
                   {"GPU_MEMORY_FREE_MIB": 100},
                   {"CGROUP_MEMORY_LIMIT_KIND": "UNKNOWN"},
                   {"CGROUP_MEMORY_LIMIT_KIND": "FINITE",
                    "CGROUP_MEMORY_HEADROOM_MIB": 100}):
        assert r2._preflight_sufficient({**ready(), **change}, config(),
                                        {"SERVICE_STATE": "ACTIVE"}) is not None
    assert r2._preflight_sufficient(ready(), config(), {"SERVICE_STATE": "UNKNOWN"})
    assert r2._preflight_sufficient(ready(), SimpleNamespace(
        ollama_base_url="http://169.254.169.254:80",
        ollama_primary_model="llama3.1:8b", ollama_num_ctx=4096),
        {"SERVICE_STATE": "ACTIVE"})


@pytest.mark.asyncio
async def test_declined_consent_and_resource_refusal_send_no_request(
        monkeypatch: pytest.MonkeyPatch) -> None:
    async def preflight(_branch: str, checkout_verifier=None):
        assert callable(checkout_verifier)
        return ready(), config()

    monkeypatch.setattr(r2.reliability, "_preflight", preflight)
    monkeypatch.setattr(r2.runner, "service_evidence", lambda: {"SERVICE_STATE": "ACTIVE"})
    monkeypatch.setattr(r2.runner, "gpu_evidence", lambda: {"GPU_MEMORY_FREE_MIB": 2000})
    monkeypatch.setattr(r2, "open_operator_terminal", lambda: nullcontext(object()))
    monkeypatch.setattr(r2, "_consent", lambda *_: False)
    monkeypatch.setattr(r2, "run_one_request", lambda *_args: (_ for _ in ()).throw(
        AssertionError("inference was not authorized")))
    declined = await r2.evaluate("a" * 40)
    assert declined["FAILURE_CLASS"] == "LOCAL_CONSENT_REQUIRED"
    assert declined["INFERENCE_ATTEMPTED"] is False

    async def low(_branch: str, checkout_verifier=None):
        return {**ready(), "HOST_MEMORY_AVAILABLE_MIB": 100}, config()

    monkeypatch.setattr(r2.reliability, "_preflight", low)
    monkeypatch.setattr(r2, "open_operator_terminal", lambda: (_ for _ in ()).throw(
        AssertionError("consent should not be requested")))
    blocked = await r2.evaluate("a" * 40)
    assert blocked["FAILURE_CLASS"] == "RESOURCE_PREFLIGHT_REJECTED"
    assert blocked["INFERENCE_ATTEMPTED"] is False


def test_local_checkout_verification_never_queries_live_network(
        monkeypatch: pytest.MonkeyPatch) -> None:
    sha = "a" * 40
    seen: list[tuple[str, ...]] = []
    responses = {
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
        ("branch", "--show-current"): r2.BRANCH,
        ("rev-parse", "HEAD"): sha,
        ("rev-parse", f"origin/{r2.BRANCH}"): sha,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
    }

    def git(*args: str) -> str:
        seen.append(args)
        return responses[args]

    monkeypatch.setattr(r2.reliability, "_git", git)
    r2.verify_pinned_checkout(sha)
    assert all("ls-remote" not in args for args in seen)
    with pytest.raises(browser.BrowserRefusal, match="PREFLIGHT_CHECKOUT"):
        r2.verify_pinned_checkout("wrong")
    responses[("status", "--porcelain=v1", "--untracked-files=all")] = "?? untracked"
    with pytest.raises(browser.BrowserRefusal, match="PREFLIGHT_CHECKOUT"):
        r2.verify_pinned_checkout(sha)


@pytest.mark.asyncio
async def test_sampler_is_bounded_and_reports_missing_ps_as_unknown(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r2, "MAX_SAMPLES", 3)
    monkeypatch.setattr(r2, "SAMPLE_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(r2, "local_resource_evidence", lambda: {
        "HOST_MEMORY_AVAILABLE_MIB": 3000, "HOST_SWAP_FREE_MIB": 200,
        "MEMORY_PRESSURE_AVG10_PCT": 1.0})
    monkeypatch.setattr(r2, "_service_sample", lambda: (500, 0, 0))

    async def ps(*_args):
        return "UNKNOWN", None

    async def gpu():
        return None, None

    monkeypatch.setattr(r2, "_ps_state", ps)
    monkeypatch.setattr(r2, "_gpu_sample", gpu)
    stop = asyncio.Event()
    samples = await r2.sample_during_request(time.monotonic(), config().ollama_base_url,
                                              "llama3.1:8b", stop)
    assert len(samples) == 3
    assert all(row["MODEL_PS_STATE"] == "UNKNOWN" for row in samples)
    assert r2._summary(samples)["GPU_USED_MIB_MAX_OBSERVED"] is None
    assert all(0 <= row["ELAPSED_MS"] <= 200000 for row in samples)
    assert samples[0]["ELAPSED_MS"] <= samples[-1]["ELAPSED_MS"]


@pytest.mark.asyncio
async def test_ps_sampling_distinguishes_listed_from_absent_without_load_claim() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": []})

    async with httpx.AsyncClient(base_url=config().ollama_base_url,
                                 trust_env=False, follow_redirects=False,
                                 transport=httpx.MockTransport(handler)) as client:
        assert await r2._ps_state(config().ollama_base_url, "llama3.1:8b", client) == ("NOT_LISTED", None)
    def listed(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/ps"
        return httpx.Response(200, json={"models": [
            {"name": "llama3.1:8b", "size_vram": 5 * 1048576}]})

    async with httpx.AsyncClient(base_url=config().ollama_base_url,
                                 trust_env=False, follow_redirects=False,
                                 transport=httpx.MockTransport(listed)) as client:
        assert await r2._ps_state(config().ollama_base_url, "llama3.1:8b", client) == ("LISTED", 5)
    assert r2._summary([{"GPU_USED_MIB": 100, "GPU_FREE_MIB": 1000,
                         "HOST_AVAILABLE_MIB": 3000, "MEMORY_PRESSURE_AVG10_PCT": 2.0},
                        {"GPU_USED_MIB": 5400, "GPU_FREE_MIB": 300,
                         "HOST_AVAILABLE_MIB": 700, "MEMORY_PRESSURE_AVG10_PCT": 12.0}]) == {
        "SAMPLE_COUNT": 2, "GPU_USED_MIB_MAX_OBSERVED": 5400,
        "GPU_FREE_MIB_MIN_OBSERVED": 300,
        "MODEL_PS_VRAM_MIB_MAX_OBSERVED": None,
        "HOST_AVAILABLE_MIB_MIN_OBSERVED": 700,
        "SWAP_FREE_MIB_MIN_OBSERVED": None,
        "MEMORY_PRESSURE_AVG10_PCT_MAX_OBSERVED": 12.0,
        "SERVICE_MEMORY_MIB_MAX_OBSERVED": None,
        "SERVICE_CGROUP_OOM_COUNT_START": None,
        "SERVICE_CGROUP_OOM_COUNT_END": None,
        "SERVICE_CGROUP_OOM_KILL_COUNT_START": None,
        "SERVICE_CGROUP_OOM_KILL_COUNT_END": None}


@pytest.mark.asyncio
async def test_gpu_sampling_timeout_reaps_owned_process(
        monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.sleep(10)

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            return -9

    process = Process()

    async def create(*_args, **_kwargs):
        return process

    monkeypatch.setattr(r2.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(r2, "GPU_SAMPLE_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        await r2._gpu_sample()
    assert process.killed and process.waited


@pytest.mark.asyncio
async def test_timeout_cancels_one_request_and_closes_transport(
        monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = False

    class SlowClient:
        calls = 0
        closed = False
        http_response_received = False
        http_status = None

        async def chat(self, *_args, **_kwargs):
            nonlocal cancelled
            self.calls += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled = True
                raise

        async def close(self):
            self.closed = True

    async def samples(*_args):
        return []

    async def metadata(*_args):
        return {"MODEL_RESIDENCY": "UNKNOWN"}

    monkeypatch.setattr(r2, "DIAGNOSTIC_REQUEST_SECONDS", 0.01)
    monkeypatch.setattr(r2, "DIAGNOSTIC_OUTER_SECONDS", 0.02)
    monkeypatch.setattr(r2, "JOURNAL_SETTLE_SECONDS", 0)
    monkeypatch.setattr(r2, "sample_during_request", samples)
    monkeypatch.setattr(r2.runner, "journal_evidence", lambda *_: {
        "STATUS": "OK", "EVENTS": [], "TRUNCATED_BY_RECORD_LIMIT": False})
    monkeypatch.setattr(r2.reliability, "local_ollama_metadata", metadata)
    client = SlowClient()
    report = await r2.run_one_request(ready(), config(), client=client)
    assert cancelled and client.closed and client.calls == 1
    assert report["MODEL_CALL_COUNT"] == 1 and report["MODEL_RESPONSE_COUNT"] == 0
    assert report["FAILURE_CLASS"] == "MODEL_TIMEOUT"
    assert report["VERDICT"] == "EXTENDED_PROBE_TIMEOUT_UNRESOLVED"
    assert report["COMPLETE_RESPONSE"] is False


@pytest.mark.asyncio
async def test_valid_response_collects_metrics_without_exposing_content(
        monkeypatch: pytest.MonkeyPatch) -> None:
    private = "PRIVATE_MODEL_TEXT"

    class Client:
        calls = 0
        closed = False
        http_response_received = True
        http_status = 200

        async def chat(self, *_args, **_kwargs):
            self.calls += 1
            return SimpleNamespace(message=SimpleNamespace(
                content='{"revision":1,"choice_id":"c1"}', tool_calls=[]),
                done=True, done_reason="stop", total_duration=80_000_000,
                load_duration=50_000_000, prompt_eval_duration=10_000_000,
                eval_duration=20_000_000, prompt_eval_count=50, eval_count=12,
                private=private)

        async def close(self):
            self.closed = True

    async def samples(*_args):
        return [{"UTC": datetime.now(timezone.utc).isoformat(), "ELAPSED_MS": 1,
                 "GPU_USED_MIB": 5500, "GPU_FREE_MIB": 644,
                 "HOST_AVAILABLE_MIB": 3000, "SWAP_FREE_MIB": 1000,
                 "MEMORY_PRESSURE_AVG10_PCT": 2.0, "SERVICE_MEMORY_MIB": 2500,
                 "SERVICE_CGROUP_OOM_COUNT": 0,
                 "SERVICE_CGROUP_OOM_KILL_COUNT": 0,
                 "MODEL_PS_STATE": "NOT_LISTED"}]

    async def metadata(*_args):
        return {"MODEL_RESIDENCY": "LOADED"}

    monkeypatch.setattr(r2, "sample_during_request", samples)
    monkeypatch.setattr(r2.runner, "journal_evidence", lambda *_: {
        "STATUS": "OK", "EVENTS": [], "TRUNCATED_BY_RECORD_LIMIT": False})
    monkeypatch.setattr(r2.reliability, "local_ollama_metadata", metadata)
    monkeypatch.setattr(r2, "JOURNAL_SETTLE_SECONDS", 0)
    client = Client()
    report = await r2.run_one_request(ready(), config(), client=client)
    assert client.calls == 1 and client.closed
    assert report["COMPLETE_RESPONSE"] is True
    assert report["STRUCTURED_ACTION_VALID"] is True
    assert report["MODEL_LOAD_DURATION_MS"] == 50
    assert report["MODEL_RESIDENCY_AFTER"] == "LOADED"
    assert private not in json.dumps(report)
    assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []


def _timed_report(elapsed: int, *, failure: str = "NONE", complete: bool = True,
                  journal: list[dict[str, object]] | None = None) -> dict[str, object]:
    start = datetime(2026, 10, 22, 3, 45, 33, tzinfo=timezone.utc)
    return {"INFERENCE_ATTEMPTED": True, "COMPLETE_RESPONSE": complete,
            "FAILURE_CLASS": failure, "CLIENT_ELAPSED_MS": elapsed,
            "CLIENT_STARTED_UTC": start.isoformat(),
            "CLIENT_ENDED_UTC": (start + timedelta(milliseconds=elapsed)).isoformat(),
            "CLOCK_DISCONTINUITY": False,
            "PROBE_JOURNAL": {"STATUS": "OK", "TRUNCATED_BY_RECORD_LIMIT": False,
                              "EVENTS": journal or []},
            "RESOURCE_SUMMARY": {"GPU_FREE_MIB_MIN_OBSERVED": 1000,
                                 "HOST_AVAILABLE_MIB_MIN_OBSERVED": 3000}}


def test_classifications_distinguish_completion_failure_ready_and_pressure() -> None:
    assert r2.classify_probe(_timed_report(59000)) == [
        "COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE"]
    assert r2.classify_probe(_timed_report(65000)) == [
        "COLD_LOAD_COMPLETED_AFTER_NORMAL_DEADLINE"]
    assert r2.classify_probe(_timed_report(120000, failure="MODEL_TIMEOUT", complete=False)) == [
        "EXTENDED_PROBE_TIMEOUT_UNRESOLVED"]
    start = datetime(2026, 10, 22, 3, 45, 33, tzinfo=timezone.utc)
    prior = {"UTC": (start + timedelta(seconds=3)).isoformat(),
             "CLASSES": ["RUNNER_FAILURE_REPORTED"]}
    after = {"UTC": (start + timedelta(seconds=121)).isoformat(),
             "CLASSES": ["RUNNER_FAILURE_REPORTED", "CANCELLATION_REPORTED"]}
    historical = {"UTC": (start - timedelta(minutes=1)).isoformat(),
                  "CLASSES": ["RUNNER_FAILURE_REPORTED"]}
    assert r2.classify_probe(_timed_report(120000, failure="MODEL_TIMEOUT",
                                           complete=False, journal=[historical, after])) == [
        "EXTENDED_PROBE_TIMEOUT_UNRESOLVED"]
    assert "RUNNER_FAILURE_BEFORE_CLIENT_CANCELLATION" in r2.classify_probe(
        _timed_report(120000, failure="MODEL_TIMEOUT", complete=False,
                      journal=[prior, after]))
    load_failure = {"UTC": (start + timedelta(seconds=4)).isoformat(),
                    "CLASSES": ["MODEL_LOAD_FAILURE_REPORTED"]}
    assert "RUNNER_FAILURE_BEFORE_CLIENT_CANCELLATION" in r2.classify_probe(
        _timed_report(120000, failure="MODEL_TIMEOUT", complete=False,
                      journal=[load_failure]))
    ready_event = {"UTC": (start + timedelta(seconds=5)).isoformat(),
                   "CLASSES": ["RUNNER_READY_REPORTED"]}
    assert "MODEL_READY_BUT_INFERENCE_STALLED" in r2.classify_probe(
        _timed_report(120000, failure="MODEL_TIMEOUT", complete=False,
                      journal=[ready_event]))
    low = _timed_report(120000, failure="MODEL_TIMEOUT", complete=False)
    low["RESOURCE_SUMMARY"] = {"GPU_FREE_MIB_MIN_OBSERVED": 100}
    assert "RESOURCE_PRESSURE_OBSERVED" in r2.classify_probe(low)
    malformed = _timed_report(80000, failure="MODEL_INVALID_JSON", complete=True)
    assert r2.classify_probe(malformed) == ["COLD_LOAD_COMPLETED_AFTER_NORMAL_DEADLINE",
                                            "PROTOCOL_OR_PREFLIGHT_BLOCKED"]
    discontinuity = _timed_report(120000, failure="MODEL_TIMEOUT", complete=False,
                                  journal=[prior])
    discontinuity["CLOCK_DISCONTINUITY"] = True
    assert r2.classify_probe(discontinuity) == ["EXTENDED_PROBE_TIMEOUT_UNRESOLVED"]


def test_journal_ready_classifier_is_not_fooled_by_negative_status() -> None:
    assert "RUNNER_READY_REPORTED" not in runner.classify_message("runner not ready; waiting")
    assert "RUNNER_READY_REPORTED" in runner.classify_message("runner ready")
