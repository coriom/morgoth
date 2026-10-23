"""One consented cold-load experiment; never a browser or production runner.

The normal browser's 60-second cold deadline is untouched. This module alone
allows one 120-second, tool-free structured request after foreground consent.
All journal and model content remains private; output contains fixed codes and
bounded numeric evidence only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import time
from typing import Any

import httpx
from loguru import logger

from agents import generalist_ollama_reliability as reliability
from agents import generalist_ollama_runner_diagnostic as runner
from agents.generalist_browser_lab import BrowserRefusal, OllamaBrowserActionSource
from agents.generalist_ollama_resources import (
    MetadataRefusal, _bounded_metadata_get, _loopback_base, _unique_pairs,
    local_resource_evidence,
)
from core.llm_client import ChatMessage, ChatResponse
from scripts.codex_local_warning_review import ReviewUnavailable, _consent, open_operator_terminal


BRANCH = "morgoth-generalist-ollama-cold-load-r2-v1"
EXPECTED_MODEL = "llama3.1:8b"
EXPECTED_CONTEXT = 4096
NORMAL_COLD_SECONDS = 60
DIAGNOSTIC_REQUEST_SECONDS = 120
DIAGNOSTIC_OUTER_SECONDS = 123
HTTP_PHASE_TIMEOUT_SECONDS = 125
SHELL_TIMEOUT_SECONDS = 300
SAMPLE_INTERVAL_SECONDS = 5
MAX_SAMPLES = 26
MAX_CHAT_RESPONSE_BYTES = 32768
GPU_SAMPLE_TIMEOUT_SECONDS = 3
JOURNAL_SETTLE_SECONDS = 0.5
MIN_HOST_MEMORY_MIB = reliability.MIN_HOST_MEMORY_MIB
MIN_CGROUP_HEADROOM_MIB = reliability.MIN_CGROUP_HEADROOM_MIB
MIN_GPU_FREE_MIB = 512


def verify_pinned_checkout(expected_sha: str) -> None:
    """Require the reviewed pushed commit locally, without network traffic."""
    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    if (reliability._git("remote", "get-url", "origin") not in {
            "git@github.com:coriom/morgoth.git", "https://github.com/coriom/morgoth.git"} or
            reliability._git("branch", "--show-current") != BRANCH or
            reliability._git("rev-parse", "HEAD") != expected_sha or
            reliability._git("rev-parse", f"origin/{BRANCH}") != expected_sha or
            reliability._git("status", "--porcelain=v1", "--untracked-files=all")):
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")


class DiagnosticChatClient:
    """One bounded loopback POST with the existing browser proposal contract."""

    def __init__(self, config: object, *, selected_model: str = EXPECTED_MODEL,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if (not _loopback_base(config.ollama_base_url) or
                re.fullmatch(r"[A-Za-z0-9_.:/-]{1,80}", selected_model) is None or
                config.ollama_num_ctx != EXPECTED_CONTEXT):
            raise BrowserRefusal("PROBE_CONFIG_REJECTED")
        self.config = config
        self.selected_model = selected_model
        self.calls = 0
        self.http_response_received = False
        self.http_status: int | None = None
        self.client = httpx.AsyncClient(
            base_url=config.ollama_base_url,
            timeout=httpx.Timeout(HTTP_PHASE_TIMEOUT_SECONDS, connect=3),
            follow_redirects=False, trust_env=False, transport=transport)

    async def close(self) -> None:
        """Close the owned HTTP connection, including after cancellation."""
        await self.client.aclose()

    async def chat(self, messages: list[ChatMessage], *, model: str,
                   tools: object, stream: bool, options: dict[str, object],
                   format: dict[str, object]) -> ChatResponse:
        """Send the source's exact schema-constrained request once, with a byte cap."""
        if self.calls != 0:
            raise BrowserRefusal("PROBE_REQUEST_LIMIT")
        if (model != self.selected_model or tools is not None or stream is not False or
                options != {"temperature": 0, "num_predict": 384} or
                type(format) is not dict or
                set(format.get("required", [])) != {"revision", "choice_id"} or
                len(messages) != 2 or [item.role for item in messages] != ["system", "user"]):
            raise BrowserRefusal("PROBE_CONTRACT_REJECTED")
        self.calls += 1
        payload = {"model": model, "messages": [item.to_ollama_dict() for item in messages],
                   "stream": False, "options": {"num_ctx": EXPECTED_CONTEXT, **options},
                   "format": format}
        async with self.client.stream("POST", "/api/chat", json=payload) as response:
            self.http_response_received = True
            self.http_status = response.status_code if 100 <= response.status_code <= 599 else None
            if response.status_code != 200:
                # Do not retain or parse an error body. The browser source
                # classifies only the bounded status; private prose is discarded.
                safe = httpx.Response(response.status_code, json={}, request=response.request)
                raise httpx.HTTPStatusError("local model status", request=safe.request,
                                            response=safe)
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_CHAT_RESPONSE_BYTES:
                raise ValueError("bounded response exceeded")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_CHAT_RESPONSE_BYTES:
                    raise ValueError("bounded response exceeded")
        parsed = json.loads(body, object_pairs_hook=_unique_pairs)
        if type(parsed) is not dict:
            raise ValueError("invalid response envelope")
        response_model = parsed.get("model")
        if response_model not in {self.selected_model, self.selected_model + ":latest"}:
            raise ValueError("unexpected response model")
        return ChatResponse.model_validate(parsed)


