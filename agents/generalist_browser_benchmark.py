"""Offline deterministic baseline on reviewed synthetic Chromium fixtures."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from agents.generalist_browser_lab import (
    BrowserRefusal, BrowserRunProgress, LexicalObservationPolicy, TaskInput,
    run_isolated_policy,
)


ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")
CASES = (
    ("heldout", "Check inventory stock item SKU-44 and report its quantity", "SKU-44", "12 units", ()),
    ("catalog", "Find the title for book BK-17 in the public catalog", "BK-17", "The Atlas of Rivers", ()),
    ("support", "Locate support request SR-9 and report its status", "SR-9", "Resolved", ()),
    ("relayout", "Check inventory stock item SKU-44 and report its quantity", "SKU-44", "12 units", ()),
    ("deferred", "Check stock item SKU-44 in zone ZONE-2", "SKU-44", "12 units",
     (TaskInput(id="zone", label="Warehouse zone", value="ZONE-2"),)),
)


async def evaluate_baseline() -> list[dict[str, object]]:
    """Use the same six-call cap, trusted inputs and independent UI checker."""
    results: list[dict[str, object]] = []
    for fixture, objective, target, expected, inputs in CASES:
        progress = BrowserRunProgress()
        started = time.monotonic()
        try:
            await run_isolated_policy(
                checkout=ROOT, browser_dir=BROWSER, fixture=fixture,
                objective=objective, target=target, expected=expected,
                source=LexicalObservationPolicy(), max_model_calls=6,
                progress=progress, synthetic_trace=True, extra_inputs=inputs)
            failure = "NONE"
        except BrowserRefusal as exc:
            failure = exc.code
        results.append({"FIXTURE": fixture.upper(),
                        "TASK_ACCEPTANCE_PASSED": progress.task_acceptance_passed,
                        "ACTION_COUNT": progress.action_count,
                        "NO_PROGRESS_COUNT": progress.no_progress_count,
                        "LOOP_DETECTED": progress.loop_detected,
                        "FAILURE_CLASS": failure,
                        "ELAPSED_MS": min(1_000_000, max(0, round(
                            (time.monotonic() - started) * 1000)))})
    return results


def main() -> None:
    """Print only bounded fixture metrics; this never invokes a model."""
    print(json.dumps(asyncio.run(evaluate_baseline()), sort_keys=True))


if __name__ == "__main__":
    main()
