"""No-inference checks for private Codex 0.162.1 critical-event reduction."""
from __future__ import annotations

import json
import subprocess

import pytest

from core.llm.codex_cli import CodexCliError, _validate_events
from scripts.codex_critical_errors import (classify_fatal_message,
                                           critical_verdict, reduce_critical_events)
from scripts import diagnose_codex_cli


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _reduce(stream: str, *, rc: int = 1, stderr: str = "") -> dict[str, object]:
    return reduce_critical_events(stream, stderr, rc, "", "SYNTHETIC_MARKER", True, False)


@pytest.mark.parametrize(("message", "code"), [
    ("Selected model is at capacity. Please try a different model.", "CODEX_FATAL_MODEL_SERVICE"),
    ("session configured event was not the first event in the stream", "CODEX_FATAL_CONFIG"),
    ("codex-linux-sandbox was required but not provided", "CODEX_FATAL_RUNTIME_RESOURCE"),
    ("Quota exceeded. Check your plan and billing details.", "CODEX_FATAL_AUTH"),
    ("stream disconnected before completion: Transport error: network failure", "CODEX_FATAL_UNKNOWN"),
    ("error: auth network config TOP_SECRET_DO_NOT_PERSIST", "CODEX_FATAL_UNKNOWN"),
])
def test_exact_source_templates_only(message: str, code: str) -> None:
    assert classify_fatal_message(message) == code
    assert "TOP_SECRET_DO_NOT_PERSIST" not in classify_fatal_message(message)


def test_early_warning_is_separate_from_critical_failure() -> None:
    stream = _stream(
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "error", "message": "TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "turn.started"},
        {"type": "error", "message": "Selected model is at capacity. Please try a different model. TOP_SECRET_DO_NOT_PERSIST"},
        {"type": "turn.failed", "error": {"message": "Selected model is at capacity. Please try a different model."}},
    )
    report = _reduce(stream, stderr="TOP_SECRET_DO_NOT_PERSIST")
    assert report["startup_warning_count"] == 1
    assert report["critical_count"] == 2
    assert report["first_critical_position"] == 4
    assert report["turn_started"] is True and report["turn_failed"] is True
    # The extra suffix makes the first critical message non-exact; both
    # critical records must agree before an attribution is published.
    assert report["critical_class"] == "CODEX_FATAL_UNKNOWN"
    assert critical_verdict(report) == "BLOCKED_CODEX_FATAL_UNKNOWN"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_two_matching_critical_events_can_be_attributed() -> None:
    message = "session configured event was not the first event in the stream"
    report = _reduce(_stream({"type": "turn.started"},
                             {"type": "error", "message": message},
                             {"type": "turn.failed", "error": {"message": message}}))
    assert report["critical_class"] == "CODEX_FATAL_CONFIG"
    assert report["failure_preceded_model_output"] is True
    assert critical_verdict(report) == "BLOCKED_CODEX_FATAL_ATTRIBUTED"


def test_malformed_unknown_and_forbidden_events_remain_visible_as_counts() -> None:
    report = _reduce("{bad-json\n" + _stream(
        {"type": "item.started", "item": {"type": "command_execution", "command": "TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "unexpected", "secret": "TOP_SECRET_DO_NOT_PERSIST"},
        {"type": "error", "message": "unrecognized private failure"}))
    assert report["unknown_event_count"] == 2
    assert report["forbidden_tool_events"] == 1
    assert report["critical_class"] == "CODEX_FATAL_UNKNOWN"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_warning_only_and_incomplete_turn_never_count_as_clean() -> None:
    warning = _stream({"type": "item.completed", "item": {"type": "error", "message": "warning"}})
    report = _reduce(warning, rc=0)
    assert report["critical_count"] == 0 and report["critical_class"] is None
    assert critical_verdict(report) == "BLOCKED_CODEX_STARTUP_UNKNOWN"
    incomplete = _reduce(_stream({"type": "turn.started"}), rc=0)
    assert incomplete["turn_completed"] is False
    assert critical_verdict(incomplete) == "BLOCKED_CODEX_FATAL_UNKNOWN"


def test_unknown_source_mixed_categories_and_oversized_are_unknown() -> None:
    mixed = ("Selected model is at capacity. Please try a different model.\n"
             "session configured event was not the first event in the stream")
    assert classify_fatal_message(mixed) == "CODEX_FATAL_UNKNOWN"
    assert classify_fatal_message(mixed * 9000) == "CODEX_FATAL_UNKNOWN"


def test_effect_and_production_validator_remain_fail_closed() -> None:
    raw = _stream({"type": "error", "message": "Selected model is at capacity. Please try a different model."})
    report = reduce_critical_events(raw, "", 1, "", "SYNTHETIC_MARKER", False, True)
    assert critical_verdict(report) == "SECURITY_FAIL"
    with pytest.raises(CodexCliError):
        _validate_events(raw)


def test_actual_diagnostic_entrypoint_redacts_private_process_output(monkeypatch) -> None:
    calls = 0
    def fake_run(argv, cwd, env, prompt, timeout):
        nonlocal calls
        calls += 1
        assert timeout == 90
        return subprocess.CompletedProcess(
            argv, 1, _stream({"type": "turn.started"},
                             {"type": "error", "message": "TOP_SECRET_DO_NOT_PERSIST"},
                             {"type": "turn.failed", "error": {"message": "TOP_SECRET_DO_NOT_PERSIST"}}),
            "TOP_SECRET_DO_NOT_PERSIST")
    monkeypatch.setattr(diagnose_codex_cli.cli, "_run", fake_run)
    report = diagnose_codex_cli.run_one_canary("/synthetic/codex", {},
                                               critical_analysis=True)
    assert calls == 1
    assert report["verdict"] == "BLOCKED_CODEX_FATAL_UNKNOWN"
    assert report["critical_count"] == 2
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
