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


BRANCH = "morgoth-generalist-compact-model-pressure-v1"
MODEL_NAME = re.compile(r"[A-Za-z0-9_.:/-]{1,80}\Z")
STAGE_A_DEADLINE_SECONDS = r2.DIAGNOSTIC_REQUEST_SECONDS
STAGE_B_MAX_CALLS = 6
SHELL_DEADLINE_SECONDS = 480
# Operator-reported Stage A, scoped to the exact installed artifact. This is
# historical evidence, not a credential, a model response, or an execution grant.
REVIEWED_STAGE_A_MODEL = "qwen3:4b-instruct"
REVIEWED_STAGE_A_DIGEST = "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
REVIEWED_STAGE_A_SUMMARY: dict[str, object] = {
    "GPU_FREE_MIB_MIN_OBSERVED": 2191,
    "HOST_AVAILABLE_MIB_MIN_OBSERVED": 6113,
    "SWAP_FREE_MIB_MIN_OBSERVED": 1746,
    "MEMORY_PRESSURE_AVG10_PCT_MAX_OBSERVED": 17.68,
    "SERVICE_CGROUP_OOM_COUNT_START": 0,
    "SERVICE_CGROUP_OOM_COUNT_END": 0,
    "SERVICE_CGROUP_OOM_KILL_COUNT_START": None,
    "SERVICE_CGROUP_OOM_KILL_COUNT_END": 0,
}
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
    """Require exact clean branch, tracking ref and live origin head."""
    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    if (operator._git("remote", "get-url", "origin") not in {
            "git@github.com:coriom/morgoth.git", "https://github.com/coriom/morgoth.git"} or
            operator._git("branch", "--show-current") != BRANCH or
            operator._git("rev-parse", "HEAD") != expected_sha or
            operator._git("rev-parse", f"origin/{BRANCH}") != expected_sha or
            operator._git("status", "--porcelain=v1", "--untracked-files=all") or
            operator._git("ls-remote", "--heads", "origin", BRANCH) !=
            f"{expected_sha}\trefs/heads/{BRANCH}"):
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
        report["HOST_SWAP_FREE_MIB"] = resources.get("HOST_SWAP_FREE_MIB")
        report["CGROUP_MEMORY_HEADROOM_MIB"] = resources.get("CGROUP_MEMORY_HEADROOM_MIB")
        report["GPU_MEMORY_FREE_MIB"] = gpu.get("GPU_MEMORY_FREE_MIB")
        for field in ("MEMORY_PRESSURE_AVG10_PCT",
                      "MEMORY_PRESSURE_FULL_AVG10_PCT",
                      "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT",
                      "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT"):
            report[field] = resources.get(field)
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


def _resource_assessment(summary: object) -> tuple[str, bool]:
    """Separate measured exhaustion/OOM from PSI contention in this experiment."""
    if type(summary) is not dict:
        return "RESOURCE_EVIDENCE_INSUFFICIENT", False
    for field, floor in (("GPU_FREE_MIB_MIN_OBSERVED", r2.MIN_GPU_FREE_MIB),
                         ("HOST_AVAILABLE_MIB_MIN_OBSERVED", 1024),
                         ("SWAP_FREE_MIB_MIN_OBSERVED", 128)):
        value = summary.get(field)
        if type(value) is not int or value < 0:
            return "RESOURCE_EVIDENCE_INSUFFICIENT", False
        if value < floor:
            return "RESOURCE_HARD_BLOCKED", False
    for kind in ("OOM", "OOM_KILL"):
        start = summary.get(f"SERVICE_CGROUP_{kind}_COUNT_START")
        end = summary.get(f"SERVICE_CGROUP_{kind}_COUNT_END")
        # A reported ending count of zero excludes a kill during that interval
        # even when the starting kill counter was not supplied.
        if kind == "OOM_KILL" and start is None and end == 0:
            continue
        if kind == "OOM_KILL" and start is None and type(end) is int and end > 0:
            return "RESOURCE_HARD_BLOCKED", False
        if type(start) is not int or type(end) is not int or min(start, end) < 0:
            return "RESOURCE_EVIDENCE_INSUFFICIENT", False
        if end < start:
            return "RESOURCE_EVIDENCE_INSUFFICIENT", False
        if end > start:
            return "RESOURCE_HARD_BLOCKED", False
    some = summary.get("MEMORY_PRESSURE_AVG10_PCT_MAX_OBSERVED")
    full = summary.get("MEMORY_PRESSURE_FULL_AVG10_PCT_MAX_OBSERVED")
    local_full = summary.get("CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT_MAX_OBSERVED")
    for value in (some, full, local_full):
        if value is not None and (type(value) not in {int, float} or not 0 <= value <= 100):
            return "RESOURCE_EVIDENCE_INSUFFICIENT", False
    # Existing R2 10% threshold denotes observed contention, not OOM.
    warning = ((some is not None and some >= 10) or
               (full is not None and full > 0) or
               (local_full is not None and local_full > 0))
    return ("RESOURCE_PRESSURE_WARNING" if warning else "RESOURCE_CLEAR"), warning


