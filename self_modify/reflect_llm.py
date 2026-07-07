"""Switchable LLM engine for the reflect job.

Two providers behind one call: ``ollama`` (default, unchanged behavior)
and ``anthropic`` (a stronger model for the reflect job specifically).
The reflect prompt and every gate stay identical — only the engine
differs, so the same-prompt/different-model comparison is a clean test
of the model-quality hypothesis.

Design constraints
------------------

- **No new dependency.** Anthropic path uses ``httpx`` (already the
  project standard). No ``anthropic`` SDK import.
- **Key hygiene.** ``ANTHROPIC_API_KEY`` is read from the environment
  only, never logged, never included in exception messages, never
  echoed to stdout, never persisted to the proposal row. On HTTP error
  we log only ``provider`` + ``status_code``; on transient error we log
  only ``type(exc).__name__``. The response body is deliberately NOT
  logged (Anthropic error responses can echo request fragments).
- **What we DO log per call:** ``provider``, ``model``, ``prompt_len``,
  ``response_len``, ``latency_ms``. That's it.

Provider resolution: ``--provider`` flag > ``REFLECT_PROVIDER`` env >
default ``ollama``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Any, Callable

import httpx
from loguru import logger

from core.config import AppConfig
from core.llm_client import ChatMessage, OllamaLLMClient


ANTHROPIC_URL: str = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION: str = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS: int = 1024
ANTHROPIC_TIMEOUT_SECS: float = 60.0

VALID_PROVIDERS: tuple[str, ...] = ("ollama", "anthropic", "claude-cli")

# claude-cli branch. --tools "" disables all tools (documented in the CLI
# help; stable across versions). No --max-turns on 2.1.x — `-p` is
# single-shot by default. Timeout is generous because the CLI's initial
# cache warmup on cold cache can take tens of seconds.
CLAUDE_CLI_BIN: str = "claude"
CLAUDE_CLI_TIMEOUT_SECS: int = 180


class ReflectLLMError(RuntimeError):
    """Engine-side failure. Never carries a request header, body, or key.

    Callers may surface ``str(exc)`` to logs and CLI output without any
    hygiene concern — the class contract is that the message is safe.
    """


def resolve_provider(cli_flag: str | None) -> str:
    """CLI flag > REFLECT_PROVIDER env > default 'ollama'.

    Empty CLI flag falls through (argparse default is None). Unknown
    values raise so a typo never silently downgrades to the default.
    """
    raw = cli_flag or os.environ.get("REFLECT_PROVIDER") or "ollama"
    resolved = raw.strip().lower()
    if resolved not in VALID_PROVIDERS:
        raise ReflectLLMError(
            f"unknown provider {resolved!r}; expected one of {VALID_PROVIDERS!r}"
        )
    return resolved


# ---------------------------------------------------------------------------
# ollama branch — byte-identical to today's reflect call path.
# ---------------------------------------------------------------------------

async def _ollama_call(
    prompt: str,
    config: AppConfig,
    client: OllamaLLMClient | None,
) -> tuple[str, dict[str, Any]]:
    owns_client = client is None
    llm = client or OllamaLLMClient(config)
    try:
        t0 = time.monotonic()
        resp = await llm.chat([ChatMessage(role="user", content=prompt)])
        latency_ms = int((time.monotonic() - t0) * 1000)
        text = (resp.message.content or "").strip()
        meta = {
            "provider": "ollama",
            "model": getattr(config, "ollama_primary_model", "unknown"),
            "prompt_len": len(prompt),
            "response_len": len(text),
            "latency_ms": latency_ms,
        }
        logger.info("reflect_chat[ollama]: {}", meta)
        return text, meta
    finally:
        if owns_client:
            await llm.close()


# ---------------------------------------------------------------------------
# anthropic branch — plain httpx, single retry on transient network error.
# ---------------------------------------------------------------------------

def _anthropic_key() -> str:
    """Return the key from env, or raise a clean refusal.

    The refusal message states EXACTLY how to add the key. It does not
    include tracebacks in the CLI path (see reflect.py's CLI wrapper).
    """
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise ReflectLLMError(
            "provider=anthropic requires ANTHROPIC_API_KEY in .env — "
            "create one at console.anthropic.com → API keys and paste as "
            "ANTHROPIC_API_KEY=sk-ant-... (also add REFLECT_ANTHROPIC_MODEL "
            "to override the default claude-sonnet-4-6)"
        )
    return key


def _anthropic_model() -> str:
    return (os.environ.get("REFLECT_ANTHROPIC_MODEL") or "").strip() or DEFAULT_ANTHROPIC_MODEL


async def _anthropic_call(
    prompt: str,
    httpx_client_factory: Callable[[], httpx.AsyncClient] | None,
) -> tuple[str, dict[str, Any]]:
    api_key = _anthropic_key()  # raises before any client work if absent
    model = _anthropic_model()
    request_body = {
        "model": model,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    factory = httpx_client_factory or (
        lambda: httpx.AsyncClient(timeout=ANTHROPIC_TIMEOUT_SECS)
    )

    t0 = time.monotonic()
    resp: httpx.Response | None = None
    for attempt in (1, 2):
        try:
            async with factory() as client:
                resp = await client.post(
                    ANTHROPIC_URL, json=request_body, headers=headers,
                )
            break
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            # Only the exception type name is logged — never headers, body,
            # or the key.
            if attempt == 2:
                logger.warning(
                    "reflect_chat[anthropic]: transient {} after retry",
                    type(exc).__name__,
                )
                raise ReflectLLMError(
                    f"anthropic call failed after retry: {type(exc).__name__}"
                ) from None
            logger.warning(
                "reflect_chat[anthropic]: transient {} — retrying once",
                type(exc).__name__,
            )
    assert resp is not None  # for type-checkers; loop guarantees this

    latency_ms = int((time.monotonic() - t0) * 1000)
    if resp.status_code >= 400:
        # Deliberately NOT including resp.text — Anthropic error bodies
        # can echo request fragments and rate-limit hints; the status
        # code is enough to diagnose.
        logger.error(
            "reflect_chat[anthropic]: HTTP {} (body not logged)",
            resp.status_code,
        )
        raise ReflectLLMError(
            f"anthropic HTTP {resp.status_code}: request refused by API"
        )

    body = resp.json()
    parts = body.get("content") or []
    text_parts = [
        p.get("text", "")
        for p in parts
        if isinstance(p, dict) and p.get("type") == "text"
    ]
    text = "".join(text_parts).strip()
    meta = {
        "provider": "anthropic",
        "model": model,
        "prompt_len": len(prompt),
        "response_len": len(text),
        "latency_ms": latency_ms,
    }
    logger.info("reflect_chat[anthropic]: {}", meta)
    return text, meta


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# claude-cli branch — shells out to the locally installed Claude Code CLI.
# ---------------------------------------------------------------------------

def _build_claude_cli_argv(prompt: str) -> list[str]:
    """Build the argv for headless single-shot invocation.

    The prompt is delivered on stdin (see ``_run_claude_cli``), not in
    argv. ``-p`` with no argument tells claude-cli to read stdin
    single-shot. Passing the prompt via argv hit ``OSError [Errno 7]
    Argument list too long`` on ~2MB-plus reflect prompts (registry
    context + a large machine-terminal JSON spec). Delivering via
    stdin removes the ARG_MAX ceiling UNCONDITIONALLY — thresholds
    breed boundary bugs.

    The ``prompt`` parameter is retained on the signature for the
    tests that assert its ABSENCE from argv; callers must still pass
    it so the accompanying ``_run_claude_cli`` receives it as stdin.
    """
    _ = prompt  # deliberately unused — prompt goes to stdin
    argv: list[str] = [
        CLAUDE_CLI_BIN,
        "-p",
        "--output-format", "json",
        # --tools "" disables ALL tools. The reflect prompt is
        # spec-generation only; any tool call would be model-side prose.
        "--tools", "",
    ]
    # Model override — env-driven; default (empty) lets the subscription
    # pick, and the JSON response reports what was actually used.
    model = (os.environ.get("REFLECT_CLI_MODEL") or "").strip()
    if model:
        argv.extend(["--model", model])
    return argv


def _run_claude_cli(argv: list[str], cwd: str, prompt: str = "") -> subprocess.CompletedProcess[str]:
    """Blocking subprocess call. Kept as a module-level helper so tests
    can patch it cleanly and the caller can drive it via to_thread.

    The prompt reaches the CLI via stdin (``input=``) — never argv.
    ``prompt`` defaults to empty for backward compatibility with test
    fixtures that don't need to send one; production callers always
    pass the assembled prompt.
    """
    return subprocess.run(
        argv,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=CLAUDE_CLI_TIMEOUT_SECS,
        cwd=cwd,
        check=False,
    )


def _parse_claude_cli_json(stdout: str) -> tuple[str, str, bool]:
    """Return (result_text, model_reported, is_error).

    ``model_reported`` = pipe-joined sorted keys of ``modelUsage`` (the
    CLI often mixes models — haiku for classification + opus for the
    actual reply — so a single-name field would be a lie).
    """
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise ReflectLLMError(
            f"claude-cli JSON parse failed: {type(exc).__name__}"
        ) from None
    if not isinstance(data, dict):
        raise ReflectLLMError("claude-cli JSON was not an object")
    is_error = bool(data.get("is_error"))
    result_text = str(data.get("result") or "")
    model_usage = data.get("modelUsage") or {}
    if isinstance(model_usage, dict) and model_usage:
        model_reported = "|".join(sorted(str(k) for k in model_usage))
    else:
        model_reported = "unknown"
    return result_text, model_reported, is_error


async def _claude_cli_call(
    prompt: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> tuple[str, dict[str, Any]]:
    """claude-cli branch. Neutral cwd is mandatory (see comment)."""
    if shutil.which(CLAUDE_CLI_BIN) is None and runner is None:
        # Fast-fail before creating a tempdir. Tests inject a runner so
        # this check is skipped in the mock path.
        raise ReflectLLMError(
            "claude CLI not found on PATH — install Claude Code "
            "(https://claude.com/claude-code) or use --provider ollama"
        )

    argv = _build_claude_cli_argv(prompt)
    # NEUTRAL cwd: a fresh tempdir prevents the CLI from loading a
    # CLAUDE.md, project skills, or any repo-local context that would
    # break prompt comparability with the other engines AND would be a
    # prompt-injection surface. Cleaned in finally regardless of outcome.
    tmp_cwd = tempfile.mkdtemp(prefix="morgoth-reflect-cli-")
    t0 = time.monotonic()
    run = runner or _run_claude_cli
    try:
        try:
            # Prompt delivered via stdin — unconditional, removes
            # ARG_MAX ceiling. runner signature accepts (argv, cwd,
            # prompt); older test runners with (argv, cwd) still work
            # thanks to the default arg on _run_claude_cli.
            completed = await asyncio.to_thread(run, argv, tmp_cwd, prompt)
        except FileNotFoundError:
            # Subprocess couldn't spawn — binary vanished between the
            # PATH check and the exec call. Same operator instruction.
            raise ReflectLLMError(
                "claude CLI not found on PATH — install Claude Code "
                "(https://claude.com/claude-code) or use --provider ollama"
            ) from None
        except subprocess.TimeoutExpired:
            raise ReflectLLMError(
                f"claude CLI timed out after {CLAUDE_CLI_TIMEOUT_SECS}s"
            ) from None
    finally:
        shutil.rmtree(tmp_cwd, ignore_errors=True)

    latency_ms = int((time.monotonic() - t0) * 1000)

    if completed.returncode != 0:
        # Do NOT surface stdout/stderr — the CLI can embed session
        # metadata or fragments of the prompt. The return code is enough
        # for triage; full output is preserved for the operator via the
        # CLI's own transcripts, not our logs.
        logger.error(
            "reflect_chat[claude-cli]: nonzero returncode {} "
            "(stdout not logged)", completed.returncode,
        )
        raise ReflectLLMError(
            f"claude CLI exited with returncode {completed.returncode}"
        )

    text, model_reported, is_error = _parse_claude_cli_json(completed.stdout)
    if is_error:
        raise ReflectLLMError("claude CLI reported is_error=true in JSON payload")

    meta = {
        "provider": "claude-cli",
        "model": model_reported,
        "prompt_len": len(prompt),
        "response_len": len(text),
        "latency_ms": latency_ms,
    }
    logger.info("reflect_chat[claude-cli]: {}", meta)
    return text.strip(), meta


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

async def reflect_chat(
    prompt: str,
    config: AppConfig,
    provider: str,
    *,
    ollama_client: OllamaLLMClient | None = None,
    httpx_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    claude_cli_runner: Callable[[list[str], str], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Send ``prompt`` to the selected engine; return (text, meta).

    ``meta`` is a small dict of scalars (provider, model, lengths,
    latency) suitable for logging or persisting to the proposal row's
    ``engine`` column — it contains NO request/response payload.

    The injectable client factories / runner are for tests only;
    production code passes neither and gets the default construction.
    """
    if provider == "ollama":
        return await _ollama_call(prompt, config, ollama_client)
    if provider == "anthropic":
        return await _anthropic_call(prompt, httpx_client_factory)
    if provider == "claude-cli":
        return await _claude_cli_call(prompt, claude_cli_runner)
    raise ReflectLLMError(
        f"unknown provider {provider!r}; expected one of {VALID_PROVIDERS!r}"
    )
