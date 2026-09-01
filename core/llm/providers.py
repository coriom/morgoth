"""Provider protocol + three implementations: ollama, claude-cli, api.

Provider.complete(prompt, *, system=None, json_mode=False, timeout=None) -> str

The three implementations wrap EXISTING code paths — they don't reinvent
the transport:
  · OllamaProvider  → wraps core.llm_client.OllamaLLMClient (uses the
                       shared client the cycle already constructed).
  · ClaudeCliProvider → wraps self_modify.reflect_llm._claude_cli_call
                        (subprocess to `claude`; neutral tempdir cwd).
  · HttpApiProvider → new: httpx to api.anthropic.com/v1/messages.
                       Key from env (ANTHROPIC_API_KEY), model from env
                       (ANTHROPIC_MODEL) or provider default, NEVER
                       logged (redaction test asserts this).

Timeout: falls back to REFLECT_LLM_TIMEOUT_SECONDS if unset — the
single-source LLM budget already used by reflect_llm.
"""

from __future__ import annotations

import json
import os
from typing import Any, Protocol

import httpx

from core.config import AppConfig
from core.llm_client import ChatMessage, OllamaLLMClient


class Provider(Protocol):
    """Common contract every backend must satisfy."""

    name: str

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        timeout: int | None = None,
    ) -> str: ...


_DEFAULT_TIMEOUT_SECS = int(
    os.environ.get("REFLECT_LLM_TIMEOUT_SECONDS") or 600
)


class OllamaProvider:
    name = "ollama"

    def __init__(self, client: OllamaLLMClient, model: str) -> None:
        self._client = client
        self._model = model  # unused today (client picks its own primary_model)

    async def complete(self, prompt, *, system=None, json_mode=False, timeout=None):
        messages: list[ChatMessage] = []
        if system:
            messages.append(ChatMessage(role="system", content=system))
        messages.append(ChatMessage(role="user", content=prompt))
        # OllamaLLMClient carries its own timeout via config — no per-call override
        # yet; timeout param reserved for symmetry with other providers.
        response = await self._client.chat(messages)
        return (response.message.content or "").strip()


class ClaudeCliProvider:
    name = "claude-cli"

    def __init__(self, model: str) -> None:
        self._model = model

    async def complete(self, prompt, *, system=None, json_mode=False, timeout=None):
        # Reuse the existing subprocess path — do not fork it.
        from self_modify.reflect_llm import _claude_cli_call
        full = f"{system}\n\n{prompt}" if system else prompt
        text, _meta = await _claude_cli_call(full, runner=None)
        return text


class HttpApiProvider:
    """Anthropic messages API. Key + model come from env; the key is NEVER
    logged (redacted in any exception surface — see test_key_redaction)."""

    name = "api"
    API_URL = "https://api.anthropic.com/v1/messages"
    DEFAULT_MODEL = "claude-haiku-4-5-20251001"

    def __init__(self, model: str) -> None:
        self._model = model if model != "default" else self.DEFAULT_MODEL

    def _get_key(self) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise HttpApiKeyMissing(
                "ANTHROPIC_API_KEY not set — set it or use provider=ollama|claude-cli"
            )
        return key

    async def complete(self, prompt, *, system=None, json_mode=False, timeout=None):
        key = self._get_key()
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        _t = timeout or _DEFAULT_TIMEOUT_SECS
        try:
            async with httpx.AsyncClient(timeout=_t) as c:
                r = await c.post(self.API_URL, headers=headers, content=json.dumps(body))
                r.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # NEVER include headers in the error surface — that leaks the key.
            raise HttpApiError(
                f"{exc.response.status_code} {exc.response.reason_phrase} "
                f"model={self._model} body={exc.response.text[:300]}"
            ) from None
        except httpx.HTTPError as exc:
            raise HttpApiError(f"{type(exc).__name__}: transport error") from None
        payload = r.json()
        blocks = payload.get("content", [])
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                return str(b.get("text", ""))
        return ""


class HttpApiError(RuntimeError):
    """Structured HTTP error — carries status + body prefix, NEVER the key."""


class HttpApiKeyMissing(RuntimeError):
    """The api provider was requested but ANTHROPIC_API_KEY isn't set."""


# ---- factory ------------------------------------------------------------


def get_provider(
    name: str, model: str, *, ollama_client: OllamaLLMClient | None = None,
) -> Provider:
    """Instantiate a provider by name. ollama needs the shared client (the
    cycle owns exactly one). claude-cli and api are stateless."""
    if name == "ollama":
        if ollama_client is None:
            raise ValueError("ollama provider requires an OllamaLLMClient instance")
        return OllamaProvider(ollama_client, model)
    if name == "claude-cli":
        return ClaudeCliProvider(model)
    if name == "api":
        return HttpApiProvider(model)
    raise ValueError(f"unknown provider {name!r}")


# ---- reachability probe (for `morgoth models`) --------------------------


def probe_reachability() -> dict[str, tuple[bool, str]]:
    """Presence/reachability check per provider — returns {name: (ok, note)}.

    ANTHROPIC_API_KEY check is PRESENCE-only: never reads the value beyond
    bool(), never logs it. Mirrors the pending_key hygiene pattern.
    """
    import shutil
    out: dict[str, tuple[bool, str]] = {}
    # ollama: assume reachable if the module can import — the actual
    # /api/tags probe requires the shared client, done at cycle start.
    out["ollama"] = (True, "checked at cycle start via health_check()")
    # claude-cli: CLI on PATH?
    if shutil.which("claude") is not None:
        out["claude-cli"] = (True, "on PATH")
    else:
        out["claude-cli"] = (False, "not on PATH (install Claude Code)")
    # api: env key present?
    if os.environ.get("ANTHROPIC_API_KEY"):
        out["api"] = (True, "ANTHROPIC_API_KEY present")
    else:
        out["api"] = (False, "ANTHROPIC_API_KEY not set")
    return out
