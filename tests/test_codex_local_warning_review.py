"""Hermetic safety tests: never launch Codex or a model."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from core.llm import codex_cli as cli
from scripts import codex_local_warning_review as review
from scripts.codex_unified_evidence import reduce_unified_events


SECRET = "TOP_SECRET_DO_NOT_PERSIST"


class FakeTerminal:
    """Capture only operator-terminal writes, never process stdout."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.writes: list[str] = []

    def write(self, value: str) -> int:
        self.writes.append(value)
        return len(value)

    def flush(self) -> None:
        pass

    def readline(self, _limit: int) -> str:
        return self.answers.pop(0) + "\n" if self.answers else "\n"


def _private(warning: str | None = None, *, tool: bool = False) -> dict[str, object]:
    events: list[dict[str, object]] = [{"type": "thread.started"}]
    if warning is not None:
        events.append({"type": "item.completed", "item": {
            "type": "error", "message": warning}})
    events.append({"type": "turn.started"})
    if tool:
        events.append({"type": "item.started", "item": {
            "type": "command_execution", "command": SECRET}})
    events.extend((
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": review.EXPECTED}},
        {"type": "turn.completed"},
    ))
    return reduce_unified_events("\n".join(json.dumps(row) for row in events), "", 0,
                                 review.EXPECTED, "", True, False,
                                 expected_text=review.EXPECTED)


def test_checkout_requires_exact_branch_origin_and_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {
        ("branch", "--show-current"): review.BRANCH,
        ("rev-parse", "HEAD"): "f" * 40,
        ("rev-parse", f"origin/{review.BRANCH}"): "f" * 40,
        ("status", "--porcelain", "--untracked-files=all"): "",
        ("merge-base", review.BASE_SHA, "HEAD"): review.BASE_SHA,
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
    }
    monkeypatch.setattr(review, "_git", lambda *args: replies[args])
    review.verify_checkout()
    replies[("rev-parse", f"origin/{review.BRANCH}")] = "e" * 40
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_CHECKOUT"):
        review.verify_checkout()
    replies[("rev-parse", f"origin/{review.BRANCH}")] = "f" * 40
    replies[("status", "--porcelain", "--untracked-files=all")] = "?? private"
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_CHECKOUT"):
        review.verify_checkout()


def test_tty_and_automation_gates_fail_before_any_inference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review.os, "isatty", lambda fd: False)
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_INTERACTIVE_TTY"):
        review.open_operator_terminal()
    monkeypatch.setenv("CI", "true")
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_INTERACTIVE_TTY"):
        review.open_operator_terminal()


def test_auth_preflight_requires_chatgpt_not_just_exit_zero(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(review.shutil, "which", lambda *args, **kwargs: "/fixed/codex")
    monkeypatch.setattr(review.cli, "discover", lambda *args: None)
    monkeypatch.setattr(review.cli, "_run", lambda *args: subprocess.CompletedProcess(
        args[0], 0, "Logged in with API key", ""))
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_AUTH"):
        review.preflight()


def test_no_inference_before_preflight_or_first_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(review, "verify_checkout", lambda: calls.append("checkout"))
    monkeypatch.setattr(review, "preflight", lambda: (calls.append("preflight") or
                                                  ("/fixed/codex", {})))
    monkeypatch.setattr(review, "run_one_text_request", lambda *_: (
        calls.append("inference"), []))
    with pytest.raises(review.ReviewUnavailable, match="OPERATOR_DECLINED"):
        review.run_once(FakeTerminal("NO"))
    assert calls == ["checkout", "preflight"]
    calls.clear()
    monkeypatch.setattr(review, "preflight", lambda: (_ for _ in ()).throw(
        review.ReviewUnavailable("PREFLIGHT_AUTH")))
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_AUTH"):
        review.run_once(FakeTerminal(review.FIRST_CONSENT))
    assert calls == ["checkout"]


def test_one_fixed_request_uses_production_boundary_and_no_response_file(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str, str, float]] = []
    cwd_seen: list[str] = []

    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((argv, cwd, prompt, timeout))
        cwd_seen.append(cwd)
        assert list(Path(cwd).iterdir()) == []
        assert argv == cli.build_argv("/fixed/codex", "default", "/dev/null")
        return subprocess.CompletedProcess(argv, 0, "\n".join((
            json.dumps({"type": "thread.started"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "error", "message": "unknown " + SECRET}}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {
                "type": "agent_message", "text": review.EXPECTED}}),
            json.dumps({"type": "turn.completed"}),
        )), "private stderr " + SECRET)

    monkeypatch.setattr(review.cli, "_run", fake_run)
    private, unknown = review.run_one_text_request("/fixed/codex", {})
    assert len(calls) == 1 and calls[0][2:] == (review.PROMPT, 90)
    assert private["EXPECTED_TEXT_RESPONSE"] is True
    assert unknown == ["unknown " + SECRET]
    assert not Path(cwd_seen[0]).exists()
    assert SECRET not in json.dumps(review.public_report(private))


