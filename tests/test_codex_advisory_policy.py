"""Hermetic fail-closed tests for the pinned production JSONL advisory policy."""
import json
import subprocess

import pytest

from core.llm import codex_cli as cli
from core.llm.codex_advisories import CODE_MODE_DISABLED, SKIP_HOST_SKILL_DISCOVERY
from scripts.codex_startup_warnings import classify_startup_warning


def stream(*, warnings=(), answers=("answer",), tail=(), start=True, complete=True):
    rows = [{"type": "thread.started", "thread_id": "private"}]
    rows += [{"type": "item.completed", "item": {"type": "error", "message": w}}
             for w in warnings]
    if start:
        rows.append({"type": "turn.started"})
    rows += [{"type": "item.completed", "item": {"type": "agent_message", "text": a}}
             for a in answers]
    if complete:
        rows.append({"type": "turn.completed"})
    rows += list(tail)
    return "\n".join(json.dumps(row) for row in rows)


def check(raw, *, final="answer", reviewed=True, marker=None):
    cli._validate_events(raw, final_text=final, reviewed_launch=reviewed,
                         forbidden_marker=marker)


def test_two_exact_pinned_advisories_and_multiple_assistant_items():
    check(stream(warnings=(SKIP_HOST_SKILL_DISCOVERY, CODE_MODE_DISABLED),
                 answers=("intermediate", "answer")))
    assert classify_startup_warning(CODE_MODE_DISABLED) == "CODEX_STARTUP_CODE_MODE_DISABLED"
    assert classify_startup_warning(SKIP_HOST_SKILL_DISCOVERY) == "CODEX_STARTUP_CONFIG_WARNING"


@pytest.mark.parametrize("message", [
    CODE_MODE_DISABLED + " suffix", "prefix " + CODE_MODE_DISABLED,
    CODE_MODE_DISABLED.replace("fail closed", "fail-closed"),
    CODE_MODE_DISABLED.replace("code-mode host is disabled", "host unavailable"),
    SKIP_HOST_SKILL_DISCOVERY + " extra",
    SKIP_HOST_SKILL_DISCOVERY.replace("skip_host_skill_discovery", "code_mode_host"),
    SKIP_HOST_SKILL_DISCOVERY.replace("/home/codex/.codex/config.toml", "/tmp/config.toml"),
    "Codex couldn't save diagnostic logs to its local database. Run `codex doctor` for diagnostics.",
    "model rerouted: model-a -> model-b (HighRiskCyberActivity)",
    "TOP_SECRET_DO_NOT_PERSIST",
])
def test_unreviewed_or_modified_diagnostic_rejected(message):
    with pytest.raises(cli.CodexCliError, match="cli_diagnostic") as exc:
        check(stream(warnings=(message,)))
    assert message not in str(exc.value)


@pytest.mark.parametrize("message", [CODE_MODE_DISABLED, SKIP_HOST_SKILL_DISCOVERY])
def test_advisories_require_reviewed_launch(message):
    with pytest.raises(cli.CodexCliError, match="cli_diagnostic"):
        check(stream(warnings=(message,)), reviewed=False)


def test_duplicate_or_in_turn_advisory_rejected():
    with pytest.raises(cli.CodexCliError, match="cli_diagnostic"):
        check(stream(warnings=(CODE_MODE_DISABLED, CODE_MODE_DISABLED)))
    raw = stream(tail=()).splitlines()
    raw.insert(2, json.dumps({"type": "item.completed", "item": {
        "type": "error", "message": CODE_MODE_DISABLED}}))
    with pytest.raises(cli.CodexCliError, match="cli_diagnostic"):
        check("\n".join(raw))


@pytest.mark.parametrize("argv_change", [
    lambda args: args + ["--enable", "code_mode_host"],
    lambda args: args[:args.index("--disable")] + args[args.index("--disable") + 2:],
    lambda args: ["other-codex", *args[1:]],
])
def test_restricted_launch_cannot_be_weakened(argv_change):
    argv = cli.build_argv("codex", "default", "/tmp/final")
    assert cli._reviewed_launch_argv(argv, "codex", "default", "/tmp/final")
    assert not cli._reviewed_launch_argv(argv_change(argv), "codex", "default", "/tmp/final")


def test_required_code_mode_denial_cannot_be_removed_from_builder(monkeypatch):
    monkeypatch.setattr(cli, "DISABLED", tuple(f for f in cli.DISABLED if f != "code_mode_host"))
    argv = cli.build_argv("codex", "default", "/tmp/final")
    assert not cli._reviewed_launch_argv(argv, "codex", "default", "/tmp/final")


def test_unsupported_binary_is_rejected_before_work_inference(monkeypatch):
    calls = []
    def run(argv, cwd, env, prompt, timeout):
        calls.append(argv[1:])
        return subprocess.CompletedProcess(argv, 0, "codex-cli 0.163.0", "")
    monkeypatch.setattr(cli, "_run", run)
    with pytest.raises(cli.CodexCliError, match="unsupported_version"):
        cli.invoke("codex", "default", {}, "synthetic", 3)
    assert calls == [["--version"]]


@pytest.mark.parametrize("tail,code", [
    (({"type": "error", "message": "private"},), "unexpected_event"),
    (({"type": "turn.failed", "error": {"message": "private"}},), "unexpected_event"),
    (({"type": "item.completed", "item": {"type": "command_execution"}},), "tool_activity"),
    (({"type": "item.completed", "item": {"type": "mcp_tool_call"}},), "tool_activity"),
    (({"type": "future.event"},), "unexpected_event"),
    (({"type": "turn.started"},), "unexpected_event"),
])
def test_critical_tool_unknown_or_post_turn_event_rejected(tail, code):
    with pytest.raises(cli.CodexCliError, match=code):
        check(stream(tail=tail))


def test_response_and_marker_gates():
    with pytest.raises(cli.CodexCliError, match="final_mismatch"):
        check(stream(answers=("answer", "different")))
    with pytest.raises(cli.CodexCliError, match="canary_failed") as exc:
        check(stream(answers=("TOP_SECRET_DO_NOT_PERSIST", "answer")),
              marker="TOP_SECRET_DO_NOT_PERSIST")
    assert "TOP_SECRET_DO_NOT_PERSIST" not in str(exc.value)
    with pytest.raises(cli.CodexCliError, match="missing_final"):
        check(stream(answers=()))


@pytest.mark.parametrize("raw,code", [
    ("{invalid", "invalid_events"),
    ("", "invalid_events"),
    (stream(complete=False), "incomplete_turn"),
    (stream(start=False), "unexpected_event"),
    (stream(tail=({"type": "thread.started"},)), "unexpected_event"),
])
def test_malformed_and_incomplete_streams_fail_closed(raw, code):
    with pytest.raises(cli.CodexCliError, match=code):
        check(raw)
