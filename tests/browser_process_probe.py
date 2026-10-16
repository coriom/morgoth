"""Fresh-interpreter, disposable-test-only browser proposal recovery probe."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

from agents.generalist_browser_mission import BrowserMissionCoordinator
from agents.generalist_mission_engine import MissionState
from agents.generalist_mission_store import ProjectMissionStore
from core.project import ProjectConfig
from memory.persistent import PersistentMemory


async def main() -> None:
    """Read an existing checkpoint in a new process without executing a browser."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    if urlsplit(dsn).path != "/morgoth_test":
        raise RuntimeError("TEST_DATABASE_REQUIRED")
    project_id = sys.argv[1]
    mission_id = UUID(sys.argv[2])
    project = ProjectConfig(id=project_id, name="Browser V9 fresh process", domain="crypto",
                            postgres_schema=project_id, chroma_prefix=f"{project_id}_",
                            vault_dir=Path("/tmp/browser_probe_vault"),
                            runtime_dir=Path("/tmp/browser_probe_runtime"))
    import core.project as project_module
    import agents.generalist_mission_store as store_module
    project_module.current_namespace = lambda: project
    store_module.current_namespace = lambda: project
    memory = PersistentMemory(SimpleNamespace(postgres_url=dsn))
    try:
        await memory.initialize()
        store = ProjectMissionStore(memory, project)
        await store.initialize_optional()
        mission, procedure = await BrowserMissionCoordinator(store, project.id).recover(mission_id)
        receipt = await store.get_receipt(mission_id, "browser_fixture_validation")
        sys.stdout.write(json.dumps({"RECOVERED": mission.state is MissionState.PENDING,
                                     "PROCEDURE_PRESENT": procedure is not None,
                                     "RECEIPT_PRESENT": receipt is not None,
                                     "APPROVAL_PENDING": procedure.approval == "PENDING_HUMAN_APPROVAL"
                                     if procedure else False}))
    finally:
        await memory.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Fixed type only; never export SQL text, connection details or paths.
        sys.stdout.write(json.dumps({"ERROR_CLASS": type(exc).__name__}))
        raise SystemExit(1) from None
