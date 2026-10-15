"""Bounded offline Project/SQL demonstration; never contacts a real service."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from uuid import UUID, uuid4

import httpx

from agents.generalist_approval_broker import ProjectApprovalBroker
from agents.generalist_capability import CapabilityRefusal
from agents.generalist_mission_engine import MissionRecord, MissionState
from agents.generalist_mission_planner import (
    FixtureProposalSource, MissionCapabilityPlanner, load_fixture_catalog,
)
from agents.generalist_operator_public_read import DisposablePostgres, _open_store
from agents.generalist_orchestrator import DeclarativeHttpReadDriver, GeneralistMissionOrchestrator


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "tests" / "fixtures" / "generalist_discovery_catalog_v1.json"
MISSION = "Find public bibliographic information for a book and summarize its title and publication metadata"


async def _recover(mission_id: UUID, key: str) -> bool:
    """Read one committed receipt in a fresh interpreter."""
    memory, store = await _open_store(os.environ["MORGOTH_TEST_POSTGRES_URL"])
    try:
        raw = await store.get_receipt(mission_id, key)
        return raw is not None and json.loads(raw).get("status") == MissionState.COMPLETE.value
    finally:
        await memory.close()


async def _recover_pending(mission_id: UUID) -> bool:
    """Verify approval-pending mission state from a fresh interpreter."""
    memory, store = await _open_store(os.environ["MORGOTH_TEST_POSTGRES_URL"])
    try:
        raw = await store.get_mission(mission_id)
        return raw is not None and MissionRecord.model_validate_json(
            raw[0]).state is MissionState.PENDING
    finally:
        await memory.close()


async def _child_check(dsn: str, mode: str, mission_id: UUID,
                       key: str | None = None) -> bool:
    """Run only a fixed bounded recovery check with no external request."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "MORGOTH_HOME": os.environ["MORGOTH_HOME"],
           "MORGOTH_PROJECT": os.environ["MORGOTH_PROJECT"],
           "MORGOTH_TEST_POSTGRES_URL": dsn}
    argv = [sys.executable, "-m", "agents.generalist_discovery_demo", mode, str(mission_id)]
    if key is not None:
        argv.append(key)
    child = await asyncio.create_subprocess_exec(
        *argv, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(child.communicate(), timeout=15)
    except TimeoutError:
        child.kill()
        await child.wait()
        return False
    return child.returncode == 0 and output == (
        b"PENDING_RECOVERED\n" if mode == "--recover-pending" else b"RECOVERED\n")


async def _exercise(dsn: str) -> dict[str, object]:
    """Run a fixture proposal, simulated grant, mock read and reuse."""
    catalog = load_fixture_catalog(CATALOG)
    memory, store = await _open_store(dsn)
    calls = 0
    try:
        planner = MissionCapabilityPlanner(store, store.project.id, catalog, FixtureProposalSource())
        mission = await planner.receive(MISSION)
        pending = await planner.advance(mission.mission_id, now_tick=1)
        if pending.state is not MissionState.PENDING:
            raise CapabilityRefusal("PLANNING_DEMO_FAILED")
        await memory.close()
        memory = None
        if not await _child_check(dsn, "--recover-pending", mission.mission_id):
            raise CapabilityRefusal("PENDING_RECOVERY_FAILED")
        memory, store = await _open_store(dsn)
        entry = catalog.get(str(pending.checkpoint["catalog_id"]))
        selected = catalog.load(entry)

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, json=entry.isolated_sample)

        driver = DeclarativeHttpReadDriver(
            entry.reviewed_request, entry.inputs, transport=httpx.MockTransport(handler),
            documentation=selected.evidence)
        broker = ProjectApprovalBroker(store, reviewed_request=entry.reviewed_request)
        engine = GeneralistMissionOrchestrator(store, store.project.id, driver, approval_broker=broker)
        descriptor = driver.inspect(selected.document, now_tick=1)
        grant = await broker._issue_after_consent(
            mission.mission_id, descriptor, provenance=f"foreground_operator_uid:{os.geteuid()}")
        receipt = await engine.execute(
            mission.mission_id, selected.document, grant=None, grant_id=grant, now_tick=1)
        key = receipt["execution_receipt"]
        if calls != 1:
            raise CapabilityRefusal("MOCK_REQUEST_COUNT_INVALID")
        await memory.close()
        memory = None

        if not await _child_check(dsn, "--recover", mission.mission_id, key):
            raise CapabilityRefusal("FRESH_PROCESS_RECOVERY_FAILED")

        memory, store = await _open_store(dsn)
        planner = MissionCapabilityPlanner(store, store.project.id, catalog, FixtureProposalSource())
        later = await planner.receive(MISSION + " again")
        reused = await planner.advance(later.mission_id, now_tick=1)
        if reused.state is not MissionState.READY or "reuse_exact_capability" not in reused.completed_steps:
            raise CapabilityRefusal("CAPABILITY_REUSE_FAILED")
        return {"VERDICT": "FIXTURE_WORKFLOW_PASS",
                "TIMELINE": ["MISSION_RECEIVED", "PLAN_CREATED", "CAPABILITY_INVENTORY_CHECKED",
                             "DOCUMENTATION_SELECTED", "DOCUMENTATION_INSPECTED",
                             "CAPABILITY_GAP_IDENTIFIED", "OPERATION_PROPOSED",
                             "ISOLATED_VALIDATION", "PENDING_HUMAN_APPROVAL",
                             "TEST_ONLY_AUTHORIZATION", "MISSION_COMPLETE",
                             "CAPABILITY_REUSED"],
                "DOCUMENTATION_SOURCE": "REVIEWED_OFFLINE_SNAPSHOT",
                "MODEL_PROPOSAL": False, "NETWORK_REQUEST_COUNT": 0,
                "MOCK_REQUEST_COUNT": calls, "RECEIPT_RECOVERED_FRESH_PROCESS": True,
                "PENDING_RECOVERED_FRESH_PROCESS": True,
                "PRODUCTION_QUALIFIED": False}
    finally:
        if memory is not None:
            await memory.close()


