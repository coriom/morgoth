"""One opt-in public Issues GET in a disposable Project, with fresh-process recovery.

Invocation requires --live-public-read and MORGOTH_TEST_POSTGRES_URL. This is
not a production approval interface and is never imported by autonomous START.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import httpx

from agents.generalist_orchestrator import (
    GeneralistMissionOrchestrator, InvocationGrant, PublicIssuesDriver,
)
from agents.generalist_mission_store import ProjectMissionStore
from core.project import current_namespace
from memory.persistent import PersistentMemory


ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "tests/fixtures/generalist_github_issues_contract_v1.json"
MOCKED_ROWS = [{"number": 1, "title": "Synthetic open issue", "state": "open",
                "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}]


async def _child(phase: str, mission_id: str | None) -> dict[str, object]:
    """Run exactly one bounded phase in its own interpreter and pool."""
    dsn = os.environ["MORGOTH_TEST_POSTGRES_URL"]
    preflight = await asyncpg.connect(dsn)
    try:
        if await preflight.fetchval("SELECT current_database()") != "morgoth_test":
            raise RuntimeError("DISPOSABLE_TEST_DATABASE_REQUIRED")
    finally:
        await preflight.close()
    project = current_namespace()
    memory = PersistentMemory(SimpleNamespace(postgres_url=dsn))
    await memory.initialize()
    try:
        store = ProjectMissionStore(memory, project)
        await store.initialize_optional()
        mode = os.environ.get("MORGOTH_PUBLIC_READ_MODE")
        if mode == "mock":
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=MOCKED_ROWS))
        elif mode == "live":
            transport = None
        else:
            raise RuntimeError("PUBLIC_READ_MODE_INVALID")
        driver = PublicIssuesDriver(transport=transport)
        engine = GeneralistMissionOrchestrator(store, project.id, driver)
        document = DOCUMENT.read_text(encoding="utf-8")
        desc = driver.inspect(document, now_tick=1)
        if phase == "prepare":
            mission = await engine.receive("Read public open Issues status", desc.capability_id)
            assessed = await engine.assess(mission.mission_id, document, MOCKED_ROWS, now_tick=1)
            return {"mission_id": str(mission.mission_id), "state": assessed.state.value,
                    "validation": "MOCKED_PASS"}
        if mission_id is None:
            raise RuntimeError("MISSION_ID_REQUIRED")
        grant = InvocationGrant(project.id, desc.capability_id, desc.version, desc.digest,
                                desc.scopes, 1, "operator_task_public_read")
        result = await engine.execute(UUID(mission_id), document, grant=grant, now_tick=1)
        evidence = result["evidence"]
        return {"mission_id": mission_id, "state": result["status"],
                "capability_id": result["capability_id"],
                "capability_version": result["capability_version"],
                "source_identity": evidence["source_identity"],
                "operation": evidence["operation"],
                "retrieved_at": evidence["retrieved_at"],
                "issue_count": evidence["issue_count"],
                "response_sha256": evidence["response_sha256"],
                "execution_receipt": result["execution_receipt"],
                "recovered": phase == "recover"}
    finally:
        await memory.close()


async def _spawn(phase: str, project_id: str, home: Path, dsn: str, mode: str,
                 mission_id: str | None = None) -> dict[str, object]:
    """Own one child; no retry and no raw stderr/stdout disclosure."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8",
           "MORGOTH_HOME": str(home), "MORGOTH_PROJECT": project_id,
           "MORGOTH_TEST_POSTGRES_URL": dsn, "MORGOTH_PUBLIC_READ_MODE": mode}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "agents.generalist_public_read_demo", "--child",
        phase, mission_id or "-", cwd=ROOT, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("PUBLIC_READ_CHILD_TIMEOUT") from None
    if proc.returncode != 0 or len(output) > 2048:
        raise RuntimeError("PUBLIC_READ_CHILD_FAILED")
    return json.loads(output)


async def run_demo(*, mode: str) -> dict[str, object]:
    """Validate mock, perform one public GET, recover receipt, remove schema."""
    if mode not in {"mock", "live"}:
        raise RuntimeError("PUBLIC_READ_MODE_INVALID")
    dsn = os.environ.get("MORGOTH_TEST_POSTGRES_URL")
    if not dsn:
        raise RuntimeError("DISPOSABLE_TEST_DATABASE_REQUIRED")
    with tempfile.TemporaryDirectory(prefix="morgoth_public_read_") as tmp:
        home = Path(tmp)
        project_id = f"fixture_github_{uuid4().hex[:8]}"
        root = home / "projects" / project_id
        root.mkdir(parents=True)
        manifest = {"id": project_id, "name": "Disposable public read", "domain": "crypto",
                    "postgres_schema": project_id, "chroma_prefix": project_id + "_",
                    "vault_dir": str(home / "vault" / project_id),
                    "runtime_dir": str(home / "runtime" / project_id)}
        (root / "project.yaml").write_text(json.dumps(manifest), encoding="utf-8")
        try:
            prepared = await _spawn("prepare", project_id, home, dsn, mode)
            completed = await _spawn("complete", project_id, home, dsn, mode,
                                     str(prepared["mission_id"]))
            recovered = await _spawn("recover", project_id, home, dsn, mode,
                                     str(prepared["mission_id"]))
            if completed["response_sha256"] != recovered["response_sha256"]:
                raise RuntimeError("RECEIPT_RECOVERY_MISMATCH")
            return {"validation": prepared["validation"], "mode": mode,
                    "prepared_state": prepared["state"],
                    "completed": completed, "fresh_process_receipt_recovered": True,
                    "production_approval": False, "public_get_count": 1 if mode == "live" else 0,
                    "mock_get_count": 1 if mode == "mock" else 0}
        finally:
            conn = await asyncpg.connect(dsn)
            try:
                if await conn.fetchval("SELECT current_database()") != "morgoth_test":
                    raise RuntimeError("DISPOSABLE_TEST_DATABASE_REQUIRED")
                await conn.execute(f'DROP SCHEMA IF EXISTS "{project_id}" CASCADE')
            finally:
                await conn.close()


def main() -> int:
    """Run only an explicitly requested disposable public read."""
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        result = asyncio.run(_child(sys.argv[2], None if sys.argv[3] == "-" else sys.argv[3]))
    elif sys.argv == [sys.argv[0], "--live-public-read"]:
        result = asyncio.run(run_demo(mode="live"))
    elif sys.argv == [sys.argv[0], "--mock-public-read"]:
        result = asyncio.run(run_demo(mode="mock"))
    else:
        raise SystemExit("PUBLIC_READ_ARGUMENTS_INVALID")
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
