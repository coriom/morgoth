"""Wiki compile endpoint.

POST /api/wiki/compile triggers a vault rebuild. The compile function itself
lives in scripts/compile_wiki.py and is reused here so the CLI, this endpoint,
and the cron all run the same code.

POST (not GET) because the call writes files to ~/Morgoth/vault. One LLM call
per entity, so the call may take a couple of minutes — acceptable for manual
or cron invocation. Failures map to HTTP 500 with a clean detail string,
mirroring the error handling in api/routes/knowledge.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from loguru import logger

from scripts.compile_wiki import compile_wiki


router = APIRouter(prefix="/api/wiki", tags=["wiki"])


@router.post("/compile")
async def compile_wiki_endpoint(request: Request) -> dict[str, Any]:
    """Recompile the Morgoth vault and return the count summary."""
    try:
        counts = await compile_wiki(
            request.app.state.persistent_memory,
            request.app.state.llm_client,
        )
    except Exception as exc:
        logger.exception("POST /api/wiki/compile failed")
        raise HTTPException(
            status_code=500, detail=f"wiki compile failed: {type(exc).__name__}"
        )
    return counts
