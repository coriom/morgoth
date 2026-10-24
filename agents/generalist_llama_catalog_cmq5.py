"""One consented Llama catalog choice and one fresh synthetic browser mission.

Readiness is non-generative. This protocol is distinct from consumed CMQ.4 and
has one durable, non-resettable campaign identity independent of source SHA.
"""

from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time

import httpx
from loguru import logger

from agents import generalist_browser_operator as operator
from agents import generalist_compact_model_qualification as compact
from agents import generalist_mission_qualification_cmq4 as cmq4
from agents import generalist_ollama_cold_load_r2 as r2
from agents.generalist_browser_lab import BrowserRefusal, SyntheticTracePolicy, _unique_pairs, run_isolated_fixture
from agents.generalist_ollama_resources import _loopback_base


BRANCH = "morgoth-generalist-llama-cmq5-v1"
PROTOCOL = "CMQ5_LLAMA_CATALOG_V1"
MODEL = "llama3.1:8b"
MODEL_DIGEST = "46e0c10c039e019119339687c3c1757cc81b9da49709a3b3924863ba87ca666e"
QUANTIZATION = "Q4_K_M"
FIXTURE = "catalog"
FIXTURE_DIGEST = "8c1512fdc1ce80102b346b66848d2acb30fdb255af2b05a9e41ad491bcad06f9"
TASK = cmq4.SCENARIOS[1]
STATE_DIR = Path("/home/corio/.local/state/morgoth/experiments/cmq5-llama-catalog")
PRELOAD_DEADLINE_SECONDS = 120
RUN_DEADLINE_SECONDS = 540
MAX_SEMANTIC_REQUESTS = 7
MAX_PRELOAD_REQUESTS = 1
KEEP_ALIVE = "10m"
SAFE_PREFLIGHT_FIELDS = ("MODEL_INSTALLED", "MODEL_RESIDENCY", "MODEL_SIZE_MIB",
                         "MODEL_REPORTED_VRAM_MIB", "HOST_MEMORY_AVAILABLE_MIB",
                         "HOST_SWAP_FREE_MIB", "CGROUP_MEMORY_HEADROOM_MIB",
                         "GPU_MEMORY_FREE_MIB", "RESOURCE_PREFLIGHT_READY",
                         "MEMORY_PRESSURE_AVG10_PCT", "MEMORY_PRESSURE_FULL_AVG10_PCT",
                         "CGROUP_MEMORY_PRESSURE_FULL_AVG10_PCT")
SAFE_STAGE_A_FIELDS = ("INFERENCE_ATTEMPTED", "MODEL_CALL_COUNT", "MODEL_RESPONSE_COUNT",
                       "COMPLETE_RESPONSE", "STRUCTURED_ACTION_VALID", "FAILURE_CLASS",
                       "INPUT_SHA256", "FIXTURE_SHA256", "MODEL_RESIDENCY_BEFORE",
                       "CLIENT_ELAPSED_MS", "MODEL_LOAD_DURATION_MS", "MODEL_TOTAL_DURATION_MS",
                       "PROMPT_EVAL_DURATION_MS", "GENERATION_DURATION_MS", "PROMPT_TOKENS",
                       "GENERATED_TOKENS", "RESOURCE_SUMMARY", "RESOURCE_ASSESSMENT",
                       "RESOURCE_PRESSURE_WARNING", "TECHNICALLY_QUALIFIED",
                       "QUALIFICATION_CLASS", "SYNTHETIC_CHOICE_CODE",
                       "CHOICE_USEFUL_AT_FROZEN_STATE", "HTTP_STATUS")


def _safe_code(value: object) -> str:
    """Reduce untrusted failures to one bounded public classification."""
    return value if type(value) is str and re.fullmatch(r"[A-Z0-9_]{3,48}", value) else "UNKNOWN_FAILURE"