class ExtendedActionSource(OllamaBrowserActionSource):
    """Reuse the exact structured browser proposal with an isolated deadline."""

    def next_timeout_seconds(self) -> int:
        """Return the experimental deadline without changing browser defaults."""
        return DIAGNOSTIC_REQUEST_SECONDS


async def _ps_state(base_url: str, model: str,
                    client: httpx.AsyncClient) -> tuple[str, int | None]:
    """Read only /api/ps; absence means not listed, not failed loading."""
    if not _loopback_base(base_url):
        return "UNKNOWN", None
    try:
        payload, _ = await asyncio.wait_for(_bounded_metadata_get(client, "/api/ps"),
                                            timeout=3)
        if (type(payload) is not dict or type(payload.get("models")) is not list or
                len(payload["models"]) > 64):
            return "UNKNOWN", None
        names = {model, model + ":latest"}
        for item in payload["models"]:
            if type(item) is not dict or type(item.get("name")) is not str:
                return "UNKNOWN", None
            if item["name"] in names or item.get("model") in names:
                vram = item.get("size_vram")
                return "LISTED", (vram // 1048576 if type(vram) is int and
                                  0 <= vram <= 10**12 else None)
        return "NOT_LISTED", None
    except (TimeoutError, httpx.HTTPError, OSError, ValueError, TypeError,
            UnicodeError, MetadataRefusal):
        return "UNKNOWN", None


