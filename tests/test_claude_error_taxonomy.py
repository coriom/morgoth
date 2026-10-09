"""Claude CLI failure codes and content-free llm_calls logging."""

from __future__ import annotations

import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from loguru import logger

from core.llm.logs import log_call
from self_modify import reflect_llm
from self_modify.reflect_llm import ReflectLLMError, _claude_cli_call


SECRET = "TOP_SECRET_DO_NOT_PERSIST"


@pytest.mark.asyncio
@pytest.mark.parametrize("case,code", [
    ("not_found", "CLAUDE_CLI_NOT_FOUND"),
    ("timeout", "CLAUDE_CLI_TIMEOUT"),
    ("exit", "CLAUDE_CLI_EXIT_NONZERO"),
    ("invalid_json", "CLAUDE_CLI_JSON_INVALID"),
    ("not_object", "CLAUDE_CLI_JSON_NOT_OBJECT"),
    ("reported_error", "CLAUDE_CLI_REPORTED_ERROR"),
])
async def test_claude_failure_codes_are_fixed_and_content_free(case, code, monkeypatch):
    async def inline_runner(fn, *args):
        return fn(*args)

    monkeypatch.setattr(reflect_llm.asyncio, "to_thread", inline_runner)
    if case == "not_found":
        monkeypatch.setattr(reflect_llm.shutil, "which", lambda _name: None)
        runner = None
    else:
        def runner(argv, cwd, prompt):
            if case == "timeout":
                raise subprocess.TimeoutExpired(argv, 1, stderr=SECRET)
            if case == "exit":
                return subprocess.CompletedProcess(argv, 1, SECRET, SECRET)
            if case == "invalid_json":
                stdout = SECRET
            elif case == "not_object":
                stdout = json.dumps([SECRET])
            else:
                stdout = json.dumps({"is_error": True, "result": SECRET})
            return subprocess.CompletedProcess(argv, 0, stdout, SECRET)

    sink = io.StringIO()
    handler = logger.add(sink, format="{message}")
    try:
        with pytest.raises(ReflectLLMError) as caught:
            await _claude_cli_call("safe prompt", runner)
    finally:
        logger.remove(handler)
    assert caught.value.safe_code == code
    assert SECRET not in str(caught.value)
    assert SECRET not in caught.value.safe_code
    assert SECRET not in sink.getvalue()


class _Connection:
    def __init__(self):
        self.args = None

    async def execute(self, query, *args):
        self.args = (query, args)


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_log_call_records_safe_code_without_changing_outcome():
    connection = _Connection()
    pm = SimpleNamespace(_require_pool=lambda: SimpleNamespace(acquire=lambda: _Acquire(connection)))

    async def fail():
        raise ReflectLLMError("safe failure", safe_code="CLAUDE_CLI_TIMEOUT")

    with pytest.raises(ReflectLLMError):
        await log_call(pm, "synthesis", "claude-cli", "default", fail)
    query, args = connection.args
    assert "error_code" in query
    assert args[-2:] == ("error:ReflectLLMError", "CLAUDE_CLI_TIMEOUT")
    assert args[4] == 0

    async def succeed():
        return "OK"

    assert await log_call(pm, "thesis", "claude-cli", "default", succeed) == "OK"
    assert connection.args[1][-2:] == ("ok", None)


@pytest.mark.asyncio
async def test_log_call_discards_untrusted_safe_code_and_secret_message():
    connection = _Connection()
    pm = SimpleNamespace(_require_pool=lambda: SimpleNamespace(acquire=lambda: _Acquire(connection)))

    async def fail():
        exc = RuntimeError(SECRET)
        exc.safe_code = SECRET + ": raw"
        raise exc

    with pytest.raises(RuntimeError):
        await log_call(pm, "synthesis", "claude-cli", "default", fail)
    assert connection.args[1][-1] is None
    assert SECRET not in repr(connection.args)
