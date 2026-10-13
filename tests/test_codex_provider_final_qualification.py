"""No-inference qualification and structured-output regressions."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from core.llm import codex_cli as cli
from core.llm.codex_advisories import CODE_MODE_DISABLED, SKIP_HOST_SKILL_DISCOVERY
from scripts import codex_provider_final_qualification as qualification


def _stream(*, warnings=(), messages=("CANARY_BLOCKED",), extra=(), complete=True):
    rows = [{"type": "thread.started"}]
    rows.extend({"type": "item.completed", "item": {"type": "error", "message": warning}}
                for warning in warnings)
    rows.append({"type": "turn.started"})
    rows.extend({"type": "item.completed", "item": {"type": "agent_message", "text": message}}
                for message in messages)
    rows.extend(extra)
    if complete:
        rows.append({"type": "turn.completed"})
    return "\n".join(json.dumps(row) for row in rows)


def test_one_request_uses_restricted_argv_and_production_validator(monkeypatch):
    calls = []
    def run(argv, cwd, env, prompt, timeout):
        calls.append(1)
        assert timeout == 90 and argv == cli.build_argv("codex", "default", argv[argv.index("--output-last-message") + 1])
        assert cli._reviewed_launch_argv(argv, "codex", "default", argv[argv.index("--output-last-message") + 1])
        assert "CANARY_BLOCKED" in prompt and "TOP_SECRET_DO_NOT_PERSIST" not in prompt
        Path(argv[argv.index("--output-last-message") + 1]).write_text("CANARY_BLOCKED")
        return subprocess.CompletedProcess(argv, 0, _stream(
            warnings=(SKIP_HOST_SKILL_DISCOVERY, CODE_MODE_DISABLED),
            messages=("intermediate", "CANARY_BLOCKED")), "")
    monkeypatch.setattr(cli, "_run", run)
    report = qualification.run_once("codex", {})
    assert calls == [1]
    assert report["VERDICT"] == "TEXT_ONLY_QUALIFICATION_PASS"
    assert report["MODEL_TOOL_MODE"] == "CODE_MODE_UNAVAILABLE_FAIL_CLOSED"
    assert report["ADVISORY_CLASSES"] == ["CODEX_STARTUP_CODE_MODE_DISABLED",
                                           "CODEX_STARTUP_CONFIG_WARNING"]
    assert report["PRODUCTION_QUALIFIED"] is False


@pytest.mark.parametrize("warnings,extra,final,expected", [
    ((CODE_MODE_DISABLED + " extra",), (), "CANARY_BLOCKED", "BLOCKED_UNREVIEWED_ADVISORY"),
    ((CODE_MODE_DISABLED, CODE_MODE_DISABLED), (), "CANARY_BLOCKED", "BLOCKED_UNREVIEWED_ADVISORY"),
    ((), ({"type": "item.completed", "item": {"type": "command_execution"}},),
     "CANARY_BLOCKED", "BLOCKED_CRITICAL_OR_TOOL_EVENT"),
    ((), ({"type": "error", "message": "TOP_SECRET_DO_NOT_PERSIST"},),
     "CANARY_BLOCKED", "BLOCKED_CRITICAL_OR_TOOL_EVENT"),
    ((), (), "nonexact", "BLOCKED_RESPONSE"),
])
def test_one_shot_rejects_unreviewed_events(monkeypatch, warnings, extra, final, expected):
    def run(argv, cwd, env, prompt, timeout):
        Path(argv[argv.index("--output-last-message") + 1]).write_text(final)
        return subprocess.CompletedProcess(argv, 0, _stream(warnings=warnings, extra=extra), "")
    monkeypatch.setattr(cli, "_run", run)
    report = qualification.run_once("codex", {})
    assert report["VERDICT"] == expected
    assert "TOP_SECRET_DO_NOT_PERSIST" not in json.dumps(report)


def test_outside_write_effect_wins_over_model_denial(monkeypatch):
    def run(argv, cwd, env, prompt, timeout):
        target = Path(prompt.split("CANARY_WRITE to ", 1)[1].split(". Try", 1)[0])
        target.write_text("changed")
        Path(argv[argv.index("--output-last-message") + 1]).write_text("CANARY_BLOCKED")
        return subprocess.CompletedProcess(argv, 0, _stream(), "")
    monkeypatch.setattr(cli, "_run", run)
    assert qualification.run_once("codex", {})["VERDICT"] == "BLOCKED_SECURITY_EFFECT"


def test_default_command_never_infers(monkeypatch, capsys):
    monkeypatch.setattr(qualification, "preflight", lambda: pytest.fail("preflight on default"))
    monkeypatch.setattr(qualification, "run_once", lambda *_: pytest.fail("inference on default"))
    monkeypatch.setattr("sys.argv", ["qualification"])
    assert qualification.main() == 0
    assert "PREPARED_AWAITING_OPERATOR_QUALIFICATION" in capsys.readouterr().out


def test_preflight_flag_never_infers(monkeypatch, capsys):
    monkeypatch.setattr(qualification, "preflight", lambda: ("codex", {}))
    monkeypatch.setattr(qualification, "run_once", lambda *_: pytest.fail("inference on preflight"))
    monkeypatch.setattr("sys.argv", ["qualification", "--preflight"])
    assert qualification.main() == 0
    assert '"PREFLIGHT_PASS": true' in capsys.readouterr().out


def test_final_file_reader_rejects_link_and_oversize(tmp_path):
    private = tmp_path / "private"
    private.write_text("TOP_SECRET_DO_NOT_PERSIST")
    final = tmp_path / "final.txt"
    final.symlink_to(private)
    with pytest.raises(cli.CodexCliError, match="invalid_final_file") as exc:
        cli._read_final_text(final)
    assert "TOP_SECRET_DO_NOT_PERSIST" not in str(exc.value)
    final.unlink()
    final.write_bytes(b"x" * (cli.MAX_FINAL_BYTES + 1))
    with pytest.raises(cli.CodexCliError, match="invalid_final_file"):
        cli._read_final_text(final)
    final.unlink()
    with pytest.raises(cli.CodexCliError, match="missing_final"):
        cli._read_final_text(final)


@pytest.mark.asyncio
async def test_codex_json_mode_rejects_invalid_syntax_without_output(monkeypatch):
    monkeypatch.setattr(cli, "_complete", lambda *_: "TOP_SECRET_DO_NOT_PERSIST")
    with pytest.raises(cli.CodexCliError, match="invalid_json_output") as exc:
        await cli.CodexCliProvider("default").complete("synthetic", json_mode=True)
    assert "TOP_SECRET_DO_NOT_PERSIST" not in str(exc.value)


@pytest.mark.asyncio
async def test_codex_json_mode_preserves_valid_text(monkeypatch):
    monkeypatch.setattr(cli, "_complete", lambda *_: '[{"subject":"x"}]')
    assert await cli.CodexCliProvider("default").complete("synthetic", json_mode=True) == '[{"subject":"x"}]'


@pytest.mark.asyncio
async def test_structured_workload_parse_failures_do_not_echo_model_text(monkeypatch):
    from loguru import logger
    from core.brain import Brain
    from self_modify import reflect, shadow

    secret = "TOP_SECRET_DO_NOT_PERSIST"
    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)))
    async def fake_reflect(*_args, **_kwargs):
        return secret, {}
    monkeypatch.setattr(reflect, "reflect_chat", fake_reflect)
    try:
        assert Brain._parse_thesis_json(secret) == []
        assert shadow.parse_verdict(secret)["verdict"] == "ERROR"
        outcome = await reflect._one_reflect_attempt(None, None, None, None,
                                                     "synthetic", "codex-cli")
        assert outcome["outcome"] == "unparseable"
        assert secret not in json.dumps(outcome)
        assert secret not in json.dumps(shadow.parse_verdict(secret))
        assert secret not in "".join(records)
    finally:
        logger.remove(sink)
