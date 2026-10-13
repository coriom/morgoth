"""Operator-only review of one confined Codex 0.162.1 text diagnostic.

This does not qualify workloads. Private JSONL and warnings are never logged;
the optional redacted display goes only to the controlling terminal.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TextIO

from core.llm import codex_cli as cli
from scripts.codex_startup_warnings import STARTUP_CODES, classify_startup_warning
from scripts.codex_unified_evidence import reduce_unified_events


ROOT = Path(__file__).resolve().parents[1]
BRANCH = "codex-01621-private-warning-review-v1"
BASE_SHA = "0641ffa2728c4cafff10193390808298d5885df3"
PROMPT = "Reply with exactly CODEX_PROVIDER_OK."
EXPECTED = "CODEX_PROVIDER_OK"
FIRST_CONSENT = "RUN ONE CONFINED TEXT PROBE"
SECOND_CONSENT = "SHOW ONE REDACTED WARNING"
CATEGORIES = frozenset({"CONFIG", "DEPRECATION", "RUNTIME", "STATE",
                        "SKILL", "MODEL_REROUTE", "OTHER", "UNDETERMINED"})
_MAX_OUTPUT = 4_000_000
_MAX_EVENTS = 512
_MAX_MESSAGE = 1024


class ReviewUnavailable(RuntimeError):
    """A fixed, non-sensitive local-review prerequisite failed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _git(*args: str) -> str:
    """Read bounded repository metadata without exporting failed Git output."""
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        raise ReviewUnavailable("PREFLIGHT_CHECKOUT") from None
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise ReviewUnavailable("PREFLIGHT_CHECKOUT")
    return result.stdout.strip()


def verify_checkout() -> None:
    """Require the exact clean, tracked-origin-equal reviewed feature branch."""
    if (_git("branch", "--show-current") != BRANCH
            or _git("rev-parse", "HEAD") != _git("rev-parse", f"origin/{BRANCH}")
            or _git("status", "--porcelain", "--untracked-files=all")
            or _git("merge-base", BASE_SHA, "HEAD") != BASE_SHA
            or _git("remote", "get-url", "origin") not in {
                "git@github.com:coriom/morgoth.git",
                "https://github.com/coriom/morgoth.git",
            }):
        raise ReviewUnavailable("PREFLIGHT_CHECKOUT")


def open_operator_terminal() -> TextIO:
    """Reject pipes, redirected output, background jobs and obvious CI use."""
    if (any(os.environ.get(key) for key in ("CI", "GITHUB_ACTIONS", "CODEBUILD_BUILD_ID"))
            or any(not os.isatty(fd) for fd in (0, 1, 2))):
        raise ReviewUnavailable("PREFLIGHT_INTERACTIVE_TTY")
    fd: int | None = None
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        if (not os.isatty(fd) or os.tcgetpgrp(fd) != os.getpgrp()
                or any(os.fstat(stream).st_rdev != os.fstat(fd).st_rdev
                       for stream in (0, 1, 2))):
            raise ReviewUnavailable("PREFLIGHT_INTERACTIVE_TTY")
        terminal = os.fdopen(fd, "r+", encoding="utf-8", buffering=1)
        fd = None
        return terminal
    except (OSError, ValueError, ReviewUnavailable):
        raise ReviewUnavailable("PREFLIGHT_INTERACTIVE_TTY") from None
    finally:
        if fd is not None:
            os.close(fd)


def preflight() -> tuple[str, dict[str, str]]:
    """Check exact binary, reviewed flags and confined ChatGPT login only."""
    env = cli.minimal_env()
    binary = shutil.which("codex", path=env.get("PATH", ""))
    if binary is None:
        raise ReviewUnavailable("PREFLIGHT_CODEX")
    try:
        with tempfile.TemporaryDirectory(prefix="morgoth-codex-check-", dir="/tmp") as cwd:
            cli.discover(binary, cwd, env)
            auth = cli._run([binary, "login", "status"], cwd, env, "", 5)
            if (auth.returncode != 0
                    or "Logged in using ChatGPT" not in auth.stdout + auth.stderr):
                raise ReviewUnavailable("PREFLIGHT_AUTH")
    except (cli.CodexCliError, OSError):
        raise ReviewUnavailable("PREFLIGHT_CODEX") from None
    return binary, env


def _consent(tty: TextIO, phrase: str) -> bool:
    """Require an exact phrase typed into the controlling terminal."""
    tty.write(f"Type {phrase} to continue; any other input stops: ")
    tty.flush()
    answer = tty.readline(128)
    return answer.rstrip("\r\n") == phrase


