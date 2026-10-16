"""Verified browser transitions and loop refusal; no live model inference."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.generalist_browser_lab import (
    BrowserAction, BrowserObservation, BrowserPlanningContext, BrowserRefusal,
    BrowserRunProgress, ObservedElement, OllamaBrowserActionSource,
    run_isolated_policy,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")


def _state(revision: int, text: str, value: str = "") -> BrowserObservation:
    return BrowserObservation(revision=revision, origin="http://127.0.0.1:1",
                              title="Fixture", text=text, elements=[
                                  ObservedElement(id="e0", tag="input", role="",
                                                  name="Stock code", href="", value=value,
                                                  input_type="text", disabled=False,
                                                  readonly=False)])


def test_revision_is_not_progress_and_repeated_fill_blocks() -> None:
    """Only changed rendered evidence counts; a repeated ineffective fill loops."""
    context = BrowserPlanningContext()
    fill = BrowserAction(kind="fill", revision=1, element_id="e0", text="SKU-44",
                         input_ref="target")
    first = context.record(_state(1, "old"), _state(2, "new", "SKU-44"), fill)
    assert first.effect == "UI_CHANGE_OBSERVED"
    assert not context.loop_detected
    second = context.record(_state(2, "new", "SKU-44"),
                            _state(3, "new", "SKU-44"), fill.model_copy(update={"revision": 2}))
    assert second.effect == "NO_OBSERVABLE_CHANGE"
    assert not context.loop_detected
    context.record(_state(3, "new", "SKU-44"), _state(4, "new", "SKU-44"),
                   fill.model_copy(update={"revision": 3}))
    assert context.loop_detected
    supplied = context.model_context(2, 35)
    assert supplied["remaining_action_calls"] == 2
    assert supplied["task_acceptance_passed"] is False
    assert len(supplied["ineffective_actions"]) == 2
    assert all(item["effect"] == "NO_OBSERVABLE_CHANGE"
               for item in supplied["verified_history"][-2:])


def test_changed_state_allows_repeated_control_and_detects_cycle() -> None:
    """The same label can be legitimate in a changed state; A-B cycles cannot."""
    context = BrowserPlanningContext()
    action = BrowserAction(kind="click", revision=1, element_id="e0")
    for index, (before, after) in enumerate((("A", "B"), ("B", "A"),
                                              ("A", "B"), ("B", "A")), 1):
        context.record(_state(index, before), _state(index + 1, after),
                       action.model_copy(update={"revision": index}))
        if index < 4:
            assert not context.loop_detected
    assert context.loop_detected
    assert all(item.effect == "UI_CHANGE_OBSERVED" for item in context.transitions)


def test_unacknowledged_or_mismatched_transition_is_not_history() -> None:
    """Invalid worker revisions cannot be passed to the model as verified facts."""
    context = BrowserPlanningContext()
    with pytest.raises(BrowserRefusal, match="INTERACTIVE_PROTOCOL_INVALID"):
        context.record(_state(1, "A"), _state(3, "B"),
                       BrowserAction(kind="click", revision=1, element_id="e0"))
    assert context.transitions == []


class _ScriptedClient:
    """Recorded choices exercise transport and Chromium, not semantic intelligence."""

    def __init__(self, names: tuple[str, ...]) -> None:
        self.names = names
        self.calls = 0
        self.planning: list[dict] = []

    async def chat(self, messages, **kwargs):
        assert kwargs["tools"] is None and kwargs["stream"] is False
        data = json.loads(messages[1].content)
        self.planning.append(data["planning"])
        desired = self.names[self.calls]
        self.calls += 1
        choice = next(item for item in data["observation"]["actions"]
                      if item["name"] == desired)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "revision": data["observation"]["revision"], "choice_id": choice["id"]}),
            tool_calls=[]), done=True, done_reason="stop")


@pytest.mark.asyncio
async def test_worker_confirmed_history_and_recovery_after_irrelevant_action() -> None:
    """Five scripted choices complete a rendered task; history has confirmed effects."""
    client = _ScriptedClient(("Quick advice", "Inventory", "SKU", "Search stock",
                              "View SKU-44"))
    progress = BrowserRunProgress()
    result = await run_isolated_policy(
        checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
        objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
        source=OllamaBrowserActionSource(client, "fixture"), progress=progress)
    assert result["VERDICT"] == "ISOLATED_PROCEDURE_VALIDATED"
    assert client.planning[0]["verified_history"] == []
    assert len(client.planning[1]["verified_history"]) == 1
    assert client.planning[1]["verified_history"][0]["label"] == "Quick advice"
    assert client.planning[1]["verified_history"][0]["task_progress"] == "TASK_PROGRESS_UNCERTAIN"
    assert client.planning[-1]["verified_history"][-1]["label"] == "Search stock"
    assert progress.action_count == progress.state_transition_count == 5
    assert progress.no_progress_count == 0 and not progress.loop_detected
    assert progress.task_acceptance_passed and result["PROCEDURE"]["approval"] == "PENDING_HUMAN_APPROVAL"


@pytest.mark.asyncio
async def test_rendered_repeated_no_change_blocks_without_extra_action() -> None:
    """A second identical no-op is acknowledged, then the controller stops."""
    client = _ScriptedClient(("Inventory", "Inventory", "Inventory"))
    progress = BrowserRunProgress()
    with pytest.raises(BrowserRefusal, match="BROWSER_LOOP_DETECTED"):
        await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
            objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
            source=OllamaBrowserActionSource(client, "fixture"), progress=progress)
    assert progress.action_count == 3 and progress.state_transition_count == 3
    assert progress.no_progress_count == 2 and progress.loop_detected
    assert client.planning[2]["ineffective_actions"] == [
        {"action": "click", "label": "Inventory", "input_ref": None}]


@pytest.mark.asyncio
async def test_post_freeze_switchboard_cycle_and_changed_layout() -> None:
    """A separately supplied layout exercises the frozen generic controller."""
    client = _ScriptedClient(("Open records desk", "Back to menu",
                              "Open records desk", "Back to menu"))
    progress = BrowserRunProgress()
    with pytest.raises(BrowserRefusal, match="BROWSER_LOOP_DETECTED"):
        await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture="switchboard",
            objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
            source=OllamaBrowserActionSource(client, "fixture"), progress=progress)
    assert progress.action_count == 4 and progress.loop_detected
    assert progress.no_progress_count == 0
    assert progress.task_acceptance_passed is False


@pytest.mark.asyncio
async def test_repeated_control_after_changed_state_can_complete() -> None:
    """A repeated label after a real state change is permitted, then UI acceptance wins."""
    client = _ScriptedClient(("Open records desk", "Back to menu",
                              "Open records desk", "Stock code", "Find record",
                              "Open stock record"))
    progress = BrowserRunProgress()
    result = await run_isolated_policy(
        checkout=ROOT, browser_dir=BROWSER, fixture="switchboard",
        objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
        source=OllamaBrowserActionSource(client, "fixture"), progress=progress)
    assert result["VERDICT"] == "ISOLATED_PROCEDURE_VALIDATED"
    assert progress.action_count == progress.state_transition_count == 6
    assert progress.task_acceptance_passed and not progress.loop_detected
    assert client.planning[3]["verified_history"][-1]["label"] == "Open records desk"


@pytest.mark.asyncio
async def test_failed_second_model_request_keeps_only_confirmed_history() -> None:
    """An unreturned proposal cannot appear in the controller's verified timeline."""
    class FailingClient(_ScriptedClient):
        async def chat(self, messages, **kwargs):
            if self.calls == 1:
                self.planning.append(json.loads(messages[1].content)["planning"])
                raise RuntimeError("private model failure")
            return await super().chat(messages, **kwargs)

    client = FailingClient(("Inventory",))
    progress = BrowserRunProgress()
    with pytest.raises(BrowserRefusal, match="MODEL_UNEXPECTED_FAILURE"):
        await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
            objective="Check stock item SKU-44", target="SKU-44", expected="12 units",
            source=OllamaBrowserActionSource(client, "fixture"), progress=progress)
    assert progress.action_count == progress.state_transition_count == 1
    assert len(client.planning[1]["verified_history"]) == 1
    assert "private model failure" not in str(progress.public())
