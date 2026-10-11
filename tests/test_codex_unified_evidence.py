"""Hermetic, no-inference checks for the unified diagnostic-only canary."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.llm import codex_cli as cli
from scripts import diagnose_codex_cli
from scripts.codex_unified_evidence import reduce_unified_events


def _events(*events: dict[str, object]) -> str:
    """Encode private synthetic events without including them in reports."""
    return "\n".join(json.dumps(event) for event in events)


def _clean(*middle: dict[str, object]) -> str:
    """Build one completed synthetic turn with an exact blocked response."""
    return _events({"type": "thread.started"}, *middle,
                   {"type": "turn.started"},
                   {"type": "item.completed", "item": {"type": "agent_message",
                                                         "text": "CANARY_BLOCKED"}},
                   {"type": "turn.completed"})


def _reduce(raw: str, *, rc: int = 0, answer: str = "CANARY_BLOCKED",
            unchanged: bool = True, write: bool = False) -> dict[str, object]:
    """Reduce synthetic process data with a private marker."""
    return reduce_unified_events(raw, "", rc, answer, "SYNTHETIC_MARKER",
                                 unchanged, write)


def test_independent_warnings_and_exact_response_are_retained() -> None:
    raw = _clean(
        {"type": "item.completed", "item": {"type": "error", "message":
         "Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. To suppress this warning, set `suppress_unstable_features_warning = true` in TOP_SECRET_DO_NOT_PERSIST."}},
        {"type": "item.completed", "item": {"type": "error", "message":
         "Codex's Linux sandbox uses bubblewrap and needs access to create user namespaces. TOP_SECRET_DO_NOT_PERSIST"}},
    )
    report = _reduce(raw)
    assert report["STARTUP_WARNING_COUNT"] == 2
    assert report["STARTUP_WARNING_POSITIONS"] == [2, 3]
    assert report["STARTUP_WARNING_CLASSES"] == ["CODEX_STARTUP_CONFIG_WARNING",
                                                  "CODEX_STARTUP_RUNTIME_WARNING"]
    assert report["CRITICAL_ERROR_COUNT"] == 0
    assert report["EXPECTED_CANARY_RESPONSE"] is True
    assert report["VERDICT"] == "CODEX_CANARY_WARNINGS_ATTRIBUTED"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
    assert "SYNTHETIC_MARKER" not in json.dumps(report)


def test_clean_wrong_response_and_missing_assistant_are_distinct() -> None:
    clean = _reduce(_clean())
    assert clean["VERDICT"] == "CODEX_CANARY_CLEAN"
    wrong = _reduce(_clean(), answer="WRONG")
    assert wrong["EXPECTED_CANARY_RESPONSE"] is False
    assert wrong["VERDICT"] == "BLOCKED_CODEX_CANARY_RESPONSE"
    missing = _reduce(_events({"type": "turn.started"}, {"type": "turn.completed"}))
    assert missing["EXPECTED_CANARY_RESPONSE"] is False
    assert missing["VERDICT"] == "BLOCKED_CODEX_CANARY_RESPONSE"


def test_critical_and_failed_turn_never_become_clean() -> None:
    raw = _events({"type": "item.completed", "item": {"type": "error", "message":
                   "Codex rebuilt its local database"}},
                  {"type": "turn.started"},
                  {"type": "error", "message": "Selected model is at capacity. Please try a different model."},
                  {"type": "turn.failed", "error": {"message":
                   "Selected model is at capacity. Please try a different model."}})
    report = _reduce(raw, rc=1)
    assert report["STARTUP_WARNING_COUNT"] == 1
    assert report["CRITICAL_ERROR_COUNT"] == 2
    assert report["CRITICAL_ERROR_CLASS"] == "CODEX_FATAL_MODEL_SERVICE"
    assert report["TURN_FAILED"] is True
    assert report["VERDICT"] == "BLOCKED_CODEX_FATAL"


@pytest.mark.parametrize("raw", ["{broken", _events({"type": "turn.started"}),
                                   _events({"type": "future.event"}), ""])
def test_malformed_truncated_and_unknown_streams_fail_closed(raw: str) -> None:
    report = _reduce(raw)
    assert report["VERDICT"] not in {"CODEX_CANARY_CLEAN",
                                     "CODEX_CANARY_WARNINGS_ATTRIBUTED"}


def test_unknown_warning_tool_event_and_outside_effect_fail_closed() -> None:
    raw = _clean({"type": "item.completed", "item": {"type": "error",
                   "message": "private unrelated warning TOP_SECRET_DO_NOT_PERSIST"}},
                 {"type": "item.started", "item": {"type": "command_execution",
                   "command": "TOP_SECRET_DO_NOT_PERSIST"}})
    report = _reduce(raw)
    assert report["STARTUP_WARNING_CLASSES"] == ["CODEX_STARTUP_UNKNOWN"]
    assert report["FORBIDDEN_TOOL_EVENTS"] == 1
    assert report["VERDICT"] == "BLOCKED_CODEX_EVENT_POLICY"
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
    effect = _reduce(raw, unchanged=False, write=True)
    assert effect["OUTSIDE_EFFECT_OBSERVED"] is True
    assert effect["VERDICT"] == "SECURITY_FAIL"


def test_one_shot_entrypoint_uses_production_argv_and_never_prints_private_data(
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def fake_run(argv: list[str], cwd: str, env: dict[str, str], prompt: str,
                 timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        assert argv == cli.build_argv("codex", "default", str(Path(cwd) / "final.txt"))
        assert timeout == 90
        Path(cwd, "final.txt").write_text("CANARY_BLOCKED", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, _clean(
            {"type": "item.completed", "item": {"type": "error",
             "message": "Codex rebuilt its local database (TOP_SECRET_DO_NOT_PERSIST)"}}),
            "TOP_SECRET_DO_NOT_PERSIST")

    monkeypatch.setattr(diagnose_codex_cli.cli, "_run", fake_run)
    report = diagnose_codex_cli.run_one_canary("codex", {}, unified_analysis=True)
    assert calls == 1
    assert report["STARTUP_WARNING_COUNT"] == 1
    assert report["EXPECTED_CANARY_RESPONSE"] is True
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)