def _stage_a_assessment(evidence: dict[str, object]) -> tuple[bool, bool, str]:
    """Require valid structured output and known hard-resource measurements."""
    elapsed = evidence.get("CLIENT_ELAPSED_MS")
    classes = evidence.get("CLASSIFICATIONS")
    if not (evidence.get("MODEL_CALL_COUNT") == 1 and
            evidence.get("MODEL_RESPONSE_COUNT") == 1 and
            evidence.get("COMPLETE_RESPONSE") is True and
            evidence.get("STRUCTURED_ACTION_VALID") is True and
            evidence.get("FAILURE_CLASS") == "NONE" and
            type(elapsed) is int and elapsed <= r2.NORMAL_COLD_SECONDS * 1000 and
            type(classes) is list):
        return False, False, "STAGE_A_FORMAT_OR_LATENCY_BLOCKED"
    if any(value not in {"COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE",
                         "RESOURCE_PRESSURE_OBSERVED"} for value in classes
           if type(value) is str) or any(type(value) is not str for value in classes):
        return False, False, "STAGE_A_OTHER_CLASSIFICATION_BLOCKED"
    status, warning = _resource_assessment(evidence.get("RESOURCE_SUMMARY"))
    if ("RESOURCE_PRESSURE_OBSERVED" in classes and not warning and
            status == "RESOURCE_CLEAR"):
        return False, False, "RESOURCE_EVIDENCE_INSUFFICIENT"
    if status in {"RESOURCE_HARD_BLOCKED", "RESOURCE_EVIDENCE_INSUFFICIENT"}:
        return False, False, status
    return True, warning, status


def _stage_a_qualified(evidence: dict[str, object]) -> bool:
    """Compatibility helper for the experimental Stage A result."""
    return _stage_a_assessment(evidence)[0]


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
    qualified, warning, assessment = _stage_a_assessment(trial)
    report["STAGE_A_QUALIFIED"] = qualified
    report["STAGE_A_RESOURCE_ASSESSMENT"] = assessment
    report["STAGE_A_PRESSURE_WARNING"] = warning
    if not report["STAGE_A_QUALIFIED"]:
        report["VERDICT"] = "STRUCTURED_INFERENCE_BLOCKED"
        report["FAILURE_CLASS"] = _failure_code(trial.get("FAILURE_CLASS"))
        if report["FAILURE_CLASS"] == "NONE":
            report["FAILURE_CLASS"] = assessment
        return report

    return await _stage_b_after_a(report, config, model, expected_sha,
                                  expected_quantization, warning)


