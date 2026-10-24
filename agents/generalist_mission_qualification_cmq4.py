"""Opt-in CMQ.4 paired decision and complete synthetic mission campaign.

Preparation is non-generative. The run path requires exact checkout, model,
resource, browser and foreground-consent gates. No production authority exists.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from types import SimpleNamespace

import httpx
from loguru import logger
from pydantic import ValidationError

from agents import generalist_browser_operator as operator
from agents import generalist_compact_model_qualification as compact
from agents import generalist_ollama_cold_load_r2 as r2
from agents.generalist_browser_decision_diagnostic import summarize_request
from agents.generalist_browser_lab import (
    BrowserActionProposal, BrowserRefusal, BrowserRunProgress,
    OllamaBrowserActionSource, SyntheticTracePolicy, _unique_pairs,
    run_isolated_fixture, run_isolated_policy,
)
from agents.generalist_ollama_plan_probe import _local_model_config
from agents.generalist_ollama_resources import _bounded_metadata_get, _loopback_base
from core.llm_client import OllamaLLMClient


BRANCH = "morgoth-generalist-mission-qualification-cmq4-consent-recovery-v1"
RECOVERY_SOURCE_SHA = "6317968a2a7fe3baf69461d74b730a18d9833631"
RECOVERY_LEDGER_SHA256 = "91f42ba231f1393e33169e19af085e0350ee75be61bd026918bdf0e2a0aba973"
QWEN = "qwen3:4b-instruct"
QWEN_DIGEST = "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0"
MAX_REQUESTS = 38
MAX_PER_MODEL = 19
LEDGER_DIR = Path("/tmp")


@dataclass(frozen=True)
class Scenario:
    """Trusted task input and independent expected rendered result."""

    fixture: str
    objective: str
    target: str
    expected: str


SCENARIOS = (
    Scenario("heldout", "Check inventory stock item SKU-44 and report its quantity",
             "SKU-44", "12 units"),
    Scenario("catalog", "Find the title of public catalog work BK-17",
             "BK-17", "The Atlas of Rivers"),
    Scenario("relayout", "Find the stock quantity for item SKU-44 in warehouse records",
             "SKU-44", "12 units"),
)


@dataclass(frozen=True)
class Candidate:
    """Installed identity fixed before the campaign's first request."""

    name: str
    digest: str
    quantization: str
    size_mib: int


@dataclass(frozen=True)
class FrozenRequest:
    """In-memory exact source-built request and reviewed rendered-state proof."""

    messages: tuple[object, object]
    kwargs: dict[str, object]
    digest: str
    source_digest: str
    summary: dict[str, object]


def _safe_code(value: object) -> str:
    """Return a fixed diagnostic code, never private exception text."""
    return value if type(value) is str and re.fullmatch(r"[A-Z_]{3,48}", value) else "UNKNOWN_FAILURE"


def _cells() -> dict[str, dict[str, object]]:
    """Declare every scheduled cell before any request can be sent."""
    return {f"{role}:{fixture}": {"STATUS": "NOT_RUN", "REASON": "NOT_STARTED",
                                 "REQUESTS": 0}
            for role in ("QWEN", "COMPETITOR")
            for fixture in ("choice", "heldout", "catalog", "relayout")}


def _ledger_path(expected_sha: str) -> Path:
    """Use one fixed local attempt record per reviewed source revision."""
    if re.fullmatch(r"[0-9a-f]{40}", expected_sha) is None:
        raise BrowserRefusal("PREFLIGHT_CHECKOUT")
    return LEDGER_DIR / f"morgoth-cmq4-{expected_sha}.json"


def _recovery_claim_path() -> Path:
    """One durable ownership name for this original attempt across successor SHAs."""
    return LEDGER_DIR / f"morgoth-cmq4-recovery-{RECOVERY_SOURCE_SHA}-{RECOVERY_LEDGER_SHA256}.claim"


