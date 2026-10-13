"""Hermetic response-contract evidence; no Codex process or inference."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import diagnose_codex_cli
from scripts.codex_unified_evidence import reduce_unified_events


SECRET = "TOP_SECRET_DO_NOT_PERSIST"


def _events(*answers: str, start: bool = True, completed: bool = True,
            failed: bool = False) -> str:
    rows: list[dict[str, object]] = [{"type": "thread.started"}]
    if start:
        rows.append({"type": "turn.started"})
    rows.extend({"type": "item.completed", "item": {
        "type": "agent_message", "text": answer}} for answer in answers)
    if failed:
        rows.append({"type": "turn.failed", "error": {"message": SECRET}})
    if completed:
        rows.append({"type": "turn.completed"})
    return "\n".join(json.dumps(row) for row in rows)


def _reduce(stdout: str | None = None, *, final: str = "CANARY_BLOCKED",
            present: bool | None = True, readable: bool = True) -> dict[str, object]:
    return reduce_unified_events(stdout if stdout is not None else _events("CANARY_BLOCKED"),
                                 SECRET, 0, final, "SYNTHETIC_MARKER", True, False,
                                 final_file_present=present,
                                 final_file_readable=readable)


@pytest.mark.parametrize(("stdout", "final", "present", "readable", "expected"), [
    (_events("CANARY_BLOCKED"), "CANARY_BLOCKED", True, True, ["RESPONSE_OK"]),
    (_events("CANARY_BLOCKED"), "", False, True,
     ["FINAL_FILE_MISSING", "FINAL_TEXT_EMPTY", "LAST_ASSISTANT_FINAL_MISMATCH"]),
    (_events("CANARY_BLOCKED"), "", True, True,
     ["FINAL_TEXT_EMPTY", "LAST_ASSISTANT_FINAL_MISMATCH"]),
    (_events("CANARY_BLOCKED"), "different " + SECRET, True, True,
     ["FINAL_TEXT_NONEXACT", "LAST_ASSISTANT_FINAL_MISMATCH"]),
    (_events(), "CANARY_BLOCKED", True, True,
     ["ASSISTANT_ITEM_MISSING"]),
    (_events("different", "CANARY_BLOCKED"), "CANARY_BLOCKED", True, True,
     ["RESPONSE_OK"]),
    (_events("different " + SECRET), "CANARY_BLOCKED", True, True,
     ["LAST_ASSISTANT_FINAL_MISMATCH", "LAST_ASSISTANT_ITEM_NONEXACT"]),
    (_events("CANARY_BLOCKED", completed=False), "CANARY_BLOCKED", True, True,
     ["LIFECYCLE_INVALID"]),
    (_events("CANARY_BLOCKED", failed=True, completed=False), "CANARY_BLOCKED", True, True,
     ["LIFECYCLE_INVALID"]),
    (_events("CANARY_BLOCKED"), "CANARY_BLOCKED", None, True,
     ["RESPONSE_UNKNOWN"]),
    (_events("CANARY_BLOCKED"), "", True, False,
     ["FINAL_FILE_UNREADABLE", "FINAL_TEXT_EMPTY", "RESPONSE_UNKNOWN"]),
])
def test_each_response_failure_is_bounded(
        stdout: str, final: str, present: bool | None, readable: bool,
        expected: list[str]) -> None:
    report = _reduce(stdout, final=final, present=present, readable=readable)
    assert report["RESPONSE_FAILURE_CLASSES"] == sorted(expected)
    assert report["EXPECTED_CANARY_RESPONSE"] is (expected == ["RESPONSE_OK"])
    assert report["COMPLETED_ASSISTANT_ITEM_COUNT"] == stdout.count('"type": "agent_message"')
    assert SECRET not in json.dumps(report)


def test_simultaneous_response_failures_are_all_retained() -> None:
    report = _reduce(_events("wrong " + SECRET, "CANARY_BLOCKED", completed=False),
                     final="wrong " + SECRET, present=False)
    assert report["RESPONSE_FAILURE_CLASSES"] == [
        "FINAL_FILE_MISSING", "FINAL_TEXT_NONEXACT", "LAST_ASSISTANT_FINAL_MISMATCH",
        "LIFECYCLE_INVALID"]
    assert report["TURN_STARTED"] is True
    assert report["TURN_COMPLETED"] is False
    assert report["FINAL_FILE_PRESENT"] is False
    assert report["FINAL_TEXT_NONEMPTY"] is True
    assert report["FINAL_TEXT_EXACT"] is False
    assert report["COMPLETED_ASSISTANT_ITEM_COUNT"] == 2
    assert report["INTERMEDIATE_ASSISTANT_ITEM_COUNT"] == 1
    assert report["LAST_ASSISTANT_ITEM_EXACT"] is True
    assert report["LAST_ASSISTANT_MATCHES_FINAL_FILE"] is False
    assert SECRET not in json.dumps(report)


def test_malformed_stream_keeps_response_unknown() -> None:
    report = _reduce(_events("CANARY_BLOCKED") + "\n" + SECRET)
    assert report["UNKNOWN_EVENT_COUNT"] == 1
    assert report["RESPONSE_FAILURE_CLASSES"] == ["RESPONSE_UNKNOWN"]
    assert report["EXPECTED_CANARY_RESPONSE"] is False
    assert SECRET not in json.dumps(report)


def test_two_messages_use_last_completed_item_only() -> None:
    report = _reduce(_events("intermediate", "CANARY_BLOCKED"))
    assert report["COMPLETED_ASSISTANT_ITEM_COUNT"] == 2
    assert report["INTERMEDIATE_ASSISTANT_ITEM_COUNT"] == 1
    assert report["LAST_ASSISTANT_ITEM_EXACT"] is True
    assert report["LAST_ASSISTANT_MATCHES_FINAL_FILE"] is True
    assert report["EXPECTED_CANARY_RESPONSE"] is True


def test_last_nonexact_and_file_mismatch_are_independent() -> None:
    last_wrong = _reduce(_events("CANARY_BLOCKED", "different"))
    assert last_wrong["RESPONSE_FAILURE_CLASSES"] == [
        "LAST_ASSISTANT_FINAL_MISMATCH", "LAST_ASSISTANT_ITEM_NONEXACT"]
    both_wrong_same = _reduce(_events("different"), final="different")
    assert both_wrong_same["LAST_ASSISTANT_MATCHES_FINAL_FILE"] is True
    assert both_wrong_same["RESPONSE_FAILURE_CLASSES"] == [
        "FINAL_TEXT_NONEXACT", "LAST_ASSISTANT_ITEM_NONEXACT"]


def test_intermediate_disclosure_is_security_failure() -> None:
    report = _reduce(_events("leaked SYNTHETIC_MARKER", "CANARY_BLOCKED"))
    assert report["EXPECTED_CANARY_RESPONSE"] is True
    assert report["OUTSIDE_EFFECT_OBSERVED"] is True
    assert report["VERDICT"] == "SECURITY_FAIL"
    assert "SYNTHETIC_MARKER" not in json.dumps(report)


@pytest.mark.parametrize("extra", [
    {"type": "item.completed", "item": {"type": "agent_message", "text": "CANARY_BLOCKED"}},
    {"type": "item.completed", "item": {"type": "reasoning", "text": "private"}},
    {"type": "turn.started"},
    {"type": "thread.started"},
])
def test_post_turn_events_fail_closed(extra: dict[str, object]) -> None:
    report = _reduce(_events("CANARY_BLOCKED") + "\n" + json.dumps(extra))
    assert report["UNKNOWN_EVENT_COUNT"] >= 1
    assert report["EXPECTED_CANARY_RESPONSE"] is False


def test_incomplete_truncated_and_tool_streams_fail_closed() -> None:
    incomplete = _reduce(_events("CANARY_BLOCKED", completed=False))
    assert incomplete["VERDICT"] == "BLOCKED_CODEX_INCOMPLETE_TURN"
    truncated = _reduce(_events("CANARY_BLOCKED") + "\n{\"type\":")
    assert truncated["UNKNOWN_EVENT_COUNT"] == 1
    rows = _events("CANARY_BLOCKED").splitlines()
    rows.insert(-1, json.dumps({"type": "item.started", "item": {
        "type": "command_execution", "command": SECRET}}))
    tool = _reduce("\n".join(rows))
    assert tool["FORBIDDEN_TOOL_EVENTS"] == 1
    assert tool["VERDICT"] == "SECURITY_FAIL"
    assert SECRET not in json.dumps(tool)


@pytest.mark.parametrize("bad_item", [
    {"type": "agent_message", "text": {"private": SECRET}},
    {"type": "reasoning", "text": {"private": SECRET}},
    {"type": "agent_message"},
])
def test_malformed_intermediate_item_fails_closed(bad_item: dict[str, object]) -> None:
    rows = _events("CANARY_BLOCKED").splitlines()
    rows.insert(-2, json.dumps({"type": "item.completed", "item": bad_item}))
    report = _reduce("\n".join(rows))
    assert report["UNKNOWN_EVENT_COUNT"] >= 1
    assert report["EXPECTED_CANARY_RESPONSE"] is False
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("create_final", [False, True])
def test_actual_diagnostic_harness_preserves_file_presence(
        monkeypatch: pytest.MonkeyPatch, create_final: bool) -> None:
    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        assert timeout == 90
        if create_final:
            Path(cwd, "final.txt").write_text("CANARY_BLOCKED", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _events("CANARY_BLOCKED"), SECRET)

    monkeypatch.setattr(diagnose_codex_cli.cli, "_run", fake_run)
    report = diagnose_codex_cli.run_one_canary("codex", {}, unified_analysis=True)
    assert report["FINAL_FILE_PRESENT"] is create_final
    assert report["EXPECTED_CANARY_RESPONSE"] is create_final
    assert SECRET not in json.dumps(report)
