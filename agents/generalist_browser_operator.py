"""Operator-only local-model evaluation on one confined synthetic browser app."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
from loguru import logger

from agents.generalist_browser_lab import (
    BrowserRefusal, BrowserRunProgress, OllamaBrowserActionSource, _sandbox_command,
    run_isolated_fixture, run_isolated_policy,
)
from agents.generalist_ollama_plan_probe import _local_model_config
from core.llm_client import OllamaLLMClient
from scripts.codex_local_warning_review import ReviewUnavailable, _consent, open_operator_terminal


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "morgoth-generalist-browser-model-v1"
BROWSER = Path("/home/corio/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64")
OBJECTIVE = "Check inventory stock item SKU-44 and report its quantity"
TARGET = "SKU-44"
EXPECTED = "12 units"


def _git(*args: str) -> str:
    """Read bounded checkout metadata without exporting Git diagnostic text."""
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                            text=True, timeout=5, check=False)
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    return result.stdout.strip()


def _checkout_verified() -> None:
    """Require exactly the clean reviewed, origin-synchronized feature branch."""
    try:
        if (_git("branch", "--show-current") != BRANCH or
            _git("rev-parse", "HEAD") != _git("rev-parse", f"origin/{BRANCH}") or
            _git("status", "--porcelain=v1", "--untracked-files=all") or
            _git("remote", "get-url", "origin") not in {
                "git@github.com:coriom/morgoth.git", "https://github.com/coriom/morgoth.git"}):
            raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    except (OSError, subprocess.SubprocessError):
        raise BrowserRefusal("PREFLIGHT_CHECKOUT") from None


async def _evaluate() -> dict[str, object]:
    """Preflight and obtain foreground consent before any local inference."""
    report: dict[str, object] = {
        "VERDICT": "MODEL_BROWSER_BLOCKED", "MODEL_CALL_COUNT": 0,
        "MODEL_RESPONSE_COUNT": 0, "MODEL_REQUEST_ATTEMPTED": False,
        "MODEL_RESPONSE_RECEIVED": False, "HTTP_RESPONSE_RECEIVED": False,
        "HTTP_STATUS": None,
        "BROWSER_ISOLATED": False, "PAGE_OBSERVED": False,
        "ACTION_PROPOSED": False, "ACTION_SCHEMA_VALID": False,
        "ACTION_POLICY_VALIDATED": False, "ACTION_COUNT": 0,
        "ISOLATED_ACTION_EXECUTED": False, "EXPECTED_UI_STATE_OBSERVED": False,
        "TASK_ACCEPTANCE_PASSED": False, "PROCEDURE_PERSISTED": False,
        "REPLAY_VALIDATED": False, "APPROVAL_PENDING": False,
        "PRODUCTION_QUALIFIED": False,
    }
    try:
        _checkout_verified()
        _sandbox_command(ROOT, BROWSER, "heldout", "--interactive-worker")
        config = _local_model_config()
    except (BrowserRefusal, OSError, ValueError) as exc:
        report["FAILURE_CLASS"] = exc.code if isinstance(exc, BrowserRefusal) else "PREFLIGHT_REJECTED"
        return report
    logger.remove()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    client = OllamaLLMClient(config, timeout=90)
    try:
        try:
            models = await asyncio.wait_for(client.list_models(), timeout=5)
            model = config.ollama_primary_model
            if model not in {item.name for item in models} and model + ":latest" not in {item.name for item in models}:
                raise BrowserRefusal("MODEL_NOT_INSTALLED")
        except (TimeoutError, httpx.HTTPError, OSError, ValueError):
            report["FAILURE_CLASS"] = "OLLAMA_UNREACHABLE"
            return report
        except BrowserRefusal as exc:
            report["FAILURE_CLASS"] = exc.code
            return report
        try:
            preflight = await run_isolated_fixture(
                checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
                objective=OBJECTIVE, target=TARGET, expected=EXPECTED)
            if preflight.get("BROWSER_ISOLATED") is not True:
                raise BrowserRefusal("BROWSER_PREFLIGHT_FAILED")
            report["BROWSER_ISOLATED"] = True
        except BrowserRefusal as exc:
            report["FAILURE_CLASS"] = exc.code
            return report
        try:
            with open_operator_terminal() as terminal:
                if not _consent(terminal, "BROWSE ONCE"):
                    report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
                    return report
        except ReviewUnavailable:
            report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
            return report
        source = OllamaBrowserActionSource(client, model)
        progress = BrowserRunProgress()
        try:
            await run_isolated_policy(
                checkout=ROOT, browser_dir=BROWSER, fixture="heldout",
                objective=OBJECTIVE, target=TARGET, expected=EXPECTED,
                source=source, max_model_calls=6, progress=progress)
            report.update({"VERDICT": "MODEL_BROWSER_PROCEDURE_VALIDATED_PENDING_APPROVAL",
                           "FAILURE_CLASS": "NONE", "APPROVAL_PENDING": True})
        except BrowserRefusal as exc:
            report["FAILURE_CLASS"] = exc.code
        except Exception:
            report["FAILURE_CLASS"] = "EVALUATION_ERROR"
        finally:
            report.update(progress.public())
            report["MODEL_CALL_COUNT"] = source.calls
            report["MODEL_RESPONSE_COUNT"] = source.responses
            report["MODEL_REQUEST_ATTEMPTED"] = source.calls > 0
            report["MODEL_RESPONSE_RECEIVED"] = source.responses > 0
            report["HTTP_RESPONSE_RECEIVED"] = source.http_response_received
            report["HTTP_STATUS"] = source.http_status
        return report
    finally:
        await client.close()


def main() -> None:
    """Emit only fixed-status, bounded metadata to stdout."""
    try:
        result = asyncio.run(_evaluate())
    except (OSError, ValueError, RuntimeError):
        result = {"VERDICT": "MODEL_BROWSER_BLOCKED", "FAILURE_CLASS": "EVALUATION_ERROR",
                  "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