def _inspect_original_ledger() -> dict[str, object]:
    """Accept only the exact complete original pre-inference consent refusal."""
    path = _ledger_path(RECOVERY_SOURCE_SHA)
    try:
        before = os.lstat(path)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
                stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1 or
                not 0 < before.st_size <= 16000):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError
            raw = os.read(descriptor, 16001)
            if len(raw) != before.st_size:
                raise ValueError
        finally:
            os.close(descriptor)
        digest = sha256(raw).hexdigest()
        if digest != RECOVERY_LEDGER_SHA256:
            raise ValueError
        original = json.loads(raw, object_pairs_hook=_unique_pairs)
        required = {"CANDIDATES", "CELLS", "FROZEN_BUDGET_CALLS",
                    "FROZEN_BUDGET_SECONDS", "INSTALLED_ALTERNATIVE_COUNT",
                    "MAX_REQUESTS", "MAX_REQUESTS_PER_MODEL", "MODEL_REQUESTS",
                    "PAIRED_INPUT_SHA256", "PRODUCTION_QUALIFIED",
                    "REAL_EXPERIMENT_EXECUTED", "RENDERED_POST_FILL_VERIFIED",
                    "REQUESTS_DISPATCHED", "REQUESTS_RESERVED_MAX",
                    "REVIEWED_FIXTURE_SHA256", "VERDICT"}
        if type(original) is not dict or set(original) != required:
            raise ValueError
        if (original["VERDICT"] != "CAMPAIGN_RECORDED_WITH_BLOCKED_CELLS" or
                original["REAL_EXPERIMENT_EXECUTED"] is not False or
                original["PRODUCTION_QUALIFIED"] is not False or
                original["RENDERED_POST_FILL_VERIFIED"] is not True or
                type(original["REQUESTS_DISPATCHED"]) is not int or
                original["REQUESTS_DISPATCHED"] != 0 or
                type(original["REQUESTS_RESERVED_MAX"]) is not int or
                original["REQUESTS_RESERVED_MAX"] != 0 or
                original["MODEL_REQUESTS"] != {"QWEN": 0, "COMPETITOR": 0} or
                any(type(value) is not int or value != 0
                    for value in original["MODEL_REQUESTS"].values()) or
                type(original["MAX_REQUESTS"]) is not int or
                original["MAX_REQUESTS"] != MAX_REQUESTS or
                type(original["MAX_REQUESTS_PER_MODEL"]) is not int or
                original["MAX_REQUESTS_PER_MODEL"] != MAX_PER_MODEL or
                type(original["FROZEN_BUDGET_CALLS"]) is not int or
                original["FROZEN_BUDGET_CALLS"] != 4 or
                type(original["FROZEN_BUDGET_SECONDS"]) is not int or
                not 0 <= original["FROZEN_BUDGET_SECONDS"] <= 260 or
                type(original["INSTALLED_ALTERNATIVE_COUNT"]) is not int or
                original["INSTALLED_ALTERNATIVE_COUNT"] < 1):
            raise ValueError
        for field in ("PAIRED_INPUT_SHA256", "REVIEWED_FIXTURE_SHA256"):
            if (type(original[field]) is not str or
                    re.fullmatch(r"[0-9a-f]{64}", original[field]) is None):
                raise ValueError
        candidates = original["CANDIDATES"]
        if (type(candidates) is not list or len(candidates) != 2 or
                any(type(item) is not dict or
                    set(item) != {"name", "digest", "quantization", "size_mib"} or
                    type(item["name"]) is not str or
                    compact.MODEL_NAME.fullmatch(item["name"]) is None or
                    type(item["digest"]) is not str or
                    re.fullmatch(r"[0-9a-f]{64}", item["digest"]) is None or
                    type(item["quantization"]) is not str or
                    type(item["size_mib"]) is not int or item["size_mib"] <= 0
                    for item in candidates) or
                candidates[0]["name"] != QWEN or candidates[0]["digest"] != QWEN_DIGEST or
                candidates[1]["name"] == QWEN):
            raise ValueError
        cells = original["CELLS"]
        if type(cells) is not dict or set(cells) != set(_cells()):
            raise ValueError
        for key, cell in cells.items():
            if (type(cell) is not dict or set(cell) != {"STATUS", "REASON", "REQUESTS"} or
                    cell["STATUS"] != "BLOCKED" or
                    cell["REASON"] != ("MODEL_CONSENT_REQUIRED" if key == "QWEN:choice"
                                       else "PREREQUISITE_OR_PRIOR_CELL_BLOCKED") or
                    type(cell["REQUESTS"]) is not int or cell["REQUESTS"] != 0):
                raise ValueError
        return {"RECOVERY_SOURCE_SHA": RECOVERY_SOURCE_SHA,
                "RECOVERY_ORIGINAL_LEDGER_SHA256": digest,
                "RECOVERY_ORIGINAL_INPUT_SHA256": original["PAIRED_INPUT_SHA256"],
                "RECOVERY_CANDIDATES": candidates}
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, BrowserRefusal):
        raise BrowserRefusal("RECOVERY_ORIGINAL_EVIDENCE_INVALID") from None


def _recovery_available(expected_sha: str) -> dict[str, object]:
    """Refuse an existing claim or successor ledger even after a new commit."""
    evidence = _inspect_original_ledger()
    if (os.path.lexists(_recovery_claim_path()) or
            os.path.lexists(_ledger_path(expected_sha))):
        raise BrowserRefusal("RECOVERY_ALREADY_CLAIMED")
    return evidence


