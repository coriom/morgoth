"""Goal-directed synthetic browser policy and privacy-safe trace proofs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.generalist_browser_lab import (
    BrowserAction, BrowserObservation, BrowserPlanningContext, BrowserRefusal,
    BrowserRunProgress, LexicalObservationPolicy, ObservedElement,
    OllamaBrowserActionSource, SyntheticTracePolicy, TaskInput, available_actions,
    run_isolated_policy,
)
from agents.generalist_browser_operator import _decision_verdict


ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")
REQUIRES_BROWSER = pytest.mark.skipif(
    not BROWSER.joinpath("chrome-headless-shell").is_file(),
    reason="reviewed Chromium unavailable in this sandbox")


def _observation(revision: int, *, text: str = "A", tag: str = "button") -> BrowserObservation:
    return BrowserObservation(revision=revision, origin="http://127.0.0.1:1",
                              title="Synthetic", text=text, elements=[ObservedElement(
                                  id="e0", tag=tag, role="", name="Open panel", href="",
                                  value="", input_type="text" if tag == "input" else "",
                                  disabled=False, readonly=False)])


def test_suppression_requires_exact_visible_state_and_binding() -> None:
    """A proven no-op disappears only in the identical state; stop stays."""
    context = BrowserPlanningContext()
    before = _observation(1)
    context.record(before, _observation(2),
                   BrowserAction(kind="click", revision=1, element_id="e0"))
    choices = available_actions(_observation(2))
    assert context.suppressed_choices(_observation(2), choices) == frozenset({"c0"})
    assert context.suppressed_choices(_observation(2, text="B"),
                                      available_actions(_observation(2, text="B"))) == frozenset()
    assert context.suppressed_choices(_observation(2, tag="input"),
                                      available_actions(_observation(2, tag="input"),
                                                        (TaskInput(id="target", label="Code", value="SKU-44"),))) == frozenset()
    assert choices[-1].id == "stop"


@pytest.mark.asyncio
async def test_native_choice_schema_excludes_proven_noop_without_fallback() -> None:
    """The model cannot select an omitted candidate; no alternative is chosen."""
    class Client:
        kwargs = None

        async def chat(self, _messages, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(message=SimpleNamespace(
                content='{"revision":2,"choice_id":"c0"}', tool_calls=[]),
                done=True, done_reason="stop")

    client = Client()
    source = OllamaBrowserActionSource(client, "fixture")
    with pytest.raises(BrowserRefusal, match="MODEL_CHOICE_NOT_AVAILABLE"):
        await source.propose("Open the panel", "SKU-44", _observation(2),
                             suppressed_ids=frozenset({"c0"}))
    assert client.kwargs["format"]["properties"]["choice_id"]["enum"] == ["stop"]
    assert client.kwargs["tools"] is None and client.kwargs["stream"] is False


def test_trace_rejects_changed_or_unreviewed_sources(tmp_path: Path) -> None:
    """A changed HTML source cannot inherit a reviewed trace vocabulary."""
    policy = SyntheticTracePolicy.from_checkout(ROOT, "heldout")
    assert policy.digest == "8d4d3d5dd624cc48057e4b999e31847fb96d51a769db8b6b58172e42e5101c01"
    fixtures = tmp_path / "tests/fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "generalist_browser_trace_manifest.json").write_bytes(
        (ROOT / "tests/fixtures/generalist_browser_trace_manifest.json").read_bytes())
    (fixtures / "generalist_browser_heldout.html").write_bytes(b"changed")
    with pytest.raises(BrowserRefusal, match="TRACE_SOURCE_UNVERIFIED"):
        SyntheticTracePolicy.from_checkout(tmp_path, "heldout")
    with pytest.raises(BrowserRefusal, match="TRACE_FIXTURE_UNREVIEWED"):
        SyntheticTracePolicy.from_checkout(ROOT, "external")


def test_trace_rejects_unmapped_page_control_without_disclosure() -> None:
    """Arbitrary page labels cannot enter the public fixture trace."""
    policy = SyntheticTracePolicy.from_checkout(ROOT, "heldout")
    before = _observation(1)
    transition = BrowserPlanningContext().record(
        before, _observation(2, text="changed"),
        BrowserAction(kind="click", revision=1, element_id="e0"))
    with pytest.raises(BrowserRefusal, match="TRACE_CONTROL_UNREVIEWED") as error:
        policy.event(before, BrowserAction(kind="click", revision=1, element_id="e0"),
                     transition, False)
    assert "Open panel" not in str(error.value)


def test_decision_gate_separates_unproductive_choices_from_service_failure() -> None:
    """A loop is decision evidence; a transport timeout is not."""
    progress = BrowserRunProgress(action_count=5, no_progress_count=3, loop_detected=True)
    assert _decision_verdict("BROWSER_LOOP_DETECTED", progress) == "MODEL_BROWSER_DECISION_LIMITED"
    assert _decision_verdict("MODEL_TIMEOUT", progress) == "MODEL_BROWSER_PROTOCOL_OR_RESOURCE_BLOCKED"
    assert _decision_verdict("MODEL_CALL_BUDGET_EXCEEDED", BrowserRunProgress(
        action_count=6, no_progress_count=0)) == "MODEL_BROWSER_PROTOCOL_OR_RESOURCE_BLOCKED"


class _ChoiceClient:
    """Mock-model decisions; the worker alone proves execution and acceptance."""

    def __init__(self, labels: tuple[str, ...]) -> None:
        self.labels = labels
        self.calls = 0
        self.observed_actions: list[list[dict]] = []
        self.evidence: list[dict] = []

    async def chat(self, messages, **kwargs):
        assert kwargs["tools"] is None and kwargs["stream"] is False
        data = json.loads(messages[1].content)
        options = data["observation"]["actions"]
        self.observed_actions.append(options)
        self.evidence.append(data["current_evidence"])
        wanted = self.labels[self.calls]
        self.calls += 1
        chosen = next(choice for choice in options if choice["name"] == wanted)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "revision": data["observation"]["revision"], "choice_id": chosen["id"]}),
            tool_calls=[]), done=True, done_reason="stop")


@pytest.mark.asyncio
@REQUIRES_BROWSER
async def test_actual_noop_suppression_and_safe_partial_trace() -> None:
    """One ineffective click is traced; its exact repeat is omitted next turn."""
    client = _ChoiceClient(("Inventory", "Inventory", "SKU", "Search stock",
                            "View SKU-44"))
    progress = BrowserRunProgress()
    result = await run_isolated_policy(
        checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
        objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
        source=OllamaBrowserActionSource(client, "fixture"), progress=progress,
        synthetic_trace=True)
    assert result["TASK_ACCEPTANCE_PASSED"] is True
    assert progress.action_count == 5 and progress.no_progress_count == 1
    assert progress.trace_source_verified and len(progress.synthetic_trace) == 5
    assert progress.synthetic_trace[0]["CONTROL"] == "C02"
    assert progress.synthetic_trace[1]["INEFFECTIVE"] is True
    assert progress.synthetic_trace[-1]["TASK_ACCEPTED"] is True
    assert not any(choice["name"] == "Inventory" for choice in client.observed_actions[2])
    assert client.evidence[2]["available_fill_choices"] == 1
    serialized = json.dumps(progress.synthetic_trace)
    assert all(private not in serialized for private in
               ("Inventory", "SKU-44", "http://", "Quick advice", "12 units"))


@pytest.mark.asyncio
@REQUIRES_BROWSER
async def test_trace_preserves_only_acknowledged_actions_after_model_failure() -> None:
    """A later model error cannot invent another synthetic decision record."""
    class FailingClient(_ChoiceClient):
        async def chat(self, messages, **kwargs):
            if self.calls == 1:
                raise RuntimeError("private response must never escape")
            return await super().chat(messages, **kwargs)

    progress = BrowserRunProgress()
    with pytest.raises(BrowserRefusal, match="MODEL_UNEXPECTED_FAILURE"):
        await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
            objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
            source=OllamaBrowserActionSource(FailingClient(("Inventory",)), "fixture"),
            progress=progress, synthetic_trace=True)
    assert progress.action_count == len(progress.synthetic_trace) == 1
    assert progress.synthetic_trace[0]["CONTROL"] == "C02"
    assert "private response" not in json.dumps(progress.synthetic_trace)


@pytest.mark.asyncio
@REQUIRES_BROWSER
@pytest.mark.parametrize("fixture,objective,target,expected", [
    ("heldout", "Check inventory stock item SKU-44 and report its quantity", "SKU-44", "12 units"),
    ("catalog", "Find the title for book BK-17 in the public catalog", "BK-17", "The Atlas of Rivers"),
    ("support", "Locate support request SR-9 and report its status", "SR-9", "Resolved"),
    ("relayout", "Check inventory stock item SKU-44 and report its quantity", "SKU-44", "12 units"),
])
async def test_offline_lexical_baseline_same_budget_and_checker(
        fixture: str, objective: str, target: str, expected: str) -> None:
    """The deterministic baseline is measured offline, never substituted live."""
    progress = BrowserRunProgress()
    result = await run_isolated_policy(
        checkout=ROOT, browser_dir=BROWSER, fixture=fixture,
        objective=objective, target=target, expected=expected,
        source=LexicalObservationPolicy(), max_model_calls=6,
        progress=progress, synthetic_trace=True)
    assert result["TASK_ACCEPTANCE_PASSED"] and progress.task_acceptance_passed
    assert progress.action_count == (6 if fixture == "relayout" else 4)
    assert progress.no_progress_count == 0 and not progress.loop_detected
    assert len(progress.synthetic_trace) == progress.action_count
    assert progress.trace_source_verified