def test_text_probe_uses_last_completed_assistant_item() -> None:
    rows = [
        {"type": "thread.started"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "intermediate"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": review.EXPECTED}},
        {"type": "turn.completed"},
    ]
    raw = "\n".join(json.dumps(row) for row in rows)
    last, unknown = review._private_event_facts(raw)
    assert last == review.EXPECTED and unknown == []
    report = reduce_unified_events(raw, "", 0, last, "", True, False,
                                   expected_text=review.EXPECTED)
    assert report["EXPECTED_TEXT_RESPONSE"] is True
    assert review.public_report(report)["TEXT_TRANSPORT_RESULT"] == "PASS"


def test_exactly_one_request_and_second_consent_for_local_display(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    calls = 0

    def request(_binary: str, _env: dict[str, str]) -> tuple[dict[str, object], list[str]]:
        nonlocal calls
        calls += 1
        return _private("unknown " + SECRET), ["unknown " + SECRET]

    monkeypatch.setattr(review, "verify_checkout", lambda: None)
    monkeypatch.setattr(review, "preflight", lambda: ("/fixed/codex", {}))
    monkeypatch.setattr(review, "run_one_text_request", request)
    tty = FakeTerminal(review.FIRST_CONSENT, "NO")
    public = review.run_once(tty)
    assert calls == 1
    assert public["TEXT_TRANSPORT_RESULT"] == "PASS"
    assert public["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert public["LOCAL_REVIEW_PERFORMED"] is False
    assert SECRET not in "".join(tty.writes)
    assert capsys.readouterr().out == ""

    tty = FakeTerminal(review.FIRST_CONSENT, review.SECOND_CONSENT, "STATE")
    public = review.run_once(tty)
    assert calls == 2  # one request per separate explicit run_once call
    assert public["LOCAL_REVIEW_PERFORMED"] is True
    assert public["LOCAL_REVIEW_CATEGORY"] == "STATE"
    assert public["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert SECRET not in "".join(tty.writes)
    assert SECRET not in json.dumps(public)


def test_failed_transport_does_not_offer_private_display(
        monkeypatch: pytest.MonkeyPatch) -> None:
    private = _private("unknown " + SECRET)
    private["PROCESS_EXIT"] = 1
    private["VERDICT"] = "CODEX_TEXT_TRANSPORT_BLOCKED_UNKNOWN"
    monkeypatch.setattr(review, "verify_checkout", lambda: None)
    monkeypatch.setattr(review, "preflight", lambda: ("/fixed/codex", {}))
    monkeypatch.setattr(review, "run_one_text_request", lambda *_: (
        private, ["unknown " + SECRET]))
    tty = FakeTerminal(review.FIRST_CONSENT, review.SECOND_CONSENT)
    public = review.run_once(tty)
    assert public["VERDICT"] == "BLOCKED_TEXT_TRANSPORT"
    assert public["LOCAL_REVIEW_PERFORMED"] is False
    assert "Redacted local warning" not in "".join(tty.writes)


def test_escaping_redaction_and_oversize_are_best_effort() -> None:
    message = ("token=" + SECRET + " https://example.test/private "
               "/home/operator/.codex/auth.json user@example.test "
               "550e8400-e29b-41d4-a716-446655440000 "
               "\x1b]52;c;PRIVATE\x07")
    displayed = review.redacted_terminal_text(message)
    assert displayed is not None
    for raw in (SECRET, "example.test", "/home/operator", "user@example.test",
                "550e8400", "\x1b", "\x07"):
        assert raw not in displayed
    assert "[CREDENTIAL]" in displayed and "[URL]" in displayed
    assert review.redacted_terminal_text("x" * 1025) is None
    assert review.redacted_terminal_text("") is None


def test_malformed_and_oversized_streams_never_yield_private_review() -> None:
    assert review._private_event_facts("{not-json") == ("", [])
    assert review._private_event_facts("x" * 4_000_001) == ("", [])
    oversized = "secret " + "x" * 1025
    raw = "\n".join((json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": oversized}}),
        json.dumps({"type": "turn.started"})))
    _, messages = review._private_event_facts(raw)
    assert messages == [oversized]
    assert review.redacted_terminal_text(messages[0]) is None


def test_public_projection_blocks_unknown_tools_and_forged_success() -> None:
    private = _private("unknown " + SECRET)
    public = review.public_report(private)
    assert public["UNKNOWN_DIAGNOSTIC_COUNT"] == 1
    assert public["PRODUCTION_QUALIFIED"] is False
    assert public["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert SECRET not in json.dumps(public)
    assert review.public_report(_private(tool=True))["VERDICT"] == "SECURITY_FAIL"
    forged = _private()
    forged["UNKNOWN_EVENT_COUNT"] = 1
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_REPORT_SHAPE"):
        review.public_report(forged)


def test_state_and_model_reroute_templates_still_block() -> None:
    state = ("Codex couldn't save diagnostic logs to its local database. "
             "Run `codex doctor` for diagnostics.")
    assert review.public_report(_private(state))["VERDICT"] == "BLOCKED_CODEX_STATE_WARNING"
    reroute = "model rerouted: model-a -> model-b (HighRiskCyberActivity)"
    assert review.public_report(_private(reroute))["VERDICT"] == "BLOCKED_CODEX_MODEL_REROUTE"


def test_redirected_main_emits_only_fixed_refusal(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(review, "open_operator_terminal", lambda: (_ for _ in ()).throw(
        review.ReviewUnavailable("PREFLIGHT_INTERACTIVE_TTY")))
    monkeypatch.setattr(review, "run_once", lambda *_: pytest.fail("must not infer"))
    assert review.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "VERDICT": "PREFLIGHT_INTERACTIVE_TTY", "PRODUCTION_QUALIFIED": False}


def test_unexpected_private_exception_never_reaches_stdout(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeContext:
        def __enter__(self) -> FakeTerminal:
            return FakeTerminal()

        def __exit__(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(review, "open_operator_terminal", lambda: FakeContext())
    monkeypatch.setattr(review, "run_once", lambda *_: (_ for _ in ()).throw(
        RuntimeError(SECRET)))
    assert review.main() == 1
    output = capsys.readouterr().out
    assert SECRET not in output
    assert json.loads(output) == {
        "VERDICT": "PREFLIGHT_FAILED", "PRODUCTION_QUALIFIED": False}
