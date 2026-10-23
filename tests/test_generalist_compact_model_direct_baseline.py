"""Direct-only selected-model gate and scripted confined-browser protocol proofs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import generalist_compact_model_direct_baseline as direct
from agents import generalist_compact_model_qualification as compact
from agents.generalist_browser_hierarchy import HierarchicalBrowserActionSource
from agents.generalist_browser_lab import (
    BrowserObservation, BrowserRefusal, BrowserRunProgress, ObservedElement,
    OllamaBrowserActionSource, VerifiedTransition, run_isolated_policy,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")
REQUIRES_BROWSER = pytest.mark.skipif(
    not BROWSER.joinpath("chrome-headless-shell").is_file(),
    reason="reviewed Chromium unavailable")


def _observation(revision: int) -> BrowserObservation:
    """Return a bounded synthetic observation for call accounting."""
    return BrowserObservation(revision=revision, origin="http://127.0.0.1:1",
                              title="Fixture", text="", elements=[ObservedElement(
                                  id="e0", tag="button", role="", name="Browse",
                                  href="", value="", input_type="", disabled=False,
                                  readonly=False)])


def test_direct_branch_requires_clean_exact_live_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The direct adapter cannot reuse a clean but differently pinned branch."""
    sha = "a" * 40
    values = {("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
              ("branch", "--show-current"): direct.BRANCH,
              ("rev-parse", "HEAD"): sha,
              ("rev-parse", f"origin/{direct.BRANCH}"): sha,
              ("status", "--porcelain=v1", "--untracked-files=all"): "",
              ("ls-remote", "--heads", "origin", direct.BRANCH):
                  f"{sha}\trefs/heads/{direct.BRANCH}"}
    monkeypatch.setattr(compact.operator, "_git", lambda *args: values[args])
    compact.verify_checkout(sha, direct.BRANCH)
    values[("ls-remote", "--heads", "origin", direct.BRANCH)] = ""
    with pytest.raises(BrowserRefusal, match="PREFLIGHT_CHECKOUT"):
        compact.verify_checkout(sha, direct.BRANCH)


class SyntheticClient:
    """Supply scripted choices only; no live model request or browser authority."""

    def __init__(self, labels: tuple[str, ...] = ()) -> None:
        self.labels = labels
        self.low_calls = 0
        self.high_calls = 0

    async def chat(self, messages: list, **kwargs: object) -> SimpleNamespace:
        """Return strict mock JSON under the controller's current schema."""
        assert kwargs["tools"] is None and kwargs["stream"] is False
        schema = kwargs["format"]
        supplied = json.loads(messages[1].content)
        if "decision" in schema["properties"]:
            self.high_calls += 1
            content = json.dumps({"decision": "pursue", "subgoal": "Find requested record",
                                  "visible_marker": "", "uncertainty": "UI path unknown"})
        else:
            actions = supplied["observation"]["actions"]
            if self.labels:
                label = self.labels[self.low_calls]
                selected = next(item for item in actions if item["name"] == label)
            else:
                selected = next(item for item in actions if item["kind"] == "click")
            self.low_calls += 1
            content = json.dumps({"revision": supplied["observation"]["revision"],
                                  "choice_id": selected["id"]})
        return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]),
                               done=True, done_reason="stop")


@pytest.mark.asyncio
async def test_six_calls_and_four_actions_are_consistent_with_two_plans() -> None:
    """Protocol possibility only; historic layer counts were not recorded."""
    client = SyntheticClient()
    source = HierarchicalBrowserActionSource(client, "fixture", loaded=True)
    for index, remaining in enumerate((6, 4, 3, 1), start=1):
        await source.propose("Find record", "ID-1", _observation(index),
                             {"remaining_action_calls": remaining})
        if index == 2:
            source.on_verified_transition(
                _observation(index), _observation(index + 1), VerifiedTransition(
                    kind="click", label="Browse", input_ref=None,
                    effect="NO_OBSERVABLE_CHANGE", controls_added=(),
                    controls_removed=(), visible_text_changed=False))
    assert source.calls == source.responses == 6
    assert source.public_hierarchy()["HIGH_LEVEL_CALL_COUNT"] == 2
    assert source.public_hierarchy()["LOW_LEVEL_CALL_COUNT"] == 4


