"""Consent, model choice, result classification and rendered baseline checks."""

from __future__ import annotations

from types import SimpleNamespace
import httpx

import pytest

from agents import generalist_browser_model_benchmark as benchmark
from agents import generalist_browser_benchmark as offline
from agents import generalist_browser_operator as operator
from agents.generalist_browser_lab import BrowserRefusal


def test_trial_classification_requires_independent_acceptance() -> None:
    """Valid actions and UI changes alone never become model qualification."""
    assert benchmark.classify_trial({"VERDICT": "MODEL_BROWSER_QUALIFIED",
                                     "TASK_ACCEPTANCE_PASSED": True}) == "SYNTHETIC_TASK_COMPLETED"
    assert benchmark.classify_trial({"VERDICT": "MODEL_BROWSER_QUALIFIED",
                                     "TASK_ACCEPTANCE_PASSED": False}) == "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
    assert benchmark.classify_trial({"VERDICT": "MODEL_BROWSER_DECISION_LIMITED",
                                     "ACTION_COUNT": 5}) == "MODEL_DECISION_LIMITED"
    assert benchmark.classify_trial({"FAILURE_CLASS": "MODEL_TIMEOUT"}) == "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
    assert benchmark.classify_trial({"FAILURE_CLASS": "MODEL_CALL_BUDGET_EXCEEDED",
                                     "ACTION_COUNT": 6}) == "MODEL_DECISION_LIMITED"


@pytest.mark.parametrize("metadata,resources,ready", [
    ({"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
      "MODEL_RESIDENCY": "NOT_LOADED", "OTHER_LOADED_MODEL_COUNT": 0,
      "MODEL_SIZE_MIB": 3000},
     {"HOST_MEMORY_AVAILABLE_MIB": 5000, "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED"}, True),
    ({"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
      "MODEL_RESIDENCY": "NOT_LOADED", "OTHER_LOADED_MODEL_COUNT": 0,
      "MODEL_SIZE_MIB": 4600},
     {"HOST_MEMORY_AVAILABLE_MIB": 5000, "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED"}, False),
    ({"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
      "MODEL_RESIDENCY": "NOT_LOADED", "OTHER_LOADED_MODEL_COUNT": 1,
      "MODEL_SIZE_MIB": 3000},
     {"HOST_MEMORY_AVAILABLE_MIB": 5000, "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED"}, False),
    ({"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
      "MODEL_RESIDENCY": "LOADED", "OTHER_LOADED_MODEL_COUNT": 0,
      "MODEL_SIZE_MIB": None},
     {"HOST_MEMORY_AVAILABLE_MIB": 5000, "CGROUP_MEMORY_LIMIT_KIND": "FINITE",
      "CGROUP_MEMORY_HEADROOM_MIB": 1500}, True),
    ({"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
      "MODEL_RESIDENCY": "LOADED", "OTHER_LOADED_MODEL_COUNT": 0,
      "MODEL_SIZE_MIB": None},
     {"HOST_MEMORY_AVAILABLE_MIB": 5000, "CGROUP_MEMORY_LIMIT_KIND": "UNKNOWN"}, False),
])
def test_alternative_model_needs_resource_evidence(metadata, resources, ready) -> None:
    """Unmeasured or pressured alternatives cannot be trialed optimistically."""
    assert operator._override_resource_ready(metadata, resources) is ready


def test_model_selection_is_tty_only_and_no_default_fallback(monkeypatch) -> None:
    """An explicit installed selection is required before the consent gate."""
    class Terminal:
        def __init__(self, answer: str):
            self.answer = answer
            self.lines = []

        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def write(self, value): self.lines.append(value)
        def flush(self): pass
        def readline(self, _limit): return self.answer

    terminal = Terminal("1\n")
    monkeypatch.setattr(benchmark, "open_operator_terminal", lambda: terminal)
    assert benchmark._operator_choice(["primary", "candidate"], "primary") == "candidate"
    assert any("candidate" in line for line in terminal.lines)
    monkeypatch.setattr(benchmark, "open_operator_terminal", lambda: Terminal("\n"))
    with pytest.raises(BrowserRefusal, match="MODEL_SELECTION_DECLINED"):
        benchmark._operator_choice(["primary", "candidate"], "primary")
    monkeypatch.setattr(benchmark, "open_operator_terminal", lambda: Terminal("4\n"))
    with pytest.raises(BrowserRefusal, match="MODEL_SELECTION_INVALID"):
        benchmark._operator_choice(["primary", "candidate"], "primary")


