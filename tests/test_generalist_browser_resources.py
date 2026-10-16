"""No-inference checks for bounded browser-model runtime evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

from agents import generalist_browser_lab as lab
from agents import generalist_browser_operator as operator
from agents import generalist_ollama_resources as resources


def _observation() -> lab.BrowserObservation:
    return lab.BrowserObservation(revision=1, origin="http://127.0.0.1:1",
                                  title="Synthetic", text="X" * 1200, elements=[])


class _DelayedClient:
    def __init__(self, delays: list[float]) -> None:
        self.delays = delays
        self.calls = 0

    async def chat(self, *_args, **_kwargs):
        delay = self.delays[self.calls]
        self.calls += 1
        await asyncio.sleep(delay)
        return SimpleNamespace(message=SimpleNamespace(
            content='{"revision":1,"choice_id":"stop"}',
            tool_calls=[]), done=True, done_reason="stop", total_duration=40_000_000,
            load_duration=20_000_000, prompt_eval_count=42, eval_count=12)


@pytest.mark.asyncio
async def test_cold_loaded_and_followup_budgets_and_metrics(monkeypatch) -> None:
    """Cold loading gets one bounded allowance; later calls cannot inherit it."""
    client = _DelayedClient([0, 0])
    source = lab.OllamaBrowserActionSource(client, "fixture", loaded=False)
    assert source.next_timeout_seconds() == lab.FIRST_COLD_MODEL_SECONDS
    assert lab.OllamaBrowserActionSource(client, "fixture", loaded=True).next_timeout_seconds() == lab.FIRST_LOADED_MODEL_SECONDS
    await source.propose("Find", "A", _observation())
    assert source.next_timeout_seconds() == lab.FOLLOWUP_MODEL_SECONDS
    await source.propose("Find", "A", _observation())
    assert source.calls == source.responses == 2
    assert source.last_total_ms == 40 and source.last_load_ms == 20
    assert source.last_prompt_tokens == 42 and source.last_output_tokens == 12
    assert source.total_elapsed_ms >= source.max_elapsed_ms >= 0


@pytest.mark.asyncio
async def test_slow_first_and_fast_followup_and_timeout(monkeypatch) -> None:
    """One slow response is measured; a timeout records an attempt, not a response."""
    monkeypatch.setattr(lab, "FIRST_COLD_MODEL_SECONDS", 0.03)
    source = lab.OllamaBrowserActionSource(_DelayedClient([0.005, 0]), "fixture", loaded=False)
    await source.propose("Find", "A", _observation())
    await source.propose("Find", "A", _observation())
    assert source.calls == source.responses == 2
    timed = lab.OllamaBrowserActionSource(_DelayedClient([0.1]), "fixture", loaded=False)
    with pytest.raises(lab.BrowserRefusal, match="MODEL_TIMEOUT"):
        await timed.propose("Find", "A", _observation())
    assert timed.calls == 1 and timed.responses == 0
    assert timed.last_elapsed_ms is not None and timed.http_response_received is False


@pytest.mark.asyncio
async def test_later_timeout_does_not_republish_prior_response_metrics(monkeypatch) -> None:
    """A failed later request retains only its own elapsed time."""
    monkeypatch.setattr(lab, "FOLLOWUP_MODEL_SECONDS", 0.02)
    source = lab.OllamaBrowserActionSource(_DelayedClient([0, 0.1]), "fixture")
    await source.propose("Find", "A", _observation())
    with pytest.raises(lab.BrowserRefusal, match="MODEL_TIMEOUT"):
        await source.propose("Find", "A", _observation())
    assert source.calls == 2 and source.responses == 1
    assert source.last_total_ms is None and source.last_load_ms is None
    assert source.last_elapsed_ms is not None


@pytest.mark.asyncio
async def test_ps_loaded_absent_and_untrusted_destinations(monkeypatch) -> None:
    """Only loopback /api/ps is queried; VRAM is reported metadata, not placement proof."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"models": [{"name": "fixture:latest",
                                                       "size_vram": 2 * 1048576}]})

    original = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(resources.httpx, "AsyncClient", client_factory)
    found = await resources.loaded_model_evidence("http://127.0.0.1:11434", "fixture")
    absent = await resources.loaded_model_evidence("http://127.0.0.1:11434", "other")
    denied = await resources.loaded_model_evidence("https://public.invalid", "fixture")
    assert found == {"MODEL_RESIDENCY": "LOADED", "MODEL_REPORTED_VRAM_MIB": 2}
    assert absent["MODEL_RESIDENCY"] == "NOT_LOADED"
    assert denied["MODEL_RESIDENCY"] == "UNKNOWN"
    assert len(seen) == 2 and all(path.endswith("/api/ps") for path in seen)


