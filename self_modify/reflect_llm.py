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

import os
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

VALID_PROVIDERS: tuple[str, ...] = ("ollama", "anthropic")


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

async def reflect_chat(
    prompt: str,
    config: AppConfig,
    provider: str,
    *,
    ollama_client: OllamaLLMClient | None = None,
    httpx_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Send ``prompt`` to the selected engine; return (text, meta).

    ``meta`` is a small dict of scalars (provider, model, lengths,
    latency) suitable for logging or persisting to the proposal row's
    ``engine`` column — it contains NO request/response payload.

    The injectable client factories are for tests only; production code
    passes neither and gets the default construction.
    """
    if provider == "ollama":
        return await _ollama_call(prompt, config, ollama_client)
    if provider == "anthropic":
        return await _anthropic_call(prompt, httpx_client_factory)
    raise ReflectLLMError(
        f"unknown provider {provider!r}; expected one of {VALID_PROVIDERS!r}"
    )
