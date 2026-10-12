"""Hermetic, no-inference checks for bounded Codex transport diagnostics."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from core.llm import codex_cli as cli
from scripts import diagnose_codex_cli
from scripts.codex_critical_errors import classify_codex_error_family
from scripts.codex_unified_evidence import reduce_unified_events


def _events(*events: dict[str, object]) -> str:
    """Encode private synthetic events without publishing their contents."""
    return "\n".join(json.dumps(event) for event in events)


def _turn(answer: str, *middle: dict[str, object]) -> str:
    """Build one completed synthetic text turn."""
    return _events({"type": "thread.started"}, *middle,
                   {"type": "turn.started"},
                   {"type": "item.completed", "item": {"type": "agent_message",
                                                         "text": answer}},
                   {"type": "turn.completed"})


def _reduce(raw: str, *, rc: int = 0, answer: str = "CANARY_BLOCKED",
            text: bool = False, unchanged: bool = True) -> dict[str, object]:
    """Reduce synthetic process data while keeping marker and text private."""
    return reduce_unified_events(raw, "", rc, answer, "SYNTHETIC_MARKER",
                                 unchanged, False,
                                 expected_text="CODEX_PROVIDER_OK" if text else None)


@pytest.mark.parametrize(("message", "expected"), [
    ("stream disconnected before completion: Incomplete response returned, reason: content_filter",
     "CODEX_ERROR_CONTENT_FILTER"),
    ("stream disconnected before completion: private transport detail TOP_SECRET_DO_NOT_PERSIST",
     "CODEX_ERROR_STREAM_DISCONNECTED"),
    ("rate limit exceeded: TOP_SECRET_DO_NOT_PERSIST",
     "CODEX_ERROR_RATE_LIMIT_REPORTED"),
    ("request timed out", "CODEX_ERROR_REQUEST_TIMEOUT"),
    ("Selected model is at capacity. Please try a different model.",
     "CODEX_ERROR_SERVER_OVERLOADED"),
    ("Quota exceeded. Check your plan and billing details.", "CODEX_ERROR_QUOTA"),
    ("Connection failed: TOP_SECRET_DO_NOT_PERSIST",
     "CODEX_ERROR_CONNECTION_FAILED"),
    ("prefix stream disconnected before completion: detail", "CODEX_ERROR_UNKNOWN"),
    ("stream disconnected before completion: ", "CODEX_ERROR_UNKNOWN"),
    ("rate limit exceeded: ", "CODEX_ERROR_UNKNOWN"),
    ("request timed out TOP_SECRET_DO_NOT_PERSIST", "CODEX_ERROR_UNKNOWN"),
    ("network auth rate limit TOP_SECRET_DO_NOT_PERSIST", "CODEX_ERROR_UNKNOWN"),
])
def test_exact_source_error_families_only(message: str, expected: str) -> None:
    assert classify_codex_error_family(message) == expected
    assert "TOP_SECRET_DO_NOT_PERSIST" not in classify_codex_error_family(message)
    assert classify_codex_error_family(message * 9000) == "CODEX_ERROR_UNKNOWN"


def test_lifecycle_positions_and_secret_sanitization() -> None:
    raw = _events(
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "error",
         "message": "Codex rebuilt its local database (TOP_SECRET_DO_NOT_PERSIST)"}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "error",
         "message": "private diagnostic TOP_SECRET_DO_NOT_PERSIST"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
        {"type": "turn.completed"},
        {"type": "item.completed", "item": {"type": "error",
         "message": "private post-turn diagnostic TOP_SECRET_DO_NOT_PERSIST"}},
    )
    report = _reduce(raw)
    assert [report[f"{phase}_DIAGNOSTIC_COUNT"] for phase in
            ("PRE_TURN", "IN_TURN", "POST_TURN")] == [1, 1, 1]
    assert [row["position"] for row in report["DIAGNOSTIC_EVENTS"]] == [2, 4, 7]
    assert [row["lifecycle"] for row in report["DIAGNOSTIC_EVENTS"]] == [
        "PRE_TURN_DIAGNOSTIC", "IN_TURN_DIAGNOSTIC", "POST_TURN_DIAGNOSTIC"]
    assert report["CRITICAL_ERROR_COUNT"] == 0
    assert report["EXPECTED_CANARY_RESPONSE"] is True
    assert report["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_group_repeated_critical_messages_without_exposing_values() -> None:
    a = "stream disconnected before completion: TOP_SECRET_DO_NOT_PERSIST"
    b = "request timed out"
    raw = _events({"type": "turn.started"},
                  {"type": "error", "message": a},
                  {"type": "error", "message": a},
                  {"type": "error", "message": b},
                  {"type": "turn.failed", "error": {"message": a}})
    report = _reduce(raw, rc=1, answer="")
    assert report["TOP_LEVEL_ERROR_COUNT"] == 3
    assert report["TURN_FAILED_COUNT"] == 1
    assert report["CRITICAL_ERROR_COUNT"] == 4
    assert report["DISTINCT_CRITICAL_MESSAGE_COUNT"] == 2
    assert report["LARGEST_REPEATED_MESSAGE_GROUP"] == 3
    assert report["FIRST_CRITICAL_POSITION"] == 2
    assert report["LAST_CRITICAL_POSITION"] == 5
    assert report["CRITICAL_ERROR_CLASSES"] == [
        "CODEX_ERROR_REQUEST_TIMEOUT", "CODEX_ERROR_STREAM_DISCONNECTED"]
    assert report["VERDICT"] == "BLOCKED_CODEX_FATAL"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


@pytest.mark.parametrize("raw", ["{broken", _events({"type": "turn.started"}),
                                   _events({"type": "future.event"}), ""])
def test_malformed_truncated_and_unknown_streams_fail_closed(raw: str) -> None:
    assert _reduce(raw)["VERDICT"] not in {
        "CODEX_CANARY_CLEAN", "CODEX_CANARY_WARNINGS_ATTRIBUTED"}


def test_text_exact_response_digest_and_tool_event() -> None:
    raw = _turn("CODEX_PROVIDER_OK")
    report = _reduce(raw, answer="CODEX_PROVIDER_OK", text=True)
    assert report["EXPECTED_TEXT_RESPONSE"] is True
    assert report["RESPONSE_BYTES"] == len("CODEX_PROVIDER_OK")
    assert report["RESPONSE_SHA256"] == hashlib.sha256(b"CODEX_PROVIDER_OK").hexdigest()
    assert report["VERDICT"] == "CODEX_TEXT_TRANSPORT_FUNCTIONAL"
    wrong = _reduce(raw, answer="WRONG", text=True)
    assert wrong["VERDICT"] == "BLOCKED_CODEX_TEXT_RESPONSE"
    attempted = _reduce(_turn("CODEX_PROVIDER_OK", {"type": "item.started",
                       "item": {"type": "command_execution",
                                "command": "TOP_SECRET_DO_NOT_PERSIST"}}),
                        answer="CODEX_PROVIDER_OK", text=True)
    assert attempted["FORBIDDEN_TOOL_EVENTS"] == 1
    assert attempted["VERDICT"] == "SECURITY_FAIL"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(attempted)


def test_text_failure_classes_and_incomplete_turn() -> None:
    message = "request timed out"
    raw = _events({"type": "turn.started"}, {"type": "error", "message": message},
                  {"type": "turn.failed", "error": {"message": message}})
    classified = _reduce(raw, rc=1, answer="", text=True)
    assert classified["VERDICT"] == "CODEX_TEXT_TRANSPORT_BLOCKED_CLASSIFIED"
    assert classified["CRITICAL_ERROR_CLASSES"] == ["CODEX_ERROR_REQUEST_TIMEOUT"]
    malformed = _reduce(raw + "\n{bad-json", rc=1, answer="", text=True)
    assert malformed["VERDICT"] == "CODEX_TEXT_TRANSPORT_BLOCKED_UNKNOWN"
    unknown = _reduce(_events({"type": "turn.started"}, {"type": "error",
                      "message": "private TOP_SECRET_DO_NOT_PERSIST"}), rc=1,
                      answer="", text=True)
    assert unknown["VERDICT"] == "CODEX_TEXT_TRANSPORT_BLOCKED_UNKNOWN"
    incomplete = _reduce(_events({"type": "turn.started"}), answer="", text=True)
    assert incomplete["VERDICT"] == "BLOCKED_CODEX_INCOMPLETE_TURN"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(unknown)


def test_resource_preflight_is_non_inference_and_fails_with_fixed_code(
        monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def sandbox(binary: str, cwd: str, env: dict[str, str],
                args: list[str]) -> tuple[list[str], dict[str, str]]:
        assert args == ["--version"]
        return ["/usr/bin/bwrap", "--", "/opt/codex/bin/codex", *args], {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return subprocess.CompletedProcess(argv, 5, "TOP_SECRET_DO_NOT_PERSIST", "")

    monkeypatch.setattr(diagnose_codex_cli, "build_sandbox_argv", sandbox)
    monkeypatch.setattr(diagnose_codex_cli.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="^PREFLIGHT_RUNTIME_RESOURCE$"):
        diagnose_codex_cli.preflight_text_runtime("codex", {})
    assert len(captured) == 1
    assert captured[0][captured[0].index("--") + 1] == "/usr/bin/python3"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in repr(captured)


def test_text_entrypoint_uses_one_production_argv_and_private_output(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        assert argv == cli.build_argv("codex", "default", str(Path(cwd) / "final.txt"))
        assert prompt == "Reply with exactly CODEX_PROVIDER_OK."
        assert timeout == 90
        Path(cwd, "final.txt").write_text("CODEX_PROVIDER_OK", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _turn("CODEX_PROVIDER_OK",
            {"type": "item.completed", "item": {"type": "error",
             "message": "Codex rebuilt its local database (TOP_SECRET_DO_NOT_PERSIST)"}}),
            "TOP_SECRET_DO_NOT_PERSIST")

    monkeypatch.setattr(diagnose_codex_cli.cli, "_run", fake_run)
    report = diagnose_codex_cli.run_one_text_probe("codex", {})
    assert calls == 1
    assert report["PRE_TURN_DIAGNOSTIC_COUNT"] == 1
    assert report["VERDICT"] == "CODEX_TEXT_TRANSPORT_FUNCTIONAL"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_old_canary_entrypoint_still_uses_one_call_and_sanitizes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        assert argv == cli.build_argv("codex", "default", str(Path(cwd) / "final.txt"))
        assert timeout == 90
        Path(cwd, "final.txt").write_text("CANARY_BLOCKED", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _turn("CANARY_BLOCKED",
            {"type": "item.completed", "item": {"type": "error",
             "message": "Codex rebuilt its local database (TOP_SECRET_DO_NOT_PERSIST)"}}), "")

    monkeypatch.setattr(diagnose_codex_cli.cli, "_run", fake_run)
    report = diagnose_codex_cli.run_one_canary("codex", {}, unified_analysis=True)
    assert calls == 1
    assert report["PRE_TURN_DIAGNOSTIC_COUNT"] == 1
    assert report["EXPECTED_CANARY_RESPONSE"] is True
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