@pytest.mark.asyncio
async def test_ps_oversize_and_malformed_are_unknown(monkeypatch) -> None:
    """Malformed resource metadata cannot become a confident residency claim."""
    original = httpx.AsyncClient
    payloads = [httpx.Response(200, content=b"x" * 65537),
                httpx.Response(200, json={"models": "wrong"})]

    def client_factory(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(lambda _r: payloads.pop(0)),
                        **kwargs)

    monkeypatch.setattr(resources.httpx, "AsyncClient", client_factory)
    for _ in range(2):
        result = await resources.loaded_model_evidence("http://127.0.0.1:11434", "fixture")
        assert result["MODEL_RESIDENCY"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_worker_wait_outlives_any_model_request_and_parent_cleans_up(monkeypatch) -> None:
    """Deadline cancellation kills an owned worker and preserves observation evidence."""
    assert lab.WORKER_ACTION_WAIT_SECONDS > lab.FIRST_COLD_MODEL_SECONDS
    assert lab.WORKER_ACTION_WAIT_SECONDS > lab.FOLLOWUP_MODEL_SECONDS
    observation = _observation().model_dump_json()
    child_code = ("import sys,time,json\n"
                  "sys.stdin.readline()\n"
                  f"print(json.dumps({{'type':'observation','value':json.loads({observation!r})}}),flush=True)\n"
                  "time.sleep(10)\n")
    monkeypatch.setattr(lab, "_sandbox_command", lambda *_args: [sys.executable, "-c", child_code])
    monkeypatch.setattr(lab, "INTERACTIVE_SECONDS", 1.2)
    original = asyncio.create_subprocess_exec
    children = []

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(lab.asyncio, "create_subprocess_exec", capture)

    class SlowSource:
        calls = 0

        async def propose(self, *_args):
            self.calls += 1
            await asyncio.sleep(10)

    source = SlowSource()
    progress = lab.BrowserRunProgress()
    with pytest.raises(lab.BrowserRefusal, match="INTERACTIVE_TIMEOUT"):
        await lab.run_isolated_policy(checkout=Path("/tmp"), browser_dir=Path("/tmp"),
                                      fixture="heldout", objective="Find", target="A",
                                      expected="B", source=source, progress=progress)
    assert progress.page_observed and progress.action_count == 0
    assert source.calls == 1 and len(children) == 1 and children[0].returncode is not None


@pytest.mark.asyncio
async def test_worker_input_timeout_is_fixed_failure(monkeypatch) -> None:
    """The independent browser worker refuses a missing action without replay."""
    monkeypatch.setattr(lab, "WORKER_ACTION_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(lab.asyncio, "to_thread", lambda *_args: asyncio.sleep(0.1))
    with pytest.raises(lab.BrowserRefusal, match="ACTION_RESPONSE_MISSING"):
        await lab._worker_action_line()


@pytest.mark.asyncio
async def test_do_not_start_model_call_without_remaining_budget(monkeypatch) -> None:
    """An impossible last request is refused before it consumes model authority."""
    observation = _observation().model_dump_json()
    child_code = ("import sys,time,json\n"
                  "sys.stdin.readline()\n"
                  f"print(json.dumps({{'type':'observation','value':json.loads({observation!r})}}),flush=True)\n"
                  "time.sleep(10)\n")
    monkeypatch.setattr(lab, "_sandbox_command", lambda *_args: [sys.executable, "-c", child_code])
    monkeypatch.setattr(lab, "INTERACTIVE_SECONDS", 1)
    source = lab.OllamaBrowserActionSource(_DelayedClient([0]), "fixture", loaded=False)
    progress = lab.BrowserRunProgress()
    with pytest.raises(lab.BrowserRefusal, match="MODEL_BUDGET_EXHAUSTED"):
        await lab.run_isolated_policy(checkout=Path("/tmp"), browser_dir=Path("/tmp"),
                                      fixture="heldout", objective="Find", target="A",
                                      expected="B", source=source, progress=progress)
    assert progress.page_observed and source.calls == 0


@pytest.mark.asyncio
async def test_parent_cancellation_reaps_owned_worker(monkeypatch) -> None:
    """Cancelled inference cannot leave its browser-worker process alive."""
    observation = _observation().model_dump_json()
    child_code = ("import sys,time,json\n"
                  "sys.stdin.readline()\n"
                  f"print(json.dumps({{'type':'observation','value':json.loads({observation!r})}}),flush=True)\n"
                  "time.sleep(10)\n")
    monkeypatch.setattr(lab, "_sandbox_command", lambda *_args: [sys.executable, "-c", child_code])
    original = asyncio.create_subprocess_exec
    children = []

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(lab.asyncio, "create_subprocess_exec", capture)

    class SlowSource:
        async def propose(self, *_args):
            await asyncio.sleep(10)

    progress = lab.BrowserRunProgress()
    task = asyncio.create_task(lab.run_isolated_policy(
        checkout=Path("/tmp"), browser_dir=Path("/tmp"), fixture="heldout",
        objective="Find", target="A", expected="B", source=SlowSource(),
        progress=progress))
    for _ in range(100):
        if progress.page_observed:
            break
        await asyncio.sleep(0.01)
    assert progress.page_observed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(children) == 1 and children[0].returncode is not None
    assert progress.action_count == 0


@pytest.mark.asyncio
async def test_low_memory_preflight_blocks_before_consent_or_inference(monkeypatch) -> None:
    """Known resource pressure is reported without asking for model authority."""
    monkeypatch.setattr(operator, "_checkout_verified", lambda: None)
    monkeypatch.setattr(operator, "_sandbox_command", lambda *args: ["bwrap"])
    monkeypatch.setattr(operator, "_local_model_config", lambda: SimpleNamespace(
        ollama_primary_model="fixture", ollama_base_url="http://127.0.0.1:11434"))
    monkeypatch.setattr(operator, "_memory_evidence", lambda: {
        "HOST_MEMORY_AVAILABLE_MIB": 800, "CGROUP_MEMORY_HEADROOM_MIB": None})

    class FakeClient:
        def __init__(self, *_args, **_kwargs): pass
        async def list_models(self): return [SimpleNamespace(name="fixture")]
        async def chat(self, *_args, **_kwargs): raise AssertionError("no inference")
        async def close(self): pass

    async def ps(*_args):
        return {"MODEL_RESIDENCY": "NOT_LOADED", "MODEL_REPORTED_VRAM_MIB": None}

    monkeypatch.setattr(operator, "OllamaLLMClient", FakeClient)
    monkeypatch.setattr(operator, "loaded_model_evidence", ps)
    monkeypatch.setattr(operator, "open_operator_terminal", lambda: (_ for _ in ()).throw(
        AssertionError("no consent after failed preflight")))
    result = await operator._evaluate()
    assert result["FAILURE_CLASS"] == "RESOURCE_PREFLIGHT_LOW_MEMORY"
    assert result["MODEL_CALL_COUNT"] == 0 and result["PRODUCTION_QUALIFIED"] is False


def test_operator_requires_live_remote_equality(monkeypatch) -> None:
    """A stale local origin-tracking ref cannot authorize operator inference."""
    sha = "a" * 40
    values = {
        ("rev-parse", "HEAD"): sha,
        ("rev-parse", f"origin/{operator.BRANCH}"): sha,
        ("branch", "--show-current"): operator.BRANCH,
        ("status", "--porcelain=v1", "--untracked-files=all"): "",
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
        ("ls-remote", "--heads", "origin", operator.BRANCH):
            f"{'b' * 40}\trefs/heads/{operator.BRANCH}",
    }
    monkeypatch.setattr(operator, "_git", lambda *args: values[args])
    with pytest.raises(lab.BrowserRefusal, match="PREFLIGHT_CHECKOUT"):
        operator._checkout_verified()
    values[("ls-remote", "--heads", "origin", operator.BRANCH)] = (
        f"{sha}\trefs/heads/{operator.BRANCH}")
    operator._checkout_verified()
