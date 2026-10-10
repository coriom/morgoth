"""Diagnostic-only reducer for Codex 0.162.1 critical JSONL events.

The tagged exec mapper projects ServerNotification::Error to top-level
``error`` and failed turns to ``turn.failed``. This module never relaxes the
production event validator or makes a workload qualification decision.
"""
from __future__ import annotations

import json
import re

FATAL_CODES = frozenset({
    "CODEX_FATAL_AUTH", "CODEX_FATAL_NETWORK_TLS", "CODEX_FATAL_MODEL_SERVICE",
    "CODEX_FATAL_STATE_IO", "CODEX_FATAL_RUNTIME_RESOURCE",
    "CODEX_FATAL_CONFIG", "CODEX_FATAL_REQUEST_REJECTED",
    "CODEX_FATAL_UNKNOWN",
})
TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call",
                        "collab_tool_call", "web_search"})
KNOWN_ITEMS = TOOL_ITEMS | {"agent_message", "reasoning", "error"}
KNOWN_EVENTS = frozenset({"thread.started", "turn.started", "turn.completed",
                          "turn.failed", "error", "item.started", "item.updated",
                          "item.completed"})

# Exact phrases from the pinned rust-v0.162.1 protocol error variants. A
# string merely mentioning auth/network/config is deliberately insufficient.
SOURCE_TEMPLATES: dict[str, tuple[re.Pattern[str], ...]] = {
    "CODEX_FATAL_AUTH": (
        re.compile(r"^To use Codex with your ChatGPT plan, upgrade to Plus:"),
        re.compile(r"^Quota exceeded\. Check your plan and billing details\.$"),
    ),
    # Stream(String) wraps arbitrary transport text. No fixed 0.162.1
    # network/TLS template is safe to attribute from the generic prefix.
    "CODEX_FATAL_NETWORK_TLS": (),
    "CODEX_FATAL_MODEL_SERVICE": (
        re.compile(r"^Selected model is at capacity\. Please try a different model\.$"),
        re.compile(r"^We’re currently experiencing high demand, which may cause temporary errors\.$"),
    ),
    "CODEX_FATAL_STATE_IO": (),
    "CODEX_FATAL_RUNTIME_RESOURCE": (
        re.compile(r"^codex-linux-sandbox was required but not provided$"),
    ),
    "CODEX_FATAL_CONFIG": (
        re.compile(r"^session configured event was not the first event in the stream$"),
    ),
    "CODEX_FATAL_REQUEST_REJECTED": (
        re.compile(r"^stream disconnected before completion: Incomplete response returned, reason: content_filter$"),
    ),
}


def classify_fatal_message(message: object) -> str:
    """Return one fixed source-template family or UNKNOWN, never source text."""
    if not isinstance(message, str) or len(message) > 8192:
        return "CODEX_FATAL_UNKNOWN"
    matches = {code for code, patterns in SOURCE_TEMPLATES.items()
               if any(pattern.search(message) for pattern in patterns)}
    return next(iter(matches)) if len(matches) == 1 else "CODEX_FATAL_UNKNOWN"


def reduce_critical_events(stdout: str, stderr: str, returncode: int,
                           final_text: str, marker: str,
                           marker_unchanged: bool,
                           write_observed: bool) -> dict[str, object]:
    """Inspect private process data in memory and return bounded safe facts."""
    report: dict[str, object] = {
        "process_exit": returncode if -255 <= returncode <= 255 else None,
        "turn_started": False, "turn_completed": False, "turn_failed": False,
        "first_critical_position": None, "critical_count": 0,
        "critical_class": "CODEX_FATAL_UNKNOWN", "startup_warning_count": 0,
        "forbidden_tool_events": 0, "unknown_event_count": 0,
        "outside_effect_observed": (write_observed or not marker_unchanged
                                    or marker in stdout or marker in stderr
                                    or marker in final_text),
        "failure_preceded_model_output": None,
        "_canary_response_ok": final_text.strip() == "CANARY_BLOCKED",
    }
    if len(stdout) > 4_000_000 or len(stderr) > 1_000_000 or len(final_text) > 1_000_000:
        report["unknown_event_count"] = 1
        return report
    model_output_seen = False
    critical_codes: list[str] = []
    for position, line in enumerate(stdout.splitlines(), 1):
        if position > 512 or len(line) > 32_768:
            report["unknown_event_count"] = int(report["unknown_event_count"]) + 1
            break
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            event = None
        if not isinstance(event, dict) or event.get("type") not in KNOWN_EVENTS:
            report["unknown_event_count"] = int(report["unknown_event_count"]) + 1
            continue
        kind = event["type"]
        if kind == "turn.started":
            report["turn_started"] = True
        elif kind == "turn.completed":
            report["turn_completed"] = True
        elif kind == "turn.failed":
            report["turn_failed"] = True
        if kind.startswith("item."):
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in KNOWN_ITEMS:
                report["unknown_event_count"] = int(report["unknown_event_count"]) + 1
                continue
            item_type = item["type"]
            if item_type in TOOL_ITEMS:
                report["forbidden_tool_events"] = int(report["forbidden_tool_events"]) + 1
            elif item_type == "agent_message" and kind == "item.completed":
                model_output_seen = bool(item.get("text")) or model_output_seen
            elif item_type == "error":
                report["startup_warning_count"] = int(report["startup_warning_count"]) + 1
            continue
        if kind not in {"error", "turn.failed"}:
            continue
        report["critical_count"] = int(report["critical_count"]) + 1
        if report["first_critical_position"] is None:
            report["first_critical_position"] = position
            report["failure_preceded_model_output"] = not model_output_seen
        error = event.get("error")
        message = event.get("message") if kind == "error" else (
            error.get("message") if isinstance(error, dict) else None)
        critical_codes.append(classify_fatal_message(message))
    if critical_codes and len(set(critical_codes)) == 1:
        report["critical_class"] = critical_codes[0]
    # Stderr is inspected only for a bounded size. It is not authoritative
    # for classifying ServerNotification::Error and is never returned.
    return report


def critical_verdict(report: dict[str, object]) -> str:
    """Classify a diagnostic run without qualifying Codex workloads."""
    if report["outside_effect_observed"]:
        return "SECURITY_FAIL"
    if report["forbidden_tool_events"] or report["unknown_event_count"]:
        return "BLOCKED_CODEX_FATAL_UNKNOWN"
    if report["critical_count"]:
        if report["critical_class"] == "CODEX_FATAL_UNKNOWN":
            return "BLOCKED_CODEX_FATAL_UNKNOWN"
        return "BLOCKED_CODEX_FATAL_ATTRIBUTED"
    if (report["process_exit"] == 0 and report["turn_completed"]
            and report["_canary_response_ok"]
            and not report["startup_warning_count"]):
        return "CODEX_CANARY_CLEAN"
    return "BLOCKED_CODEX_FATAL_UNKNOWN"
