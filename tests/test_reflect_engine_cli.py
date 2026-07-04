"""claude-cli branch tests.

Every case injects a runner (never spawns real ``claude``). The argv,
JSON parsing, error paths, and neutral-cwd behavior are all exercised
against synthetic subprocess.CompletedProcess objects.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from loguru import logger

from self_modify import reflect_llm
from self_modify.reflect_llm import (
    CLAUDE_CLI_BIN,
    ReflectLLMError,
    _build_claude_cli_argv,
    _parse_claude_cli_json,
    reflect_chat,
    resolve_provider,
)


# ---------- resolution matrix picks up claude-cli --------------------------

def test_resolve_provider_accepts_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFLECT_PROVIDER", raising=False)
    assert resolve_provider("claude-cli") == "claude-cli"


def test_resolve_provider_env_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLECT_PROVIDER", "claude-cli")
    assert resolve_provider(None) == "claude-cli"


def test_resolve_provider_still_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFLECT_PROVIDER", raising=False)
    with pytest.raises(ReflectLLMError):
        resolve_provider("claude_cli")  # underscore, wrong


# ---------- argv construction ---------------------------------------------

def test_argv_zero_tools_and_json_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REFLECT_CLI_MODEL", raising=False)
    argv = _build_claude_cli_argv("hello world")
    assert argv[0] == CLAUDE_CLI_BIN
    assert "-p" in argv
    # prompt is a single argv element (never shell-split).
    assert argv[argv.index("-p") + 1] == "hello world"
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""
    # No --model when env not set — the CLI/subscription picks.
    assert "--model" not in argv


def test_argv_honors_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLECT_CLI_MODEL", "opus")
    argv = _build_claude_cli_argv("p")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "opus"


def test_argv_empty_model_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REFLECT_CLI_MODEL", "   ")
    argv = _build_claude_cli_argv("p")
    assert "--model" not in argv


# ---------- JSON parser: real shape from Phase A --------------------------

REAL_SHAPE = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "api_error_status": None,
    "duration_ms": 1899,
    "num_turns": 1,
    "result": "PROBE_OK",
    "stop_reason": "end_turn",
    "session_id": "abc-def",
    "modelUsage": {
        "claude-haiku-4-5-20251001": {"inputTokens": 346, "outputTokens": 13},
        "claude-opus-4-7": {"inputTokens": 6, "outputTokens": 11},
    },
}


def test_parse_happy_path_extracts_result_and_model_keys() -> None:
    text, model, is_err = _parse_claude_cli_json(json.dumps(REAL_SHAPE))
    assert text == "PROBE_OK"
    assert not is_err
    # Sorted-and-joined so we don't rely on dict insertion order.
    assert model == "claude-haiku-4-5-20251001|claude-opus-4-7"


def test_parse_is_error_true_surfaces_bool() -> None:
    body = dict(REAL_SHAPE, is_error=True, result="", subtype="error")
    _, _, is_err = _parse_claude_cli_json(json.dumps(body))
    assert is_err is True


def test_parse_missing_modelUsage_falls_back_to_unknown() -> None:
    body = {"result": "x", "is_error": False}
    _, model, _ = _parse_claude_cli_json(json.dumps(body))
    assert model == "unknown"


def test_parse_unparseable_raises_clean_error() -> None:
    with pytest.raises(ReflectLLMError) as excinfo:
        _parse_claude_cli_json("<html>not json</html>")
    assert "JSON parse failed" in str(excinfo.value)


# ---------- end-to-end reflect_chat (runner mocked) ----------------------

def _completed(stdout: str, rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr="")


@pytest.mark.asyncio
async def test_claude_cli_happy_path_returns_result_text() -> None:
    captured: dict[str, Any] = {}

    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["cwd"] = cwd
        return _completed(json.dumps(REAL_SHAPE))

    text, meta = await reflect_chat(
        "the reflect prompt",
        SimpleNamespace(),
        "claude-cli",
        claude_cli_runner=_runner,
    )
    assert text == "PROBE_OK"
    assert meta["provider"] == "claude-cli"
    assert meta["model"] == "claude-haiku-4-5-20251001|claude-opus-4-7"
    assert meta["prompt_len"] == len("the reflect prompt")
    assert meta["response_len"] == len("PROBE_OK")
    assert "latency_ms" in meta
    # Prompt passed as ONE argv element (never shell-split).
    argv = captured["argv"]
    assert argv[argv.index("-p") + 1] == "the reflect prompt"


@pytest.mark.asyncio
async def test_claude_cli_uses_neutral_tempdir_and_cleans_up() -> None:
    captured_cwd: list[str] = []

    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        captured_cwd.append(cwd)
        # cwd must exist during the runner call
        assert Path(cwd).is_dir()
        # cwd must NOT be the repo — the whole point is neutrality
        assert not cwd.startswith(str(Path.cwd()))
        return _completed(json.dumps(REAL_SHAPE))

    await reflect_chat(
        "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
    )
    # After reflect_chat returns, the tempdir is gone.
    assert captured_cwd, "runner was never invoked"
    assert not Path(captured_cwd[0]).exists(), (
        "reflect_chat must remove the neutral tempdir in finally"
    )


@pytest.mark.asyncio
async def test_claude_cli_binary_missing_raises_clean_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch shutil.which so the branch's fast-fail fires.
    monkeypatch.setattr(reflect_llm.shutil, "which", lambda _n: None)
    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat("p", SimpleNamespace(), "claude-cli")
    msg = str(excinfo.value)
    assert "claude CLI not found on PATH" in msg
    assert "--provider ollama" in msg


@pytest.mark.asyncio
async def test_claude_cli_filenotfound_from_subprocess_also_clean() -> None:
    """The PATH check races the exec — cover the case where shutil.which
    passes but subprocess.run raises FileNotFoundError anyway."""

    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("claude")

    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
        )
    assert "claude CLI not found on PATH" in str(excinfo.value)


@pytest.mark.asyncio
async def test_claude_cli_timeout_raises_clean_message() -> None:
    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=180)

    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
        )
    assert "timed out" in str(excinfo.value)


@pytest.mark.asyncio
async def test_claude_cli_nonzero_returncode_raises_clean_message() -> None:
    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return _completed("some noise", rc=127)

    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
        )
    assert "returncode 127" in str(excinfo.value)


@pytest.mark.asyncio
async def test_claude_cli_is_error_true_raises_clean_message() -> None:
    body = dict(REAL_SHAPE, is_error=True, subtype="error", result="")

    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return _completed(json.dumps(body))

    with pytest.raises(ReflectLLMError) as excinfo:
        await reflect_chat(
            "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
        )
    assert "is_error=true" in str(excinfo.value)


# ---------- HYGIENE: no full stdout leaks into DEBUG logs ----------------

@pytest.mark.asyncio
async def test_claude_cli_stdout_never_appears_in_debug_logs() -> None:
    """The CLI's JSON stdout contains session_id, cost breakdown, and
    potentially the prompt echo. Contract: nothing in the raw stdout
    should ever hit a log record. Only meta scalars are logged.
    """
    SENTINEL = "SESSION-4425dec7-dcad-4c15-b93b-DO-NOT-LOG"
    body = dict(REAL_SHAPE, session_id=SENTINEL, result="ok")

    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return _completed(json.dumps(body))

    buf = io.StringIO()
    handler_id = logger.add(buf, level="DEBUG", format="{message}")
    try:
        await reflect_chat(
            "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
        )
    finally:
        logger.remove(handler_id)
    assert SENTINEL not in buf.getvalue(), (
        "raw stdout LEAKED into DEBUG log — session_id contract broken"
    )


@pytest.mark.asyncio
async def test_claude_cli_nonzero_stdout_never_appears_in_error_logs() -> None:
    """On nonzero returncode we must NOT log the stdout — it may
    contain prompt fragments or session ids."""
    SENTINEL = "STDOUT-SESSION-FRAGMENT-LEAK-CANARY"

    def _runner(argv: list[str], cwd: str) -> subprocess.CompletedProcess[str]:
        return _completed(SENTINEL, rc=1)

    buf = io.StringIO()
    handler_id = logger.add(buf, level="DEBUG", format="{message}")
    try:
        with pytest.raises(ReflectLLMError):
            await reflect_chat(
                "p", SimpleNamespace(), "claude-cli", claude_cli_runner=_runner,
            )
    finally:
        logger.remove(handler_id)
    assert SENTINEL not in buf.getvalue()
