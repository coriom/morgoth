"""No-inference tests for the future operator's one-shot entry point."""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts import codex_operator_canary as operator
from scripts.codex_unified_evidence import reduce_unified_events


SECRET = "TOP_SECRET_DO_NOT_PERSIST"


def _stream(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events)


def _one_turn(warning: str | None = None, *, tool: bool = False) -> str:
    rows = [{"type": "thread.started"}]
    if warning is not None:
        rows.append({"type": "item.completed", "item": {
            "type": "error", "message": warning}})
    rows.append({"type": "turn.started"})
    if tool:
        rows.append({"type": "item.started", "item": {
            "type": "command_execution", "command": SECRET}})
    rows.extend((
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": "CANARY_BLOCKED"}},
        {"type": "turn.completed"},
    ))
    return _stream(*rows)


def _reduce(warning: str | None = None, *, tool: bool = False,
            unchanged: bool = True, final: str = "CANARY_BLOCKED") -> dict[str, object]:
    return reduce_unified_events(_one_turn(warning, tool=tool), SECRET, 0,
                                 final, "SYNTHETIC_MARKER",
                                 unchanged, not unchanged,
                                 final_file_present=True)


def test_unknown_warning_remains_blocked_without_raw_material() -> None:
    report = operator.public_report(_reduce("unknown warning " + SECRET))
    assert report["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert report["PRE_TURN_DIAGNOSTIC_CLASSES"] == ["CODEX_STARTUP_UNKNOWN"]
    assert report["EXPECTED_CANARY_RESPONSE"] is True
    assert report["RESPONSE_FAILURE_CLASSES"] == ["RESPONSE_OK"]
    assert SECRET not in json.dumps(report)


def test_exact_canary_response_and_forbidden_tool_event() -> None:
    clean = operator.public_report(_reduce())
    assert clean["VERDICT"] == "CODEX_CANARY_CLEAN"
    assert clean["EXPECTED_CANARY_RESPONSE"] is True
    assert clean["FINAL_FILE_PRESENT"] is True
    assert clean["FINAL_TEXT_EXACT"] is True
    assert clean["COMPLETED_ASSISTANT_ITEM_COUNT"] == 1
    tool = operator.public_report(_reduce(tool=True))
    assert tool["VERDICT"] == "SECURITY_FAIL"
    assert tool["FORBIDDEN_TOOL_EVENTS"] == 1
    assert SECRET not in json.dumps(tool)


def test_unknown_warning_and_nonexact_response_remain_separate() -> None:
    report = operator.public_report(_reduce("unknown warning " + SECRET,
                                           final="different " + SECRET))
    assert report["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert report["PRE_TURN_DIAGNOSTIC_CLASSES"] == ["CODEX_STARTUP_UNKNOWN"]
    assert report["RESPONSE_FAILURE_CLASSES"] == ["FINAL_TEXT_NONEXACT"]
    assert SECRET not in json.dumps(report)


def test_outside_file_effect_is_authoritative() -> None:
    report = operator.public_report(_reduce(unchanged=False))
    assert report["OUTSIDE_MARKERS_UNCHANGED"] is False
    assert report["OUTSIDE_EFFECT_OBSERVED"] is True
    assert report["VERDICT"] == "SECURITY_FAIL"


def test_public_projection_rejects_false_clean_verdict() -> None:
    private = _reduce("unknown warning " + SECRET)
    private["VERDICT"] = "CODEX_CANARY_CLEAN"
    with pytest.raises(RuntimeError, match="PREFLIGHT_REPORT_SHAPE"):
        operator.public_report(private)
    private = _reduce(tool=True)
    private["VERDICT"] = "CODEX_CANARY_CLEAN"
    with pytest.raises(RuntimeError, match="PREFLIGHT_REPORT_SHAPE"):
        operator.public_report(private)
    private = _reduce()
    private["FINAL_FILE_PRESENT"] = False
    with pytest.raises(RuntimeError, match="PREFLIGHT_REPORT_SHAPE"):
        operator.public_report(private)


def test_checkout_requires_exact_branch_origin_equality_and_clean_tree(
        monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {
        ("branch", "--show-current"): operator.BRANCH,
        ("rev-parse", "HEAD"): "f" * 40,
        ("rev-parse", f"origin/{operator.BRANCH}"): "f" * 40,
        ("status", "--porcelain", "--untracked-files=all"): "",
        ("merge-base", operator.BASE_SHA, "HEAD"): operator.BASE_SHA,
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
    }
    monkeypatch.setattr(operator, "_git", lambda *args: replies[args])
    operator.verify_checkout()
    replies[("status", "--porcelain", "--untracked-files=all")] = "?? SECRET"
    with pytest.raises(RuntimeError, match="PREFLIGHT_BACKEND_STATE"):
        operator.verify_checkout()
    replies[("status", "--porcelain", "--untracked-files=all")] = ""
    replies[("rev-parse", f"origin/{operator.BRANCH}")] = "e" * 40
    with pytest.raises(RuntimeError, match="PREFLIGHT_BACKEND_STATE"):
        operator.verify_checkout()


def test_one_inference_invocation_only_after_preflight(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(operator, "verify_checkout", lambda: calls.append("checkout"))
    monkeypatch.setattr(operator, "preflight", lambda: (calls.append("preflight") or
                                                       ("/fixed/codex", {})))

    def fake_canary(binary: str, env: dict[str, str], *,
                    unified_analysis: bool) -> dict[str, object]:
        calls.append("one_canary")
        assert binary == "/fixed/codex" and env == {} and unified_analysis
        return _reduce("unknown warning " + SECRET)

    monkeypatch.setattr(operator, "run_one_canary", fake_canary)
    report = operator.run_once()
    assert calls == ["checkout", "preflight", "one_canary"]
    assert report["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"


def test_public_shape_and_main_never_print_raw_secret(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    private = _reduce("unknown warning " + SECRET)
    private["PROCESS_EXIT"] = 9000
    with pytest.raises(RuntimeError, match="PREFLIGHT_REPORT_SHAPE"):
        operator.public_report(private)
    monkeypatch.setattr(operator, "run_once", lambda: operator.public_report(_reduce()))
    assert operator.main() == 0
    assert SECRET not in capsys.readouterr().out
    monkeypatch.setattr(operator, "run_once", lambda: (_ for _ in ()).throw(
        subprocess.TimeoutExpired("private", 90)))
    assert operator.main() == 1
    assert capsys.readouterr().out.strip() == '{"VERDICT": "BLOCKED_CODEX_PREFLIGHT"}'