def _state_path(name: str) -> Path:
    """Return a fixed file in the dedicated experiment directory."""
    return STATE_DIR / name


def _secure_directory(*, create: bool) -> bool:
    """Validate the fixed state path and reject symlinks and shared state."""
    path = Path(STATE_DIR)
    for component in reversed((path, *path.parents)):
        if component == Path("/"):
            continue
        try:
            info = os.lstat(component)
        except FileNotFoundError:
            if not create:
                return False
            os.mkdir(component, 0o700)
            info = os.lstat(component)
        if not stat.S_ISDIR(info.st_mode):
            raise BrowserRefusal("STATE_DIRECTORY_UNTRUSTED")
        if component == path and (info.st_uid != os.getuid() or
                                  stat.S_IMODE(info.st_mode) != 0o700):
            raise BrowserRefusal("STATE_DIRECTORY_UNTRUSTED")
        if component != path and component.is_relative_to(Path.home()) and (
                info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise BrowserRefusal("STATE_DIRECTORY_UNTRUSTED")
    return True


def _state_available() -> bool:
    """Never treat a missing claim but present ledger as a fresh experiment."""
    if not _secure_directory(create=False):
        return True
    return not any(os.path.lexists(_state_path(name)) for name in (
        "campaign.claim", "attempt.json", "attempt.json.tmp"))


def _write_exclusive(path: Path, payload: bytes) -> None:
    """Create one mode-0600 evidence file and sync its directory entry."""
    if len(payload) > 16000:
        raise BrowserRefusal("STATE_RECORD_OVERSIZE")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        if os.write(descriptor, payload) != len(payload):
            raise OSError
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _write_attempt(report: dict[str, object], *, first: bool = False) -> None:
    """Persist bounded accounting atomically after exclusive claim creation."""
    payload = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    path = _state_path("attempt.json")
    if first:
        _write_exclusive(path, payload)
        return
    if len(payload) > 16000:
        raise BrowserRefusal("STATE_RECORD_OVERSIZE")
    before = os.lstat(path)
    if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or
            stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
        raise BrowserRefusal("STATE_RECORD_UNTRUSTED")
    temp = _state_path("attempt.json.tmp")
    _write_exclusive(temp, payload)
    os.replace(temp, path)
    directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _cancel_before_attempt(expected_sha: str, reason: str) -> None:
    """Preserve a refusal without creating a campaign claim or reservation."""
    if reason not in {"LOAD_CONSENT_REQUIRED", "MODEL_CONSENT_REQUIRED"}:
        raise BrowserRefusal("CANCELLATION_REASON_INVALID")
    _secure_directory(create=True)
    payload = json.dumps({"PROTOCOL": PROTOCOL, "SOURCE_SHA": expected_sha,
                          "MODEL_DIGEST": MODEL_DIGEST, "REASON": reason,
                          "PRELOAD_REQUESTS": 0, "SEMANTIC_REQUESTS": 0},
                         sort_keys=True, separators=(",", ":")).encode()
    _write_exclusive(_state_path(f"consent-refusal-{secrets.token_hex(12)}.json"), payload)


def _claim(expected_sha: str) -> None:
    """Claim this protocol once, regardless of later commits or restarts."""
    _secure_directory(create=True)
    payload = json.dumps({"PROTOCOL": PROTOCOL, "SOURCE_SHA": expected_sha,
                          "MODEL": MODEL, "MODEL_DIGEST": MODEL_DIGEST,
                          "FIXTURE_SHA256": FIXTURE_DIGEST},
                         sort_keys=True, separators=(",", ":")).encode()
    _write_exclusive(_state_path("campaign.claim"), payload)


def _public_preflight(report: dict[str, object]) -> dict[str, object]:
    """Copy only fixed non-sensitive resource fields."""
    return {key: report.get(key) for key in SAFE_PREFLIGHT_FIELDS}


async def readiness(expected_sha: str) -> tuple[dict[str, object], object | None]:
    """Check Git, exact model metadata, resources and reviewed browser without generation."""
    report: dict[str, object] = {"VERDICT": "READINESS_BLOCKED", "FAILURE_CLASS": "UNKNOWN_FAILURE",
                                 "PROTOCOL": PROTOCOL, "SOURCE_SHA": expected_sha,
                                 "MODEL": MODEL, "MODEL_DIGEST": MODEL_DIGEST,
                                 "FIXTURE_SHA256": FIXTURE_DIGEST,
                                 "INFERENCE_ATTEMPTED": False, "PRELOAD_ATTEMPTED": False,
                                 "PRODUCTION_QUALIFIED": False}
    try:
        compact.verify_checkout(expected_sha, BRANCH)
        if not _state_available():
            raise BrowserRefusal("CAMPAIGN_ALREADY_CONSUMED")
        policy = SyntheticTracePolicy.from_checkout(operator.ROOT, FIXTURE)
        if policy.digest != FIXTURE_DIGEST:
            raise BrowserRefusal("FIXTURE_DIGEST_MISMATCH")
        preflight, config = await compact._preflight(MODEL, expected_sha, QUANTIZATION, BRANCH)
        report["PREFLIGHT"] = _public_preflight(preflight)
        if config is None or preflight.get("MODEL_DIGEST_SHA256") != MODEL_DIGEST:
            report["FAILURE_CLASS"] = _safe_code(preflight.get("FAILURE_CLASS"))
            if report["FAILURE_CLASS"] == "NONE":
                report["FAILURE_CLASS"] = "MODEL_IDENTITY_MISMATCH"
            return report, None
        if (type(preflight.get("MODEL_SIZE_MIB")) is not int or
                preflight["MODEL_SIZE_MIB"] <= 0 or
                preflight.get("MODEL_RESIDENCY") not in {"LOADED", "NOT_LOADED"}):
            raise BrowserRefusal("MODEL_RESOURCE_EVIDENCE_INSUFFICIENT")
        browser = await run_isolated_fixture(checkout=operator.ROOT, browser_dir=operator.BROWSER,
                                             fixture=FIXTURE, objective=TASK.objective,
                                             target=TASK.target, expected=TASK.expected)
        if browser.get("BROWSER_ISOLATED") is not True or browser.get("TASK_ACCEPTANCE_PASSED") is not True:
            raise BrowserRefusal("BROWSER_PREFLIGHT_FAILED")
        report.update(VERDICT="READINESS_READY", FAILURE_CLASS="NONE",
                      BROWSER_ISOLATED=True, REVIEWED_FIXTURE_VERIFIED=True)
        return report, config
    except BrowserRefusal as exc:
        report["FAILURE_CLASS"] = exc.code
        if isinstance(exc, operator.GitCheckoutRefusal):
            report["CHECKOUT_OPERATION"] = exc.operation
            report["CHECKOUT_FAILURE"] = exc.failure
    except (OSError, ValueError, TypeError, httpx.HTTPError, TimeoutError):
        report["FAILURE_CLASS"] = "READINESS_UNAVAILABLE"
    return report, None


async def _preload_request(base_url: str, *, transport: httpx.AsyncBaseTransport | None = None
                           ) -> dict[str, object]:
    """Send one empty-prompt local load request, never a generation prompt."""
    if not _loopback_base(base_url):
        raise BrowserRefusal("LOCAL_MODEL_CONFIG_REJECTED")
    started = time.monotonic()
    result: dict[str, object] = {"REQUEST_ATTEMPTED": True, "RESPONSE_RECEIVED": False,
                                 "COMPLETE_NON_GENERATIVE_LOAD": False,
                                 "FAILURE_CLASS": "PRELOAD_UNAVAILABLE"}
    stop = asyncio.Event()
    sampler = asyncio.create_task(r2.sample_during_request(started, base_url, MODEL, stop))
    try:
        async with httpx.AsyncClient(base_url=base_url, trust_env=False, follow_redirects=False,
                                     transport=transport, timeout=httpx.Timeout(
                                         PRELOAD_DEADLINE_SECONDS, connect=2)) as client:
            async with asyncio.timeout(PRELOAD_DEADLINE_SECONDS + 2):
                async with client.stream("POST", "/api/generate", json={
                        "model": MODEL, "prompt": "", "stream": False,
                        "keep_alive": KEEP_ALIVE}) as response:
                    if response.status_code != 200:
                        result["HTTP_STATUS"] = response.status_code if 100 <= response.status_code <= 599 else None
                        result["FAILURE_CLASS"] = "PRELOAD_HTTP_REJECTED"
                        return result
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4096:
                            raise BrowserRefusal("PRELOAD_RESPONSE_OVERSIZE")
        result["RESPONSE_RECEIVED"] = True
        envelope = json.loads(raw, object_pairs_hook=_unique_pairs)
        if (type(envelope) is not dict or envelope.get("model") != MODEL or
                envelope.get("done") is not True or envelope.get("done_reason") not in
                {None, "stop", "load"} or envelope.get("response") != "" or
                any(envelope.get(key) is not None and
                    (type(envelope[key]) is not int or envelope[key] != 0)
                    for key in ("eval_count", "prompt_eval_count")) or
                "thinking" in envelope or "message" in envelope):
            raise BrowserRefusal("PRELOAD_NOT_NON_GENERATIVE")
        result["COMPLETE_NON_GENERATIVE_LOAD"] = True
        result["FAILURE_CLASS"] = "NONE"
        for field, key in (("LOAD_DURATION_MS", "load_duration"),
                           ("TOTAL_DURATION_MS", "total_duration")):
            value = envelope.get(key)
            result[field] = min(200000, value // 1000000) if type(value) is int and value >= 0 else None
    except BrowserRefusal as exc:
        result["FAILURE_CLASS"] = exc.code
    except (TimeoutError, httpx.TimeoutException):
        result["FAILURE_CLASS"] = "PRELOAD_TIMEOUT"
    except (httpx.HTTPError, OSError, ValueError, TypeError, UnicodeError):
        result["FAILURE_CLASS"] = "PRELOAD_TRANSPORT_OR_ENVELOPE_INVALID"
    finally:
        result["CLIENT_ELAPSED_MS"] = min(200000, max(0, round((time.monotonic() - started) * 1000)))
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
        result["RESOURCE_SUMMARY"] = r2._summary(samples[:r2.MAX_SAMPLES])
        assessment, warning = compact._resource_assessment(result["RESOURCE_SUMMARY"])
        result["RESOURCE_ASSESSMENT"] = assessment
        result["RESOURCE_PRESSURE_WARNING"] = warning
    return result


def _initial_report(expected_sha: str, preflight: dict[str, object], frozen: cmq4.FrozenRequest
                    ) -> dict[str, object]:
    """Create the initial bounded report and first request reservation."""
    cold = preflight.get("MODEL_RESIDENCY") != "LOADED"
    return {"PROTOCOL": PROTOCOL, "SOURCE_SHA": expected_sha, "MODEL": MODEL,
            "MODEL_DIGEST": MODEL_DIGEST, "QUANTIZATION": QUANTIZATION,
            "FIXTURE": FIXTURE, "FIXTURE_SHA256": FIXTURE_DIGEST,
            "FROZEN_INPUT_SHA256": frozen.digest,
            "VERDICT": "ATTEMPT_IN_PROGRESS", "FAILURE_CLASS": "NONE",
            "PRELOAD_REQUESTS_ATTEMPTED": 0, "PRELOAD_RESPONSE_COUNT": 0,
            "PRELOAD_RESERVED_MAX": 1 if cold else 0,
            "SEMANTIC_REQUESTS_ATTEMPTED": 0, "SEMANTIC_RESPONSE_COUNT": 0,
            "SEMANTIC_RESERVED_MAX": 0 if cold else 1,
            "STAGE_A": {"STATUS": "NOT_RUN"}, "STAGE_B": {"STATUS": "NOT_RUN"},
            "MODEL_RESIDENCY_BEFORE": preflight.get("MODEL_RESIDENCY"),
            "REAL_EXPERIMENT_EXECUTED": False,
            "TASK_ACCEPTANCE_PASSED": False, "PRODUCTION_QUALIFIED": False}


def _terminal(report: dict[str, object], *, verdict: str, failure: str) -> None:
    """Persist one terminal classification without altering attempted counts."""
    report["VERDICT"] = verdict
    report["FAILURE_CLASS"] = _safe_code(failure)
    report["REAL_EXPERIMENT_EXECUTED"] = (None if report["SEMANTIC_RESERVED_MAX"] else
                                          report["SEMANTIC_REQUESTS_ATTEMPTED"] > 0)
    _write_attempt(report)


async def _operational(expected_sha: str, ready: dict[str, object], config: object,
                       preflight: dict[str, object]) -> dict[str, object]:
    """Execute only after separate load and inference consents were obtained."""
    deadline = time.monotonic() + RUN_DEADLINE_SECONDS
    frozen = await asyncio.wait_for(cmq4._frozen_post_fill(
        scenario=TASK, setup_controls=("Book catalog", "Work code"),
        setup_codes=("C02", "C03"), useful_code="C04"), timeout=60)
    if frozen.source_digest != FIXTURE_DIGEST or TASK.expected in (
            frozen.messages[0].content + frozen.messages[1].content):
        raise BrowserRefusal("FROZEN_REQUEST_UNTRUSTED")
    report = _initial_report(expected_sha, preflight, frozen)
    claimed = False
    ledger_owned = False
    try:
        # Recheck the exact checkout before irrevocably claiming the campaign.
        compact.verify_checkout(expected_sha, BRANCH)
        if not _state_available():
            raise BrowserRefusal("CAMPAIGN_ALREADY_CONSUMED")
        _claim(expected_sha)
        claimed = True
        _write_attempt(report, first=True)
        ledger_owned = True
        if preflight.get("MODEL_RESIDENCY") != "LOADED":
            result = await asyncio.wait_for(_preload_request(config.ollama_base_url),
                                            timeout=max(1, deadline - time.monotonic()))
            report["PRELOAD_REQUESTS_ATTEMPTED"] = 1
            report["PRELOAD_RESPONSE_COUNT"] = int(result["RESPONSE_RECEIVED"] is True)
            report["PRELOAD"] = result
            if result["COMPLETE_NON_GENERATIVE_LOAD"] is True:
                report["PRELOAD_RESERVED_MAX"] = 0
            if (result["COMPLETE_NON_GENERATIVE_LOAD"] is not True or
                    result["RESOURCE_ASSESSMENT"] in {
                        "RESOURCE_HARD_BLOCKED", "RESOURCE_EVIDENCE_INSUFFICIENT"}):
                failure = (result["RESOURCE_ASSESSMENT"] if result["COMPLETE_NON_GENERATIVE_LOAD"]
                           else result["FAILURE_CLASS"])
                _terminal(report, verdict="PRELOAD_BLOCKED", failure=str(failure))
                return report
            _write_attempt(report)
            fresh, fresh_config = await asyncio.wait_for(
                compact._preflight(MODEL, expected_sha, QUANTIZATION, BRANCH),
                timeout=max(1, deadline - time.monotonic()))
            report["POST_LOAD_PREFLIGHT"] = _public_preflight(fresh)
            if (fresh_config is None or fresh.get("MODEL_DIGEST_SHA256") != MODEL_DIGEST or
                    fresh.get("MODEL_RESIDENCY") != "LOADED"):
                raise BrowserRefusal("POST_LOAD_RESOURCE_OR_RESIDENCY_BLOCKED")
            preflight, config = fresh, fresh_config
            report["SEMANTIC_RESERVED_MAX"] = 1
            _write_attempt(report)
        candidate = cmq4.Candidate(MODEL, MODEL_DIGEST, QUANTIZATION,
                                   int(preflight["MODEL_SIZE_MIB"]))
        stage_a = await asyncio.wait_for(cmq4._choice_probe(
            candidate, frozen, preflight, config, fixture=FIXTURE, useful_code="C04"),
            timeout=max(1, deadline - time.monotonic()))
        if (stage_a.get("MODEL_CALL_COUNT") != 1 or
                type(stage_a.get("MODEL_RESPONSE_COUNT")) is not int or
                not 0 <= stage_a["MODEL_RESPONSE_COUNT"] <= 1):
            raise BrowserRefusal("STAGE_A_ACCOUNTING_INVALID")
        report["SEMANTIC_REQUESTS_ATTEMPTED"] = 1
        report["SEMANTIC_RESPONSE_COUNT"] = stage_a.get("MODEL_RESPONSE_COUNT", 0)
        report["SEMANTIC_RESERVED_MAX"] = 0
        report["STAGE_A"] = {key: stage_a.get(key) for key in SAFE_STAGE_A_FIELDS}
        report["STAGE_A"]["STATUS"] = "RECORDED"
        report["REAL_EXPERIMENT_EXECUTED"] = True
        _write_attempt(report)
        if stage_a.get("TECHNICALLY_QUALIFIED") is not True:
            _terminal(report, verdict="STAGE_A_BLOCKED",
                      failure=str(stage_a.get("FAILURE_CLASS", "STAGE_A_NOT_QUALIFIED")))
            return report
        report["SEMANTIC_RESERVED_MAX"] = 6
        report["STAGE_B"] = {"STATUS": "ATTEMPTED_RESULT_UNKNOWN", "RESERVED_MAX": 6}
        _write_attempt(report)
        seed = preflight.copy()
        seed["MODEL_DIGEST_SHA256"] = MODEL_DIGEST
        browser = await asyncio.wait_for(compact._stage_b_after_a(
            seed, MODEL, expected_sha, QUANTIZATION,
            bool(stage_a.get("RESOURCE_PRESSURE_WARNING")), branch=BRANCH,
            hierarchical=False, fixture=FIXTURE, objective=TASK.objective,
            target=TASK.target, expected=TASK.expected),
            timeout=max(1, deadline - time.monotonic()))
        if browser.get("STAGE_B_CALL_COUNT_UNKNOWN") is True:
            raise BrowserRefusal("MISSION_CALL_COUNT_UNKNOWN")
        stage = browser.get("STAGE_B")
        actual = stage.get("MODEL_CALL_COUNT") if type(stage) is dict else 0
        responses = stage.get("MODEL_RESPONSE_COUNT") if type(stage) is dict else 0
        if (type(actual) is not int or not 0 <= actual <= 6 or
                type(responses) is not int or not 0 <= responses <= actual):
            raise BrowserRefusal("MISSION_ACCOUNTING_INVALID")
        report["SEMANTIC_REQUESTS_ATTEMPTED"] += actual
        report["SEMANTIC_RESPONSE_COUNT"] += responses
        report["SEMANTIC_RESERVED_MAX"] = 0
        report["STAGE_B"] = {key: stage.get(key) for key in compact.SAFE_STAGE_B_FIELDS} if type(stage) is dict else {}
        report["STAGE_B"].update(STATUS="RECORDED" if actual else "BLOCKED",
                                 FAILURE_CLASS=_safe_code(browser.get("FAILURE_CLASS")))
        report["TASK_ACCEPTANCE_PASSED"] = (browser.get("TASK_ACCEPTANCE_PASSED") is True and
                                             report["STAGE_B"].get("TRACE_SOURCE_VERIFIED") is True and
                                             report["STAGE_B"].get("TRACE_COMPLETE") is True)
        _terminal(report, verdict="SYNTHETIC_MISSION_COMPLETED" if report["TASK_ACCEPTANCE_PASSED"]
                  else "SYNTHETIC_MISSION_BLOCKED",
                  failure="NONE" if report["TASK_ACCEPTANCE_PASSED"] else
                  _safe_code(browser.get("FAILURE_CLASS")))
        return report
    except (KeyboardInterrupt, asyncio.CancelledError):
        report["FAILURE_CLASS"] = "CAMPAIGN_INTERRUPTED"
    except BrowserRefusal as exc:
        report["FAILURE_CLASS"] = exc.code
    except (OSError, ValueError, TypeError, httpx.HTTPError, TimeoutError):
        report["FAILURE_CLASS"] = "CAMPAIGN_RESOURCE_OR_STATE_FAILURE"
    except Exception:
        report["FAILURE_CLASS"] = "CAMPAIGN_UNEXPECTED_FAILURE"
    report["VERDICT"] = "CAMPAIGN_BLOCKED"
    report["REAL_EXPERIMENT_EXECUTED"] = (None if report["SEMANTIC_RESERVED_MAX"] else
                                          report["SEMANTIC_REQUESTS_ATTEMPTED"] > 0)
    report["CLAIMED"] = claimed
    if ledger_owned:
        report["TERMINAL_REPORT_PERSISTED"] = True
        try:
            _write_attempt(report)
        except (OSError, ValueError, TypeError, BrowserRefusal):
            report["TERMINAL_REPORT_PERSISTED"] = False
    return report


async def evaluate(expected_sha: str, *, run: bool = False) -> dict[str, object]:
    """Prepare non-generatively; run one consented and non-replayable protocol."""
    logger.remove()
    try:
        ready, config = await asyncio.wait_for(readiness(expected_sha), timeout=60)
        if config is None or not run:
            return ready
        preflight = ready["PREFLIGHT"]
        cold = preflight.get("MODEL_RESIDENCY") != "LOADED"
        if cold and not compact._consented("LOAD LLAMA ONCE"):
            _cancel_before_attempt(expected_sha, "LOAD_CONSENT_REQUIRED")
            return {**ready, "VERDICT": "CONSENT_REFUSED_BEFORE_ATTEMPT",
                    "FAILURE_CLASS": "LOAD_CONSENT_REQUIRED"}
        if not compact._consented("MODEL ONCE LLAMA"):
            _cancel_before_attempt(expected_sha, "MODEL_CONSENT_REQUIRED")
            return {**ready, "VERDICT": "CONSENT_REFUSED_BEFORE_ATTEMPT",
                    "FAILURE_CLASS": "MODEL_CONSENT_REQUIRED"}
        return await _operational(expected_sha, ready, config, preflight)
    except (OSError, ValueError, TypeError, BrowserRefusal, TimeoutError):
        return {"VERDICT": "PREPARATION_OR_STATE_BLOCKED", "FAILURE_CLASS": "PREPARATION_OR_STATE_FAILURE",
                "PROTOCOL": PROTOCOL, "INFERENCE_ATTEMPTED": False,
                "PRODUCTION_QUALIFIED": False}


def main() -> None:
    """Print only bounded statuses; no operation is the default CLI action."""
    parser = argparse.ArgumentParser(description="CMQ.5 Llama catalog feasibility")
    parser.add_argument("--expected-sha", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--readiness", action="store_true")
    modes.add_argument("--run-once", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(evaluate(args.expected_sha, run=args.run_once))
    except (KeyboardInterrupt, Exception):
        result = {"VERDICT": "PROCESS_INTERRUPTED", "PRODUCTION_QUALIFIED": False}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