def _claim_recovery(evidence: dict[str, object], expected_sha: str) -> None:
    """Durably claim the original attempt after consent and before dispatch."""
    claim = _recovery_claim_path()
    payload = json.dumps({"SOURCE_SHA": evidence["RECOVERY_SOURCE_SHA"],
                          "SOURCE_LEDGER_SHA256": evidence["RECOVERY_ORIGINAL_LEDGER_SHA256"],
                          "SUCCESSOR_SHA": expected_sha}, sort_keys=True).encode()
    descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(LEDGER_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _record_preconsent_refusal(evidence: dict[str, object], expected_sha: str) -> None:
    """Keep bounded cancellation evidence without reserving a model request."""
    path = LEDGER_DIR / (f"morgoth-cmq4-consent-refusal-{RECOVERY_SOURCE_SHA}-"
                         f"{expected_sha}-{secrets.token_hex(12)}.json")
    data = json.dumps({"REASON": "MODEL_CONSENT_REQUIRED",
                       "SOURCE_SHA": evidence["RECOVERY_SOURCE_SHA"],
                       "SOURCE_LEDGER_SHA256": evidence["RECOVERY_ORIGINAL_LEDGER_SHA256"],
                       "SUCCESSOR_SHA": expected_sha,
                       "REQUESTS_DISPATCHED": 0, "REQUESTS_RESERVED_MAX": 0},
                      sort_keys=True, separators=(",", ":")).encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(descriptor, data) != len(data):
            raise OSError
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(LEDGER_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_ledger(path: Path, report: dict[str, object], *, first: bool = False) -> None:
    """Persist only safe bounded attempt accounting; refuse an existing run."""
    data = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    if len(data) > 16000:
        raise BrowserRefusal("CAMPAIGN_REPORT_OVERSIZE")
    if first:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    temp = path.with_name(path.name + ".tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, path)


async def _installed_candidates(base_url: str) -> tuple[Candidate, Candidate | None, int]:
    """Select the smallest reviewed llama-family Q4_K_M alternative."""
    if not _loopback_base(base_url):
        raise BrowserRefusal("LOCAL_MODEL_CONFIG_REJECTED")
    async with httpx.AsyncClient(base_url=base_url, trust_env=False,
                                 follow_redirects=False,
                                 timeout=httpx.Timeout(5, connect=2)) as client:
        payload, _ = await asyncio.wait_for(_bounded_metadata_get(client, "/api/tags"), 6)
    if (type(payload) is not dict or type(payload.get("models")) is not list or
            len(payload["models"]) > 32):
        raise BrowserRefusal("MODEL_CATALOG_INVALID")
    qwen: Candidate | None = None
    alternatives: list[Candidate] = []
    for item in payload["models"]:
        if type(item) is not dict:
            raise BrowserRefusal("MODEL_CATALOG_INVALID")
        name, digest, size = item.get("name"), item.get("digest"), item.get("size")
        details = item.get("details")
        quant = details.get("quantization_level") if type(details) is dict else None
        family = details.get("family") if type(details) is dict else None
        if (type(name) is not str or compact.MODEL_NAME.fullmatch(name) is None or
                type(digest) is not str or re.fullmatch(r"[0-9a-f]{64}", digest) is None or
                type(quant) is not str or re.fullmatch(r"[A-Za-z0-9_]{1,24}", quant) is None or
                type(size) is not int or size <= 0):
            continue
        candidate = Candidate(name, digest, quant, (size + 1048575) // 1048576)
        if name == QWEN:
            qwen = candidate
        elif family == "llama" and quant == "Q4_K_M":
            alternatives.append(candidate)
    if qwen is None or qwen.digest != QWEN_DIGEST or qwen.quantization != "Q4_K_M":
        raise BrowserRefusal("QWEN_IDENTITY_UNVERIFIED")
    alternatives.sort(key=lambda item: (item.size_mib, item.name))
    return qwen, alternatives[0] if alternatives else None, len(alternatives)


class _PreparationClient:
    """Script two setup actions, then capture the actual source-built request."""

    def __init__(self) -> None:
        self.calls = 0
        self.snapshot: tuple[tuple[object, object], dict[str, object]] | None = None

    async def chat(self, messages: list[object], **kwargs: object) -> SimpleNamespace:
        """Respond synthetically; this method never accesses Ollama."""
        payload = json.loads(messages[1].content)
        wanted = ("Inventory", "SKU", "Stop or abstain")[self.calls]
        if self.calls == 2:
            self.snapshot = ((messages[0], messages[1]), kwargs.copy())
        self.calls += 1
        choice = next(item for item in payload["observation"]["actions"]
                      if item["name"] == wanted)
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "revision": payload["observation"]["revision"], "choice_id": choice["id"]}),
            tool_calls=[]), done=True, done_reason="stop")