def _private_event_facts(stdout: str) -> tuple[str, list[str]]:
    """Extract only the last assistant item and pre-turn unknowns in memory."""
    if not stdout or len(stdout) > _MAX_OUTPUT:
        return "", []
    started = False
    last_assistant = ""
    unknown: list[str] = []
    for position, line in enumerate(stdout.splitlines(), 1):
        if position > _MAX_EVENTS or len(line) > 32_768:
            return "", []
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            return "", []
        if not isinstance(event, dict):
            return "", []
        if event.get("type") == "turn.started":
            started = True
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            return "", []
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            last_assistant = item["text"]
        elif not started and item.get("type") == "error":
            message = item.get("message")
            if isinstance(message, str) and classify_startup_warning(message) == "CODEX_STARTUP_UNKNOWN":
                unknown.append(message)
    return last_assistant, unknown


def _blocked_private_result() -> dict[str, object]:
    """Return a fixed failed transport record without exception text."""
    return reduce_unified_events("", "", None, "", "", True, False,
                                 expected_text=EXPECTED)


def run_one_text_request(binary: str, env: dict[str, str]) -> tuple[dict[str, object], list[str]]:
    """Make one fixed text request; discard the CLI final file into /dev/null."""
    with tempfile.TemporaryDirectory(prefix="morgoth-codex-", dir="/tmp") as cwd:
        try:
            result = cli._run(cli.build_argv(binary, "default", "/dev/null"), cwd,
                              env, PROMPT, 90)
        except cli.CodexCliError:
            return _blocked_private_result(), []
        final_text, unknown = _private_event_facts(result.stdout)
        report = reduce_unified_events(result.stdout, result.stderr, result.returncode,
                                       final_text, "", True, False,
                                       expected_text=EXPECTED)
        return report, unknown


