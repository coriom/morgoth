"""Operator-local Ollama reliability preflight and optional one-call probe.

The default mode makes only bounded non-generative loopback requests. The
separate --probe mode requires foreground typed consent and never opens a
browser or executes a capability.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.parse import urlsplit
from typing import Callable

from loguru import logger

from agents.generalist_browser_lab import (
    BrowserObservation, BrowserRefusal, FIRST_COLD_MODEL_SECONDS,
    FIRST_LOADED_MODEL_SECONDS, FOLLOWUP_MODEL_SECONDS, INTERACTIVE_SECONDS,
    WORKER_ACTION_WAIT_SECONDS, OllamaBrowserActionSource, ObservedElement,
)
from agents.generalist_ollama_resources import (
    local_ollama_metadata, local_resource_evidence,
)
from agents.generalist_ollama_plan_probe import _local_model_config
from core.llm_client import OllamaLLMClient
from scripts.codex_local_warning_review import ReviewUnavailable, _consent, open_operator_terminal


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "morgoth-generalist-ollama-reliability-v1"
MIN_HOST_MEMORY_MIB = 2048
MIN_CGROUP_HEADROOM_MIB = 1024


def _git(*args: str) -> str:
    """Read checkout metadata without exposing remote diagnostics."""
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                            text=True, timeout=8, check=False,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    return result.stdout.strip()


def _checkout_verified(expected_branch: str | None = None) -> None:
    """Require the exact clean pushed feature branch before operator use."""
    branch = expected_branch or BRANCH
    try:
        if _git("remote", "get-url", "origin") not in {
                "git@github.com:coriom/morgoth.git", "https://github.com/coriom/morgoth.git"}:
            raise BrowserRefusal("PREFLIGHT_CHECKOUT")
        local = _git("rev-parse", "HEAD")
        remote = _git("ls-remote", "--heads", "origin", branch).split()
        if (_git("branch", "--show-current") != branch or
                local != _git("rev-parse", f"origin/{branch}") or
                remote != [local, f"refs/heads/{branch}"] or
                _git("status", "--porcelain=v1", "--untracked-files=all")):
            raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    except (OSError, subprocess.SubprocessError):
        raise BrowserRefusal("PREFLIGHT_CHECKOUT") from None


def _timeout_evidence() -> dict[str, object]:
    """Expose the reviewed browser and probe deadline relationships."""
    return {
        "FIRST_COLD_MODEL_SECONDS": FIRST_COLD_MODEL_SECONDS,
        "FIRST_LOADED_MODEL_SECONDS": FIRST_LOADED_MODEL_SECONDS,
        "FOLLOWUP_MODEL_SECONDS": FOLLOWUP_MODEL_SECONDS,
        "WORKER_ACTION_WAIT_SECONDS": WORKER_ACTION_WAIT_SECONDS,
        "BROWSER_INTERACTION_SECONDS": INTERACTIVE_SECONDS,
        "BROWSER_MAX_MODEL_CALLS": 6,
        "HTTP_CLIENT_TIMEOUT_SECONDS": 90,
        "BROWSER_DEADLINES_ORDERED": (
            FOLLOWUP_MODEL_SECONDS < WORKER_ACTION_WAIT_SECONDS and
            FIRST_LOADED_MODEL_SECONDS < WORKER_ACTION_WAIT_SECONDS and
            FIRST_COLD_MODEL_SECONDS < WORKER_ACTION_WAIT_SECONDS and
            WORKER_ACTION_WAIT_SECONDS < INTERACTIVE_SECONDS),
    }


def _tcp_probe(base_url: str) -> bool:
    """Check only the configured loopback socket, without sending a request."""
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme == "http" and parsed.hostname in
                 {"localhost", "127.0.0.1", "::1"} and parsed.port is not None and
                 not parsed.username and not parsed.password and
                 parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        return False
    if not valid:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=2):
            return True
    except (OSError, ValueError):
        return False


async def _preflight(expected_branch: str | None = None,
                     checkout_verifier: Callable[[], None] | None = None
                     ) -> tuple[dict[str, object], object | None]:
    """Gather safe local facts without a generative request or model warming."""
    report: dict[str, object] = {
        "VERDICT": "NON_GENERATIVE_PREFLIGHT_INCOMPLETE",
        "INFERENCE_ATTEMPTED": False, "RESPONSE_RECEIVED": False,
        "LOCAL_TCP_CONNECT": "UNKNOWN", "MODEL_INSTALLED": None,
        "MODEL_RESIDENCY": "UNKNOWN", "MODEL_SIZE_MIB": None,
        "MODEL_REPORTED_VRAM_MIB": None, "OTHER_LOADED_MODEL_COUNT": None,
        "OLLAMA_TAGS_RESPONSIVE": False, "OLLAMA_PS_RESPONSIVE": False,
        "OLLAMA_TAGS_STATUS": "NOT_REQUESTED", "OLLAMA_PS_STATUS": "NOT_REQUESTED",
        "COMPETING_INFERENCE_REQUESTS": "UNKNOWN", "CPU_GPU_PLACEMENT": "UNKNOWN",
        "PRODUCTION_QUALIFIED": False,
    }
    report.update(_timeout_evidence())
    try:
        if checkout_verifier is not None:
            checkout_verifier()
        elif expected_branch is None:
            _checkout_verified()
        else:
            _checkout_verified(expected_branch)
        config = _local_model_config()
    except (BrowserRefusal, OSError, ValueError) as exc:
        report["FAILURE_CLASS"] = (exc.code if isinstance(exc, BrowserRefusal)
                                   else "LOCAL_CONFIG_REJECTED")
        return report, None
    report.update(local_resource_evidence())
    report["LOCAL_TCP_CONNECT"] = "PASS" if _tcp_probe(config.ollama_base_url) else "BLOCKED"
    if report["LOCAL_TCP_CONNECT"] != "PASS":
        report["FAILURE_CLASS"] = "LOCAL_TCP_CONNECT_BLOCKED"
        return report, config
    report.update(await local_ollama_metadata(config.ollama_base_url,
                                              config.ollama_primary_model))
    if report["OLLAMA_TAGS_RESPONSIVE"] is not True:
        report["FAILURE_CLASS"] = "OLLAMA_TAGS_UNAVAILABLE"
    elif report["MODEL_INSTALLED"] is not True:
        report["FAILURE_CLASS"] = "MODEL_NOT_INSTALLED"
    elif report["OLLAMA_PS_RESPONSIVE"] is not True:
        report["FAILURE_CLASS"] = "OLLAMA_PS_UNAVAILABLE"
    else:
        report["VERDICT"] = "NON_GENERATIVE_PREFLIGHT_COMPLETE"
        report["FAILURE_CLASS"] = "NONE"
    return report, config


def _synthetic_observation() -> BrowserObservation:
    """Supply a fixed small browser-like menu without opening a browser."""
    return BrowserObservation(
        revision=1, origin="http://127.0.0.1:1", title="Synthetic stock lookup",
        text="Look up a public synthetic stock record.",
        elements=[
            ObservedElement(id="e0", tag="button", role="", name="Quick advice",
                            href="", value="", input_type="", disabled=False,
                            readonly=False),
            ObservedElement(id="e1", tag="button", role="", name="Inventory",
                            href="", value="", input_type="", disabled=False,
                            readonly=False),
        ])


async def _probe(report: dict[str, object], config: object) -> dict[str, object]:
    """Make at most one consented, tool-free structured request via the browser source."""
    if (report["VERDICT"] != "NON_GENERATIVE_PREFLIGHT_COMPLETE" or
            report["MODEL_RESIDENCY"] not in {"LOADED", "NOT_LOADED"}):
        report["VERDICT"] = "PROBE_PREFLIGHT_BLOCKED"
        return report
    if ((report["HOST_MEMORY_AVAILABLE_MIB"] is not None and
         report["HOST_MEMORY_AVAILABLE_MIB"] < MIN_HOST_MEMORY_MIB) or
            (report["CGROUP_MEMORY_HEADROOM_MIB"] is not None and
             report["CGROUP_MEMORY_HEADROOM_MIB"] < MIN_CGROUP_HEADROOM_MIB)):
        report["VERDICT"] = "PROBE_RESOURCE_BLOCKED"
        report["FAILURE_CLASS"] = "RESOURCE_PREFLIGHT_LOW_MEMORY"
        return report
    try:
        with open_operator_terminal() as terminal:
            if not _consent(terminal, "MODEL ONCE"):
                report["VERDICT"] = "PROBE_CONSENT_DECLINED"
                report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
                return report
    except ReviewUnavailable:
        report["VERDICT"] = "PROBE_CONSENT_DECLINED"
        report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
        return report

    # Match the reviewed browser operator transport after clearing proxies.
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy",
                "https_proxy", "all_proxy"):
        os.environ.pop(key, None)
    client = OllamaLLMClient(config, timeout=90)
    source = OllamaBrowserActionSource(
        client, config.ollama_primary_model,
        loaded=report["MODEL_RESIDENCY"] == "LOADED")
    report["MODEL_RESIDENCY_BEFORE"] = report["MODEL_RESIDENCY"]
    try:
        try:
            report["MODEL_REQUEST_STARTED_AT_UTC"] = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds")
            await source.propose("Check inventory stock item SKU-44", "SKU-44",
                                 _synthetic_observation())
            report["VERDICT"] = "STRUCTURED_RESPONSE_CONFIRMED"
            report["FAILURE_CLASS"] = "NONE"
        except BrowserRefusal as exc:
            report["VERDICT"] = "LOCAL_INFERENCE_BLOCKED"
            report["FAILURE_CLASS"] = exc.code
        except Exception:
            report["VERDICT"] = "LOCAL_INFERENCE_BLOCKED"
            report["FAILURE_CLASS"] = "PROBE_UNEXPECTED_FAILURE"
        finally:
            report["MODEL_REQUEST_ENDED_AT_UTC"] = datetime.now(timezone.utc).isoformat(
                timespec="milliseconds")
            report.update({
                "INFERENCE_ATTEMPTED": source.calls == 1,
                "RESPONSE_RECEIVED": source.responses == 1,
                "HTTP_RESPONSE_RECEIVED": source.http_response_received,
                "HTTP_STATUS": source.http_status,
                "MODEL_REQUEST_ELAPSED_MS": source.last_elapsed_ms,
                "MODEL_TOTAL_DURATION_MS": source.last_total_ms,
                "MODEL_LOAD_DURATION_MS": source.last_load_ms,
                "PROMPT_EVAL_DURATION_MS": source.last_prompt_eval_ms,
                "GENERATION_DURATION_MS": source.last_generation_ms,
                "PROMPT_TOKENS": source.last_prompt_tokens,
                "GENERATED_TOKENS": source.last_output_tokens,
            })
    finally:
        await client.close()
    after = await local_ollama_metadata(config.ollama_base_url,
                                        config.ollama_primary_model)
    report["MODEL_RESIDENCY_AFTER"] = after["MODEL_RESIDENCY"]
    report["OLLAMA_PS_RESPONSIVE_AFTER"] = after["OLLAMA_PS_RESPONSIVE"]
    pressure = local_resource_evidence()
    report["HOST_MEMORY_AVAILABLE_MIB_AFTER"] = pressure["HOST_MEMORY_AVAILABLE_MIB"]
    report["CGROUP_MEMORY_HEADROOM_MIB_AFTER"] = pressure["CGROUP_MEMORY_HEADROOM_MIB"]
    report["MEMORY_PRESSURE_AVG10_PCT_AFTER"] = pressure["MEMORY_PRESSURE_AVG10_PCT"]
    return report


async def _evaluate(probe: bool = False) -> dict[str, object]:
    """Run safe preflight and optionally one separately consented local call."""
    logger.remove()
    report, config = await _preflight()
    if not probe or config is None:
        return report
    return await _probe(report, config)


def main() -> None:
    """Emit only fixed, bounded metadata; never prompts or response bodies."""
    if sys.argv[1:] not in ([], ["--probe"]):
        result: dict[str, object] = {"VERDICT": "INVALID_INVOCATION",
                                     "PRODUCTION_QUALIFIED": False}
    else:
        try:
            result = asyncio.run(_evaluate(sys.argv[1:] == ["--probe"]))
        except (OSError, ValueError, RuntimeError):
            result = {"VERDICT": "DIAGNOSTIC_ERROR", "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