async def _frozen_post_fill() -> FrozenRequest:
    """Capture one actual model request after rendered, worker-acknowledged setup."""
    policy = SyntheticTracePolicy.from_checkout(operator.ROOT, "heldout")
    client = _PreparationClient()
    source = OllamaBrowserActionSource(client, "scripted-preparation", loaded=None)
    progress = BrowserRunProgress()
    try:
        await run_isolated_policy(
            checkout=operator.ROOT, browser_dir=operator.BROWSER,
            fixture="heldout", objective=SCENARIOS[0].objective,
            target=SCENARIOS[0].target, expected=SCENARIOS[0].expected,
            source=source, max_model_calls=6, progress=progress, synthetic_trace=True)
    except BrowserRefusal as exc:
        if exc.code != "MODEL_ABSTAINED":
            raise
    if (client.snapshot is None or progress.action_count != 2 or
            progress.state_transition_count != 2 or progress.no_progress_count != 0 or
            progress.task_acceptance_passed or not progress.trace_source_verified or
            len(progress.synthetic_trace) != 2 or
            [item.get("CONTROL") for item in progress.synthetic_trace] != ["C02", "C03"] or
            any(item.get("UI_EFFECT") != "UI_CHANGE_OBSERVED"
                for item in progress.synthetic_trace)):
        raise BrowserRefusal("CHOICE_PROVENANCE_INVALID")
    messages, kwargs = client.snapshot
    if (kwargs.get("model") != "scripted-preparation" or
            kwargs.get("tools", "sentinel") is not None or
            kwargs.get("stream") is not False or
            kwargs.get("options") != {"temperature": 0, "num_predict": 384}):
        raise BrowserRefusal("CHOICE_REQUEST_INVALID")
    payload = json.loads(messages[1].content)
    summary = summarize_request(payload, kwargs["format"], operator.ROOT, "heldout")
    if (summary["REMAINING_MODEL_CALLS"] != 4 or
            summary["TARGET_IN_VISIBLE_INPUT"] is not True or
            summary["CHOICE_SCHEMA_MATCHES_CATALOG"] is not True or
            not any(item["CONTROL"] == "C04" and item["KIND"] == "CLICK"
                    for item in summary["CANDIDATES"])):
        raise BrowserRefusal("CHOICE_PROVENANCE_INVALID")
    request = {"messages": [item.model_dump(mode="json") for item in messages],
               "tools": None, "stream": False, "options": kwargs["options"],
               "format": kwargs["format"]}
    digest = sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return FrozenRequest(messages, {key: value for key, value in kwargs.items()
                                    if key != "model"}, digest, policy.digest, summary)


def _decode_choice(response: object, frozen: FrozenRequest) -> str:
    """Apply the existing strict choice envelope, JSON and schema checks."""
    try:
        message = response.message
        content = message.content
        if message.tool_calls:
            raise BrowserRefusal("MODEL_TOOL_ACTIVITY")
        if response.done is not True or getattr(response, "done_reason", None) == "length":
            raise BrowserRefusal("MODEL_GENERATION_TRUNCATED")
        if getattr(response, "done_reason", None) not in {None, "stop"}:
            raise BrowserRefusal("MODEL_ENVELOPE_INVALID")
        if content is None or content == "":
            raise BrowserRefusal("MODEL_CONTENT_MISSING")
        if type(content) is not str or len(content.encode()) > 2048:
            raise BrowserRefusal("MODEL_ENVELOPE_INVALID")
        value = json.loads(content, object_pairs_hook=_unique_pairs)
        proposal = BrowserActionProposal.model_validate(value)
        schema = frozen.kwargs["format"]
        if (proposal.revision not in schema["properties"]["revision"]["enum"] or
                proposal.choice_id not in schema["properties"]["choice_id"]["enum"]):
            raise BrowserRefusal("MODEL_CHOICE_INVALID")
        return proposal.choice_id
    except BrowserRefusal:
        raise
    except json.JSONDecodeError:
        raise BrowserRefusal("MODEL_INVALID_JSON") from None
    except ValidationError:
        raise BrowserRefusal("MODEL_ACTION_SCHEMA_INVALID") from None
    except (TypeError, ValueError, KeyError, AttributeError, UnicodeError):
        raise BrowserRefusal("MODEL_ENVELOPE_INVALID") from None


