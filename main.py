"""Main entry point for Morgoth."""

from __future__ import annotations

import asyncio
import os

import uvicorn
from loguru import logger

from api.server import app


def _wire_log_rotation() -> None:
    """Attach a rotating file sink honouring LOG_RETENTION_DAYS.

    The env value was previously declared in core/config but never
    consumed by any sink. Writes to logs/morgoth.log with a 50 MB
    per-file cap and daily rotation, keeping LOG_RETENTION_DAYS days
    of history. Default 5 days matches the .env value the operator
    already had. systemd's journald continues to capture stdout — this
    is an ADDITIONAL sink, not a replacement.
    """
    retention_days = int(os.environ.get("LOG_RETENTION_DAYS") or 5)
    try:
        logger.add(
            "logs/morgoth.log",
            rotation="50 MB",
            retention=f"{retention_days} days",
            compression="gz",
            enqueue=True,
        )
    except Exception as exc:
        logger.warning("log-file sink not attached (non-fatal): {}", exc)


async def main() -> None:
    """Run the Morgoth API server and trigger the bootstrap protocol."""

    _wire_log_rotation()
    logger.info("Starting Morgoth Phase 1 runtime")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