def main() -> int:
    """Create and destroy an owned morgoth_test cluster for this opt-in demo."""
    if len(sys.argv) == 4 and sys.argv[1] == "--recover":
        try:
            result = asyncio.run(_recover(UUID(sys.argv[2]), sys.argv[3]))
        except (ValueError, KeyError, CapabilityRefusal):
            return 2
        sys.stdout.write("RECOVERED\n" if result else "MISSING\n")
        return 0 if result else 2
    if len(sys.argv) == 3 and sys.argv[1] == "--recover-pending":
        try:
            result = asyncio.run(_recover_pending(UUID(sys.argv[2])))
        except (ValueError, KeyError, CapabilityRefusal):
            return 2
        sys.stdout.write("PENDING_RECOVERED\n" if result else "MISSING\n")
        return 0 if result else 2
    if len(sys.argv) != 1:
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="morgoth_discovery_") as location:
            root = Path(location)
            project_id = "fixture_" + uuid4().hex[:12]
            project_dir = root / "projects" / project_id
            project_dir.mkdir(parents=True)
            (project_dir / "project.yaml").write_text(json.dumps({
                "id": project_id, "name": "Discovery demo", "domain": "crypto",
                "postgres_schema": project_id, "chroma_prefix": project_id + "_",
                "vault_dir": str(root / "vault"), "runtime_dir": str(root / "runtime")}))
            previous = {name: os.environ.get(name) for name in ("MORGOTH_HOME", "MORGOTH_PROJECT")}
            os.environ["MORGOTH_HOME"], os.environ["MORGOTH_PROJECT"] = str(root), project_id
            try:
                with DisposablePostgres(root) as dsn:
                    result = asyncio.run(_exercise(dsn))
            finally:
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    except (CapabilityRefusal, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, CapabilityRefusal) else "DEMO_UNAVAILABLE"
        sys.stdout.write(json.dumps({"VERDICT": code, "PRODUCTION_QUALIFIED": False}) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
