"""Task → (provider, model) resolver.

Env format: MORGOTH_LLM_<TASK>=provider[:model]
  provider ∈ {ollama, claude-cli, api}
  model is provider-specific; "default" or omitted → provider chooses.

Unset env → registry falls back to the default in tasks.DEFAULTS. This
must reproduce pre-refactor behavior byte-identically — grep-locked in
tests/test_llm_registry.py so a future edit can't silently flip a default.

Legacy alias: THESIS_GENERATOR (from the earlier experiment script) is
honored as an alias of MORGOTH_LLM_THESIS. If both are set,
MORGOTH_LLM_THESIS wins.
"""

from __future__ import annotations

import os
from typing import Literal

from core.llm import tasks as T

ProviderName = Literal["ollama", "claude-cli", "api"]
_VALID_PROVIDERS: tuple[str, ...] = ("ollama", "claude-cli", "api")


def _env_key(task: str) -> str:
    return f"MORGOTH_LLM_{task.upper()}"


def _read_env_for(task: str) -> str | None:
    val = os.environ.get(_env_key(task))
    if val is not None and val.strip():
        return val.strip()
    for alias in T.LEGACY_ALIASES.get(task, ()):
        val = os.environ.get(alias)
        if val is not None and val.strip():
            legacy = val.strip()
            # Legacy THESIS_GENERATOR values were just the provider name.
            if ":" not in legacy:
                legacy += ":default"
            return legacy
    return None


def _parse_spec(spec: str) -> tuple[str, str]:
    """'provider[:model]' → (provider, model). Raises ValueError on bad shape."""
    if not spec:
        raise ValueError("empty provider spec")
    if ":" in spec:
        provider, model = spec.split(":", 1)
    else:
        provider, model = spec, "default"
    provider = provider.strip().lower()
    model = model.strip() or "default"
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"unknown provider {provider!r}; expected one of {_VALID_PROVIDERS!r}"
        )
    return provider, model


def resolve(task: str) -> tuple[ProviderName, str]:
    """Resolve a task to (provider, model). Never raises: bad env falls
    back to the default with a printed warning (so a typo doesn't crash
    the cycle — it just logs and stays on the pre-refactor path)."""
    if task not in T.DEFAULTS:
        raise KeyError(f"unknown task {task!r}; add it to tasks.DEFAULTS")
    override = _read_env_for(task)
    if override is None:
        return _parse_spec(T.DEFAULTS[task])  # type: ignore[return-value]
    try:
        return _parse_spec(override)  # type: ignore[return-value]
    except ValueError as exc:
        # Fail-safe: keep the default, print the reason so the operator sees it.
        # We don't want a typo in an env override to crash the cycle silently.
        print(f"WARN: {_env_key(task)}={override!r} invalid ({exc}); using default")
        return _parse_spec(T.DEFAULTS[task])  # type: ignore[return-value]


def routing_table() -> list[dict[str, str]]:
    """Snapshot of every task's current routing. For `morgoth models`."""
    out = []
    for task in T.all_tasks():
        override = _read_env_for(task)
        provider, model = resolve(task)
        out.append({
            "task": task, "provider": provider, "model": model,
            "source": "env" if override is not None else "default",
            "default": T.DEFAULTS[task],
        })
    return out
