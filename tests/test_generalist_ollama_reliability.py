"""Hermetic, no-inference tests for local Ollama reliability evidence."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
import json
from types import SimpleNamespace

import httpx
import pytest

from agents import generalist_browser_lab as lab
from agents import generalist_ollama_reliability as reliability
from agents import generalist_ollama_resources as resources


def _config() -> SimpleNamespace:
    return SimpleNamespace(ollama_base_url="http://127.0.0.1:11434",
                           ollama_primary_model="fixture", ollama_num_ctx=4096)


def _metadata(*, loaded: bool = False) -> dict[str, object]:
    return {
        "OLLAMA_TAGS_RESPONSIVE": True, "OLLAMA_PS_RESPONSIVE": True,
        "OLLAMA_TAGS_STATUS": "OK", "OLLAMA_PS_STATUS": "OK",
        "OLLAMA_TAGS_ELAPSED_MS": 2, "OLLAMA_PS_ELAPSED_MS": 3,
        "MODEL_INSTALLED": True, "MODEL_SIZE_MIB": 1024,
        "MODEL_RESIDENCY": "LOADED" if loaded else "NOT_LOADED",
        "MODEL_REPORTED_VRAM_MIB": 512 if loaded else None,
        "OTHER_LOADED_MODEL_COUNT": 0,
        "COMPETING_INFERENCE_REQUESTS": "UNKNOWN", "CPU_GPU_PLACEMENT": "UNKNOWN",
    }


def _resources() -> dict[str, object]:
    return {
        "HOST_MEMORY_AVAILABLE_MIB": 4435, "HOST_SWAP_TOTAL_MIB": 1024,
        "HOST_SWAP_FREE_MIB": 900, "MEMORY_PRESSURE_AVG10_PCT": 0.2,
        "CGROUP_MEMORY_LIMIT_MIB": None, "CGROUP_MEMORY_CURRENT_MIB": 300,
        "CGROUP_MEMORY_HEADROOM_MIB": None, "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED",
        "CGROUP_SWAP_LIMIT_MIB": None, "CGROUP_SWAP_CURRENT_MIB": 0,
        "OLLAMA_PROCESS_COUNT": 1, "MODEL_RUNNER_PROCESS_COUNT": 0,
    }


@pytest.mark.asyncio
async def test_metadata_is_bounded_loopback_get_only(monkeypatch) -> None:
    """Installed and resident facts are extracted without exposing names or bodies."""
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append((request.method, request.url.path))
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "fixture", "size": 1048576000}]})
        return httpx.Response(200, json={"models": [
            {"name": "fixture", "size_vram": 536870912}, {"name": "other-private-model"}]})

    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(resources.httpx, "AsyncClient", factory)
    found = await resources.local_ollama_metadata("http://127.0.0.1:11434", "fixture")
    assert paths == [("GET", "/api/tags"), ("GET", "/api/ps")]
    assert found["MODEL_INSTALLED"] is True and found["MODEL_RESIDENCY"] == "LOADED"
    assert found["MODEL_REPORTED_VRAM_MIB"] == 512
    assert found["OLLAMA_TAGS_STATUS"] == found["OLLAMA_PS_STATUS"] == "OK"
    assert found["OTHER_LOADED_MODEL_COUNT"] == 1
    assert found["CPU_GPU_PLACEMENT"] == "UNKNOWN"
    assert "other-private-model" not in json.dumps(found)


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["https://example.com", "http://169.254.169.254:80",
                                  "http://127.0.0.1:11434/other",
                                  "http://user:secret@localhost:11434"])
async def test_non_loopback_or_credentialed_base_never_requests(monkeypatch, base: str) -> None:
    monkeypatch.setattr(resources.httpx, "AsyncClient", lambda **_kwargs: (
        _ for _ in ()).throw(AssertionError("no HTTP allowed")))
    result = await resources.local_ollama_metadata(base, "fixture")
    assert result["MODEL_INSTALLED"] is None and result["MODEL_RESIDENCY"] == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [(302, "REDIRECT_REFUSED"),
                                         (404, "HTTP_4XX"), (503, "HTTP_5XX")])
async def test_fixed_metadata_http_classes(monkeypatch, status: int, code: str) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(resources.httpx, "AsyncClient", lambda *args, **kwargs:
                        original(*args, transport=httpx.MockTransport(
                            lambda _r: httpx.Response(status, headers={"location": "http://example.com"})),
                                 **kwargs))
    result = await resources.local_ollama_metadata("http://127.0.0.1:11434", "fixture")
    assert result["OLLAMA_TAGS_STATUS"] == code
    assert result["MODEL_INSTALLED"] is None


@pytest.mark.asyncio
async def test_metadata_total_deadline_cancels_stalled_local_transport(monkeypatch) -> None:
    cancelled = False

    async def stalled(*_args):
        nonlocal cancelled
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled = True
            raise

    monkeypatch.setattr(resources, "_local_ollama_metadata_inner", stalled)
    monkeypatch.setattr(resources, "METADATA_TOTAL_SECONDS", 0.01)
    result = await resources.local_ollama_metadata("http://127.0.0.1:11434", "fixture")
    assert result["OLLAMA_TAGS_STATUS"] == "TOTAL_TIMEOUT"
    assert result["MODEL_INSTALLED"] is None and cancelled


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(302, headers={"location": "http://example.com"}),
    httpx.Response(200, content=b"{" + b"x" * 65536),
    httpx.Response(200, content=b'{"models":[{"name":"fixture","name":"other"}]}'),
    httpx.Response(200, json={"models": [{"name": ["fixture"]}]}),
])
async def test_redirect_oversize_duplicate_or_malformed_metadata_is_unknown(
        monkeypatch, response: httpx.Response) -> None:
    original = httpx.AsyncClient
    monkeypatch.setattr(resources.httpx, "AsyncClient", lambda *args, **kwargs:
                        original(*args, transport=httpx.MockTransport(lambda _r: response),
                                 **kwargs))
    result = await resources.local_ollama_metadata("http://127.0.0.1:11434", "fixture")
    assert result["MODEL_INSTALLED"] is None and result["MODEL_RESIDENCY"] == "UNKNOWN"
    assert result["OLLAMA_TAGS_STATUS"] in {"REDIRECT_REFUSED", "RESPONSE_OVERSIZE",
                                              "INVALID_JSON_OR_SCHEMA", "INVALID_SCHEMA"}


@pytest.mark.asyncio
async def test_non_generating_preflight_classifies_missing_ps_without_guess(monkeypatch) -> None:
    monkeypatch.setattr(reliability, "_checkout_verified", lambda: None)
    monkeypatch.setattr(reliability, "_local_model_config", _config)
    monkeypatch.setattr(reliability, "_tcp_probe", lambda *_args: True)
    monkeypatch.setattr(reliability, "local_resource_evidence", _resources)

    async def metadata(*_args):
        return {**_metadata(), "OLLAMA_PS_RESPONSIVE": False,
                "MODEL_RESIDENCY": "UNKNOWN"}

    monkeypatch.setattr(reliability, "local_ollama_metadata", metadata)
    monkeypatch.setattr(reliability, "OllamaLLMClient", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("no inference")))
    report = await reliability._evaluate()
    assert report["FAILURE_CLASS"] == "OLLAMA_PS_UNAVAILABLE"
    assert report["INFERENCE_ATTEMPTED"] is False
    assert report["CPU_GPU_PLACEMENT"] == "UNKNOWN"
    assert report["COMPETING_INFERENCE_REQUESTS"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_blocked_local_socket_stops_before_http_or_inference(monkeypatch) -> None:
    monkeypatch.setattr(reliability, "_checkout_verified", lambda: None)
    monkeypatch.setattr(reliability, "_local_model_config", _config)
    monkeypatch.setattr(reliability, "local_resource_evidence", _resources)
    monkeypatch.setattr(reliability, "_tcp_probe", lambda *_args: False)
    monkeypatch.setattr(reliability, "local_ollama_metadata", lambda *_args:
                        (_ for _ in ()).throw(AssertionError("no HTTP")))
    report = await reliability._evaluate(probe=True)
    assert report["FAILURE_CLASS"] == "LOCAL_TCP_CONNECT_BLOCKED"
    assert report["INFERENCE_ATTEMPTED"] is False


def _ready() -> dict[str, object]:
    return {"VERDICT": "NON_GENERATIVE_PREFLIGHT_COMPLETE", "FAILURE_CLASS": "NONE",
            "MODEL_RESIDENCY": "NOT_LOADED", **_resources(),
            "INFERENCE_ATTEMPTED": False, "RESPONSE_RECEIVED": False,
            "PRODUCTION_QUALIFIED": False}


@pytest.mark.asyncio
async def test_declined_consent_and_known_low_memory_make_zero_requests(monkeypatch) -> None:
    monkeypatch.setattr(reliability, "open_operator_terminal", lambda: nullcontext(object()))
    monkeypatch.setattr(reliability, "_consent", lambda *_args: False)
    monkeypatch.setattr(reliability, "OllamaLLMClient", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("no inference")))
    declined = await reliability._probe(_ready(), _config())
    assert declined["VERDICT"] == "PROBE_CONSENT_DECLINED"
    assert declined["INFERENCE_ATTEMPTED"] is False
    low = _ready()
    low["HOST_MEMORY_AVAILABLE_MIB"] = 100
    blocked = await reliability._probe(low, _config())
    assert blocked["VERDICT"] == "PROBE_RESOURCE_BLOCKED"
    assert blocked["INFERENCE_ATTEMPTED"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [(None, "STRUCTURED_RESPONSE_CONFIRMED"),
                                              (httpx.ReadTimeout("PRIVATE_SECRET"),
                                               "LOCAL_INFERENCE_BLOCKED")])
async def test_one_consented_call_reports_only_bounded_metrics_and_cleans_up(
        monkeypatch, failure: Exception | None, expected: str) -> None:
    calls = []

    class Client:
        closed = False

        def __init__(self, config, timeout):
            assert timeout == 90

        async def chat(self, messages, **kwargs):
            calls.append((messages, kwargs))
            if failure is not None:
                raise failure
            return SimpleNamespace(message=SimpleNamespace(
                content='{"revision":1,"choice_id":"c1"}', tool_calls=[]),
                done=True, done_reason="stop", total_duration=30_000_000,
                load_duration=10_000_000, prompt_eval_duration=8_000_000,
                eval_duration=5_000_000, prompt_eval_count=34, eval_count=9)

        async def close(self):
            self.closed = True

    client = Client(_config(), 90)
    monkeypatch.setattr(reliability, "OllamaLLMClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(reliability, "open_operator_terminal", lambda: nullcontext(object()))
    monkeypatch.setattr(reliability, "_consent", lambda *_args: True)
    monkeypatch.setattr(reliability, "local_resource_evidence", _resources)

    async def metadata(*_args):
        return _metadata(loaded=True)

    monkeypatch.setattr(reliability, "local_ollama_metadata", metadata)
    report = await reliability._probe(_ready(), _config())
    assert report["VERDICT"] == expected and report["INFERENCE_ATTEMPTED"] is True
    assert len(calls) == 1 and client.closed
    assert calls[0][1]["tools"] is None and calls[0][1]["stream"] is False
    assert set(calls[0][1]["format"]["required"]) == {"revision", "choice_id"}
    assert report["MODEL_RESIDENCY_BEFORE"] == "NOT_LOADED"
    assert report["MODEL_RESIDENCY_AFTER"] == "LOADED"
    assert report["MODEL_REQUEST_STARTED_AT_UTC"] <= report["MODEL_REQUEST_ENDED_AT_UTC"]
    assert "PRIVATE_SECRET" not in json.dumps(report)
    if failure is None:
        assert report["RESPONSE_RECEIVED"] is True
        assert report["MODEL_LOAD_DURATION_MS"] == 10
        assert report["PROMPT_EVAL_DURATION_MS"] == 8
        assert report["GENERATION_DURATION_MS"] == 5
    else:
        assert report["FAILURE_CLASS"] == "MODEL_TIMEOUT"
        assert report["RESPONSE_RECEIVED"] is False


@pytest.mark.asyncio
async def test_probe_does_not_run_if_residency_unknown(monkeypatch) -> None:
    monkeypatch.setattr(reliability, "open_operator_terminal", lambda:
                        (_ for _ in ()).throw(AssertionError("no consent")))
    report = _ready()
    report["MODEL_RESIDENCY"] = "UNKNOWN"
    result = await reliability._probe(report, _config())
    assert result["VERDICT"] == "PROBE_PREFLIGHT_BLOCKED"
    assert result["INFERENCE_ATTEMPTED"] is False


def test_timeout_hierarchy_is_coherent_without_extending_deadlines() -> None:
    facts = reliability._timeout_evidence()
    assert facts["FIRST_COLD_MODEL_SECONDS"] == 60
    assert facts["WORKER_ACTION_WAIT_SECONDS"] == 70
    assert facts["BROWSER_INTERACTION_SECONDS"] == 260
    assert facts["BROWSER_MAX_MODEL_CALLS"] == 6
    assert facts["BROWSER_DEADLINES_ORDERED"] is True
    assert lab.FIRST_COLD_MODEL_SECONDS == 60


def test_resource_sample_has_only_bounded_numbers_and_unknowns() -> None:
    """Linux-local metadata does not export paths, commands or model names."""
    evidence = resources.local_resource_evidence()
    assert evidence["CGROUP_MEMORY_LIMIT_KIND"] in {"UNKNOWN", "FINITE", "UNLIMITED"}
    assert all(value is None or type(value) is int and 0 <= value <= 1_000_000
               for key, value in evidence.items() if key.endswith("_MIB"))
    assert evidence["OLLAMA_PROCESS_COUNT"] is None or 0 <= evidence["OLLAMA_PROCESS_COUNT"] <= 4096
    assert "/proc/" not in json.dumps(evidence)


def test_tcp_probe_refuses_nonloopback_without_socket(monkeypatch) -> None:
    monkeypatch.setattr(reliability.socket, "create_connection", lambda *_args, **_kwargs:
                        (_ for _ in ()).throw(AssertionError("no socket")))
    for base in ("http://169.254.169.254:80", "https://localhost:11434",
                 "http://user:secret@localhost:11434", "http://localhost:11434/other"):
        assert reliability._tcp_probe(base) is False


def test_checkout_preflight_requires_live_origin_equality(monkeypatch) -> None:
    sha = "a" * 40
    values = {
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
        ("rev-parse", "HEAD"): sha,
        ("rev-parse", f"origin/{reliability.BRANCH}"): sha,
        ("branch", "--show-current"): reliability.BRANCH,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("ls-remote", "--heads", "origin", reliability.BRANCH):
            f"{'b' * 40}\trefs/heads/{reliability.BRANCH}",
    }
    monkeypatch.setattr(reliability, "_git", lambda *args: values[args])
    with pytest.raises(lab.BrowserRefusal, match="PREFLIGHT_CHECKOUT"):
        reliability._checkout_verified()
    values[("ls-remote", "--heads", "origin", reliability.BRANCH)] = (
        f"{sha}\trefs/heads/{reliability.BRANCH}")
    reliability._checkout_verified()