async def _stage_b_after_a(report: dict[str, object], config: object, model: str,
                           expected_sha: str, expected_quantization: str,
                           warning: bool) -> dict[str, object]:
    """Acknowledge pressure, remeasure resources, then invoke existing BROWSE ONCE gate."""
    if warning:
        if not _consented("MEMORY PRESSURE REVIEWED"):
            report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
            report["FAILURE_CLASS"] = "PRESSURE_ACKNOWLEDGEMENT_REQUIRED"
            return report
        report["PRESSURE_ACKNOWLEDGED"] = True
    fresh, fresh_config = await _preflight(model, expected_sha, expected_quantization)
    report["FRESH_RESOURCE_PREFLIGHT_READY"] = fresh_config is not None
    report["FRESH_MEMORY_PRESSURE_SOME_AVG10_PCT"] = fresh.get("MEMORY_PRESSURE_AVG10_PCT")
    report["FRESH_MEMORY_PRESSURE_FULL_AVG10_PCT"] = fresh.get("MEMORY_PRESSURE_FULL_AVG10_PCT")
    report["FRESH_CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT"] = fresh.get(
        "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT")
    if (fresh_config is None or fresh.get("MODEL_DIGEST_SHA256") !=
            report.get("MODEL_DIGEST_SHA256")):
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = "FRESH_RESOURCE_OR_MODEL_IDENTITY_BLOCKED"
        return report
    swap_free = fresh.get("HOST_SWAP_FREE_MIB")
    report["FRESH_SWAP_FREE_MIB"] = swap_free
    if type(swap_free) is not int or swap_free < 128:
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = "FRESH_SWAP_HEADROOM_BLOCKED"
        return report
    # A high full-stall average means all non-idle tasks were stalled together.
    # Keep the existing 10% contention threshold as a conservative Stage B stop.
    for field in ("MEMORY_PRESSURE_FULL_AVG10_PCT",
                  "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT"):
        value = fresh.get(field)
        if type(value) in {int, float} and value >= 10:
            report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
            report["FAILURE_CLASS"] = "FRESH_FULL_STALL_PRESSURE_BLOCKED"
            return report
    _, fresh_oom, fresh_oom_kill = r2._service_sample()
    report["FRESH_SERVICE_OOM_COUNT"] = fresh_oom
    report["FRESH_SERVICE_OOM_KILL_COUNT"] = fresh_oom_kill
    if (type(fresh_oom) is not int or type(fresh_oom_kill) is not int or
            fresh_oom != 0 or fresh_oom_kill != 0):
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = "FRESH_OOM_EVIDENCE_UNAVAILABLE_OR_CHANGED"
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


async def reviewed_stage_b(expected_sha: str) -> dict[str, object]:
    """Use operator-reported Stage A evidence without repeating its inference."""
    logger.remove()
    report, config = await _preflight(REVIEWED_STAGE_A_MODEL, expected_sha, "Q4_K_M")
    report["STAGE_A_EVIDENCE_SOURCE"] = "OPERATOR_REPORTED"
    report["STAGE_A_RESOURCE_SUMMARY"] = REVIEWED_STAGE_A_SUMMARY.copy()
    report["STAGE_A_CALL_REPEATED"] = False
    if config is None:
        return report
    if report.get("MODEL_DIGEST_SHA256") != REVIEWED_STAGE_A_DIGEST:
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = "REVIEWED_MODEL_DIGEST_MISMATCH"
        return report
    stage_a = {
        "MODEL_CALL_COUNT": 1, "MODEL_RESPONSE_COUNT": 1,
        "COMPLETE_RESPONSE": True, "STRUCTURED_ACTION_VALID": True,
        "CLIENT_ELAPSED_MS": 49537, "FAILURE_CLASS": "NONE",
        "CLASSIFICATIONS": ["COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE",
                            "RESOURCE_PRESSURE_OBSERVED"],
        "RESOURCE_SUMMARY": REVIEWED_STAGE_A_SUMMARY,
    }
    qualified, warning, assessment = _stage_a_assessment(stage_a)
    report["STAGE_A_QUALIFIED"] = qualified
    report["STAGE_A_PRESSURE_WARNING"] = warning
    report["STAGE_A_RESOURCE_ASSESSMENT"] = assessment
    if not qualified:
        report["VERDICT"] = "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED"
        report["FAILURE_CLASS"] = assessment
        return report
    return await _stage_b_after_a(report, config, REVIEWED_STAGE_A_MODEL,
                                  expected_sha, "Q4_K_M", warning)


def main() -> None:
    """Emit bounded evidence only; never export model or page text."""
    if (len(sys.argv) == 4 and sys.argv[1] == "--reviewed-stage-b" and
            sys.argv[2] == "--expected-sha"):
        try:
            result = asyncio.run(reviewed_stage_b(sys.argv[3]))
        except KeyboardInterrupt:
            result = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                      "FAILURE_CLASS": "OPERATOR_OR_SHELL_INTERRUPTED",
                      "PRODUCTION_QUALIFIED": False}
        except Exception:
            result = {"VERDICT": "MODEL_RESOURCE_OR_PROTOCOL_BLOCKED",
                      "FAILURE_CLASS": "QUALIFICATION_ERROR",
                      "PRODUCTION_QUALIFIED": False}
    elif (len(sys.argv) not in {7, 8} or sys.argv[1] != "--model" or
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