async def _choice_probe(candidate: Candidate, frozen: FrozenRequest,
                        preflight: dict[str, object], config: object) -> dict[str, object]:
    """Send exactly one frozen source request and collect actual resource evidence."""
    report: dict[str, object] = {
        "INFERENCE_ATTEMPTED": True, "MODEL_CALL_COUNT": 1,
        "MODEL_RESPONSE_COUNT": 0, "COMPLETE_RESPONSE": False,
        "STRUCTURED_ACTION_VALID": False, "FAILURE_CLASS": "UNKNOWN_FAILURE",
        "INPUT_SHA256": frozen.digest, "FIXTURE_SHA256": frozen.source_digest,
        "MODEL_RESIDENCY_BEFORE": preflight.get("MODEL_RESIDENCY"),
        "PRODUCTION_QUALIFIED": False,
    }
    timeout = 45 if preflight.get("MODEL_RESIDENCY") == "LOADED" else 60
    client = OllamaLLMClient(config, timeout=90)
    start = time.monotonic()
    stop = asyncio.Event()
    sampler = asyncio.create_task(r2.sample_during_request(
        start, config.ollama_base_url, candidate.name, stop))
    choice_id: str | None = None
    try:
        try:
            response = await asyncio.wait_for(client.chat(
                list(frozen.messages), model=candidate.name, **frozen.kwargs), timeout)
            report["MODEL_RESPONSE_COUNT"] = 1
            report["COMPLETE_RESPONSE"] = bool(response.done is True and
                getattr(response, "done_reason", None) in {None, "stop"} and
                type(response.message.content) is str and response.message.content != "")
            for key, attribute in (("MODEL_LOAD_DURATION_MS", "load_duration"),
                                   ("MODEL_TOTAL_DURATION_MS", "total_duration"),
                                   ("PROMPT_EVAL_DURATION_MS", "prompt_eval_duration"),
                                   ("GENERATION_DURATION_MS", "eval_duration")):
                value = getattr(response, attribute, None)
                report[key] = min(200000, value // 1000000) if type(value) is int and value >= 0 else None
            for key, attribute in (("PROMPT_TOKENS", "prompt_eval_count"),
                                   ("GENERATED_TOKENS", "eval_count")):
                value = getattr(response, attribute, None)
                report[key] = min(100000, value) if type(value) is int and value >= 0 else None
            choice_id = _decode_choice(response, frozen)
            report["STRUCTURED_ACTION_VALID"] = True
            report["FAILURE_CLASS"] = "NONE"
        except BrowserRefusal as exc:
            report["FAILURE_CLASS"] = exc.code
        except (TimeoutError, httpx.TimeoutException):
            report["FAILURE_CLASS"] = "MODEL_TIMEOUT"
        except httpx.ConnectError:
            report["FAILURE_CLASS"] = "MODEL_CONNECTION_FAILED"
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            report["HTTP_STATUS"] = status if 100 <= status <= 599 else None
            try:
                body = exc.response.json()
            except (ValueError, UnicodeError):
                body = None
            if status in {400, 422} and type(body) is dict and body.get("code") == "unsupported_format":
                report["FAILURE_CLASS"] = "MODEL_FORMAT_UNSUPPORTED"
            elif status in {500, 503} and type(body) is dict and body.get("code") == "resource_exhausted":
                report["FAILURE_CLASS"] = "MODEL_RESOURCE_EXHAUSTED"
            elif status == 503:
                report["FAILURE_CLASS"] = "MODEL_SERVICE_UNAVAILABLE"
            else:
                report["FAILURE_CLASS"] = ("MODEL_HTTP_4XX" if 400 <= status < 500 else
                                           "MODEL_HTTP_5XX" if 500 <= status < 600 else
                                           "MODEL_HTTP_UNEXPECTED")
        except httpx.HTTPError:
            report["FAILURE_CLASS"] = "MODEL_TRANSPORT_FAILED"
        except Exception:
            report["FAILURE_CLASS"] = "MODEL_ENVELOPE_INVALID"
    finally:
        report["CLIENT_ELAPSED_MS"] = min(200000, max(0, round((time.monotonic() - start) * 1000)))
        stop.set()
        try:
            samples = await asyncio.wait_for(sampler, 6)
        except Exception:
            sampler.cancel()
            try:
                await sampler
            except (asyncio.CancelledError, Exception):
                pass
            samples = []
        await client.close()
    summary = r2._summary(samples[:r2.MAX_SAMPLES])
    report["RESOURCE_SUMMARY"] = summary
    assessment, warning = compact._resource_assessment(summary)
    report["RESOURCE_ASSESSMENT"] = assessment
    report["RESOURCE_PRESSURE_WARNING"] = warning
    report["CLASSIFICATIONS"] = (["COLD_LOAD_COMPLETED_WITHIN_NORMAL_DEADLINE"]
        if report["FAILURE_CLASS"] == "NONE" and report["CLIENT_ELAPSED_MS"] <= 60000
        else []) + (["RESOURCE_PRESSURE_OBSERVED"] if warning else [])
    qualified, _, status = compact._stage_a_assessment(report)
    report["TECHNICALLY_QUALIFIED"] = qualified
    report["QUALIFICATION_CLASS"] = status
    if choice_id is not None:
        payload = json.loads(frozen.messages[1].content)
        selected = next(item for item in payload["observation"]["actions"]
                        if item["id"] == choice_id)
        code = ("STOP" if selected["kind"] == "stop" else
                SyntheticTracePolicy.from_checkout(operator.ROOT, "heldout").controls[selected["name"]])
        report["SYNTHETIC_CHOICE_CODE"] = code
        report["CHOICE_USEFUL_AT_FROZEN_STATE"] = code == "C04"
    return report


async def _browser_preflight() -> None:
    """Validate each reviewed fixture with its own task before any inference."""
    for task in SCENARIOS:
        SyntheticTracePolicy.from_checkout(operator.ROOT, task.fixture)
        result = await run_isolated_fixture(
            checkout=operator.ROOT, browser_dir=operator.BROWSER,
            fixture=task.fixture, objective=task.objective,
            target=task.target, expected=task.expected)
        if result.get("BROWSER_ISOLATED") is not True:
            raise BrowserRefusal("BROWSER_PREFLIGHT_FAILED")


def _mission_outcome(browser: dict[str, object]) -> str:
    """Classify independent acceptance separately from executable actions."""
    if browser.get("TASK_ACCEPTANCE_PASSED") is True:
        return "INDEPENDENTLY_ACCEPTED_MISSION"
    failure = browser.get("FAILURE_CLASS")
    if failure == "BROWSER_LOOP_DETECTED":
        return "LOOP_TERMINATED"
    if failure in {"MODEL_CALL_BUDGET_EXCEEDED", "MODEL_BUDGET_EXHAUSTED"}:
        return "MISSION_BUDGET_EXHAUSTED"
    if failure == "MODEL_ABSTAINED":
        return "SAFE_ABSTENTION"
    stage = browser.get("STAGE_B")
    if (type(stage) is dict and type(stage.get("ACTION_COUNT")) is int and
            stage["ACTION_COUNT"] > 0 and type(stage.get("NO_PROGRESS_COUNT")) is int and
            stage["NO_PROGRESS_COUNT"] > 0):
        return "VALID_BUT_UNPRODUCTIVE_ACTIONS"
    return "TECHNICAL_OR_PREFLIGHT_FAILURE"


async def evaluate(expected_sha: str, *, run: bool = False,
                   recover: bool = False) -> dict[str, object]:
    """Prepare a frozen matrix, then optionally recover one consent-only refusal."""
    logger.remove()
    claim_owned = False
    report: dict[str, object] = {
        "VERDICT": "PREPARATION_BLOCKED", "REAL_EXPERIMENT_EXECUTED": False,
        "REQUESTS_DISPATCHED": 0, "REQUESTS_RESERVED_MAX": 0,
        "MAX_REQUESTS": MAX_REQUESTS,
        "MAX_REQUESTS_PER_MODEL": MAX_PER_MODEL,
        "MODEL_REQUESTS": {"QWEN": 0, "COMPETITOR": 0}, "CELLS": _cells(),
        "PRODUCTION_QUALIFIED": False,
    }
    try:
        compact.verify_checkout(expected_sha, BRANCH)
        if run and not recover:
            raise BrowserRefusal("RECOVERY_REQUIRED")
        recovery_evidence = _recovery_available(expected_sha) if run else None
        if recovery_evidence is not None:
            report.update({key: recovery_evidence[key] for key in (
                "RECOVERY_SOURCE_SHA", "RECOVERY_ORIGINAL_LEDGER_SHA256",
                "RECOVERY_ORIGINAL_INPUT_SHA256")})
        config = _local_model_config()
        qwen, competitor, alternative_count = await _installed_candidates(config.ollama_base_url)
        report["INSTALLED_ALTERNATIVE_COUNT"] = alternative_count
        if competitor is None:
            raise BrowserRefusal("COMPETITOR_NOT_INSTALLED")
        report["CANDIDATES"] = [candidate.__dict__ for candidate in (qwen, competitor)]
        if recovery_evidence is not None and report["CANDIDATES"] != recovery_evidence["RECOVERY_CANDIDATES"]:
            raise BrowserRefusal("RECOVERY_CANDIDATE_IDENTITY_CHANGED")
        await _browser_preflight()
        frozen = await _frozen_post_fill()
        report["PAIRED_INPUT_SHA256"] = frozen.digest
        report["REVIEWED_FIXTURE_SHA256"] = frozen.source_digest
        report["RENDERED_POST_FILL_VERIFIED"] = True
        report["FROZEN_BUDGET_CALLS"] = frozen.summary["REMAINING_MODEL_CALLS"]
        report["FROZEN_BUDGET_SECONDS"] = frozen.summary["REMAINING_SECONDS"]
        if not run:
            report["VERDICT"] = "PREPARATION_READY_REAL_EXPERIMENT_NOT_EXECUTED"
            return report
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(key, None)
        path = _ledger_path(expected_sha)
        for role, candidate in (("QWEN", qwen), ("COMPETITOR", competitor)):
            if role == "COMPETITOR":
                sys.stderr.write("CMQ.4 model transition: review residency in another terminal; "
                                 "any unload is a separate operator action.\n")
            if role == "COMPETITOR" and not compact._consented("CONTINUE MODEL BLOCK"):
                report["CELLS"]["COMPETITOR:choice"] = {
                    "STATUS": "BLOCKED", "REASON": "MODEL_TRANSITION_CONSENT_REQUIRED",
                    "REQUESTS": 0}
                if claim_owned:
                    _write_ledger(path, report)
                break
            key = f"{role}:choice"
            preflight, ready_config = await compact._preflight(
                candidate.name, expected_sha, candidate.quantization, BRANCH)
            if ready_config is None or preflight.get("MODEL_DIGEST_SHA256") != candidate.digest:
                report["CELLS"][key] = {"STATUS": "BLOCKED", "REASON":
                    "MODEL_OR_RESOURCE_PREFLIGHT_BLOCKED", "REQUESTS": 0}
                if claim_owned:
                    _write_ledger(path, report)
                break
            if not compact._consented("MODEL ONCE COMPACT"):
                report["CELLS"][key] = {"STATUS": "BLOCKED", "REASON":
                    "MODEL_CONSENT_REQUIRED", "REQUESTS": 0}
                if claim_owned:
                    _write_ledger(path, report)
                break
            if role == "QWEN":
                compact.verify_checkout(expected_sha, BRANCH)
                if _recovery_available(expected_sha) != recovery_evidence:
                    raise BrowserRefusal("RECOVERY_ORIGINAL_EVIDENCE_CHANGED")
                _claim_recovery(recovery_evidence, expected_sha)
                claim_owned = True
            # Reserve one attempt before dispatch. A crash cannot erase it.
            report["CELLS"][key] = {"STATUS": "ATTEMPTED_RESULT_UNKNOWN",
                                    "REASON": "REQUEST_RESERVED", "REQUESTS": None,
                                    "RESERVED_MAX": 1}
            report["REQUESTS_RESERVED_MAX"] += 1
            _write_ledger(path, report, first=role == "QWEN")
            probe = await _choice_probe(candidate, frozen, preflight, ready_config)
            report["REQUESTS_RESERVED_MAX"] -= 1
            report["REQUESTS_DISPATCHED"] += 1
            report["MODEL_REQUESTS"][role] += 1
            report["CELLS"][key] = {"STATUS": "RECORDED", "REASON":
                _safe_code(probe["FAILURE_CLASS"]), "REQUESTS": 1, "EVIDENCE": probe,
                "OUTCOME": ("VALID_USEFUL_CHOICE" if probe.get("CHOICE_USEFUL_AT_FROZEN_STATE")
                            else "VALID_BUT_UNPRODUCTIVE_CHOICE" if probe.get("TECHNICALLY_QUALIFIED")
                            else "TECHNICAL_OR_STRUCTURED_RESPONSE_BLOCKED")}
            _write_ledger(path, report)
            if not probe["TECHNICALLY_QUALIFIED"]:
                for task in SCENARIOS:
                    report["CELLS"][f"{role}:{task.fixture}"] = {
                        "STATUS": "BLOCKED", "REASON": "TECHNICAL_QUALIFICATION_MISSING",
                        "REQUESTS": 0}
                _write_ledger(path, report)
                continue
            for task in SCENARIOS:
                mission_key = f"{role}:{task.fixture}"
                # The worker may dispatch all six requests. Reserve that ceiling
                # before entering it; an interrupted mission can never be retried.
                if (report["REQUESTS_DISPATCHED"] +
                        report["REQUESTS_RESERVED_MAX"] + 6 > MAX_REQUESTS):
                    raise BrowserRefusal("CAMPAIGN_CALL_BUDGET_EXCEEDED")
                report["CELLS"][mission_key] = {
                    "STATUS": "ATTEMPTED_RESULT_UNKNOWN", "REASON": "SIX_CALL_RESERVATION",
                    "REQUESTS": None, "RESERVED_MAX": 6}
                report["REQUESTS_RESERVED_MAX"] += 6
                _write_ledger(path, report)
                seed = preflight.copy()
                seed["MODEL_DIGEST_SHA256"] = candidate.digest
                browser = await compact._stage_b_after_a(
                    seed, candidate.name, expected_sha, candidate.quantization,
                    bool(probe["RESOURCE_PRESSURE_WARNING"]), branch=BRANCH,
                    hierarchical=False, fixture=task.fixture,
                    objective=task.objective, target=task.target, expected=task.expected)
                stage = browser.get("STAGE_B")
                if browser.get("STAGE_B_CALL_COUNT_UNKNOWN") is True:
                    raise BrowserRefusal("CAMPAIGN_ATTEMPT_COUNT_UNKNOWN")
                actual = stage.get("MODEL_CALL_COUNT") if type(stage) is dict else 0
                if type(actual) is not int or not 0 <= actual <= 6:
                    raise BrowserRefusal("CAMPAIGN_ACCOUNTING_INVALID")
                report["REQUESTS_RESERVED_MAX"] -= 6
                report["REQUESTS_DISPATCHED"] += actual
                report["MODEL_REQUESTS"][role] += actual
                if report["MODEL_REQUESTS"][role] > MAX_PER_MODEL:
                    raise BrowserRefusal("CAMPAIGN_CALL_BUDGET_EXCEEDED")
                report["CELLS"][mission_key] = {
                    "STATUS": "RECORDED" if actual else "BLOCKED",
                    "REASON": _safe_code(browser.get("FAILURE_CLASS")),
                    "REQUESTS": actual,
                    "TASK_ACCEPTANCE_PASSED": browser.get("TASK_ACCEPTANCE_PASSED") is True,
                    "EVIDENCE": stage if type(stage) is dict else None,
                    "OUTCOME": _mission_outcome(browser),
                    "PLANNER_MODE": "DIRECT", "PRODUCTION_QUALIFIED": False}
                _write_ledger(path, report)
                if actual == 0:
                    break
        report["REAL_EXPERIMENT_EXECUTED"] = report["REQUESTS_DISPATCHED"] > 0
        for cell in report["CELLS"].values():
            if cell["STATUS"] == "NOT_RUN":
                cell.update(STATUS="BLOCKED", REASON="PREREQUISITE_OR_PRIOR_CELL_BLOCKED")
        report["VERDICT"] = "CAMPAIGN_RECORDED_WITH_BLOCKED_CELLS" if any(
            item["STATUS"] != "RECORDED" for item in report["CELLS"].values()
            ) else "CAMPAIGN_RECORDED"
        if claim_owned:
            _write_ledger(path, report)
        elif report["CELLS"]["QWEN:choice"]["REASON"] == "MODEL_CONSENT_REQUIRED":
            _record_preconsent_refusal(recovery_evidence, expected_sha)
            report["VERDICT"] = "CONSENT_REFUSED_BEFORE_INFERENCE"
            report["RECOVERY_RETRYABLE"] = True
        return report
    except FileExistsError:
        report["FAILURE_CLASS"] = "RECOVERY_ALREADY_CLAIMED"
    except BrowserRefusal as exc:
        report["FAILURE_CLASS"] = exc.code
    except (OSError, ValueError, TypeError, httpx.HTTPError, TimeoutError):
        report["FAILURE_CLASS"] = "PREPARATION_OR_TRANSPORT_FAILED"
    except Exception:
        report["FAILURE_CLASS"] = "CAMPAIGN_UNEXPECTED_FAILURE"
    if run:
        report["VERDICT"] = "CAMPAIGN_BLOCKED"
    if claim_owned:
        report["RECOVERY_CLAIMED"] = True
    report["REAL_EXPERIMENT_EXECUTED"] = (
        None if report["REQUESTS_RESERVED_MAX"] else report["REQUESTS_DISPATCHED"] > 0)
    for cell in report["CELLS"].values():
        if cell["STATUS"] == "NOT_RUN":
            cell.update(STATUS="BLOCKED", REASON="PREPARATION_OR_PRIOR_CELL_BLOCKED")
    return report


def inspect_recovery(expected_sha: str) -> dict[str, object]:
    """Read only the exact original ledger and current exclusive ownership state."""
    try:
        compact.verify_checkout(expected_sha, BRANCH)
        evidence = _recovery_available(expected_sha)
        return {"VERDICT": "RECOVERY_ELIGIBLE", "RECOVERY_SOURCE_SHA": evidence["RECOVERY_SOURCE_SHA"],
                "RECOVERY_ORIGINAL_LEDGER_SHA256": evidence["RECOVERY_ORIGINAL_LEDGER_SHA256"],
                "RECOVERY_ORIGINAL_INPUT_SHA256": evidence["RECOVERY_ORIGINAL_INPUT_SHA256"],
                "REAL_EXPERIMENT_EXECUTED": False, "PRODUCTION_QUALIFIED": False}
    except BrowserRefusal as exc:
        return {"VERDICT": "RECOVERY_BLOCKED", "FAILURE_CLASS": exc.code,
                "PRODUCTION_QUALIFIED": False}
    except (OSError, ValueError, TypeError):
        return {"VERDICT": "RECOVERY_BLOCKED", "FAILURE_CLASS": "RECOVERY_INSPECTION_FAILED",
                "PRODUCTION_QUALIFIED": False}


def main() -> None:
    """Print only bounded metadata; default/help never open a model transport."""
    parser = argparse.ArgumentParser(description="CMQ.4 synthetic local-model campaign")
    parser.add_argument("--expected-sha", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--inspect-recovery", action="store_true")
    mode.add_argument("--recover-consent-only", action="store_true")
    arguments = parser.parse_args()
    try:
        report = (inspect_recovery(arguments.expected_sha) if arguments.inspect_recovery else
                  asyncio.run(evaluate(arguments.expected_sha,
                                       run=arguments.run or arguments.recover_consent_only,
                                       recover=arguments.recover_consent_only)))
    except (KeyboardInterrupt, Exception):
        report = {"VERDICT": "CAMPAIGN_INTERRUPTED_OR_FAILED",
                  "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
