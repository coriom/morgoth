"""One selected local model: structured action, then separately consented browser task.

This opt-in laboratory entry point never changes the configured Ollama model.
The first request uses the R2 diagnostic envelope; the second stage reuses
the reviewed P1 hierarchical browser controller and its six-call ceiling.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import Any

import httpx
from loguru import logger

from agents import generalist_browser_operator as operator
from agents import generalist_ollama_cold_load_r2 as r2
from agents import generalist_ollama_runner_diagnostic as runner
from agents.generalist_browser_hierarchical_benchmark import classify as classify_browser
from agents.generalist_browser_lab import BrowserRefusal
from agents.generalist_ollama_plan_probe import _local_model_config
from agents.generalist_ollama_resources import (
    _bounded_metadata_get, _loopback_base, local_ollama_metadata,
    local_resource_evidence,
)
from scripts.codex_local_warning_review import (
    ReviewUnavailable, _consent, open_operator_terminal,
)


BRANCH = "morgoth-generalist-compact-model-qualification-v1"
MODEL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,80}\Z")
STAGE_A_DEADLINE_SECONDS = r2.DIAGNOSTIC_REQUEST_SECONDS
STAGE_B_MAX_CALLS = 6
SHELL_DEADLINE_SECONDS = 480
SAFE_STAGE_A_FIELDS = (
    "CLIENT_STARTED_UTC", "CLIENT_ENDED_UTC", "CLIENT_ELAPSED_MS",
    "MODEL_RESIDENCY_BEFORE", "MODEL_RESIDENCY_AFTER", "MODEL_CALL_COUNT",
    "MODEL_RESPONSE_COUNT", "INFERENCE_ATTEMPTED", "RESPONSE_RECEIVED",
    "HTTP_RESPONSE_RECEIVED", "HTTP_STATUS", "MODEL_TOTAL_DURATION_MS",
    "MODEL_LOAD_DURATION_MS", "PROMPT_EVAL_DURATION_MS",
    "GENERATION_DURATION_MS", "PROMPT_TOKENS", "GENERATED_TOKENS",
    "FAILURE_CLASS", "COMPLETE_RESPONSE", "STRUCTURED_ACTION_VALID",
    "SAMPLES", "RESOURCE_SUMMARY", "CLASSIFICATIONS", "CLOCK_DISCONTINUITY",
)
SAFE_STAGE_B_FIELDS = (
    "MODEL_CALL_COUNT", "MODEL_RESPONSE_COUNT", "MODEL_RESIDENCY",
    "ACTION_COUNT", "VALID_ACTION_COUNT", "NO_PROGRESS_COUNT",
    "LOOP_DETECTED", "ACTION_SCHEMA_VALID_COUNT", "ACTION_CHOICE_VALID_COUNT",
    "MODEL_REQUEST_ELAPSED_MS_TOTAL", "MODEL_REQUEST_ELAPSED_MS_MAX",
    "MODEL_LOAD_DURATION_MS_LAST", "HOST_MEMORY_AVAILABLE_MIB",
    "CGROUP_MEMORY_HEADROOM_MIB", "TRACE_SOURCE_VERIFIED", "TRACE_COMPLETE",
    "SYNTHETIC_ACTION_TRACE", "TASK_ACCEPTANCE_PASSED", "FAILURE_CLASS",
)


def verify_checkout(expected_sha: str) -> None:
    """Require one exact clean branch and synchronized local origin ref."""
    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    if (operator._git("remote", "get-url", "origin") not in {
            "git@github.com:coriom/morgoth.git", "https://github.com/coriom/morgoth.git"} or
            operator._git("branch", "--show-current") != BRANCH or
            operator._git("rev-parse", "HEAD") != expected_sha or
            operator._git("rev-parse", f"origin/{BRANCH}") != expected_sha or
            operator._git("status", "--porcelain=v1", "--untracked-files=all")):
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")


def _resource_ready(metadata: dict[str, object], resources: dict[str, object],
                    gpu: dict[str, object]) -> bool:
    """Use existing alternative-model headroom guard plus measured GPU free space."""
    free = gpu.get("GPU_MEMORY_FREE_MIB")
    return (operator._override_resource_ready(metadata, resources) and
            type(free) is int and free >= r2.MIN_GPU_FREE_MIB)


def _consented(wording: str) -> bool:
    """Require a controlling foreground terminal and one exact typed phrase."""
    try:
        with open_operator_terminal() as terminal:
            return _consent(terminal, wording)
    except ReviewUnavailable:
        return False


async def _inventory(base_url: str, model: str) -> tuple[int, str | None, str | None]:
    """Read bounded installed metadata; return count, quantization and digest."""
    async with httpx.AsyncClient(base_url=base_url, trust_env=False,
                                 follow_redirects=False,
                                 timeout=httpx.Timeout(5, connect=2)) as client:
        payload, _ = await asyncio.wait_for(
            _bounded_metadata_get(client, "/api/tags"), timeout=6)
    if (type(payload) is not dict or type(payload.get("models")) is not list or
            len(payload["models"]) > 32):
        raise BrowserRefusal("MODEL_CATALOG_INVALID")
    found: dict[str, object] | None = None
    for item in payload["models"]:
        if (type(item) is not dict or type(item.get("name")) is not str or
                MODEL_NAME.fullmatch(item["name"]) is None):
            raise BrowserRefusal("MODEL_CATALOG_INVALID")
        if item["name"] in {model, model + ":latest"}:
            found = item
    if found is None:
        return len(payload["models"]), None, None
    details = found.get("details")
    quantization = details.get("quantization_level") if type(details) is dict else None
    digest = found.get("digest")
    if (type(quantization) is not str or len(quantization) > 24 or
            type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise BrowserRefusal("MODEL_IDENTITY_UNVERIFIED")
    return len(payload["models"]), quantization, digest


async def _preflight(model: str, expected_sha: str,
                     expected_quantization: str) -> tuple[dict[str, object], object | None]:
    """Read only installed-model and resource metadata, with no generation."""
    report: dict[str, object] = {
        "VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED", "FAILURE_CLASS": "PREFLIGHT_REJECTED",
        "INFERENCE_ATTEMPTED": False, "STAGE_A_QUALIFIED": False,
        "STAGE_B_ATTEMPTED": False, "TASK_ACCEPTANCE_PASSED": False,
        "PRODUCTION_QUALIFIED": False, "SELECTED_MODEL_ROLE": "EXPERIMENT_ONLY",
        "MODEL_INSTALLED": None, "MODEL_RESIDENCY": "UNKNOWN",
    }
    try:
        verify_checkout(expected_sha)
        if MODEL_NAME.fullmatch(model) is None:
            raise BrowserRefusal("MODEL_SELECTION_INVALID")
        if re.fullmatch(r"[A-Za-z0-9_]{1,24}", expected_quantization) is None:
            raise BrowserRefusal("MODEL_QUANTIZATION_INVALID")
        config = _local_model_config()
        if not _loopback_base(config.ollama_base_url):
            raise BrowserRefusal("LOCAL_MODEL_CONFIG_REJECTED")
        count, quantization, digest = await _inventory(config.ollama_base_url, model)
        report["INSTALLED_MODEL_COUNT"] = count
        if quantization is None:
            raise BrowserRefusal("MODEL_NOT_INSTALLED")
        if quantization != expected_quantization:
            raise BrowserRefusal("MODEL_QUANTIZATION_MISMATCH")
        report["MODEL_QUANTIZATION_VERIFIED"] = True
        report["MODEL_DIGEST_SHA256"] = digest
        metadata = await local_ollama_metadata(config.ollama_base_url, model)
        resources = local_resource_evidence()
        gpu = runner.gpu_evidence()
        report["MODEL_INSTALLED"] = metadata.get("MODEL_INSTALLED")
        report["MODEL_RESIDENCY"] = metadata.get("MODEL_RESIDENCY")
        report["MODEL_SIZE_MIB"] = metadata.get("MODEL_SIZE_MIB")
        report["MODEL_REPORTED_VRAM_MIB"] = metadata.get("MODEL_REPORTED_VRAM_MIB")
        report["HOST_MEMORY_AVAILABLE_MIB"] = resources.get("HOST_MEMORY_AVAILABLE_MIB")
        report["CGROUP_MEMORY_HEADROOM_MIB"] = resources.get("CGROUP_MEMORY_HEADROOM_MIB")
        report["GPU_MEMORY_FREE_MIB"] = gpu.get("GPU_MEMORY_FREE_MIB")
        report["RESOURCE_PREFLIGHT_READY"] = _resource_ready(metadata, resources, gpu)
        if not report["RESOURCE_PREFLIGHT_READY"]:
            raise BrowserRefusal("RESOURCE_PREFLIGHT_REJECTED")
        report["VERDICT"] = "NON_GENERATIVE_PREFLIGHT_COMPLETE"
        report["FAILURE_CLASS"] = "NONE"
        return report, config
    except BrowserRefusal as exc:
        report["FAILURE_CLASS"] = exc.code
    except (httpx.HTTPError, OSError, ValueError, TypeError, TimeoutError):
        report["FAILURE_CLASS"] = "LOCAL_METADATA_UNAVAILABLE"
    return report, None


def _stage_a_qualified(evidence: dict[str, object]) -> bool:
    """Require a valid structured choice within the normal cold deadline."""
    elapsed = evidence.get("CLIENT_ELAPSED_MS")
    classes = evidence.get("CLASSIFICATIONS")
    return (evidence.get("MODEL_CALL_COUNT") == 1 and
            evidence.get("MODEL_RESPONSE_COUNT") == 1 and
            evidence.get("COMPLETE_RESPONSE") is True and
            evidence.get("STRUCTURED_ACTION_VALID") is True and
            type(elapsed) is int and elapsed <= r2.NORMAL_COLD_SECONDS * 1000 and
            type(classes) is list and
            "RESOURCE_PRESSURE_OBSERVED" not in classes)


def _failure_code(value: object) -> str:
    """Emit only fixed-shape status codes, never exception or model text."""
    return value if type(value) is str and re.fullmatch(r"[A-Z_]{3,48}", value) else "UNKNOWN_FAILURE"


async def evaluate(model: str, expected_sha: str, expected_quantization: str,
                   *, preflight_only: bool = False
                   ) -> dict[str, object]:
    """Run at most one Stage A call, then one separately consented P1 trial."""
    logger.remove()
    report, config = await _preflight(model, expected_sha, expected_quantization)
    if config is None or preflight_only:
        return report
    if not _consented("MODEL ONCE COMPACT"):
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = "STAGE_A_CONSENT_REQUIRED"
        return report

    try:
        trial = await r2.run_one_request({"MODEL_RESIDENCY": report["MODEL_RESIDENCY"]},
                                         config, selected_model=model)
    except Exception:
        report["VERDICT"] = "STRUCTURED_INFERENCE_BLOCKED"
        report["FAILURE_CLASS"] = "STAGE_A_EXECUTION_FAILED"
        return report
    report["STAGE_A"] = {key: trial.get(key) for key in SAFE_STAGE_A_FIELDS}
    report["INFERENCE_ATTEMPTED"] = trial.get("INFERENCE_ATTEMPTED") is True
    report["STAGE_A_QUALIFIED"] = _stage_a_qualified(trial)
    if not report["STAGE_A_QUALIFIED"]:
        report["VERDICT"] = "STRUCTURED_INFERENCE_BLOCKED"
        report["FAILURE_CLASS"] = _failure_code(trial.get("FAILURE_CLASS"))
        if report["FAILURE_CLASS"] == "NONE":
            report["FAILURE_CLASS"] = "STAGE_A_SLOW_OR_PRESSURED"
        return report

    try:
        _, quantization, digest = await _inventory(config.ollama_base_url, model)
        if (quantization != expected_quantization or digest is None or
                digest != report["MODEL_DIGEST_SHA256"]):
            raise BrowserRefusal("MODEL_IDENTITY_CHANGED")
    except (BrowserRefusal, httpx.HTTPError, OSError, ValueError, TimeoutError):
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = "MODEL_IDENTITY_CHANGED_OR_UNAVAILABLE"
        return report

    # The existing operator path rechecks the exact branch, model headroom,
    # confined fixture, then obtains its independent BROWSE ONCE consent.
    try:
        browser = await operator._evaluate(branch=BRANCH, fixture="heldout",
                                           objective=operator.OBJECTIVE,
                                           target=operator.TARGET,
                                           expected=operator.EXPECTED,
                                           model_override=model, hierarchical=True)
    except Exception:
        report["VERDICT"] = "HIERARCHICAL_PROTOCOL_OR_RESOURCE_BLOCKED"
        report["FAILURE_CLASS"] = "BROWSER_EVALUATION_FAILED"
        return report
    report["STAGE_B_ATTEMPTED"] = browser.get("MODEL_CALL_COUNT", 0) > 0
    report["STAGE_B"] = {key: browser.get(key) for key in SAFE_STAGE_B_FIELDS}
    if report["STAGE_B"].get("TRACE_SOURCE_VERIFIED") is not True:
        report["STAGE_B"]["SYNTHETIC_ACTION_TRACE"] = []
        report["STAGE_B"]["TRACE_COMPLETE"] = False
    report["TASK_ACCEPTANCE_PASSED"] = browser.get("TASK_ACCEPTANCE_PASSED") is True
    report["VERDICT"] = classify_browser(browser)
    report["FAILURE_CLASS"] = _failure_code(browser.get("FAILURE_CLASS"))
    report["PRODUCTION_QUALIFIED"] = False
    return report


def main() -> None:
    """Emit bounded evidence only; never export model or page text."""
    if (len(sys.argv) not in {7, 8} or sys.argv[1] != "--model" or
            sys.argv[3] != "--expected-sha" or
            sys.argv[5] != "--expect-quantization" or
            (len(sys.argv) == 8 and sys.argv[7] != "--preflight-only")):
        result: dict[str, Any] = {"VERDICT": "INVALID_INVOCATION",
                                  "PRODUCTION_QUALIFIED": False}
    else:
        try:
            result = asyncio.run(evaluate(sys.argv[2], sys.argv[4], sys.argv[6],
                                          preflight_only=len(sys.argv) == 8))
        except KeyboardInterrupt:
            result = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                      "FAILURE_CLASS": "OPERATOR_OR_SHELL_INTERRUPTED",
                      "PRODUCTION_QUALIFIED": False}
        except Exception:
            result = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                      "FAILURE_CLASS": "QUALIFICATION_ERROR",
                      "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