_REDACTIONS = (
    (re.compile(r"(?i)(?:https?|wss?)://[^\s'\"]+"), "[URL]"),
    (re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/-]{4,}"), "[CREDENTIAL]"),
    (re.compile(r"(?i)\b(?:token|api[_-]?key|secret|password|authorization|cookie)\s*[:=]\s*[^\s'\"]+"), "[CREDENTIAL]"),
    (re.compile(r"(?i)\b(?:sk-[A-Za-z0-9_-]{6,}|gh[pousr]_[A-Za-z0-9_]{6,})\b"), "[CREDENTIAL]"),
    (re.compile(r"\b[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[IDENTIFIER]"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "[IDENTIFIER]"),
    (re.compile(r"(?<![A-Za-z0-9])(?:~?/|[A-Za-z]:[\\/])(?:[A-Za-z0-9._@%+-]+[\\/])*[A-Za-z0-9._@%+-]+"), "[PATH]"),
    (re.compile(r"\b(?:[A-Za-z0-9_-]{24,}|[0-9]{6,})\b"), "[IDENTIFIER]"),
)


def redacted_terminal_text(message: str) -> str | None:
    """Escape controls and best-effort redact one short private warning."""
    if not message or len(message) > _MAX_MESSAGE:
        return None
    displayed = ascii(message)
    for pattern, replacement in _REDACTIONS:
        displayed = pattern.sub(replacement, displayed)
    return displayed if len(displayed) <= 4096 else None


def public_report(private: dict[str, object]) -> dict[str, object]:
    """Export fixed metadata only; a manual category never qualifies Codex."""
    records = private.get("DIAGNOSTIC_EVENTS")
    if not isinstance(records, list) or len(records) > 20:
        raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
    pre: list[str] = []
    for row in records:
        if not isinstance(row, dict):
            raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
        if row.get("lifecycle") == "PRE_TURN_DIAGNOSTIC":
            code = row.get("class")
            if not isinstance(code, str) or code not in STARTUP_CODES:
                raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
            pre.append(code)
    for key in ("PRE_TURN_DIAGNOSTIC_COUNT", "FORBIDDEN_TOOL_EVENTS",
                "CRITICAL_ERROR_COUNT", "UNKNOWN_EVENT_COUNT"):
        value = private.get(key)
        if type(value) is not int or not 0 <= value <= 512:
            raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
    if private["PRE_TURN_DIAGNOSTIC_COUNT"] != len(pre):
        raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
    exit_code = private.get("PROCESS_EXIT")
    if exit_code is not None and (type(exit_code) is not int or not -255 <= exit_code <= 255):
        raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
    if any(type(private.get(key)) is not bool for key in (
            "TURN_STARTED", "TURN_COMPLETED", "TURN_FAILED", "EXPECTED_TEXT_RESPONSE")):
        raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
    transport_ok = private.get("VERDICT") == "CODEX_TEXT_TRANSPORT_FUNCTIONAL"
    if transport_ok and (exit_code != 0 or not private["TURN_STARTED"]
                         or not private["TURN_COMPLETED"] or private["TURN_FAILED"]
                         or not private["EXPECTED_TEXT_RESPONSE"]
                         or private["FORBIDDEN_TOOL_EVENTS"]
                         or private["CRITICAL_ERROR_COUNT"]
                         or private["UNKNOWN_EVENT_COUNT"]):
        raise ReviewUnavailable("PREFLIGHT_REPORT_SHAPE")
    unknown_count = pre.count("CODEX_STARTUP_UNKNOWN")
    if private["FORBIDDEN_TOOL_EVENTS"]:
        verdict = "SECURITY_FAIL"
    elif not transport_ok:
        verdict = "BLOCKED_TEXT_TRANSPORT"
    elif unknown_count:
        verdict = "BLOCKED_CODEX_WARNING_UNKNOWN"
    elif "CODEX_STARTUP_STATE_WARNING" in pre:
        verdict = "BLOCKED_CODEX_STATE_WARNING"
    elif "CODEX_STARTUP_MODEL_REROUTE" in pre:
        verdict = "BLOCKED_CODEX_MODEL_REROUTE"
    else:
        verdict = "BLOCKED_SEPARATE_SECURITY_REVIEW"
    return {
        "PROCESS_EXIT": exit_code,
        "TURN_COMPLETED": private.get("TURN_COMPLETED") is True,
        "TEXT_TRANSPORT_RESULT": "PASS" if transport_ok else "BLOCKED",
        "PRE_TURN_DIAGNOSTIC_COUNT": private["PRE_TURN_DIAGNOSTIC_COUNT"],
        "EXACT_SOURCE_TEMPLATE_CLASSES": sorted(set(pre) - {"CODEX_STARTUP_UNKNOWN"}),
        "UNKNOWN_DIAGNOSTIC_COUNT": unknown_count,
        "FORBIDDEN_TOOL_EVENTS": private["FORBIDDEN_TOOL_EVENTS"],
        "CRITICAL_ERROR_COUNT": private["CRITICAL_ERROR_COUNT"],
        "UNKNOWN_EVENT_COUNT": private["UNKNOWN_EVENT_COUNT"],
        "LOCAL_REVIEW_PERFORMED": False,
        "LOCAL_REVIEW_CATEGORY": "NOT_REVIEWED",
        "PRODUCTION_QUALIFIED": False,
        "VERDICT": verdict,
    }


def run_once(tty: TextIO) -> dict[str, object]:
    """Gate exactly one inference, then offer one optional local review."""
    verify_checkout()
    binary, env = preflight()
    tty.write("One confined text inference; no automatic retry. ")
    if not _consent(tty, FIRST_CONSENT):
        raise ReviewUnavailable("OPERATOR_DECLINED")
    verify_checkout()
    private, unknown = run_one_text_request(binary, env)
    public = public_report(private)
    if (public["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
            and public["TEXT_TRANSPORT_RESULT"] == "PASS"
            and public["UNKNOWN_DIAGNOSTIC_COUNT"] == 1 and len(unknown) == 1
            and public["UNKNOWN_EVENT_COUNT"] == 0):
        rendered = redacted_terminal_text(unknown[0])
        if rendered is not None:
            tty.write("Local redaction is best-effort and cannot guarantee removal of secrets. ")
            tty.write("Terminal scrollback or session recording may retain displayed text. ")
            tty.write("Do not paste it into any remote service.\n")
            if _consent(tty, SECOND_CONSENT):
                tty.write("Redacted local warning: " + rendered + "\n")
                tty.write("Choose CONFIG, DEPRECATION, RUNTIME, STATE, SKILL, ")
                tty.write("MODEL_REROUTE, OTHER, or UNDETERMINED: ")
                tty.flush()
                choice = tty.readline(32).strip().upper()
                public["LOCAL_REVIEW_PERFORMED"] = True
                public["LOCAL_REVIEW_CATEGORY"] = (choice if choice in CATEGORIES
                                                   else "UNDETERMINED")
    return public


def main() -> int:
    """Emit only fixed JSON metadata, including on failed preflight."""
    try:
        with open_operator_terminal() as tty:
            report = run_once(tty)
    except BaseException as exc:
        code = exc.code if isinstance(exc, ReviewUnavailable) else "PREFLIGHT_FAILED"
        print(json.dumps({"VERDICT": code, "PRODUCTION_QUALIFIED": False}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if report["VERDICT"] == "BLOCKED_SEPARATE_SECURITY_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
