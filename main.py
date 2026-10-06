"""Main entry point for Morgoth."""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn
from loguru import logger
from core.project_engine_lease import ProjectEngineLease, ProjectEngineLeaseError


def _wire_log_rotation() -> None:
    """Attach a rotating file sink honouring LOG_RETENTION_DAYS.

    The env value was previously declared in core/config but never
    consumed by any sink. Writes to logs/morgoth.log with a 50 MB
    per-file cap and daily rotation, keeping LOG_RETENTION_DAYS days
    of history. Default 5 days matches the .env value the operator
    already had. systemd's journald continues to capture stdout — this
    is an ADDITIONAL sink, not a replacement.
    """
    from core.project import current_project
    project = current_project()
    log_path = "logs/morgoth.log" if project.is_legacy else str(project.runtime_dir / "logs/morgoth.log")
    retention_days = int(os.environ.get("LOG_RETENTION_DAYS") or 5)
    try:
        logger.add(
            log_path,
            rotation="50 MB",
            retention=f"{retention_days} days",
            compression="gz",
            enqueue=True,
        )
    except Exception as exc:
        logger.warning("log-file sink not attached (non-fatal): {}", exc)


async def _serve(lease: ProjectEngineLease | None = None) -> None:
    """Preserve the historical bind and autonomous lifespan behavior."""
    from api.server import app
    if lease is not None:
        app.state.project_engine_lease = lease
    _wire_log_rotation()
    logger.info("Starting Morgoth Phase 1 runtime")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    """Run legacy unchanged; require a Project lease for managed engines."""
    from core.project import current_project

    project = current_project()
    if project.is_legacy:
        await _serve()
        return
    try:
        with ProjectEngineLease(project) as lease:
            await _serve(lease)
    except ProjectEngineLeaseError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(3) from None


if __name__ == "__main__":
    asyncio.run(main())
