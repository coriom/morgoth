"""No-inference tests for source-template attribution and fail-closed events."""
from __future__ import annotations

import json

import pytest

from core.llm.codex_cli import CodexCliError, _validate_events
from scripts.codex_startup_warnings import classify_startup_warning, reduce_startup_events


@pytest.mark.parametrize(("message", "expected"), [
    ("Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in PRIVATE_PATH.", "CODEX_STARTUP_CONFIG_WARNING"),
    ("Codex is ignoring 2 unrecognized configuration settings. Check for typos or deprecated settings.\nPRIVATE_PATH", "CODEX_STARTUP_CONFIG_WARNING"),
    ("`[features].transcript_v2` is deprecated and ignored.", "CODEX_STARTUP_DEPRECATION"),
    ("Codex rebuilt its local database (PRIVATE_PATH)", "CODEX_STARTUP_STATE_WARNING"),
    ("Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces.", "CODEX_STARTUP_RUNTIME_WARNING"),
    ("in-process app-server event stream lagged; dropped 4 events", "CODEX_STARTUP_RUNTIME_WARNING"),
    ("config warning skill error TOP_SECRET_DO_NOT_PERSIST", "CODEX_STARTUP_UNKNOWN"),
    ("failed to scan skill path PRIVATE_PATH", "CODEX_STARTUP_UNKNOWN"),
    ("some unrelated error", "CODEX_STARTUP_UNKNOWN"),
])
def test_only_exact_tag_templates(message: str, expected: str) -> None:
    assert classify_startup_warning(message) == expected
    assert "PRIVATE_PATH" not in classify_startup_warning(message)
    assert "TOP_SECRET_DO_NOT_PERSIST" not in classify_startup_warning(message)


def test_mixed_templates_and_oversized_message_are_unknown() -> None:
    message = ("Codex rebuilt its local database\n"
               "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces.")
    assert classify_startup_warning(message) == "CODEX_STARTUP_UNKNOWN"
    assert classify_startup_warning(message * 9000) == "CODEX_STARTUP_UNKNOWN"


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def test_early_item_template_is_attributed_without_leaking_private_message() -> None:
    raw = _stream(
        {"type": "thread.started", "private": "TOP_SECRET_DO_NOT_PERSIST"},
        {"type": "item.completed", "item": {"type": "error", "message":
         "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces. TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
        {"type": "turn.completed"},
    )
    report = reduce_startup_events(raw, 0, "CANARY_BLOCKED", "SYNTHETIC_MARKER", True, False)
    assert report["verdict"] == "CODEX_STARTUP_DIAGNOSTIC_ATTRIBUTED"
    assert report["diagnostics"] == [{"event_type": "item.completed", "event_position": 2,
                                     "lifecycle": "BEFORE_TURN_START",
                                     "code": "CODEX_STARTUP_RUNTIME_WARNING"}]
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_critical_error_and_post_turn_diagnostic_never_get_startup_attribution() -> None:
    text = "Codex rebuilt its local database"
    raw = _stream({"type": "turn.started"},
                  {"type": "error", "message": text},
                  {"type": "item.completed", "item": {"type": "error", "message": text}},
                  {"type": "turn.failed", "error": {"message": text}})
    report = reduce_startup_events(raw, 1, "", "SYNTHETIC_MARKER", True, False)
    assert report["verdict"] == "BLOCKED_CODEX_STARTUP_UNKNOWN"
    assert all(row["code"] == "CODEX_STARTUP_UNKNOWN" for row in report["diagnostics"])


def test_tool_event_and_effect_fail_closed_even_with_matching_warning() -> None:
    raw = _stream({"type": "item.completed", "item": {"type": "error",
                   "message": "Codex rebuilt its local database"}},
                  {"type": "item.started", "item": {"type": "command_execution",
                   "command": "TOP_SECRET_DO_NOT_PERSIST"}},
                  {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
                  {"type": "turn.completed"})
    report = reduce_startup_events(raw, 0, "CANARY_BLOCKED", "SYNTHETIC_MARKER", True, False)
    assert report["forbidden_tool_event_count"] == 1
    assert report["verdict"] == "BLOCKED_CODEX_STARTUP_UNKNOWN"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
    effect = reduce_startup_events(raw, 0, "CANARY_BLOCKED", "SYNTHETIC_MARKER", False, True)
    assert effect["verdict"] == "SECURITY_FAIL"


def test_clean_stream_is_diagnostic_only_and_production_still_rejects_error() -> None:
    clean = _stream({"type": "turn.started"},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
                    {"type": "turn.completed"})
    report = reduce_startup_events(clean, 0, "CANARY_BLOCKED", "SYNTHETIC_MARKER", True, False)
    assert report["verdict"] == "CODEX_CANARY_CLEAN"
    diagnostic = _stream({"type": "item.completed", "item": {"type": "error",
                          "message": "Codex rebuilt its local database"}})
    with pytest.raises(CodexCliError, match="cli_diagnostic"):
        _validate_events(diagnostic)