@pytest.mark.asyncio
async def test_selected_model_runs_one_trial_with_exact_task(monkeypatch) -> None:
    """The wrapper neither supplies a scripted action nor repeats inference."""
    monkeypatch.setattr(operator, "_checkout_verified", lambda *_args: None)
    monkeypatch.setattr(operator, "_sandbox_command", lambda *_args: ["bwrap"])
    monkeypatch.setattr(benchmark, "_local_model_config", lambda: SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434", ollama_primary_model="primary"))
    monkeypatch.setattr(benchmark, "_installed_names", lambda _base: _names())
    monkeypatch.setattr(benchmark, "_operator_choice", lambda _names, _configured: "candidate")
    seen = []

    async def _names(): return ["primary", "candidate"]

    async def fake_trial(**kwargs):
        seen.append(kwargs)
        return {"VERDICT": "MODEL_BROWSER_DECISION_LIMITED", "ACTION_COUNT": 5,
                "TASK_ACCEPTANCE_PASSED": False, "PRODUCTION_QUALIFIED": False}

    monkeypatch.setattr(operator, "_evaluate", fake_trial)
    report = await benchmark._evaluate("changed_layout")
    assert report["VERDICT"] == "MODEL_DECISION_LIMITED"
    assert report["COMPARATIVE_EVIDENCE"] == "INSUFFICIENT_COMPARATIVE_EVIDENCE"
    assert len(seen) == 1 and seen[0]["model_override"] == "candidate"
    assert seen[0]["fixture"] == "relayout"
    assert report["PRODUCTION_QUALIFIED"] is False


@pytest.mark.asyncio
async def test_installed_catalog_is_loopback_only_bounded_and_data_only(monkeypatch) -> None:
    """No model generation or proxy access is needed to enumerate candidates."""
    real_client = httpx.AsyncClient
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.method, request.url.path))
        return httpx.Response(200, json={"models": [
            {"name": "candidate:latest"}, {"name": "configured:latest"}]})

    monkeypatch.setattr(benchmark.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    assert await benchmark._installed_names("http://127.0.0.1:11434") == [
        "candidate:latest", "configured:latest"]
    assert observed == [("GET", "/api/tags")]
    with pytest.raises(BrowserRefusal, match="LOCAL_MODEL_CONFIG_REJECTED"):
        await benchmark._installed_names("http://example.com:11434")


@pytest.mark.asyncio
async def test_preflight_failure_never_attempts_trial(monkeypatch) -> None:
    """No model request follows a failed catalog or terminal selection."""
    monkeypatch.setattr(operator, "_checkout_verified", lambda *_args: None)
    monkeypatch.setattr(operator, "_sandbox_command", lambda *_args: ["bwrap"])
    monkeypatch.setattr(benchmark, "_local_model_config", lambda: SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434", ollama_primary_model="primary"))

    async def catalog(_base):
        raise BrowserRefusal("MODEL_CATALOG_INVALID")

    async def forbidden(**_kwargs):
        raise AssertionError("no trial")

    monkeypatch.setattr(benchmark, "_installed_names", catalog)
    monkeypatch.setattr(operator, "_evaluate", forbidden)
    report = await benchmark._evaluate("inventory")
    assert report["MODEL_CALL_COUNT"] == 0
    assert report["FAILURE_CLASS"] == "MODEL_CATALOG_INVALID"


@pytest.mark.asyncio
async def test_unmeasured_alternative_cannot_reach_inference(monkeypatch) -> None:
    """A locally installed model is not automatically safe to load."""
    monkeypatch.setattr(operator, "_checkout_verified", lambda *_args: None)
    monkeypatch.setattr(operator, "_sandbox_command", lambda *_args: ["bwrap"])
    monkeypatch.setattr(operator.SyntheticTracePolicy, "from_checkout",
                        lambda *_args: object())
    monkeypatch.setattr(operator, "_local_model_config", lambda: SimpleNamespace(
        ollama_base_url="http://127.0.0.1:11434", ollama_primary_model="primary"))

    class FakeClient:
        def __init__(self, *_args, **_kwargs): pass
        async def list_models(self):
            return [SimpleNamespace(name="primary"), SimpleNamespace(name="candidate")]
        async def chat(self, *_args, **_kwargs):
            raise AssertionError("unmeasured model must not infer")
        async def close(self): pass

    async def metadata(*_args):
        return {"MODEL_INSTALLED": True, "OLLAMA_PS_RESPONSIVE": True,
                "MODEL_RESIDENCY": "NOT_LOADED", "OTHER_LOADED_MODEL_COUNT": 0,
                "MODEL_SIZE_MIB": 4600}

    monkeypatch.setattr(operator, "OllamaLLMClient", FakeClient)
    monkeypatch.setattr(operator, "loaded_model_evidence", metadata)
    monkeypatch.setattr(operator, "local_ollama_metadata", metadata)
    monkeypatch.setattr(operator, "local_resource_evidence", lambda: {
        "HOST_MEMORY_AVAILABLE_MIB": 4800,
        "CGROUP_MEMORY_LIMIT_KIND": "UNLIMITED"})
    report = await operator._evaluate(branch=benchmark.BRANCH,
                                      model_override="candidate")
    assert report["FAILURE_CLASS"] == "ALTERNATIVE_MODEL_RESOURCE_UNVERIFIED"
    assert report["MODEL_CALL_COUNT"] == 0
    assert report["PRODUCTION_QUALIFIED"] is False


@pytest.mark.asyncio
async def test_lexical_reference_uses_actual_confined_browser() -> None:
    """Reference task completion comes from the same rendered acceptance gate."""
    if not operator.BROWSER.joinpath("chrome-headless-shell").is_file():
        pytest.skip("reviewed Chromium unavailable")
    results = await offline.evaluate_baseline()
    assert len(results) == 5
    assert all(row["ACTION_COUNT"] <= 6 for row in results)
    by_fixture = {row["FIXTURE"]: row for row in results}
    assert all(by_fixture[name]["TASK_ACCEPTANCE_PASSED"] for name in (
        "HELDOUT", "CATALOG", "SUPPORT", "RELAYOUT"))
    assert by_fixture["DEFERRED"]["TASK_ACCEPTANCE_PASSED"] is False
    assert by_fixture["DEFERRED"]["FAILURE_CLASS"] == "MODEL_ABSTAINED"