@pytest.mark.asyncio
async def test_direct_entry_uses_same_gates_but_no_hierarchy(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    async def preflight(_model: str, _sha: str, _quant: str, branch: str):
        calls.append("preflight")
        assert branch == direct.BRANCH
        return {"MODEL_DIGEST_SHA256": compact.REVIEWED_STAGE_A_DIGEST,
                "HOST_SWAP_FREE_MIB": 1700, "STAGE_B_ATTEMPTED": False,
                "PRODUCTION_QUALIFIED": False}, SimpleNamespace(ollama_base_url="http://127.0.0.1:11434")
    async def browser(**kwargs: object) -> dict[str, object]:
        calls.append("browser")
        assert kwargs == {"branch": direct.BRANCH, "fixture": "heldout",
                          "objective": compact.operator.OBJECTIVE,
                          "target": compact.operator.TARGET,
                          "expected": compact.operator.EXPECTED,
                          "model_override": "qwen3:4b-instruct", "hierarchical": False}
        return {"VERDICT": "MODEL_BROWSER_QUALIFIED", "FAILURE_CLASS": "NONE",
                "MODEL_CALL_COUNT": 4, "MODEL_RESPONSE_COUNT": 4,
                "ACTION_COUNT": 4, "VALID_ACTION_COUNT": 4,
                "NO_PROGRESS_COUNT": 0, "LOOP_DETECTED": False,
                "TASK_ACCEPTANCE_PASSED": True, "TRACE_SOURCE_VERIFIED": True,
                "TRACE_COMPLETE": True, "SYNTHETIC_ACTION_TRACE": []}
    monkeypatch.setattr(compact, "_preflight", preflight)
    monkeypatch.setattr(compact, "_consented", lambda phrase: calls.append(phrase) or True)
    monkeypatch.setattr(compact.r2, "_service_sample", lambda: (None, 0, 0))
    monkeypatch.setattr(compact, "_inventory", lambda *_args: asyncio.sleep(
        0, result=(1, "Q4_K_M", compact.REVIEWED_STAGE_A_DIGEST)))
    monkeypatch.setattr(compact.operator, "_evaluate", browser)
    result = await direct.evaluate("a" * 40)
    assert calls == ["preflight", "MEMORY PRESSURE REVIEWED", "preflight", "browser"]
    assert result["VERDICT"] == "SYNTHETIC_TASK_COMPLETED"
    assert result["PLANNER_MODE"] == "DIRECT"
    assert result["STAGE_A_CALL_REPEATED"] is False
    assert result["DIRECT_INFERENCE_ATTEMPTED"] is True
    assert result["PRODUCTION_QUALIFIED"] is False


def test_future_hierarchy_counts_are_in_safe_projection() -> None:
    """Only bounded existing counters pass; advisory text never does."""
    for field in ("HIGH_LEVEL_CALL_COUNT", "LOW_LEVEL_CALL_COUNT",
                  "SUBGOAL_REVISION_COUNT", "SUBGOAL_STATUS"):
        assert field in compact.SAFE_STAGE_B_FIELDS
    assert "subgoal" not in compact.SAFE_STAGE_B_FIELDS


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["pressure_declined", "model_drift", "fresh_low_swap"])
async def test_gate_failure_never_enters_direct_browser(
        monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    count = 0
    async def preflight(_model: str, _sha: str, _quant: str, _branch: str):
        nonlocal count
        count += 1
        digest = ("b" * 64 if failure == "model_drift" else
                  compact.REVIEWED_STAGE_A_DIGEST)
        return {"MODEL_DIGEST_SHA256": digest,
                "HOST_SWAP_FREE_MIB": 20 if failure == "fresh_low_swap" and count == 2 else 1700,
                "STAGE_B_ATTEMPTED": False, "PRODUCTION_QUALIFIED": False}, SimpleNamespace(ollama_base_url="http://127.0.0.1:11434")
    monkeypatch.setattr(compact, "_preflight", preflight)
    monkeypatch.setattr(compact, "_consented", lambda _phrase: failure != "pressure_declined")
    monkeypatch.setattr(compact.operator, "_evaluate", lambda **_kwargs: (
        _ for _ in ()).throw(AssertionError("direct browser forbidden")))
    result = await direct.evaluate("a" * 40)
    assert result["DIRECT_INFERENCE_ATTEMPTED"] is False
    assert result["PRODUCTION_QUALIFIED"] is False
    assert result["FAILURE_CLASS"] in {
        "PRESSURE_ACKNOWLEDGEMENT_REQUIRED", "REVIEWED_MODEL_DIGEST_MISMATCH",
        "FRESH_SWAP_HEADROOM_BLOCKED"}


@pytest.mark.asyncio
@REQUIRES_BROWSER
async def test_confined_direct_scripted_actions_reach_independent_acceptance() -> None:
    """Scripted choices prove controller behavior, not Qwen decision quality."""
    client = SyntheticClient(("Inventory", "SKU", "SKU", "Search stock", "View SKU-44"))
    source = OllamaBrowserActionSource(client, "fixture", loaded=True)
    progress = BrowserRunProgress()
    result = await run_isolated_policy(
        checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
        objective="Check inventory stock item SKU-44 and report its quantity",
        target="SKU-44", expected="12 units", source=source,
        max_model_calls=6, progress=progress, synthetic_trace=True)
    assert result["TASK_ACCEPTANCE_PASSED"] is True
    assert source.calls == source.responses == 5
    assert progress.action_count == 5 and progress.no_progress_count == 1
    assert progress.task_acceptance_passed and not progress.loop_detected
    assert len(progress.synthetic_trace) == 5
