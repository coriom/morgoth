"""One consent-gated hierarchical trial in a reviewed disposable browser fixture."""

from __future__ import annotations

import asyncio
import json
import sys

from agents import generalist_browser_operator as operator


BRANCH = "morgoth-generalist-hierarchical-planner-v1"


def classify(report: dict[str, object]) -> str:
    """Require independent browser acceptance for a successful verdict."""
    if report.get("TASK_ACCEPTANCE_PASSED") is True and report.get("VERDICT") == "MODEL_BROWSER_QUALIFIED":
        return "HIERARCHICAL_TASK_COMPLETED"
    if report.get("FAILURE_CLASS") in {"HIERARCHICAL_BUDGET_LIMITED", "MODEL_CALL_BUDGET_EXCEEDED",
                                       "MODEL_BUDGET_EXHAUSTED"}:
        return "HIERARCHICAL_BUDGET_LIMITED"
    if report.get("FAILURE_CLASS") in {"MODEL_ABSTAINED", "BROWSER_LOOP_DETECTED",
                                       "BROWSER_INEFFECTIVE_ACTION_REJECTED"}:
        return "HIERARCHICAL_DECISION_LIMITED"
    return "HIERARCHICAL_PROTOCOL_OR_RESOURCE_BLOCKED"


async def evaluate() -> dict[str, object]:
    """Use existing checkout, resource, browser and foreground-consent gates."""
    result = await operator._evaluate(branch=BRANCH, hierarchical=True)
    result["VERDICT"] = classify(result)
    result["COMPARATIVE_EVIDENCE"] = "INSUFFICIENT_COMPARATIVE_EVIDENCE"
    result["PRODUCTION_QUALIFIED"] = False
    return result


def main() -> None:
    """Print bounded fixed-code evidence; never expose prompts or responses."""
    try:
        result = asyncio.run(evaluate())
    except Exception:
        result = {"VERDICT": "HIERARCHICAL_PROTOCOL_OR_RESOURCE_BLOCKED",
                  "FAILURE_CLASS": "EVALUATION_ERROR", "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
