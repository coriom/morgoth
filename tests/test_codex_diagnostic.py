"""No-inference tests for the one-shot diagnostic-only Codex reducer."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import diagnose_codex_cli as D
from core.llm import codex_cli as C


def stream(*events: dict[str, object]) -> str:
    """Build synthetic JSONL without logging its private contents."""
    return "\n".join(json.dumps(event) for event in events)


@pytest.mark.parametrize(("message", "expected"), [
    ("failed to parse config TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_CONFIG"),
    ("failed to load skills TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_SKILL_DISCOVERY"),
    ("unknown feature TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_UNSUPPORTED_FEATURE"),
    ("sandbox resource unavailable TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_SANDBOX_RESOURCE"),
    ("not logged in TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_AUTH_SESSION"),
    ("model unavailable TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_MODEL_SERVICE"),
    ("tls handshake failed TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_NETWORK"),
    ("skill and config and TOP_SECRET_DO_NOT_PERSIST", "CODEX_DIAG_UNKNOWN"),
    ("failed to load skills; failed to parse config", "CODEX_DIAG_UNKNOWN"),
])
def test_fixed_private_classifier_never_returns_message(message: str, expected: str) -> None:
    assert D.classify_message(message) == expected
    assert "TOP_SECRET_DO_NOT_PERSIST" not in D.classify_message(message)


def test_item_error_lifecycle_and_sanitization() -> None:
    raw = stream(
        {"type": "thread.started", "thread_id": "TOP_SECRET_DO_NOT_PERSIST"},
        {"type": "item.completed", "item": {"type": "error", "message": "failed to load skills TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "error", "message": "failed to load skills TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
        {"type": "turn.completed"},
    )
    report = D.classify_events(raw, 0, "CANARY_BLOCKED", "SYNTHETIC_MARKER", True, False)
    assert report["process_exited_successfully"] is True
    assert report["turn_completed"] is True
    assert report["final_assistant_present"] is True
    assert report["expected_canary_response"] is True
    assert report["diagnostic_count"] == 2
    assert report["diagnostics"] == [
        {"event_type": "item.completed", "event_position": 2,
         "lifecycle": "BEFORE_TURN_START", "code": "CODEX_DIAG_SKILL_DISCOVERY"},
        {"event_type": "item.completed", "event_position": 4,
         "lifecycle": "AFTER_TURN_START", "code": "CODEX_DIAG_SKILL_DISCOVERY"},
    ]
    assert D.verdict(report) == "CODEX_DIAGNOSTIC_ATTRIBUTED"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
    assert "SYNTHETIC_MARKER" not in json.dumps(report)


def test_top_level_failure_and_tool_items_remain_distinct() -> None:
    raw = stream(
        {"type": "turn.started"},
        {"type": "item.started", "item": {"type": "command_execution", "command": "TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "error", "message": "connection refused TOP_SECRET_DO_NOT_PERSIST"},
        {"type": "turn.failed", "error": {"message": "model unavailable TOP_SECRET_DO_NOT_PERSIST"}},
    )
    report = D.classify_events(raw, 1, "", "SYNTHETIC_MARKER", True, False)
    assert report["turn_completed"] is False
    assert report["forbidden_tool_event_count"] == 1
    assert report["diagnostics"] == [
        {"event_type": "error", "event_position": 3,
         "lifecycle": "AFTER_TURN_START", "code": "CODEX_DIAG_NETWORK"},
        {"event_type": "turn.failed", "event_position": 4,
         "lifecycle": "AFTER_TURN_FAILED", "code": "CODEX_DIAG_MODEL_SERVICE"},
    ]
    assert D.verdict(report) == "BLOCKED_CODEX_DIAGNOSTIC_UNKNOWN"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_unknown_shapes_and_observed_effect_fail_closed() -> None:
    raw = stream({"type": "item.completed", "item": {"type": "mystery", "value": "TOP_SECRET_DO_NOT_PERSIST"}},
                 {"type": "future.event", "message": "TOP_SECRET_DO_NOT_PERSIST"})
    unknown = D.classify_events(raw, 0, "", "SYNTHETIC_MARKER", True, False)
    assert unknown["unknown_event_count"] == 2
    assert D.verdict(unknown) == "BLOCKED_CODEX_DIAGNOSTIC_UNKNOWN"
    effect = D.classify_events(raw, 0, "", "SYNTHETIC_MARKER", True, True)
    assert D.verdict(effect) == "SECURITY_FAIL"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(effect)


def test_missing_diagnostic_and_overflow_cannot_be_attributed() -> None:
    clean = D.classify_events(stream({"type": "turn.started"}, {"type": "turn.completed"}),
                              0, "CANARY_BLOCKED", "SYNTHETIC_MARKER", True, False)
    assert D.verdict(clean) == "BLOCKED_CODEX_DIAGNOSTIC_UNKNOWN"
    oversized = D.classify_events("x" * 4_000_001, 0, "", "SYNTHETIC_MARKER", True, False)
    assert oversized["diagnostics"] == [{"event_type": "UNKNOWN", "event_position": 0,
                                         "lifecycle": "UNKNOWN", "code": "CODEX_DIAG_UNKNOWN"}]
    assert D.verdict(oversized) == "BLOCKED_CODEX_DIAGNOSTIC_UNKNOWN"


def test_original_production_event_policy_still_rejects_diagnostics() -> None:
    raw = stream({"type": "turn.started"},
                 {"type": "item.completed", "item": {"type": "error", "message": "failed to load skills"}},
                 {"type": "turn.completed"})
    with pytest.raises(C.CodexCliError, match="cli_diagnostic"):
        C._validate_events(raw)


def test_one_shot_runner_uses_actual_argv_and_emits_only_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert argv == C.build_argv("codex", "default", f"{cwd}/final.txt")
        assert "TOP_SECRET_DO_NOT_PERSIST" not in prompt
        assert timeout == 90
        return subprocess.CompletedProcess(argv, 0, stream(
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "error", "message": "failed to load skills TOP_SECRET_DO_NOT_PERSIST"}},
            {"type": "turn.completed"},
        ), "TOP_SECRET_DO_NOT_PERSIST")
    monkeypatch.setattr(C, "_run", fake_run)
    result = D.run_one_canary("codex", {})
    assert len(calls) == 1
    assert result["verdict"] == "CODEX_DIAGNOSTIC_ATTRIBUTED"
    assert result["outside_markers_unchanged"] is True
    assert result["forbidden_tool_event_count"] == 0
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(result)


def test_startup_runner_classifies_only_private_source_template(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        Path(cwd, "final.txt").write_text("CANARY_BLOCKED", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stream(
            {"type": "thread.started"},
            {"type": "item.completed", "item": {"type": "error", "message":
                "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces. TOP_SECRET_DO_NOT_PERSIST"}},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
            {"type": "turn.completed"}), "")
    monkeypatch.setattr(C, "_run", fake_run)
    result = D.run_one_canary("codex", {}, startup_analysis=True)
    assert calls == 1
    # The private canary suffix makes this an unknown whole message.  An
    # otherwise recognizable prefix must not become an attributed warning.
    assert result["verdict"] == "BLOCKED_CODEX_STARTUP_UNKNOWN"
    assert result["diagnostics"] == [{"event_type": "item.completed",
                                     "event_position": 2,
                                     "lifecycle": "BEFORE_TURN_START",
                                     "code": "CODEX_STARTUP_UNKNOWN"}]
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(result)
