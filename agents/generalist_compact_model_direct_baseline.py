"""One consent-gated direct-controller Qwen trial on the reviewed synthetic fixture.

This reuses CMQ.1's exact model and pressure gates and the existing P1
browser operator. It neither repeats Stage A nor changes the default model.
"""

from __future__ import annotations

import asyncio
import json
import sys

from agents import generalist_compact_model_qualification as compact


BRANCH = "morgoth-generalist-compact-model-direct-baseline-v1"


async def evaluate(expected_sha: str) -> dict[str, object]:
    """Run one direct synthetic trial after reviewed and fresh resource gates."""
    report = await compact.reviewed_stage_b(
        expected_sha, branch=BRANCH, hierarchical=False)
    report["PLANNER_MODE"] = "DIRECT"
    report["DIRECT_INFERENCE_ATTEMPTED"] = report.get("STAGE_B_ATTEMPTED") is True
    report["COMPARATIVE_EVIDENCE"] = "SEPARATE_NONRANDOMIZED_TRIALS"
    report["PRODUCTION_QUALIFIED"] = False
    return report


def main() -> None:
    """Print only bounded status metadata, never model or page content."""
    if len(sys.argv) != 3 or sys.argv[1] != "--expected-sha":
        report: dict[str, object] = {
            "VERDICT": "INVALID_INVOCATION", "PRODUCTION_QUALIFIED": False}
    else:
        try:
            report = asyncio.run(evaluate(sys.argv[2]))
        except KeyboardInterrupt:
            report = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                      "FAILURE_CLASS": "OPERATOR_OR_SHELL_INTERRUPTED",
                      "PRODUCTION_QUALIFIED": False}
        except Exception:
            report = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                      "FAILURE_CLASS": "DIRECT_BASELINE_ERROR",
                      "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
