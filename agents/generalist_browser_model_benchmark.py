"""One consent-gated, synthetic browser decision trial per local model.

Model selection is operator-controlled. This module never changes Morgoth's
configured model, downloads a model, or runs concurrent inference trials.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
import os
import re
import sys

import httpx

from agents import generalist_browser_operator as operator
from agents.generalist_browser_lab import BrowserRefusal
from agents.generalist_ollama_plan_probe import _local_model_config
from agents.generalist_ollama_resources import _bounded_metadata_get, _loopback_base
from scripts.codex_local_warning_review import ReviewUnavailable, open_operator_terminal


BRANCH = "morgoth-generalist-planner-benchmark-v1"
_MODEL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,80}\Z")


@dataclass(frozen=True)
class BenchmarkTask:
    """Reviewed synthetic task inputs and independent expected UI result."""

    fixture: str
    objective: str
    target: str
    expected: str


TASKS = {
    "inventory": BenchmarkTask("heldout", operator.OBJECTIVE, operator.TARGET,
                                operator.EXPECTED),
    "changed_layout": BenchmarkTask(
        "relayout", "Check inventory stock item SKU-44 and report its quantity",
        "SKU-44", "12 units"),
    "catalog": BenchmarkTask(
        "catalog", "Find the title for book BK-17 in the public catalog",
        "BK-17", "The Atlas of Rivers"),
    "support": BenchmarkTask(
        "support", "Locate support request SR-9 and report its status",
        "SR-9", "Resolved"),
}


def classify_trial(report: dict[str, object]) -> str:
    """Classify actual model evidence without treating valid clicks as success."""
    if report.get("TASK_ACCEPTANCE_PASSED") is True and report.get("VERDICT") == "MODEL_BROWSER_QUALIFIED":
        return "SYNTHETIC_TASK_COMPLETED"
    if report.get("VERDICT") == "MODEL_BROWSER_DECISION_LIMITED":
        return "MODEL_DECISION_LIMITED"
    if (report.get("FAILURE_CLASS") in {"MODEL_CALL_BUDGET_EXCEEDED", "MODEL_ABSTAINED"}
            and type(report.get("ACTION_COUNT")) is int):
        return "MODEL_DECISION_LIMITED"
    return "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"


async def _installed_names(base: str) -> list[str]:
    """Discover bounded installed model names from the configured loopback API."""
    if not _loopback_base(base):
        raise BrowserRefusal("LOCAL_MODEL_CONFIG_REJECTED")
    async with httpx.AsyncClient(base_url=base, trust_env=False,
                                 follow_redirects=False,
                                 timeout=httpx.Timeout(5, connect=2)) as client:
        payload, _ = await asyncio.wait_for(
            _bounded_metadata_get(client, "/api/tags"), timeout=6)
    if (type(payload) is not dict or type(payload.get("models")) is not list or
            len(payload["models"]) > 32 or
            any(type(item) is not dict or type(item.get("name")) is not str or
                _MODEL_NAME.fullmatch(item["name"]) is None
                for item in payload["models"])):
        raise BrowserRefusal("MODEL_CATALOG_INVALID")
    return sorted({item["name"] for item in payload["models"]})


def _operator_choice(names: list[str], configured: str) -> str | None:
    """Show installed alternatives privately and require an explicit selection."""
    if configured not in names and configured + ":latest" not in names:
        raise BrowserRefusal("MODEL_NOT_INSTALLED")
    alternatives = [name for name in names if name not in {configured, configured + ":latest"}]
    try:
        with open_operator_terminal() as terminal:
            terminal.write("Synthetic browser trial; one model, six choices maximum.\n")
            terminal.write("0: configured model\n")
            for index, name in enumerate(alternatives, 1):
                terminal.write(f"{index}: {name}\n")
            terminal.write("Select one installed model number (or blank to stop): ")
            terminal.flush()
            choice = terminal.readline(8).strip()
    except ReviewUnavailable:
        raise BrowserRefusal("LOCAL_CONSENT_REQUIRED") from None
    if not choice or not choice.isascii() or not choice.isdecimal():
        raise BrowserRefusal("MODEL_SELECTION_DECLINED")
    index = int(choice)
    if index > len(alternatives):
        raise BrowserRefusal("MODEL_SELECTION_INVALID")
    return None if index == 0 else alternatives[index - 1]


async def _evaluate(task_id: str) -> dict[str, object]:
    """Choose an installed model, then run one separately consented trial."""
    result: dict[str, object] = {
        "TASK_ID": task_id, "VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
        "FAILURE_CLASS": "PREFLIGHT_REJECTED", "MODEL_CALL_COUNT": 0,
        "TASK_ACCEPTANCE_PASSED": False, "PRODUCTION_QUALIFIED": False,
        "COMPARATIVE_EVIDENCE": "INSUFFICIENT_COMPARATIVE_EVIDENCE",
    }
    try:
        operator._checkout_verified(BRANCH)
        task = TASKS[task_id]
        operator._sandbox_command(operator.ROOT, operator.BROWSER, task.fixture,
                                  "--interactive-worker")
        config = _local_model_config()
        # The shared Ollama transport does not disable proxy inheritance.
        # Remove proxy environment before constructing it; never alter .env.
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                    "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
        names = await _installed_names(config.ollama_base_url)
        result["INSTALLED_MODEL_COUNT"] = len(names)
        result["ALTERNATIVE_MODEL_COUNT"] = sum(name not in {
            config.ollama_primary_model, config.ollama_primary_model + ":latest"}
            for name in names)
        selected = _operator_choice(names, config.ollama_primary_model)
    except BrowserRefusal as exc:
        result["FAILURE_CLASS"] = exc.code
        return result
    except (httpx.HTTPError, OSError, ValueError, TimeoutError, TypeError):
        result["FAILURE_CLASS"] = "MODEL_CATALOG_UNAVAILABLE"
        return result
    trial = await operator._evaluate(
        branch=BRANCH, fixture=task.fixture, objective=task.objective,
        target=task.target, expected=task.expected, model_override=selected)
    # The operator runner exports only fixed codes, counts and reviewed
    # fixture-scoped trace IDs; the model name stays on the private terminal.
    result.update(trial)
    result["TASK_ID"] = task_id
    result["VERDICT"] = classify_trial(trial)
    result["DECISION_OUTCOME"] = (
        "INDEPENDENT_COMPLETION" if trial.get("TASK_ACCEPTANCE_PASSED") is True else
        "SAFE_ABSTENTION" if trial.get("FAILURE_CLASS") == "MODEL_ABSTAINED" else
        "LOOP_TERMINATED" if trial.get("LOOP_DETECTED") is True else
        "UNPRODUCTIVE_SELECTION" if result["VERDICT"] == "MODEL_DECISION_LIMITED" else
        "TECHNICAL_FAILURE")
    result["COMPARATIVE_EVIDENCE"] = "INSUFFICIENT_COMPARATIVE_EVIDENCE"
    result["PRODUCTION_QUALIFIED"] = False
    return result


def main() -> None:
    """Emit bounded one-trial metadata; never include prompts or model text."""
    parser = argparse.ArgumentParser(description="One consent-gated synthetic browser trial")
    parser.add_argument("--task", choices=tuple(TASKS), required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(_evaluate(args.task))
    except Exception:
        result = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                  "FAILURE_CLASS": "BENCHMARK_ERROR", "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
