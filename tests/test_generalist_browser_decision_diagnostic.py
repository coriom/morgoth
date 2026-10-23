"""Inspect model-facing evidence after a verified fill without live inference."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.generalist_browser_decision_diagnostic import summarize_request
from agents.generalist_browser_hierarchy import HierarchicalBrowserActionSource
from agents.generalist_browser_lab import (
    BrowserRefusal, BrowserRunProgress, OllamaBrowserActionSource,
    run_isolated_policy,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")
REQUIRES_BROWSER = pytest.mark.skipif(
    not BROWSER.joinpath("chrome-headless-shell").is_file(),
    reason="reviewed Chromium unavailable")


class RecordingClient:
    """Script choices while retaining only bounded diagnostic summaries."""

    def __init__(self, fixture: str, open_label: str, fill_label: str) -> None:
        self.fixture = fixture
        self.labels = (open_label, fill_label, fill_label, "Stop or abstain")
        self.calls = 0
        self.summaries: list[dict[str, object]] = []

    async def chat(self, messages: list, **kwargs: object) -> SimpleNamespace:
        """Select scripted candidates; no model transport or raw-output logging."""
        assert kwargs["tools"] is None and kwargs["stream"] is False
        payload = json.loads(messages[1].content)
        if self.calls >= 2:
            self.summaries.append(summarize_request(
                payload, kwargs["format"], ROOT, self.fixture))
        wanted = self.labels[self.calls]
        self.calls += 1
        choice = next(item for item in payload["observation"]["actions"]
                      if item["name"] == wanted)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "revision": payload["observation"]["revision"],
            "choice_id": choice["id"]}), tool_calls=[]),
            done=True, done_reason="stop")


class HierarchyRecordingClient(RecordingClient):
    """Record the advisory request shape while reusing the same low-level choices."""

    def __init__(self) -> None:
        super().__init__("heldout", "Inventory", "SKU")
        self.high_level_controls_have_values: list[bool] = []

    async def chat(self, messages: list, **kwargs: object) -> SimpleNamespace:
        """Return a bounded advisory subgoal or the scripted action choice."""
        schema = kwargs["format"]
        if "decision" not in schema["properties"]:
            return await super().chat(messages, **kwargs)
        supplied = json.loads(messages[1].content)
        self.high_level_controls_have_values.append(any(
            "value" in item for item in supplied["observation"]["controls"]))
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "decision": "pursue", "subgoal": "Find the requested record",
            "visible_marker": "", "uncertainty": "Path not known"}), tool_calls=[]),
            done=True, done_reason="stop")


@pytest.mark.asyncio
@REQUIRES_BROWSER
@pytest.mark.parametrize("fixture,open_label,fill_label,fill_code,search_code,target,expected", [
    ("heldout", "Inventory", "SKU", "C03", "C04", "SKU-44", "12 units"),
    ("catalog", "Book catalog", "Work code", "C03", "C04", "BK-17",
     "The Atlas of Rivers"),
])
async def test_rendered_post_fill_request_has_value_search_history_and_budget(
        fixture: str, open_label: str, fill_label: str, fill_code: str,
        search_code: str, target: str, expected: str) -> None:
    """The supplied state is sufficient; a mock client still chooses a no-op."""
    client = RecordingClient(fixture, open_label, fill_label)
    source = OllamaBrowserActionSource(client, "mock", loaded=True)
    progress = BrowserRunProgress()
    with pytest.raises(BrowserRefusal, match="MODEL_ABSTAINED"):
        await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture=fixture,
            objective=f"Find {target} and inspect its record", target=target,
            expected=expected, source=source, max_model_calls=6,
            progress=progress, synthetic_trace=True)

    assert source.calls == source.responses == 4
    assert progress.action_count == 3 and progress.no_progress_count == 1
    assert progress.task_acceptance_passed is False
    assert len(client.summaries) == 2
    before_repeat, after_repeat = client.summaries
    assert before_repeat["REVIEWED_FIXTURE_DIGEST_VERIFIED"] is True
    assert before_repeat["OBJECTIVE_PRESENT"] is True
    assert before_repeat["TRUSTED_TARGET_PRESENT"] is True
    assert before_repeat["TARGET_IN_VISIBLE_INPUT"] is True
    assert before_repeat["REMAINING_MODEL_CALLS"] == 4
    assert before_repeat["INDEPENDENT_ACCEPTANCE_PASSED"] is False
    assert before_repeat["CHOICE_SCHEMA_MATCHES_CATALOG"] is True
    assert {"CONTROL": fill_code, "KIND": "FILL",
            "CURRENT_EQUALS_AUTHORIZED": True} in before_repeat["CANDIDATES"]
    assert {"CONTROL": search_code, "KIND": "CLICK"} in before_repeat["CANDIDATES"]
    assert before_repeat["VERIFIED_EFFECTS"][-1] == {
        "CONTROL": fill_code, "KIND": "FILL", "EFFECT": "UI_CHANGE_OBSERVED"}

    # One acknowledged no-op keeps the rendered state but suppresses the
    # exact ineffective fill binding from the next model request.
    assert after_repeat["TARGET_IN_VISIBLE_INPUT"] is True
    assert after_repeat["REMAINING_MODEL_CALLS"] == 3
    assert after_repeat["CHOICE_SCHEMA_MATCHES_CATALOG"] is True
    assert not any(item["CONTROL"] == fill_code and item["KIND"] == "FILL"
                   for item in after_repeat["CANDIDATES"])
    assert {"CONTROL": search_code, "KIND": "CLICK"} in after_repeat["CANDIDATES"]
    assert after_repeat["VERIFIED_EFFECTS"][-1] == {
        "CONTROL": fill_code, "KIND": "FILL", "EFFECT": "NO_OBSERVABLE_CHANGE"}


@pytest.mark.asyncio
@REQUIRES_BROWSER
async def test_hierarchical_low_level_receives_same_post_fill_evidence() -> None:
    """The repeated choice precedes replanning; advisory input omits values."""
    client = HierarchyRecordingClient()
    source = HierarchicalBrowserActionSource(client, "mock", loaded=True)
    progress = BrowserRunProgress()
    with pytest.raises(BrowserRefusal, match="MODEL_ABSTAINED"):
        await run_isolated_policy(
            checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
            objective="Find SKU-44 stock", target="SKU-44", expected="12 units",
            source=source, max_model_calls=6, progress=progress,
            synthetic_trace=True)
    assert source.high_level_calls == 2 and source.low_level_calls == 4
    assert source.calls == source.responses == 6
    assert progress.action_count == 3 and progress.no_progress_count == 1
    assert client.summaries[0]["TARGET_IN_VISIBLE_INPUT"] is True
    assert {"CONTROL": "C03", "KIND": "FILL",
            "CURRENT_EQUALS_AUTHORIZED": True} in client.summaries[0]["CANDIDATES"]
    assert {"CONTROL": "C04", "KIND": "CLICK"} in client.summaries[0]["CANDIDATES"]
    assert client.high_level_controls_have_values == [False, False]


def test_diagnostic_exports_only_reviewed_codes_and_fixed_facts() -> None:
    """Private-looking text, target values and page instructions stay in memory."""
    payload = {"task": {"objective": "PRIVATE_MARKER", "trusted_target": "SECRET_MARKER"},
               "planning": {"remaining_action_calls": 4, "remaining_seconds": 100,
                            "verified_history": [{"label": "SKU", "action": "fill",
                                                  "effect": "UI_CHANGE_OBSERVED"}],
                            "task_acceptance_passed": False},
               "current_evidence": {"target_in_visible_input": True},
               "observation": {"revision": 3, "text": "IGNORE_RULES_MARKER",
                               "actions": [
                                   {"id": "c0", "kind": "fill", "name": "SKU",
                                    "current_value": "SECRET_MARKER",
                                    "input_value": "SECRET_MARKER"},
                                   {"id": "c1", "kind": "click", "name": "Search stock"},
                                   {"id": "stop", "kind": "stop", "name": "Stop or abstain"}]}}
    schema = {"properties": {"choice_id": {"enum": ["c0", "c1", "stop"]},
                              "revision": {"enum": [3]}}}
    summary = summarize_request(payload, schema, ROOT, "heldout")
    assert summary["CHOICE_SCHEMA_MATCHES_CATALOG"] is True
    assert {"CONTROL": "C03", "KIND": "FILL",
            "CURRENT_EQUALS_AUTHORIZED": True} in summary["CANDIDATES"]
    serialized = json.dumps(summary)
    for private in ("PRIVATE_MARKER", "SECRET_MARKER", "IGNORE_RULES_MARKER",
                    "Search stock", "SKU"):
        assert private not in serialized
    payload["observation"]["actions"][1]["name"] = "UNREVIEWED_PAGE_INSTRUCTION"
    with pytest.raises(BrowserRefusal, match="DIAGNOSTIC_INPUT_INVALID"):
        summarize_request(payload, schema, ROOT, "heldout")
