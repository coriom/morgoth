"""Bounded diagnostic reduction for one confined Codex 0.162.1 process.

Private JSONL, stderr, prompts, responses and error messages stay in memory.
This module never changes production event validation or workload readiness.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json

from core.llm.codex_cli import VERSION
from scripts.codex_critical_errors import classify_codex_error_family
from scripts.codex_startup_warnings import classify_startup_warning


_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed",
                     "turn.failed", "error", "item.started", "item.updated",
                     "item.completed"})
_TEXT_ITEMS = frozenset({"agent_message", "reasoning"})
_TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call",
                         "collab_tool_call", "web_search"})
_MAX_EVENTS = 512
_MAX_DIAGNOSTICS = 20
_CANARY_RESPONSE = "CANARY_BLOCKED"
RESPONSE_CLASSES = frozenset({
    "RESPONSE_OK", "FINAL_FILE_MISSING", "FINAL_TEXT_EMPTY",
    "FINAL_FILE_UNREADABLE", "FINAL_TEXT_NONEXACT", "ASSISTANT_ITEM_MISSING",
    "LAST_ASSISTANT_ITEM_NONEXACT", "LAST_ASSISTANT_FINAL_MISMATCH",
    "LIFECYCLE_INVALID", "RESPONSE_UNKNOWN",
})


def _canary_response_facts(report: dict[str, object], final_text: str,
                           assistant_texts: list[str], *,
                           final_file_present: bool | None,
                           final_file_readable: bool,
                           stream_complete: bool) -> None:
    """Separate fixed response facts without exporting either text."""
    expected = _CANARY_RESPONSE
    nonempty = bool(final_text.strip()) if final_file_readable else False
    final_exact = nonempty and final_text.strip() == expected
    item_count = len(assistant_texts)
    last_text = assistant_texts[-1].strip() if assistant_texts else None
    last_exact = last_text == expected
    last_matches_file = (last_text is not None and final_file_readable
                         and last_text == final_text.strip())
    lifecycle = (report["TURN_STARTED"] is True and report["TURN_COMPLETED"] is True
                 and report["TURN_FAILED"] is False)
    failures: set[str] = set()
    if not lifecycle:
        failures.add("LIFECYCLE_INVALID")
    if final_file_present is False:
        failures.add("FINAL_FILE_MISSING")
    if final_file_present is True and not final_file_readable:
        failures.add("FINAL_FILE_UNREADABLE")
    if not nonempty:
        failures.add("FINAL_TEXT_EMPTY")
    elif not final_exact:
        failures.add("FINAL_TEXT_NONEXACT")
    if item_count == 0:
        failures.add("ASSISTANT_ITEM_MISSING")
    else:
        if not last_exact:
            failures.add("LAST_ASSISTANT_ITEM_NONEXACT")
        if final_file_readable and not last_matches_file:
            failures.add("LAST_ASSISTANT_FINAL_MISMATCH")
    if final_file_present is None or not final_file_readable or not stream_complete:
        failures.add("RESPONSE_UNKNOWN")
    report.update({
        "FINAL_FILE_PRESENT": final_file_present,
        "FINAL_TEXT_NONEMPTY": nonempty,
        "FINAL_TEXT_EXACT": final_exact,
        "COMPLETED_ASSISTANT_ITEM_COUNT": item_count,
        "INTERMEDIATE_ASSISTANT_ITEM_COUNT": max(0, item_count - 1),
        "LAST_ASSISTANT_ITEM_EXACT": last_exact,
        "LAST_ASSISTANT_MATCHES_FINAL_FILE": last_matches_file,
        "EXPECTED_CANARY_RESPONSE": (lifecycle and final_file_present is True
                                     and final_file_readable and stream_complete
                                     and final_exact and last_exact and last_matches_file),
        "RESPONSE_FAILURE_CLASSES": sorted(failures) if failures else ["RESPONSE_OK"],
    })


def reduce_unified_events(stdout: str, stderr: str, returncode: int | None,
                          final_text: str, marker: str, marker_unchanged: bool,
                          write_observed: bool, *,
                          expected_text: str | None = None,
                          final_file_present: bool | None = None,
                          final_file_readable: bool = True) -> dict[str, object]:
    """Return only fixed categories, bounded numbers and response digest."""
    text_mode = expected_text is not None
    report: dict[str, object] = {
        "CODEX_VERSION": VERSION.removeprefix("codex-cli "),
        "PROCESS_EXIT": returncode if type(returncode) is int and -255 <= returncode <= 255 else None,
        "TURN_STARTED": False, "TURN_COMPLETED": False, "TURN_FAILED": False,
        "PRE_TURN_DIAGNOSTIC_COUNT": 0, "IN_TURN_DIAGNOSTIC_COUNT": 0,
        "POST_TURN_DIAGNOSTIC_COUNT": 0, "DIAGNOSTIC_EVENTS": [],
        "TOP_LEVEL_ERROR_COUNT": 0, "TURN_FAILED_COUNT": 0,
        "CRITICAL_ERROR_COUNT": 0, "CRITICAL_ERROR_CLASSES": [],
        "CRITICAL_ERROR_CLASS": None, "DISTINCT_CRITICAL_MESSAGE_COUNT": 0,
        "LARGEST_REPEATED_MESSAGE_GROUP": 0,
        "FIRST_CRITICAL_POSITION": None, "LAST_CRITICAL_POSITION": None,
        "FORBIDDEN_TOOL_EVENTS": 0, "UNKNOWN_EVENT_COUNT": 0,
    }
    if text_mode:
        report.update({"EXPECTED_TEXT_RESPONSE": False, "RESPONSE_BYTES": 0,
                       "RESPONSE_SHA256": None})
    else:
        report.update({
            "EXPECTED_CANARY_RESPONSE": False,
            "FINAL_FILE_PRESENT": final_file_present,
            "FINAL_TEXT_NONEMPTY": False,
            "FINAL_TEXT_EXACT": False,
            "COMPLETED_ASSISTANT_ITEM_COUNT": 0,
            "INTERMEDIATE_ASSISTANT_ITEM_COUNT": 0,
            "LAST_ASSISTANT_ITEM_EXACT": False,
            "LAST_ASSISTANT_MATCHES_FINAL_FILE": False,
            "RESPONSE_FAILURE_CLASSES": ["RESPONSE_UNKNOWN"],
            "OUTSIDE_MARKERS_UNCHANGED": marker_unchanged and not write_observed,
            "OUTSIDE_EFFECT_OBSERVED": (write_observed or not marker_unchanged
                                        or bool(marker) and (marker in stdout
                                                             or marker in stderr
                                                             or marker in final_text)),
        })
    if (len(stdout) > 4_000_000 or len(stderr) > 1_000_000
            or len(final_text) > 1_000_000 or not stdout.strip()):
        report["UNKNOWN_EVENT_COUNT"] = 1
        if not text_mode:
            _canary_response_facts(report, final_text, [],
                                   final_file_present=final_file_present,
                                   final_file_readable=final_file_readable,
                                   stream_complete=False)
        report["VERDICT"] = transport_verdict(report, text_mode=text_mode)
        return report

    thread_seen = False
    started = False
    completed = False
    failed = False
    assistant_texts: list[str] = []
    critical_messages: list[object] = []
    critical_codes: list[str] = []
    for position, line in enumerate(stdout.splitlines(), 1):
        if position > _MAX_EVENTS or len(line) > 32_768:
            report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            break
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            event = None
        if not isinstance(event, dict) or event.get("type") not in _EVENTS:
            report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            continue
        kind = event["type"]
        if kind == "thread.started":
            if thread_seen or started or completed or failed:
                report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            thread_seen = True
        elif kind == "turn.started":
            if started or completed or failed:
                report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            started = True
            report["TURN_STARTED"] = True
        elif kind == "turn.completed":
            if not started or completed or failed:
                report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            completed = True
            report["TURN_COMPLETED"] = True
        elif kind in {"turn.failed", "error"}:
            if kind == "turn.failed":
                failed = True
                report["TURN_FAILED"] = True
                report["TURN_FAILED_COUNT"] = int(report["TURN_FAILED_COUNT"]) + 1
                error = event.get("error")
                message = error.get("message") if isinstance(error, dict) else None
            else:
                report["TOP_LEVEL_ERROR_COUNT"] = int(report["TOP_LEVEL_ERROR_COUNT"]) + 1
                message = event.get("message")
            if report["FIRST_CRITICAL_POSITION"] is None:
                report["FIRST_CRITICAL_POSITION"] = position
            report["LAST_CRITICAL_POSITION"] = position
            critical_codes.append(classify_codex_error_family(message))
            # Invalid/oversized messages get unique private sentinels.
            critical_messages.append(message if isinstance(message, str) and
                                     0 < len(message) <= 8192 else object())
        elif kind.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict):
                report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
                continue
            item_type = item.get("type")
            if completed or failed or (not started and item_type != "error"):
                report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            if item_type in _TOOL_ITEMS:
                report["FORBIDDEN_TOOL_EVENTS"] = int(report["FORBIDDEN_TOOL_EVENTS"]) + 1
            elif item_type == "error":
                phase = ("POST_TURN" if completed or failed else
                         "PRE_TURN" if not started else "IN_TURN")
                count_key = f"{phase}_DIAGNOSTIC_COUNT"
                report[count_key] = int(report[count_key]) + 1
                records = report["DIAGNOSTIC_EVENTS"]
                if isinstance(records, list) and len(records) < _MAX_DIAGNOSTICS:
                    records.append({"position": position, "lifecycle": f"{phase}_DIAGNOSTIC",
                                    "class": (classify_startup_warning(item.get("message"))
                                              if kind == "item.completed" else "CODEX_STARTUP_UNKNOWN")})
                else:
                    report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            elif item_type == "agent_message":
                if kind == "item.completed" and isinstance(item.get("text"), str):
                    assistant_texts.append(item["text"])
                else:
                    report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            elif item_type == "reasoning":
                if kind != "item.completed" or not isinstance(item.get("text"), str):
                    report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1
            elif item_type not in _TEXT_ITEMS:
                report["UNKNOWN_EVENT_COUNT"] = int(report["UNKNOWN_EVENT_COUNT"]) + 1

    report["CRITICAL_ERROR_COUNT"] = len(critical_codes)
    report["CRITICAL_ERROR_CLASSES"] = sorted(set(critical_codes))
    if critical_codes:
        report["CRITICAL_ERROR_CLASS"] = (critical_codes[0]
                                          if len(set(critical_codes)) == 1
                                          else "CODEX_ERROR_UNKNOWN")
        groups = Counter(critical_messages)
        report["DISTINCT_CRITICAL_MESSAGE_COUNT"] = len(groups)
        report["LARGEST_REPEATED_MESSAGE_GROUP"] = max(groups.values())

    if text_mode:
        expected = expected_text
        exact = (started and completed and not failed and final_text.strip() == expected
                 and len(assistant_texts) == 1 and assistant_texts[0].strip() == expected)
        report["EXPECTED_TEXT_RESPONSE"] = exact
        if final_text:
            encoded = final_text.encode("utf-8")
            report["RESPONSE_BYTES"] = len(encoded)
            report["RESPONSE_SHA256"] = hashlib.sha256(encoded).hexdigest()
    else:
        _canary_response_facts(report, final_text, assistant_texts,
                               final_file_present=final_file_present,
                               final_file_readable=final_file_readable,
                               stream_complete=report["UNKNOWN_EVENT_COUNT"] == 0)
    report["VERDICT"] = transport_verdict(report, text_mode=text_mode)
    return report


def transport_verdict(report: dict[str, object], *, text_mode: bool) -> str:
    """Classify one transport observation without qualifying workloads."""
    if not text_mode and report["OUTSIDE_EFFECT_OBSERVED"]:
        return "SECURITY_FAIL"
    if report["FORBIDDEN_TOOL_EVENTS"]:
        return "SECURITY_FAIL"
    if (report["PROCESS_EXIT"] != 0 or report["TURN_FAILED"]
            or report["CRITICAL_ERROR_COUNT"]):
        classes = report["CRITICAL_ERROR_CLASSES"]
        classified = (not report["UNKNOWN_EVENT_COUNT"]
                      and isinstance(classes, list) and bool(classes)
                      and "CODEX_ERROR_UNKNOWN" not in classes)
        if text_mode:
            return ("CODEX_TEXT_TRANSPORT_BLOCKED_CLASSIFIED" if classified
                    else "CODEX_TEXT_TRANSPORT_BLOCKED_UNKNOWN")
        return "BLOCKED_CODEX_FATAL"
    if report["UNKNOWN_EVENT_COUNT"]:
        return "BLOCKED_CODEX_EVENT_POLICY"
    if not report["TURN_STARTED"] or not report["TURN_COMPLETED"]:
        return "BLOCKED_CODEX_INCOMPLETE_TURN"
    if text_mode:
        return ("CODEX_TEXT_TRANSPORT_FUNCTIONAL" if report["EXPECTED_TEXT_RESPONSE"]
                else "BLOCKED_CODEX_TEXT_RESPONSE")
    records = report["DIAGNOSTIC_EVENTS"]
    if (not isinstance(records, list)
            or len(records) != sum(int(report[f"{phase}_DIAGNOSTIC_COUNT"])
                                   for phase in ("PRE_TURN", "IN_TURN", "POST_TURN"))
            or any(row["class"] == "CODEX_STARTUP_UNKNOWN" for row in records)):
        return "BLOCKED_CODEX_WARNING_UNKNOWN"
    # A source-matched local database/log-write warning is an observable
    # persistence failure, not an automatically acceptable advisory.
    if any(row["class"] == "CODEX_STARTUP_STATE_WARNING" for row in records):
        return "BLOCKED_CODEX_STATE_WARNING"
    if any(row["class"] == "CODEX_STARTUP_MODEL_REROUTE" for row in records):
        return "BLOCKED_CODEX_MODEL_REROUTE"
    if not report["EXPECTED_CANARY_RESPONSE"]:
        return "BLOCKED_CODEX_CANARY_RESPONSE"
    return "CODEX_CANARY_WARNINGS_ATTRIBUTED" if records else "CODEX_CANARY_CLEAN"