async def _gpu_sample() -> tuple[int | None, int | None]:
    """Run one fixed local GPU memory query and reap it on timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/lib/wsl/lib/nvidia-smi",
            "--query-gpu=memory.used,memory.free",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None, None
    try:
        output, _ = await asyncio.wait_for(process.communicate(),
                                           timeout=GPU_SAMPLE_TIMEOUT_SECONDS)
        if process.returncode != 0 or len(output) > 128:
            return None, None
        match = re.fullmatch(rb"\s*(\d{1,6}),\s*(\d{1,6})\s*", output)
        return (int(match.group(1)), int(match.group(2))) if match else (None, None)
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        raise


def _service_sample() -> tuple[int | None, int | None, int | None]:
    """Read only fixed Ollama service cgroup memory and OOM counters."""
    scope = Path("/sys/fs/cgroup/system.slice/ollama.service")
    memory = oom = oom_kill = None
    try:
        raw = (scope / "memory.current").read_text()[:32].strip()
        if raw.isdigit():
            memory = min(1_000_000, int(raw) // 1048576)
        for line in (scope / "memory.events").read_text()[:512].splitlines():
            words = line.split()
            if len(words) == 2 and words[1].isdigit():
                if words[0] == "oom":
                    oom = min(1_000_000, int(words[1]))
                elif words[0] == "oom_kill":
                    oom_kill = min(1_000_000, int(words[1]))
    except (OSError, ValueError):
        pass
    return memory, oom, oom_kill


async def _sample_once(started_mono: float, model: str,
                       client: httpx.AsyncClient) -> dict[str, object]:
    """Take one bounded in-memory resource sample with no raw metadata output."""
    start_mono = time.monotonic()
    utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    resources = local_resource_evidence()
    service_memory, oom, oom_kill = _service_sample()
    ps, gpu = await asyncio.gather(
        _ps_state(str(client.base_url), model, client), _gpu_sample(),
        return_exceptions=True)
    used, free = gpu if type(gpu) is tuple else (None, None)
    ps_state, ps_vram = ps if type(ps) is tuple else ("UNKNOWN", None)
    return {
        "UTC": utc,
        "ELAPSED_MS": max(0, min(200000, round((start_mono - started_mono) * 1000))),
        "SPAN_MS": max(0, min(10000, round((time.monotonic() - start_mono) * 1000))),
        "MODEL_PS_STATE": ps_state if ps_state in {"LISTED", "NOT_LISTED", "UNKNOWN"} else "UNKNOWN",
        "MODEL_PS_VRAM_MIB": ps_vram,
        "GPU_USED_MIB": used, "GPU_FREE_MIB": free,
        "HOST_AVAILABLE_MIB": resources.get("HOST_MEMORY_AVAILABLE_MIB"),
        "SWAP_FREE_MIB": resources.get("HOST_SWAP_FREE_MIB"),
        "MEMORY_PRESSURE_AVG10_PCT": resources.get("MEMORY_PRESSURE_AVG10_PCT"),
        "MEMORY_PRESSURE_FULL_AVG10_PCT": resources.get("MEMORY_PRESSURE_FULL_AVG10_PCT"),
        "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT": resources.get(
            "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT"),
        "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT": resources.get(
            "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT"),
        "SERVICE_MEMORY_MIB": service_memory,
        "SERVICE_CGROUP_OOM_COUNT": oom,
        "SERVICE_CGROUP_OOM_KILL_COUNT": oom_kill,
    }


async def sample_during_request(started_mono: float, base_url: str,
                                model: str, stop: asyncio.Event) -> list[dict[str, object]]:
    """Sample at most 26 times at five-second spacing without overlapping GETs."""
    samples: list[dict[str, object]] = []
    async with httpx.AsyncClient(base_url=base_url, trust_env=False,
                                 follow_redirects=False,
                                 timeout=httpx.Timeout(3, connect=2)) as client:
        for index in range(MAX_SAMPLES):
            if stop.is_set():
                break
            samples.append(await _sample_once(started_mono, model, client))
            next_due = started_mono + (index + 1) * SAMPLE_INTERVAL_SECONDS
            wait = max(0, next_due - time.monotonic())
            if wait == 0 and time.monotonic() - next_due > SAMPLE_INTERVAL_SECONDS:
                # A slow local source may skip a slot but cannot cause a burst.
                continue
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except TimeoutError:
                pass
    return samples


def _summary(samples: list[dict[str, object]]) -> dict[str, object]:
    """Compute observed extrema only; missing measurements remain unknown."""
    def maximum(key: str) -> int | float | None:
        values = [row[key] for row in samples if type(row.get(key)) in {int, float}]
        return max(values) if values else None

    def minimum(key: str) -> int | float | None:
        values = [row[key] for row in samples if type(row.get(key)) in {int, float}]
        return min(values) if values else None

    return {
        "SAMPLE_COUNT": len(samples),
        "GPU_USED_MIB_MAX_OBSERVED": maximum("GPU_USED_MIB"),
        "GPU_FREE_MIB_MIN_OBSERVED": minimum("GPU_FREE_MIB"),
        "MODEL_PS_VRAM_MIB_MAX_OBSERVED": maximum("MODEL_PS_VRAM_MIB"),
        "HOST_AVAILABLE_MIB_MIN_OBSERVED": minimum("HOST_AVAILABLE_MIB"),
        "SWAP_FREE_MIB_MIN_OBSERVED": minimum("SWAP_FREE_MIB"),
        "MEMORY_PRESSURE_AVG10_PCT_MAX_OBSERVED": maximum(
            "MEMORY_PRESSURE_AVG10_PCT"),
        "MEMORY_PRESSURE_FULL_AVG10_PCT_MAX_OBSERVED": maximum(
            "MEMORY_PRESSURE_FULL_AVG10_PCT"),
        "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT_MAX_OBSERVED": maximum(
            "CGROUP_MEMORY_PRESSURE_SOME_AVG10_PCT"),
        "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT_MAX_OBSERVED": maximum(
            "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT"),
        "SERVICE_MEMORY_MIB_MAX_OBSERVED": maximum("SERVICE_MEMORY_MIB"),
        "SERVICE_CGROUP_OOM_COUNT_START": next((r["SERVICE_CGROUP_OOM_COUNT"]
            for r in samples if type(r.get("SERVICE_CGROUP_OOM_COUNT")) is int), None),
        "SERVICE_CGROUP_OOM_COUNT_END": next((r["SERVICE_CGROUP_OOM_COUNT"]
            for r in reversed(samples) if type(r.get("SERVICE_CGROUP_OOM_COUNT")) is int), None),
        "SERVICE_CGROUP_OOM_KILL_COUNT_START": next((r["SERVICE_CGROUP_OOM_KILL_COUNT"]
            for r in samples if type(r.get("SERVICE_CGROUP_OOM_KILL_COUNT")) is int), None),
        "SERVICE_CGROUP_OOM_KILL_COUNT_END": next((r["SERVICE_CGROUP_OOM_KILL_COUNT"]
            for r in reversed(samples) if type(r.get("SERVICE_CGROUP_OOM_KILL_COUNT")) is int), None),
    }


def classify_probe(report: dict[str, object]) -> list[str]:
    """Classify only measured timing, failures and pressure; no causal guess."""
    if report.get("INFERENCE_ATTEMPTED") is not True:
        return ["PROTOCOL_OR_PREFLIGHT_BLOCKED"]
    classes: list[str] = []
    elapsed = report.get("CLIENT_ELAPSED_MS")
    complete = report.get("COMPLETE_RESPONSE") is True
    if complete and type(elapsed) is int:
        classes.append("COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE" if
                       elapsed <= NORMAL_COLD_SECONDS * 1000 else
                       "COLD_LOAD_COMPLETED_AFTER_NORMAL_DEADLINE")
    journal = report.get("PROBE_JOURNAL")
    started = report.get("CLIENT_STARTED_UTC")
    ended = report.get("CLIENT_ENDED_UTC")
    if (type(journal) is dict and journal.get("STATUS") == "OK" and
            journal.get("TRUNCATED_BY_RECORD_LIMIT") is False and
            report.get("CLOCK_DISCONTINUITY") is False and
            type(started) is str and type(ended) is str):
        try:
            start_dt, end_dt = datetime.fromisoformat(started), datetime.fromisoformat(ended)
            events = journal.get("EVENTS", [])
            if type(events) is list:
                prior = []
                ready = []
                for item in events[:64]:
                    if type(item) is not dict or type(item.get("UTC")) is not str or type(item.get("CLASSES")) is not list:
                        continue
                    event_dt = datetime.fromisoformat(item["UTC"])
                    if not (start_dt <= event_dt < end_dt):
                        continue
                    event_classes = item["CLASSES"]
                    if ({"RUNNER_FAILURE_REPORTED", "MODEL_LOAD_FAILURE_REPORTED"}
                            .intersection(event_classes) and
                            "CANCELLATION_REPORTED" not in event_classes):
                        prior.append(event_dt)
                    if "RUNNER_READY_REPORTED" in event_classes:
                        ready.append(event_dt)
                if prior and report.get("FAILURE_CLASS") == "MODEL_TIMEOUT":
                    classes.append("RUNNER_FAILURE_BEFORE_CLIENT_CANCELLATION")
                if ready and not complete and report.get("FAILURE_CLASS") == "MODEL_TIMEOUT":
                    classes.append("MODEL_READY_BUT_INFERENCE_STALLED")
        except (ValueError, TypeError):
            pass
    summary = report.get("RESOURCE_SUMMARY")
    if type(summary) is dict:
        pressure = (
            (type(summary.get("GPU_FREE_MIB_MIN_OBSERVED")) is int and
             summary["GPU_FREE_MIB_MIN_OBSERVED"] < 512) or
            (type(summary.get("HOST_AVAILABLE_MIB_MIN_OBSERVED")) is int and
             summary["HOST_AVAILABLE_MIB_MIN_OBSERVED"] < 1024) or
            (type(summary.get("SWAP_FREE_MIB_MIN_OBSERVED")) is int and
             summary["SWAP_FREE_MIB_MIN_OBSERVED"] < 128) or
            (type(summary.get("MEMORY_PRESSURE_AVG10_PCT_MAX_OBSERVED")) in {int, float} and
             summary["MEMORY_PRESSURE_AVG10_PCT_MAX_OBSERVED"] >= 10) or
            (type(summary.get("SERVICE_CGROUP_OOM_COUNT_START")) is int and
             type(summary.get("SERVICE_CGROUP_OOM_COUNT_END")) is int and
             summary["SERVICE_CGROUP_OOM_COUNT_END"] > summary["SERVICE_CGROUP_OOM_COUNT_START"]))
        if pressure:
            classes.append("RESOURCE_PRESSURE_OBSERVED")
    if report.get("FAILURE_CLASS") not in {"NONE", "MODEL_TIMEOUT"}:
        classes.append("PROTOCOL_OR_PREFLIGHT_BLOCKED")
    if not complete and report.get("FAILURE_CLASS") == "MODEL_TIMEOUT" and not classes:
        classes.append("EXTENDED_PROBE_TIMEOUT_UNRESOLVED")
    return classes or ["PROTOCOL_OR_PREFLIGHT_BLOCKED"]


def _preflight_sufficient(report: dict[str, object], config: object | None,
                          service: dict[str, object]) -> str | None:
    """Reject non-cold, uninstalled, resource-unknown or unreviewed conditions."""
    if (config is None or report.get("VERDICT") != "NON_GENERATIVE_PREFLIGHT_COMPLETE" or
            service.get("SERVICE_STATE") != "ACTIVE" or
            report.get("MODEL_INSTALLED") is not True or
            report.get("MODEL_RESIDENCY") != "NOT_LOADED" or
            report.get("OTHER_LOADED_MODEL_COUNT") != 0 or
            getattr(config, "ollama_primary_model", None) != EXPECTED_MODEL or
            getattr(config, "ollama_num_ctx", None) != EXPECTED_CONTEXT or
            not _loopback_base(getattr(config, "ollama_base_url", ""))):
        return "PROBE_PREFLIGHT_REJECTED"
    available = report.get("HOST_MEMORY_AVAILABLE_MIB")
    gpu_free = report.get("GPU_MEMORY_FREE_MIB")
    kind = report.get("CGROUP_MEMORY_LIMIT_KIND")
    if (type(available) is not int or available < MIN_HOST_MEMORY_MIB or
            type(gpu_free) is not int or gpu_free < MIN_GPU_FREE_MIB or
            kind not in {"FINITE", "UNLIMITED"} or
            (kind == "FINITE" and (type(report.get("CGROUP_MEMORY_HEADROOM_MIB")) is not int or
                                   report["CGROUP_MEMORY_HEADROOM_MIB"] < MIN_CGROUP_HEADROOM_MIB))):
        return "RESOURCE_PREFLIGHT_REJECTED"
    return None


async def run_one_request(report: dict[str, object], config: object,
                          *, client: DiagnosticChatClient | None = None,
                          selected_model: str | None = None,
                          source_type: type[OllamaBrowserActionSource] = ExtendedActionSource
                          ) -> dict[str, object]:
    """Perform exactly one bounded request and collect only acknowledged facts."""
    model = selected_model or config.ollama_primary_model
    owned = client or DiagnosticChatClient(config, selected_model=model)
    source = source_type(owned, model, loaded=report.get("MODEL_RESIDENCY") == "LOADED")
    started_mono = time.monotonic()
    started_utc = datetime.now(timezone.utc)
    report["CLIENT_STARTED_UTC"] = started_utc.isoformat(timespec="milliseconds")
    report["MODEL_RESIDENCY_BEFORE"] = report.get("MODEL_RESIDENCY")
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample_during_request(
        started_mono, config.ollama_base_url, model, stop))
    failure = "NONE"
    try:
        try:
            await asyncio.wait_for(source.propose(
                "Check inventory stock item SKU-44", "SKU-44",
                reliability._synthetic_observation()), timeout=DIAGNOSTIC_OUTER_SECONDS)
        except BrowserRefusal as exc:
            failure = exc.code
        except TimeoutError:
            failure = "MODEL_TIMEOUT"
        except Exception:
            failure = "PROBE_UNEXPECTED_FAILURE"
    finally:
        ended_mono = time.monotonic()
        ended_utc = datetime.now(timezone.utc)
        stop.set()
        try:
            samples = await asyncio.wait_for(sampler, timeout=6)
        except Exception:
            sampler.cancel()
            try:
                await sampler
            except (asyncio.CancelledError, Exception):
                pass
            samples = []
        await owned.close()
        report["CLIENT_ENDED_UTC"] = ended_utc.isoformat(timespec="milliseconds")
        report["CLIENT_ELAPSED_MS"] = max(0, min(200000, round(
            (ended_mono - started_mono) * 1000)))
        report["MODEL_CALL_COUNT"] = source.calls
        report["MODEL_RESPONSE_COUNT"] = source.responses
        report["INFERENCE_ATTEMPTED"] = source.calls == 1
        report["RESPONSE_RECEIVED"] = source.responses == 1
        report["HTTP_RESPONSE_RECEIVED"] = (source.http_response_received or
                                            bool(getattr(owned, "http_response_received", False)))
        report["HTTP_STATUS"] = source.http_status or getattr(owned, "http_status", None)
        report["MODEL_TOTAL_DURATION_MS"] = source.last_total_ms
        report["MODEL_LOAD_DURATION_MS"] = source.last_load_ms
        report["PROMPT_EVAL_DURATION_MS"] = source.last_prompt_eval_ms
        report["GENERATION_DURATION_MS"] = source.last_generation_ms
        report["PROMPT_TOKENS"] = source.last_prompt_tokens
        report["GENERATED_TOKENS"] = source.last_output_tokens
        report["FAILURE_CLASS"] = failure
        report["COMPLETE_RESPONSE"] = (source.responses == 1 and failure not in {
            "MODEL_GENERATION_TRUNCATED", "MODEL_CONTENT_MISSING",
            "MODEL_ENVELOPE_INVALID", "MODEL_TOOL_ACTIVITY"})
        report["STRUCTURED_ACTION_VALID"] = failure == "NONE"
        report["SAMPLES"] = samples[:MAX_SAMPLES]
        report["RESOURCE_SUMMARY"] = _summary(samples[:MAX_SAMPLES])
        drift = abs((ended_utc - started_utc).total_seconds() * 1000 -
                    report["CLIENT_ELAPSED_MS"])
        report["CLOCK_DISCONTINUITY"] = drift > 2000
    # Give a cancellation-induced service log a bounded chance to land.
    await asyncio.sleep(JOURNAL_SETTLE_SECONDS)
    first = started_utc - timedelta(seconds=1)
    last = ended_utc + timedelta(seconds=2)
    if timedelta(0) <= last - first <= timedelta(minutes=3):
        report["PROBE_JOURNAL"] = runner.journal_evidence(
            first.astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            last.astimezone().strftime("%Y-%m-%d %H:%M:%S"))
    else:
        report["PROBE_JOURNAL"] = {"STATUS": "UNAVAILABLE", "EVENTS": []}
    after = await reliability.local_ollama_metadata(config.ollama_base_url, model)
    report["MODEL_RESIDENCY_AFTER"] = after.get("MODEL_RESIDENCY", "UNKNOWN")
    report["CLASSIFICATIONS"] = classify_probe(report)
    report["VERDICT"] = report["CLASSIFICATIONS"][0]
    return report


async def evaluate(expected_sha: str) -> dict[str, object]:
    """Verify pushed checkout, cold resources and foreground consent first."""
    logger.remove()
    report, config = await reliability._preflight(
        BRANCH, checkout_verifier=lambda: verify_pinned_checkout(expected_sha))
    service = runner.service_evidence()
    report.update(service)
    report.update(runner.gpu_evidence())
    report["EXPERIMENTAL_REQUEST_DEADLINE_SECONDS"] = DIAGNOSTIC_REQUEST_SECONDS
    report["EXPERIMENTAL_OUTER_DEADLINE_SECONDS"] = DIAGNOSTIC_OUTER_SECONDS
    report["HTTP_PHASE_TIMEOUT_SECONDS"] = HTTP_PHASE_TIMEOUT_SECONDS
    report["SHELL_TIMEOUT_SECONDS"] = SHELL_TIMEOUT_SECONDS
    report["SAMPLE_INTERVAL_SECONDS"] = SAMPLE_INTERVAL_SECONDS
    report["MAX_SAMPLES"] = MAX_SAMPLES
    report["NORMAL_COLD_DEADLINE_UNCHANGED_SECONDS"] = NORMAL_COLD_SECONDS
    report["PRODUCTION_QUALIFIED"] = False
    report["PINNED_LOCAL_CHECKOUT_VERIFIED"] = report.get("FAILURE_CLASS") != "PREFLIGHT_CHECKOUT"
    refusal = _preflight_sufficient(report, config, service)
    if refusal:
        report["VERDICT"] = "PROTOCOL_OR_PREFLIGHT_BLOCKED"
        report["FAILURE_CLASS"] = refusal
        report["CLASSIFICATIONS"] = ["PROTOCOL_OR_PREFLIGHT_BLOCKED"]
        return report
    try:
        with open_operator_terminal() as terminal:
            if not _consent(terminal, "MODEL ONCE EXTENDED"):
                report["VERDICT"] = "PROTOCOL_OR_PREFLIGHT_BLOCKED"
                report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
                report["CLASSIFICATIONS"] = ["PROTOCOL_OR_PREFLIGHT_BLOCKED"]
                return report
    except ReviewUnavailable:
        report["VERDICT"] = "PROTOCOL_OR_PREFLIGHT_BLOCKED"
        report["FAILURE_CLASS"] = "LOCAL_CONSENT_REQUIRED"
        report["CLASSIFICATIONS"] = ["PROTOCOL_OR_PREFLIGHT_BLOCKED"]
        return report
    return await run_one_request(report, config)


def main() -> None:
    """Emit only fixed statuses and bounded observations; never private text."""
    if (len(sys.argv) != 3 or sys.argv[1] != "--probe" or
            re.fullmatch(r"[0-9a-f]{40}", sys.argv[2]) is None):
        report: dict[str, Any] = {"VERDICT": "INVALID_INVOCATION",
                                  "PRODUCTION_QUALIFIED": False}
    else:
        try:
            report = asyncio.run(evaluate(sys.argv[2]))
        except KeyboardInterrupt:
            report = {"VERDICT": "PROTOCOL_OR_PREFLIGHT_BLOCKED",
                      "FAILURE_CLASS": "OPERATOR_OR_SHELL_INTERRUPTED",
                      "PRODUCTION_QUALIFIED": False}
        except Exception:
            report = {"VERDICT": "PROTOCOL_OR_PREFLIGHT_BLOCKED",
                      "FAILURE_CLASS": "PROBE_UNEXPECTED_FAILURE",
                      "PRODUCTION_QUALIFIED": False}
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
