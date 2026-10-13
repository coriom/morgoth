"""Hermetic tests for the non-disclosing, one-request diagnostic."""
from __future__ import annotations

import json

import pytest

from scripts import codex_automated_warning_diagnostic as diagnostic
from scripts import codex_local_warning_review as review
from scripts.codex_unified_evidence import reduce_unified_events


SECRET = "TOP_SECRET_DO_NOT_PERSIST"


def _private(warning: str) -> dict[str, object]:
    events = [
        {"type": "thread.started"},
        {"type": "item.completed", "item": {"type": "error", "message": warning}},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {
            "type": "agent_message", "text": review.EXPECTED}},
        {"type": "turn.completed"},
    ]
    return reduce_unified_events("\n".join(json.dumps(row) for row in events),
                                 SECRET, 0, review.EXPECTED, "", True, False,
                                 expected_text=review.EXPECTED)


def test_exact_checkout_required(monkeypatch: pytest.MonkeyPatch) -> None:
    replies = {
        ("branch", "--show-current"): diagnostic.BRANCH,
        ("rev-parse", "HEAD"): "f" * 40,
        ("rev-parse", f"origin/{diagnostic.BRANCH}"): "f" * 40,
        ("status", "--porcelain", "--untracked-files=all"): "",
        ("merge-base", diagnostic.BASE_SHA, "HEAD"): diagnostic.BASE_SHA,
        ("remote", "get-url", "origin"): "git@github.com:coriom/morgoth.git",
    }
    monkeypatch.setattr(review, "_git", lambda *args: replies[args])
    diagnostic.verify_checkout()
    replies[("rev-parse", f"origin/{diagnostic.BRANCH}")] = "e" * 40
    with pytest.raises(review.ReviewUnavailable, match="PREFLIGHT_CHECKOUT"):
        diagnostic.verify_checkout()


def test_one_request_and_no_private_output(monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    monkeypatch.setattr(diagnostic, "verify_checkout", lambda: calls.append("checkout"))
    monkeypatch.setattr(review, "preflight", lambda: (calls.append("preflight") or
                                                ("/fixed/codex", {})))
    monkeypatch.setattr(review, "run_one_text_request", lambda *_: (
        calls.append("request") or _private("unknown " + SECRET), [SECRET]))
    assert diagnostic.main(["--one-synthetic-probe"]) == 1
    assert calls == ["checkout", "preflight", "checkout", "request"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    report = json.loads(output.out)
    assert report["UNKNOWN_DIAGNOSTIC_COUNT"] == 1
    assert report["VERDICT"] == "BLOCKED_CODEX_WARNING_UNKNOWN"
    assert report["PRODUCTION_QUALIFIED"] is False
    assert not any("message" in key.lower() for key in report)


def test_no_request_on_preflight_failure(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    monkeypatch.setattr(diagnostic, "verify_checkout", lambda: calls.append("checkout"))
    monkeypatch.setattr(review, "preflight", lambda: (_ for _ in ()).throw(
        review.ReviewUnavailable("PREFLIGHT_AUTH")))
    monkeypatch.setattr(review, "run_one_text_request", lambda *_: calls.append("request"))
    assert diagnostic.main(["--one-synthetic-probe"]) == 1
    assert calls == ["checkout"]
    assert json.loads(capsys.readouterr().out) == {
        "PRODUCTION_QUALIFIED": False, "VERDICT": "PREFLIGHT_AUTH"}


def test_explicit_opt_in_is_required(monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(diagnostic, "run_once", lambda: pytest.fail("request was started"))
    with pytest.raises(SystemExit, match="2"):
        diagnostic.main([])
    assert "TOP_SECRET" not in capsys.readouterr().err


def test_known_state_warning_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostic, "verify_checkout", lambda: None)
    monkeypatch.setattr(review, "preflight", lambda: ("/fixed/codex", {}))
    warning = "Codex rebuilt its local database"
    monkeypatch.setattr(review, "run_one_text_request", lambda *_: (_private(warning), []))
    report = diagnostic.run_once()
    assert report["VERDICT"] == "BLOCKED_CODEX_STATE_WARNING"
    assert report["PRODUCTION_QUALIFIED"] is False
