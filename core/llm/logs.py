"""Usage-log writer for llm_calls.

Wraps a provider call, measures latency + response size, INSERTs one
row into llm_calls. NO content columns — sizes only.

Non-fatal on write failure: a full disk shouldn't break the cycle.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from loguru import logger


async def log_call(
    pm: Any,  # PersistentMemory — kept as Any to avoid a circular import
    task: str,
    provider: str,
    model: str,
    fn: Callable[[], Awaitable[str]],
    *,
    prompt_bytes: int = 0,
) -> str:
    """Await `fn()`, insert one llm_calls row, propagate the result / error.

    Errors from `fn` are re-raised AFTER an outcome=error row is written —
    so the caller's exception handling stays intact but the log records
    both successes and failures.
    """
    t0 = time.monotonic()
    outcome = "ok"
    response = ""
    exc_captured: Exception | None = None
    try:
        response = await fn()
    except Exception as exc:
        outcome = f"error:{type(exc).__name__}"
        exc_captured = exc
    latency_ms = int((time.monotonic() - t0) * 1000)
    try:
        pool = pm._require_pool() if pm is not None else None
        if pool is not None:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO llm_calls
                      (task, provider, model, prompt_bytes, response_bytes, latency_ms, outcome)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    task, provider, model,
                    int(prompt_bytes), int(len(response or "")),
                    int(latency_ms), outcome,
                )
    except Exception as write_exc:
        logger.warning("llm_calls insert failed (non-fatal): {}", write_exc)
    if exc_captured is not None:
        raise exc_captured
    return response
